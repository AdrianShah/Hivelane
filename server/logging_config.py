"""Log formatting. Human-readable locally, JSON everywhere else.

No log shipper is wired up yet, but hosts capture stdout and every aggregator
can parse JSON lines — emitting structured records now means nothing has to be
re-instrumented when one is added.

Two filters guard what reaches stdout, whatever a call site logs:

- RedactingFilter caps every message at MAX_MESSAGE_CHARS (a client-chosen
  name or path cannot turn one record into half a megabyte) and masks room
  codes, which are join credentials, in app messages ("room ABCDEFGH") and in
  uvicorn's access log (/ws/<code>, /rooms/<code>/...): a code becomes its
  first two characters plus a short hash, enough to correlate lines.
- ThrottleFilter lets each message template through at most
  MAX_PER_TEMPLATE_PER_MINUTE times a minute (errors always pass) and then
  logs how many it suppressed, so one client retrying a rejected handshake
  cannot flood the disk or the log bill.

Both sit on the stdout handler (redaction as its formatter, so the shared
record is not rewritten) and on uvicorn's loggers, not on the loggers the app
writes to, so tests that read caplog still see every record as logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import time
from typing import Any

try:
    from . import config
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]

HUMAN_FORMAT = "%(asctime)s %(levelname)s %(message)s"

MAX_MESSAGE_CHARS = 2000
MAX_PER_TEMPLATE_PER_MINUTE = 120
# Message templates tracked by the throttle at once (oldest window dropped).
_MAX_TEMPLATES = 2048

# Attributes LogRecord always carries; anything else was attached by the caller
# via `extra=` and belongs in the payload.
_STANDARD = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}

# "room ABCDEFGH", "Room ABCDEFGH:", "rooms ABCDEFGH" (codes are 6-8 of A-Z0-9).
_ROOM_WORD = re.compile(r"(\b(?i:rooms?)\s+)([A-Z0-9]{6,8})\b")
# /ws/<code> and /rooms/<code> in paths (access log, messages that echo one).
_ROOM_PATH = re.compile(r"(/ws/|/rooms/)([A-Za-z0-9-]{4,16})(?=[/\s?#\"']|$)")


def mask_code(code: str) -> str:
    """A room code as logs show it: two characters and a short hash."""
    digest = hashlib.sha256(code.strip().upper().encode("utf-8", "replace")).hexdigest()[:6]
    return f"{code[:2]}…{digest}"


def redact(text: str) -> str:
    text = _ROOM_WORD.sub(lambda m: m.group(1) + mask_code(m.group(2)), text)
    text = _ROOM_PATH.sub(lambda m: m.group(1) + mask_code(m.group(2)), text)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + f"… ({len(text) - MAX_MESSAGE_CHARS} more chars)"
    return text


class RedactingFormatter(logging.Formatter):
    """Wraps the stdout formatter and redacts its output. A formatter, not a
    filter: the record itself is shared with every other handler (caplog in
    tests included) and stays as it was logged."""

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


class RedactingFilter(logging.Filter):
    """For uvicorn's own loggers, whose handlers and formatters uvicorn
    installs: rewrites the request path (access log) or message in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) >= 3:
            # uvicorn's AccessFormatter unpacks (client, method, path, version,
            # status) from args, so only the path is rewritten.
            args = list(record.args)
            args[2] = redact(str(args[2]))
            record.args = tuple(args)
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


class ThrottleFilter(logging.Filter):
    """At most `per_minute` records per (logger, template) per minute."""

    def __init__(self, per_minute: int = MAX_PER_TEMPLATE_PER_MINUTE, clock: Any = time.monotonic) -> None:
        super().__init__()
        self.per_minute = per_minute
        self._clock = clock
        # key -> [window start, passed, suppressed]
        self._windows: dict[tuple[str, str], list[float]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if self.per_minute <= 0 or record.levelno >= logging.ERROR:
            return True
        key = (record.name, str(record.msg)[:200])
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now - window[0] >= 60.0:
            suppressed = int(window[2]) if window is not None else 0
            window = [now, 0, 0]
            self._windows[key] = window
            while len(self._windows) > _MAX_TEMPLATES:
                self._windows.pop(next(iter(self._windows)))
            if suppressed:
                record.msg = f"{record.msg} [{suppressed} similar message(s) suppressed in the last minute]"
        if window[1] >= self.per_minute:
            window[2] += 1
            return False
        window[1] += 1
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "env": config.ENV,
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: int = logging.INFO) -> None:
    """Install the right formatter on the root logger. Safe to call once."""
    handler = logging.StreamHandler(sys.stdout)
    inner = logging.Formatter(HUMAN_FORMAT) if config.IS_DEVELOPMENT else JsonFormatter()
    handler.setFormatter(RedactingFormatter(inner))
    handler.addFilter(ThrottleFilter())
    redacting = RedactingFilter()
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's loggers have their own handlers (and do not propagate) when it
    # configured logging itself; filter at the logger so its output is covered.
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(redacting)
    _startup_warnings()


def _startup_warnings() -> None:
    log = logging.getLogger("relay.config")
    if config.IS_DEVELOPMENT:
        log.warning(
            "HIVELANE_ENV=development (the default): text logs, no HSTS, localhost "
            "CORS origins. Set HIVELANE_ENV=production for any relay reachable from "
            "the internet."
        )
    if config.WEBHOOKS_ALLOW_PRIVATE:
        log.warning(
            "HIVELANE_WEBHOOKS_ALLOW_PRIVATE is on: room owners can point webhooks at "
            "private, loopback and link-local addresses (SSRF protection is off)"
        )
