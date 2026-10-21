"""The `synergo hook claude-code` hot path, standard library only.

Claude Code starts the hook as a fresh process before and after every edit, so
everything this module imports is paid on every file write the agent makes.
The daemon side needs fastapi/uvicorn/websockets/pydantic; none of that may
leak in here (synergo/tests/test_hook_fast.py checks sys.modules), and neither
may the heavier corners of the standard library: the POST is hand-rolled over
the `_socket` C module rather than urllib.request (which alone drags in email,
base64, http.client and friends) or even socket.py (enum, selectors), and JSON
goes through synergo.hook_json. On a cold Windows start each of those is tens
of milliseconds, paid around every edit; scripts/bench_hook.py measures it.

Fails open unconditionally: any problem means exit 0 and no decision. When the
daemon cannot be reached at all, the user is told so at most once per
OFFLINE_NOTICE_INTERVAL via a systemMessage, so coordination never silently
stops without anyone noticing.
"""

from __future__ import annotations

import os
import sys
import time

try:
    import _socket as _sock
except ImportError:  # pragma: no cover - CPython always ships _socket
    import socket as _sock

from synergo import hook_json
from synergo.adapters import claude_code

# Type checkers treat any name TYPE_CHECKING as True; importing typing's own
# costs the hook a few milliseconds per edit.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Optional

LOOPBACK = "127.0.0.1"
# Must match synergo.daemon.DEFAULT_PORT (asserted in the tests); importing it
# from there would pull the whole daemon stack into the hook.
DEFAULT_PORT = 4600

HOOK_POST_TIMEOUT = 2.0
# Loopback connects complete in the kernel even when the daemon's event loop is
# busy, so a slow connect means nothing is listening. Windows retries a refused
# loopback SYN for the full timeout instead of failing fast, so without this cap
# a stopped daemon would add HOOK_POST_TIMEOUT to every single edit.
CONNECT_TIMEOUT = 0.25
MAX_RESPONSE_BYTES = 1 << 20

OFFLINE_NOTICE_INTERVAL = 600.0
STATE_FILENAME = "hook_state.json"

OFFLINE_NOT_RUNNING = (
    "Synergo offline: the local daemon isn't running, so edits aren't being coordinated. "
    "Run `synergo join <ROOM> --name <NAME>` (or `synergo status`)."
)
OFFLINE_NOT_RESPONDING = (
    "Synergo offline: the local daemon isn't responding, so edits aren't being coordinated. "
    "Check `synergo status`, or restart it with `synergo join <ROOM> --name <NAME>`."
)


class DaemonUnreachable(Exception):
    """Nothing answered on the daemon port in time; the message is user-facing."""


class BadResponse(Exception):
    """The daemon answered, but not with a usable ack."""


# --- transport ----------------------------------------------------------------


def _parse_response(raw: bytes) -> Any:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise BadResponse("truncated response")
    lines = head.split(b"\r\n")
    parts = lines[0].split(None, 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise BadResponse(f"bad status line {lines[0]!r}")
    if parts[1] != b"200":
        raise BadResponse(f"daemon answered {parts[1].decode(errors='replace')}")
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    if headers.get(b"transfer-encoding", b"").lower() == b"chunked":
        body = _dechunk(body)
    elif b"content-length" in headers:
        length = int(headers[b"content-length"])
        if len(body) < length:
            raise BadResponse("truncated body")
        body = body[:length]
    return hook_json.loads(body)


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while True:
        size_line, sep, rest = body.partition(b"\r\n")
        if not sep:
            raise BadResponse("truncated chunk")
        size = int(size_line.split(b";", 1)[0], 16)
        if size == 0:
            return bytes(out)
        out += rest[:size]
        body = rest[size + 2 :]


def _complete(raw: bytes) -> bool:
    """True once a Content-Length response is fully read, so we need not wait
    for the server's FIN."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return False
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return len(body) >= int(value.strip())
            except ValueError:
                return False
    return False


def post_event(event: dict[str, Any], port: int, timeout: float = HOOK_POST_TIMEOUT) -> Any:
    """POST one NormalizedEvent-shaped dict to the daemon; returns the parsed ack.

    Raises DaemonUnreachable when nothing answers (the offline notice case) and
    BadResponse / ValueError / OSError for anything else, all of which the
    caller swallows.
    """
    body = hook_json.dumps(event).encode("utf-8")
    request = (
        f"POST /events HTTP/1.1\r\n"
        f"Host: {LOOPBACK}:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii") + body

    deadline = time.monotonic() + timeout
    sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        try:
            sock.settimeout(min(CONNECT_TIMEOUT, timeout))
            sock.connect((LOOPBACK, port))
        except OSError as exc:
            raise DaemonUnreachable(OFFLINE_NOT_RUNNING) from exc

        chunks: list[bytes] = []
        received = 0
        try:
            sock.settimeout(max(deadline - time.monotonic(), 0.001))
            sock.sendall(request)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("hook deadline passed")
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise BadResponse("response too large")
                if _complete(b"".join(chunks)):
                    break
        except TimeoutError as exc:
            raise DaemonUnreachable(OFFLINE_NOT_RESPONDING) from exc
    finally:
        sock.close()
    return _parse_response(b"".join(chunks))


# --- offline notice -----------------------------------------------------------


def synergo_home() -> str:
    return os.environ.get("SYNERGO_HOME") or os.path.join(os.path.expanduser("~"), ".synergo")


def state_path() -> str:
    return os.path.join(synergo_home(), STATE_FILENAME)


def claim_offline_notice(now: Optional[float] = None) -> bool:
    """True if the offline notice is due, recording that it is being shown.

    Returns False when the state cannot be written: without a record the hook
    would nag on every single edit, which is worse than staying quiet.
    """
    now = time.time() if now is None else now
    path = state_path()
    state: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = hook_json.loads(handle.read())
        if isinstance(loaded, dict):
            state = loaded
    except (OSError, ValueError):
        pass

    last = state.get("offline_notified_at")
    if isinstance(last, (int, float)) and 0 <= now - last < OFFLINE_NOTICE_INTERVAL:
        return False

    state["offline_notified_at"] = now
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(hook_json.dumps(state))
        # Atomic swap: two hooks firing at once must not leave a torn file.
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


# --- entrypoint ---------------------------------------------------------------


def _read_stdin() -> Any:
    # Bytes, decoded as UTF-8 (what Claude Code writes): text-mode stdin on
    # Windows decodes with the ANSI code page and mangles non-ASCII prompts.
    buffer = getattr(sys.stdin, "buffer", None)
    raw = buffer.read() if buffer is not None else sys.stdin.read()
    return hook_json.loads(raw or "{}")


def run_claude_code(port: int) -> int:
    """Handle one Claude Code hook invocation: stdin payload in, response out.

    Always returns 0. Claude Code reads exit 2 as "block", so any other exit
    status from a broken hook would be a Synergo outage stalling the agent.
    """
    try:
        payload = _read_stdin()
        if not isinstance(payload, dict):
            return 0
        event = claude_code.normalize_payload(payload)
        if event is None:
            return 0
        try:
            ack = post_event(event, port)
        except DaemonUnreachable as exc:
            output = claude_code.render_notice(event, str(exc))
            if output and claim_offline_notice():
                sys.stdout.write(output)
            return 0
        output = claude_code.render_ack(event, ack)
        if output:
            sys.stdout.write(output)
        return 0
    except BaseException:
        return 0
