"""The `synergo hook <agent>` hot path, standard library only.

The agent (Claude Code, Codex, ...) starts the hook as a fresh process before and after every edit, so
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

Lazy start: when nothing listens on the daemon port and the agent's cwd is in
a project with a `.synergo.toml`, the hook starts the background daemon itself
(detached, without waiting) and lets this one event through silently. The
spawn branch is the only place `subprocess` is imported: it costs tens of
milliseconds, paid once per daemon start rather than once per edit.
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
from synergo import adapters
from synergo.adapters import base

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

# Must match synergo.config.PROJECT_FILE (asserted in the tests); importing it
# from there would pull tomllib into every hook.
PROJECT_FILE = ".synergo.toml"
# A burst of hooks finding the daemon down (one prompt fans out into several
# edits) must start one daemon, not one each; and a daemon that fails to start
# must not be respawned around every single edit.
SPAWN_LOCK_FILENAME = "spawn.lock"
SPAWN_LOCK_SECONDS = 15.0
PID_FILENAME = "daemon.pid"
LOG_DIRNAME = "logs"
# The background daemon's raw stdout/stderr: tracebacks from before its own
# rotating log (daemon.log) is set up. Truncated once it outgrows this.
SPAWN_OUTPUT_FILENAME = "daemon.out.log"
SPAWN_OUTPUT_LIMIT = 1 << 20

OFFLINE_NOT_RUNNING = (
    "Synergo offline: the local daemon isn't running, so edits aren't being coordinated. "
    "Run `synergo up` in your repo (or `synergo doctor`)."
)
OFFLINE_NOT_RESPONDING = (
    "Synergo offline: the local daemon isn't responding, so edits aren't being coordinated. "
    "Check `synergo status`, or restart it with `synergo up`."
)


class DaemonUnreachable(Exception):
    """Nothing answered on the daemon port in time; the message is user-facing.

    `not_running` means the connect itself failed (nothing listening), as
    opposed to a daemon that accepted but never answered: only the former is
    worth starting a daemon over.
    """

    def __init__(self, message: str, *, not_running: bool = False) -> None:
        super().__init__(message)
        self.not_running = not_running


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
            raise DaemonUnreachable(OFFLINE_NOT_RUNNING, not_running=True) from exc

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


# --- lazy start -----------------------------------------------------------------


def find_project_file(cwd: str) -> Optional[str]:
    """Nearest .synergo.toml at or above cwd, by plain os.path walking (no
    pathlib, no tomllib: this runs on the hook path whenever the daemon is
    down). Existence is all that is checked; the daemon parses it."""
    if not cwd:
        return None
    try:
        current = os.path.abspath(cwd)
    except (OSError, ValueError):
        return None
    while True:
        candidate = os.path.join(current, PROJECT_FILE)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def spawn_lock_path() -> str:
    return os.path.join(synergo_home(), SPAWN_LOCK_FILENAME)


def pid_path() -> str:
    return os.path.join(synergo_home(), PID_FILENAME)


def spawn_output_path() -> str:
    return os.path.join(synergo_home(), LOG_DIRNAME, SPAWN_OUTPUT_FILENAME)


def claim_spawn(now: Optional[float] = None) -> bool:
    """True if this hook may start the daemon, recording that it is doing so.

    At most one claim per SPAWN_LOCK_SECONDS, judged by the lock file's mtime.
    A fresh lock is created with O_EXCL, so of several hooks racing on a
    missing lock exactly one wins. Returns False when the lock cannot be
    written: an unrecorded spawn could repeat on every edit.
    """
    now = time.time() if now is None else now
    path = spawn_lock_path()
    try:
        age: Optional[float] = now - os.stat(path).st_mtime
    except OSError:
        age = None
    if age is not None:
        # Either side of now: a coarse filesystem clock can date a lock a
        # little in the future, and that is still a fresh lock.
        if -SPAWN_LOCK_SECONDS < age < SPAWN_LOCK_SECONDS:
            return False
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        os.makedirs(synergo_home(), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def daemon_command(port: int, *, windowless: Optional[bool] = None) -> list[str]:
    """argv for the background daemon: `python -m synergo.service`, logging to
    its rotating file. On Windows pythonw.exe when it exists, so no console
    window ever appears for a process the user never started by hand."""
    executable = sys.executable
    if windowless is None:
        windowless = os.name == "nt"
    if windowless:
        candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.isfile(candidate):
            executable = candidate
    return [executable, "-m", "synergo.service", "--port", str(port), "--log-file"]


def spawn_daemon(port: int) -> Any:
    """Start the background daemon fully detached and return its Popen.

    Detached means: no console, its own process group/session (a Ctrl+C or a
    hook runner reaping the hook's group must not take it down), stdin from
    the null device and output to a file — never the hook's own stdout pipe,
    which Claude Code reads to EOF and would then wait on for the daemon's
    whole life. Its cwd is SYNERGO_HOME so it never pins a repo directory
    (Windows cannot delete a directory a process is running in).

    subprocess is imported here, not at module level: it drags in enum, re and
    signal, which every other hook invocation must not pay for.
    """
    import subprocess

    home = synergo_home()
    output_path = spawn_output_path()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    try:
        mode = "wb" if os.path.getsize(output_path) > SPAWN_OUTPUT_LIMIT else "ab"
    except OSError:
        mode = "ab"
    command = daemon_command(port)
    with open(output_path, mode) as output:
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": output,
            "stderr": subprocess.STDOUT,
            "cwd": home,
            "close_fds": True,
        }
        if os.name == "nt":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            try:
                # Out of any job object the hook runs in, so the daemon is not
                # killed along with it; refused when the job forbids breakaway.
                process = subprocess.Popen(
                    command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options
                )
            except OSError:
                process = subprocess.Popen(command, creationflags=flags, **options)
        else:
            process = subprocess.Popen(command, start_new_session=True, **options)
    try:
        with open(pid_path(), "w", encoding="ascii") as handle:
            handle.write(str(process.pid))
    except OSError:
        pass
    return process


def maybe_start_daemon(cwd: str, port: int) -> bool:
    """Start the daemon if this cwd belongs to a Synergo project and nobody
    else started one in the last SPAWN_LOCK_SECONDS. True means this event
    started it, so the caller lets the event through without the offline
    notice: the daemon is on its way, not missing."""
    if find_project_file(cwd) is None:
        return False
    if not claim_spawn():
        return False
    try:
        spawn_daemon(port)
    except Exception:  # noqa: BLE001 - the hook must fail open, whatever broke
        return False
    return True


# --- entrypoint ---------------------------------------------------------------


def _read_stdin() -> Any:
    # Bytes, decoded as UTF-8 (what Claude Code writes): text-mode stdin on
    # Windows decodes with the ANSI code page and mangles non-ASCII prompts.
    buffer = getattr(sys.stdin, "buffer", None)
    raw = buffer.read() if buffer is not None else sys.stdin.read()
    return hook_json.loads(raw or "{}")


def run_agent(agent_id: str, port: int) -> int:
    """Handle one hook invocation for any registered adapter: stdin payload in,
    response out.

    One native call may yield several events (a Codex patch touching several
    files): each is posted, and the strictest ack decides. Returns the
    adapter's exit code, which is 0 for everything but an explicit block in
    an agent that blocks by exit status; any failure here means exit 0 —
    several agents read exit 2 as "block", so a broken hook must never stall
    the agent.
    """
    try:
        adapter = adapters.load(agent_id)
        payload = _read_stdin()
        events = adapter.normalize(payload)
        if not events:
            return 0
        results: list[Any] = []
        for event in events:
            try:
                ack = post_event(event, port)
            except DaemonUnreachable as exc:
                if exc.not_running and maybe_start_daemon(str(event.get("cwd") or ""), port):
                    return 0
                notice = adapter.render_notice(event, str(exc))
                if notice.stdout and claim_offline_notice():
                    sys.stdout.write(notice.stdout)
                return 0
            except (BadResponse, ValueError, OSError):
                ack = None  # unreadable ack: this file reads as allow
            results.append((event, ack))
        chosen = base.strictest(results)
        if chosen is None:
            return 0
        output = adapter.render(chosen[0], chosen[1])
        if output.stdout:
            sys.stdout.write(output.stdout)
        return output.exit_code if output.exit_code in (0, 2) else 0
    except BaseException:
        return 0


def run_claude_code(port: int) -> int:
    """Back-compat entry: `run_agent("claude-code", port)`."""
    return run_agent("claude-code", port)
