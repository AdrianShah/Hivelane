"""Run and manage the per-machine Hivelane daemon.

One background daemon serves every repo on the machine (see hivelane.daemon).
The CLI starts it (`hivelane up`), the agent hook starts it lazily the first
time an agent works in a repo with a `.hivelane.toml`, and an autostart entry
can start it at login. This module is the API all of those use. It is not on
the hook's hot path, so urllib and subprocess are fine here; the one piece the
hook shares, the detached spawn itself, lives in hivelane.hook_fast.

Runnable as `python -m hivelane.service --port N [--log-file]`, which is what
every background start actually executes.
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from hivelane import auth, hook_fast
from hivelane.config import HivelaneConfig, hivelane_home

DEFAULT_PORT = hook_fast.DEFAULT_PORT
LOOPBACK = "127.0.0.1"
STATUS_TIMEOUT = 1.0
# A listening daemon completes a loopback connect in the kernel even while its
# event loop is busy, so a connect slower than this means nothing listens.
# Windows retries a refused loopback connect for the whole timeout instead of
# failing fast; without this cap every "is it running?" would cost a second.
CONNECT_PROBE = hook_fast.CONNECT_TIMEOUT
# Registering a repo may run git twice (display name, repo identity) inside
# the daemon, each bounded by hivelane.daemon.GIT_TIMEOUT.
REGISTER_TIMEOUT = 15.0
POLL_INTERVAL = 0.1

# daemon.log plus four rotations: five files of at most 5 MB each.
LOG_FILENAME = "daemon.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 4
LOG_TAIL_LINES = 25

LAUNCHD_LABEL = "dev.hivelane.daemon"
SYSTEMD_UNIT = "hivelane.service"
WINDOWS_STARTUP_SCRIPT = "hivelane-daemon.vbs"


class ServiceError(Exception):
    """The daemon could not be started, reached or changed. User-facing."""


class DaemonAuthError(ServiceError):
    """A daemon answers on the port but refuses this client's token."""


def auth_problem(port: int) -> str:
    """Why a daemon on port refused us, and what to do about it."""
    path = auth.token_path()
    if auth.read_token() is None:
        return (
            f"the daemon on {LOOPBACK}:{port} requires a token and {path} is missing or "
            "unreadable; run `hivelane down` then `hivelane up` to restart it with a fresh one"
        )
    return (
        f"the daemon on {LOOPBACK}:{port} refused the token in {path}. It was probably "
        "started with a different HIVELANE_HOME; use the same one, or stop that daemon"
    )


# A proxy-less opener: HTTP(S)_PROXY in the environment must never route a
# loopback call through some corporate proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# Daemons this process spawned, so liveness checks can reap them (a child that
# exited but was never waited on still "exists" as a zombie on POSIX).
_spawned: dict[int, subprocess.Popen[bytes]] = {}


# --- paths ---------------------------------------------------------------------


def log_path() -> Path:
    """The background daemon's rotating log."""
    return hivelane_home() / "logs" / LOG_FILENAME


def output_path() -> Path:
    """The background daemon's raw stdout/stderr (crashes before logging starts)."""
    return Path(hook_fast.spawn_output_path())


def pid_path() -> Path:
    return hivelane_home() / hook_fast.PID_FILENAME


def read_pid() -> Optional[int]:
    try:
        pid = int(pid_path().read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _write_pid(pid: int) -> None:
    try:
        pid_path().parent.mkdir(parents=True, exist_ok=True)
        pid_path().write_text(str(pid), encoding="ascii")
    except OSError:
        pass


def _remove_pid(only_if: Optional[int] = None) -> None:
    """Remove the pid file; with only_if, only while it still names that pid
    (a daemon exiting after losing a start race must not delete the winner's)."""
    if only_if is not None and read_pid() not in (None, only_if):
        return
    try:
        pid_path().unlink()
    except OSError:
        pass


# --- talking to the daemon -----------------------------------------------------


def _request(
    port: int,
    method: str,
    path: str,
    body: Any = None,
    timeout: float = STATUS_TIMEOUT,
    *,
    authenticate: bool = True,
) -> Any:
    """One JSON call to the daemon. The token is read from disk per call (it
    is a few bytes), so a regenerated token never needs a restart here. A 401
    becomes DaemonAuthError; other HTTP errors propagate as HTTPError."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if authenticate:
        headers.update(auth.auth_headers())
    request = urllib.request.Request(
        f"http://{LOOPBACK}:{port}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and authenticate:
            raise DaemonAuthError(auth_problem(port)) from None
        raise
    return json.loads(raw) if raw else None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read() or b"{}").get("detail")
    except (OSError, ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):
        # FastAPI validation errors: one line per field.
        detail = "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}"
            for item in detail
            if isinstance(item, dict)
        )
    return str(detail or exc.reason or exc.code)


_UNREACHABLE = (OSError, ValueError, http.client.HTTPException)


def status(port: int, timeout: float = 1.0) -> Optional[dict]:
    """The daemon's GET /status, or None if nothing usable answers on port.

    Raises DaemonAuthError when a daemon answers but refuses our token: that
    is not "no daemon", and treating it as one would start a second daemon
    that can only fail to bind the port.
    """
    if not _port_accepts(port, min(timeout, CONNECT_PROBE)):
        return None
    try:
        data = _request(port, "GET", "/status", timeout=timeout)
    except _UNREACHABLE:
        return None
    return data if isinstance(data, dict) else None


def health(port: int, timeout: float = 1.0) -> bool:
    """True when a Hivelane daemon answers on port (unauthenticated /health)."""
    if not _port_accepts(port, min(timeout, CONNECT_PROBE)):
        return False
    try:
        data = _request(port, "GET", "/health", timeout=timeout, authenticate=False)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            return False
        # A daemon from before /health existed: it still answers /status.
        try:
            return status(port, timeout) is not None
        except DaemonAuthError:
            return True
    except _UNREACHABLE:
        return False
    return isinstance(data, dict) and data.get("ok") is True


def is_running(port: int) -> bool:
    return health(port)


def _status_quietly(port: int, timeout: float = 1.0) -> tuple[Optional[dict], bool]:
    """(status or None, whether a daemon refused our token)."""
    try:
        return status(port, timeout), False
    except DaemonAuthError:
        return None, True


def _pid_from(current: dict) -> int:
    pid = current.get("pid")
    if isinstance(pid, int) and pid > 0:
        return pid
    return read_pid() or 0


# --- processes -------------------------------------------------------------------


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    process = _spawned.get(pid)
    if process is not None and process.poll() is not None:
        return False
    if os.name == "nt":
        # Not os.kill(pid, 0): on Windows that is TerminateProcess.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means it exists but belongs to someone else.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _looks_like_daemon(pid: int) -> bool:
    """Whether pid plausibly is a hivelane daemon (a recorded pid may have been
    reused by an unrelated process, which must never be killed). Checks the
    command line where the OS shows it (Linux /proc, `ps` elsewhere on POSIX)
    and the executable's name on Windows; True when it cannot tell."""
    command = ""
    try:
        if os.name == "nt":
            command = _windows_image_name(pid)
        elif Path(f"/proc/{pid}/cmdline").exists():
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(bytes(1), b" ").decode("utf-8", "replace")
        else:
            result = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            command = result.stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return True
    if not command.strip():
        return True
    lowered = command.lower()
    return "hivelane" in lowered or "python" in lowered


def _windows_image_name(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)
    return True


def _kill(pid: int) -> None:
    import signal

    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        return
    if os.name != "nt" and not _wait_dead(pid, 2.0):
        try:
            os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except OSError:
            pass


def _port_accepts(port: int, timeout: float = CONNECT_PROBE) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((LOOPBACK, port))
        except OSError:
            return False
    return True


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def log_tail(lines: int = LOG_TAIL_LINES) -> str:
    """The last lines of the daemon's log and raw output, for error messages."""
    chunks = []
    for path in (log_path(), output_path()):
        tail = _tail(path, lines)
        if tail:
            chunks.append(f"--- {path} ---\n" + "\n".join(tail))
    return "\n".join(chunks) or f"(nothing logged yet; see {log_path()})"


# --- lifecycle ---------------------------------------------------------------------


def start_background(port: int, *, wait: float = 8.0) -> int:
    """Start the daemon detached in the background and return its pid once it
    answers on port. An already-running daemon is left alone and its pid
    returned. Raises ServiceError (with the log tail) if it never comes up,
    and DaemonAuthError when the daemon already there refuses our token."""
    # Before the daemon exists, so whichever of us creates the token file, the
    # daemon and this CLI read the same one.
    try:
        auth.ensure_token()
    except OSError as exc:
        raise ServiceError(f"could not create the daemon token {auth.token_path()} ({exc})") from None
    current = status(port)
    if current is not None:
        return _pid_from(current)
    try:
        process = hook_fast.spawn_daemon(port)
    except OSError as exc:
        raise ServiceError(f"could not start the daemon ({exc})") from None
    _spawned[process.pid] = process
    deadline = time.monotonic() + wait
    while True:
        current = status(port, timeout=0.5)
        if current is not None:
            pid = _pid_from(current) or process.pid
            _write_pid(pid)
            return pid
        exit_code = process.poll()
        if exit_code is not None or time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL)
    why = (
        f"exited with status {exit_code}"
        if exit_code is not None
        else f"did not answer within {wait:.0f}s"
    )
    raise ServiceError(
        f"the daemon on {LOOPBACK}:{port} {why}. Log: {log_path()}\n{log_tail()}"
    )


def stop(port: int, timeout: float = 5.0) -> bool:
    """Stop the daemon on port: ask it to shut down gracefully (it flushes
    batched edits and closes its relay sockets), then fall back to killing the
    pid it reported. True when no daemon is left on port."""
    # A daemon that refuses our token cannot be asked to shut down; it is
    # treated like a hung one (killed by the pid we recorded, if it is ours).
    current, _ = _status_quietly(port)
    pid = _pid_from(current) if current is not None else read_pid()
    if current is None:
        # Nothing answers. A live pid only counts as ours if something still
        # holds the port (a hung daemon); otherwise the pid may have been
        # reused by an unrelated process, which must not be killed.
        if pid and _pid_alive(pid) and _port_accepts(port) and _looks_like_daemon(pid):
            _kill(pid)
            _wait_dead(pid, timeout)
        if not _port_accepts(port):
            _remove_pid()
            return True
        return False

    try:
        _request(port, "POST", "/shutdown", {}, timeout=timeout)
    except _UNREACHABLE:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not health(port, timeout=0.3) and not _pid_alive(pid):
            break
        time.sleep(POLL_INTERVAL)
    else:
        if pid and _pid_alive(pid):
            _kill(pid)
            _wait_dead(pid, 2.0)
    if health(port, timeout=0.3):
        return False
    _remove_pid()
    return True


# --- repos ---------------------------------------------------------------------------


def registration(root: Path, config: HivelaneConfig) -> dict[str, Any]:
    """POST /repos body for root under config. Team settings are sent only
    when a project file supplied them, so a repo without one never overrides
    the windows its room already uses."""
    return {
        "root": str(Path(root).expanduser().resolve()),
        "room": config.room,
        "relay": config.relay,
        "name": config.name,
        "agent": config.agent,
        "settings": config.room_settings() if config.root is not None else None,
        "ignore": list(config.ignore),
        "share_diffs": config.share_diffs,
        "redact": config.redact,
        "redact_patterns": list(config.redact_patterns),
        # Secrets, over the token-guarded loopback socket only: the e2e
        # passphrase (rooms that say e2e) and the owner token, if any.
        "room_key": config.room_key() if config.e2e else None,
        "owner_token": config.owner_token(),
    }


def connect_repo(port: int, root: Path, config: HivelaneConfig) -> dict:
    """Register root with the running daemon (replacing any earlier
    registration) and return its /status entry."""
    if not config.room:
        raise ServiceError(f"no room is configured for {root}")
    if not _port_accepts(port):
        raise ServiceError(f"no daemon answering on {LOOPBACK}:{port}")
    try:
        entry = _request(port, "POST", "/repos", registration(root, config), timeout=REGISTER_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise ServiceError(f"the daemon refused {root}: {_error_detail(exc)}") from None
    except _UNREACHABLE as exc:
        raise ServiceError(f"no daemon answering on {LOOPBACK}:{port} ({exc})") from None
    if not isinstance(entry, dict):
        raise ServiceError("the daemon sent an unexpected reply")
    return entry


def disconnect_repo(port: int, root: Path) -> bool:
    """Stop coordinating root. True if the daemon had it registered; False if
    it did not, or no daemon is running."""
    body = {"root": str(Path(root).expanduser().resolve())}
    if not _port_accepts(port):
        return False
    try:
        result = _request(port, "POST", "/repos/remove", body, timeout=REGISTER_TIMEOUT)
    except _UNREACHABLE:
        return False
    return bool(isinstance(result, dict) and result.get("removed"))


# --- serving -------------------------------------------------------------------------


class _LogStream(io.TextIOBase):
    """sys.stdout/stderr for a console-less daemon: whole lines into logging.

    The daemon reports through print(), which suits `hivelane serve` in a
    terminal; in the background the same lines land in the rotating log.
    """

    def __init__(self, logger: Any, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""
        self._lock = threading.Lock()
        self._local = threading.local()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        # A failing handler reports on stderr, which is us: drop, not recurse.
        if getattr(self._local, "busy", False):
            return len(text)
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        self._local.busy = True
        try:
            for line in lines:
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        finally:
            self._local.busy = False
        return len(text)

    def flush(self) -> None:
        return None


def _log_to_file() -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.raiseExceptions = False
    logger = logging.getLogger("hivelane")
    # Before uvicorn configures its own handlers, which bind to sys.stderr as
    # it is at that moment.
    sys.stdout = _LogStream(logger, logging.INFO)
    sys.stderr = _LogStream(logger, logging.WARNING)


def serve(port: int, *, log_file: bool = False) -> int:
    """Run the multi-repo daemon in this process until it is stopped
    (POST /shutdown, `stop()`, Ctrl+C). Returns the exit status.

    Exits 0 at once if a daemon already answers on port, so racing starts
    (two hooks, a login item plus `hivelane up`) leave exactly one running.
    """
    if log_file:
        _log_to_file()
    if health(port, timeout=0.5):
        existing, _ = _status_quietly(port, timeout=0.5)
        print(
            f"hivelane: a daemon is already serving {LOOPBACK}:{port} "
            f"(pid {(existing or {}).get('pid', '?')}); not starting another"
        )
        return 0

    import asyncio

    from hivelane.daemon import Daemon

    daemon = Daemon(port=port)
    pid = os.getpid()
    _write_pid(pid)
    if log_file:
        print(f"hivelane: daemon {pid} starting (log {log_path()})")
    try:
        code = asyncio.run(daemon.run())
    except KeyboardInterrupt:
        return 0
    finally:
        _remove_pid(only_if=pid)
    if code != 0 and _port_accepts(port) and not health(port, timeout=0.5):
        # Another program holds the port. Exit 0: launchd (KeepAlive on a
        # failed exit) and systemd (Restart=on-failure) would otherwise
        # restart us every few seconds, forever, to fail the same way.
        print(
            f"hivelane: port {port} is in use by another program; not serving "
            "(set HIVELANE_PORT to use another port)",
            file=sys.stderr,
        )
        return 0
    return code


# --- autostart ---------------------------------------------------------------------


def _target(platform: Optional[str]) -> str:
    name = platform or sys.platform
    if name.startswith("darwin") or name == "macos":
        return "macos"
    if name.startswith("win") or name == "windows":
        return "windows"
    return "linux"


def autostart_command(port: int, *, platform: Optional[str] = None) -> list[str]:
    return hook_fast.daemon_command(port, windowless=_target(platform) == "windows")


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / SYSTEMD_UNIT


def windows_startup_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (
        Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        / WINDOWS_STARTUP_SCRIPT
    )


def _home_override() -> dict[str, str]:
    """HIVELANE_HOME as it is now, so the login-started daemon uses the same
    state directory as the CLI that installed it."""
    value = os.environ.get("HIVELANE_HOME")
    return {"HIVELANE_HOME": value} if value else {}


def launchd_plist(port: int) -> str:
    plist: dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": autostart_command(port, platform="macos"),
        "RunAtLoad": True,
        # Restart after a crash, not after `hivelane down` (a clean exit).
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "WorkingDirectory": str(hivelane_home()),
        "StandardOutPath": str(output_path()),
        "StandardErrorPath": str(output_path()),
    }
    env = _home_override()
    if env:
        plist["EnvironmentVariables"] = env
    return plistlib.dumps(plist).decode("utf-8")


def _systemd_path(path: str) -> str:
    """A path for a systemd path setting (WorkingDirectory=). systemd does not
    unquote those (only Exec*= and Environment= are split and unquoted), so
    a quoted value is not absolute and the unit is rejected. Written bare:
    the value runs to the end of the line, spaces included; only % (a
    specifier) needs escaping."""
    return path.replace("%", "%%")


def _systemd_quote(arg: str) -> str:
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def systemd_unit(port: int) -> str:
    exec_start = " ".join(_systemd_quote(a) for a in autostart_command(port, platform="linux"))
    lines = [
        "[Unit]",
        "Description=Hivelane daemon (coordinates AI coding agents across your repos)",
        # network-online.target does not exist in a user manager; the
        # daemon reconnects on its own once the network is up.
        # A port held by something else exits 0 (no restart loop); anything
        # else that keeps failing gives up after 5 tries in 5 minutes.
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        # Restart after a crash, not after `hivelane down` (a clean exit).
        "Restart=on-failure",
        "RestartSec=5",
        f"WorkingDirectory={_systemd_path(str(hivelane_home()))}",
    ]
    for key, value in _home_override().items():
        lines.append(f"Environment={_systemd_quote(f'{key}={value}')}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _vbs_string(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def windows_startup_script(port: int) -> str:
    """A Startup-folder .vbs: runs at login with no admin rights (unlike an
    ONLOGON scheduled task) and, unlike a .cmd, flashes no console window."""
    command = subprocess.list2cmdline(autostart_command(port, platform="windows"))
    lines = [
        "' Hivelane daemon autostart, written by `hivelane service install`.",
        "' Delete this file (or run `hivelane service uninstall`) to stop it.",
        'Set shell = CreateObject("WScript.Shell")',
    ]
    for key, value in _home_override().items():
        lines.append(f'shell.Environment("Process")({_vbs_string(key)}) = {_vbs_string(value)}')
    lines += [
        f"shell.CurrentDirectory = {_vbs_string(str(hivelane_home()))}",
        f"shell.Run {_vbs_string(command)}, 0, False",
        "",
    ]
    return "\r\n".join(lines)


def _run(command: list[str]) -> tuple[bool, str]:
    """Run a service-manager command; (succeeded, its output)."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def install_autostart(port: int, *, platform: Optional[str] = None) -> str:
    """Start the daemon at login for this user (no admin rights anywhere).
    Returns what was done, for the CLI to print."""
    target = _target(platform)
    output_path().parent.mkdir(parents=True, exist_ok=True)

    if target == "macos":
        path = launchd_plist_path()
        _write(path, launchd_plist(port))
        if shutil.which("launchctl") is None:
            return f"wrote launchd agent {path}; it starts the daemon at your next login"
        _run(["launchctl", "unload", str(path)])
        ok, detail = _run(["launchctl", "load", "-w", str(path)])
        if ok:
            return f"installed launchd agent {path}: the daemon starts at login (and is starting now)"
        return f"wrote launchd agent {path}, but `launchctl load` failed ({detail}); it applies at next login"

    if target == "linux":
        path = systemd_unit_path()
        _write(path, systemd_unit(port))
        if shutil.which("systemctl") is None:
            return (
                f"wrote systemd user unit {path}; systemctl was not found, so enable it "
                f"yourself with `systemctl --user enable --now {SYSTEMD_UNIT}`"
            )
        _run(["systemctl", "--user", "daemon-reload"])
        ok, detail = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        if ok:
            return f"installed and enabled systemd user unit {path}: the daemon starts at login"
        return (
            f"wrote systemd user unit {path}, but enabling it failed ({detail}); "
            f"try `systemctl --user enable --now {SYSTEMD_UNIT}`"
        )

    path = windows_startup_path()
    _write(path, windows_startup_script(port))
    return f"added {path}: the daemon starts hidden at login"


def uninstall_autostart(*, platform: Optional[str] = None) -> str:
    """Remove the login item install_autostart created. Leaves a running
    daemon alone except where the service manager owns it (systemd stops the
    unit it disabled)."""
    target = _target(platform)
    if target == "macos":
        path = launchd_plist_path()
        if not path.exists():
            return "no launchd agent was installed"
        if shutil.which("launchctl") is not None:
            _run(["launchctl", "unload", "-w", str(path)])
        path.unlink()
        return f"removed launchd agent {path}"

    if target == "linux":
        path = systemd_unit_path()
        if not path.exists():
            return "no systemd user unit was installed"
        if shutil.which("systemctl") is not None:
            _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        path.unlink()
        if shutil.which("systemctl") is not None:
            _run(["systemctl", "--user", "daemon-reload"])
        return f"removed systemd user unit {path}"

    path = windows_startup_path()
    if not path.exists():
        return "no Startup-folder entry was installed"
    path.unlink()
    return f"removed {path}"


# --- entrypoint ------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hivelane.service",
        description="Run the per-machine Hivelane daemon in the foreground.",
    )
    parser.add_argument("--port", type=int, default=None, help=f"loopback port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--log-file",
        action="store_true",
        help="log to HIVELANE_HOME/logs/daemon.log (rotated) instead of the console",
    )
    args = parser.parse_args(argv)
    port = args.port
    if port is None:
        try:
            port = int(os.environ.get("HIVELANE_PORT") or DEFAULT_PORT)
        except ValueError:
            port = DEFAULT_PORT
    return serve(port, log_file=args.log_file)


if __name__ == "__main__":
    raise SystemExit(main())
