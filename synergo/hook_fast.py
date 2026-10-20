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

Auth: every POST carries `X-Synergo-Token`, the contents of
SYNERGO_HOME/token (see synergo.auth), read fresh with a plain open() on each
invocation: no cache survives a process, so a regenerated token is picked up
by the very next edit. A missing token file fails open like a missing daemon.

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
# One invocation's whole budget across every event it posts (a Codex patch
# can touch many files). Every agent kills a PreToolUse hook after 5 s (the
# timeout synergo installs), and a killed hook loses any block it found, so
# posting stops here and the strictest ack so far decides. Leaves room for the
# interpreter's own start-up.
HOOK_TOTAL_BUDGET = 3.0
# Below this much budget a further post is not worth starting.
MIN_POST_BUDGET = 0.05
# Loopback connects complete in the kernel even when the daemon's event loop is
# busy, so a slow connect means nothing is listening. Windows retries a refused
# loopback SYN for the full timeout instead of failing fast, so without this cap
# a stopped daemon would add HOOK_POST_TIMEOUT to every single edit.
CONNECT_TIMEOUT = 0.25
MAX_RESPONSE_BYTES = 1 << 20

OFFLINE_NOTICE_INTERVAL = 600.0
STATE_FILENAME = "hook_state.json"

# Must match synergo.auth (asserted in the tests); importing it from there
# would pull pathlib and secrets into every hook.
TOKEN_FILENAME = "token"
TOKEN_HEADER = "X-Synergo-Token"
# The token is url-safe base64; anything else in the file is not ours and is
# never written into a request header. The same rule as synergo.auth (asserted
# in the tests): one token is valid for both or for neither.
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
MIN_TOKEN_CHARS = 32
MAX_TOKEN_CHARS = 512

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
OFFLINE_REJECTED = (
    "Synergo offline: the local daemon rejected this hook's request (HTTP {status}), so edits "
    "aren't being coordinated. Restart it with `synergo up` (an older daemon may still be running)."
)
OFFLINE_UNAUTHORIZED = (
    "Synergo offline: the local daemon refused this hook's token, so edits aren't being "
    "coordinated. Run `synergo doctor` (the hook and the daemon must share SYNERGO_HOME)."
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
    if parts[1] == b"401":
        # Not a broken ack: a daemon that will refuse every edit's check until
        # someone fixes the token. Worth the (rate-limited) offline notice.
        raise DaemonUnreachable(OFFLINE_UNAUTHORIZED)
    if parts[1] != b"200":
        # Version skew (an old daemon rejecting a newer hook's event with
        # 422), a crashing daemon (500): every edit would be silently allowed
        # forever, so it is worth the (rate-limited) notice.
        raise DaemonUnreachable(OFFLINE_REJECTED.format(status=parts[1].decode("ascii", "replace")[:3]))
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        key = name.strip().lower()
        if key == b"content-length" and key in headers:
            raise BadResponse("duplicate Content-Length")
        headers[key] = value.strip()
    encoding = headers.get(b"transfer-encoding", b"").lower()
    if encoding == b"chunked":
        body = _dechunk(body)
    elif encoding:
        raise BadResponse(f"unsupported Transfer-Encoding {encoding!r}")
    elif b"content-length" in headers:
        length = int(headers[b"content-length"])
        if length < 0:
            raise BadResponse("negative Content-Length")
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


def _expected_length(raw: Any) -> int:
    """Total response size once the header is in and carries a Content-Length,
    so we need not wait for the server's FIN; -1 while the header is still
    incomplete; past MAX_RESPONSE_BYTES (read to EOF) without a usable length."""
    end = raw.find(b"\r\n\r\n")
    if end < 0:
        return -1
    for line in bytes(raw[:end]).split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                break
            if length >= 0:
                return end + 4 + length
            break
    return MAX_RESPONSE_BYTES + 1


def post_event(
    event: dict[str, Any],
    port: int,
    timeout: float = HOOK_POST_TIMEOUT,
    token: Optional[str] = None,
) -> Any:
    """POST one NormalizedEvent-shaped dict to the daemon; returns the parsed ack.

    `token` defaults to the token file's contents, read now and never cached.
    Raises DaemonUnreachable when nothing answers or the daemon refuses the
    token (the offline notice cases) and BadResponse / ValueError / OSError
    for anything else, all of which the caller swallows.
    """
    if token is None:
        token = read_token()
    auth = f"{TOKEN_HEADER}: {token}\r\n" if token else ""
    body = hook_json.dumps(event).encode("utf-8")
    request = (
        f"POST /events HTTP/1.1\r\n"
        f"Host: {LOOPBACK}:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{auth}"
        f"Connection: close\r\n\r\n"
    ).encode("ascii") + body

    deadline = time.monotonic() + timeout
    sock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        try:
            sock.settimeout(min(CONNECT_TIMEOUT, timeout))
            sock.connect((LOOPBACK, port))
        except OSError as exc:
            # Refused, or (on Windows, which retries a refused loopback SYN
            # until the timeout) a connect that never completes.
            raise DaemonUnreachable(OFFLINE_NOT_RUNNING, not_running=True) from exc

        buffer = bytearray()
        expected = -1  # total bytes once the header (with a Content-Length) is in
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
                buffer += chunk
                if len(buffer) > MAX_RESPONSE_BYTES:
                    raise BadResponse("response too large")
                # The header is parsed once, so a big body costs no
                # re-scanning per recv.
                if expected < 0:
                    expected = _expected_length(buffer)
                if 0 <= expected <= len(buffer):
                    break
        except TimeoutError as exc:
            raise DaemonUnreachable(OFFLINE_NOT_RESPONDING) from exc
    finally:
        sock.close()
    return _parse_response(bytes(buffer))


# --- offline notice -----------------------------------------------------------


def synergo_home() -> str:
    return os.environ.get("SYNERGO_HOME") or os.path.join(os.path.expanduser("~"), ".synergo")


def state_path() -> str:
    return os.path.join(synergo_home(), STATE_FILENAME)


def token_path() -> str:
    return os.path.join(synergo_home(), TOKEN_FILENAME)


def read_token() -> Optional[str]:
    """The daemon token from SYNERGO_HOME/token, or None (missing, unreadable,
    or not a token): the request then goes out without one and the daemon's
    refusal fails open like any other."""
    try:
        with open(token_path(), encoding="ascii") as handle:
            text = handle.read(MAX_TOKEN_CHARS + 2).strip()
    except (OSError, ValueError):
        return None
    if not MIN_TOKEN_CHARS <= len(text) <= MAX_TOKEN_CHARS or not _TOKEN_CHARS.issuperset(text):
        return None
    return text


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
        # Move the stale lock aside under a name of our own instead of
        # removing it: between our stat and a remove, another hook may have
        # replaced it with a fresh lock, which a plain remove would delete
        # (letting both of us win). A rename is atomic, so we can look at
        # what we actually took and hand back a fresh lock we took by mistake.
        aside = f"{path}.{os.getpid()}.stale"
        try:
            os.replace(path, aside)
        except OSError:
            return False  # someone else got to it first
        try:
            taken_age = now - os.stat(aside).st_mtime
        except OSError:
            taken_age = SPAWN_LOCK_SECONDS
        if -SPAWN_LOCK_SECONDS < taken_age < SPAWN_LOCK_SECONDS:
            try:
                os.replace(aside, path)
            except OSError:
                pass
            return False
        try:
            os.remove(aside)
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
            # DETACHED_PROCESS already means no console (CREATE_NO_WINDOW is
            # ignored alongside it, per the CreateProcess docs).
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
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
    # No pid file write here: the daemon writes its own once it serves.
    # Popen.pid is the pythonw.exe redirector's pid in a Windows venv, and a
    # daemon that exits because one is already serving would leave it behind.
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
    adapter: Any = None
    payload: Any = None
    wrote = False
    try:
        adapter = adapters.load(agent_id)
        payload = _read_stdin()
        events = adapter.normalize(payload)
        if not events:
            return 0
        results: list[Any] = []
        failure: Optional[tuple[dict[str, Any], DaemonUnreachable]] = None
        deadline = time.monotonic() + HOOK_TOTAL_BUDGET
        for event in events:
            remaining = deadline - time.monotonic()
            if remaining < MIN_POST_BUDGET:
                break  # out of budget: the strictest ack so far decides
            try:
                ack = post_event(event, port, timeout=min(HOOK_POST_TIMEOUT, remaining))
            except DaemonUnreachable as exc:
                failure = (event, exc)
                break
            except (BadResponse, ValueError, OSError):
                ack = None  # unreadable ack: this file reads as allow
            results.append((event, ack))
            if base.ack_decision(ack) == "block":
                # One blocked file denies the whole call: checking the rest
                # would only record claims for edits that never happen.
                break
        chosen = base.strictest(results)
        if failure is not None and (chosen is None or base.ack_decision(chosen[1]) == "allow"):
            # Nothing stricter than allow was learnt before the daemon went
            # away: this is the offline case. A block or warn already in hand
            # still decides, below — it must never be lost to a later failure.
            event, exc = failure
            if exc.not_running and maybe_start_daemon(str(event.get("cwd") or ""), port):
                return 0
            notice = adapter.render_notice(event, str(exc))
            if notice.stdout and claim_offline_notice():
                sys.stdout.write(notice.stdout)
                wrote = True
            return 0
        if chosen is None:
            return 0
        output = adapter.render(chosen[0], chosen[1])
        if output.stdout:
            sys.stdout.write(output.stdout)
            wrote = True
        return output.exit_code if output.exit_code in (0, 2) else 0
    except BaseException:
        return 0
    finally:
        # Some agents (Cursor) need a well-formed answer even when synergo
        # decides nothing; theirs is a no-decision form, never an approval.
        if not wrote and adapter is not None:
            try:
                text = adapter.passthrough(payload)
                if text:
                    sys.stdout.write(text)
            except BaseException:  # noqa: BLE001
                pass

