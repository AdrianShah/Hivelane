"""Log formatting. Human-readable locally, JSON everywhere else.

No log shipper is wired up yet, but hosts capture stdout and every aggregator
can parse JSON lines — emitting structured records now means nothing has to be
re-instrumented when one is added.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

try:
    from . import config
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]

HUMAN_FORMAT = "%(asctime)s %(levelname)s %(message)s"

# Attributes LogRecord always carries; anything else was attached by the caller
# via `extra=` and belongs in the payload.
_STANDARD = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"asctime", "message", "taskName"}


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
    if config.IS_DEVELOPMENT:
        handler.setFormatter(logging.Formatter(HUMAN_FORMAT))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
