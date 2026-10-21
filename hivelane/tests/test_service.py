"""hivelane.service: the background daemon's lifecycle against a real detached
process, the repo API on top of it, and the autostart files it writes.

Every test gets its own HIVELANE_HOME and port, and any daemon started is
stopped (or killed) in teardown so none outlives the test run.
"""

from __future__ import annotations

import os
import plistlib
import socket
import sys
import time
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hivelane import auth, hook_fast, service
from hivelane.config import HivelaneConfig


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "hivelane-home"
    monkeypatch.setenv("HIVELANE_HOME", str(path))
    monkeypatch.delenv("HIVELANE_NAME", raising=False)
    monkeypatch.delenv("HIVELANE_RELAY", raising=False)
    return path


@pytest.fixture
def port(home: Path) -> Iterator[int]:
    """A free port; whatever daemon ends up on it is gone after the test."""
    number = free_port()
    yield number
    pids = {service.read_pid()}
    current = service.status(number)
    if current is not None:
        pids.add(current.get("pid"))
        service.stop(number, timeout=5.0)
    for pid in pids:
        if isinstance(pid, int) and service._pid_alive(pid):
            service._kill(pid)


def test_nothing_running_is_reported_quickly(port: int, tmp_path: Path) -> None:
    started = time.monotonic()
    assert service.status(port) is None
    assert service.is_running(port) is False
    assert service.disconnect_repo(port, tmp_path) is False
    assert service.stop(port) is True  # nothing left on the port
    # A refused loopback connect takes ~2 s on Windows; uncapped, these four
    # calls would take ~8 s. The bound is loose enough for a slow runner.
    assert time.monotonic() - started < 5.0
    assert service.log_path() == Path(os.environ["HIVELANE_HOME"]) / "logs" / "daemon.log"


def test_background_daemon_lifecycle(port: int, home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    pid = service.start_background(port, wait=20.0)

    assert pid > 0
    status = service.status(port)
    assert status is not None
    assert (status["pid"], status["port"], status["repos"]) == (pid, port, [])
    assert status["version"]
    assert service.is_running(port)
    assert service.read_pid() == pid
    # Already running: the same daemon, not a second one.
    assert service.start_background(port) == pid

    # The token the daemon demands is the one in HIVELANE_HOME, and nothing
    # else gets past /health.
    token = auth.read_token()
    assert token and (home / "token").is_file()
    if os.name != "nt":
        assert (home / "token").stat().st_mode & 0o777 == 0o600
    assert service.health(port)
    with pytest.raises(urllib.error.HTTPError) as refused:
        service._request(port, "GET", "/status", authenticate=False)
    assert refused.value.code == 401
    # serve() in another process (a login item, a racing hook) backs off too.
    assert service.serve(port) == 0

    config = HivelaneConfig(
        room="TEAM1",
        relay=f"ws://127.0.0.1:{free_port()}/ws",  # nobody there: stays connecting
        name="ada",
        block_window_seconds=10.0,
        warn_window_seconds=30.0,
        ignore=("*.lock",),
        root=repo,
    )
    entry = service.connect_repo(port, repo, config)
    assert entry["root"] == str(repo.resolve())
    assert (entry["room"], entry["name"], entry["connected"], entry["fatal"]) == ("TEAM1", "ada", False, None)
    assert entry["settings"] == {"block_window_seconds": 10.0, "warn_window_seconds": 30.0, "e2e": False}
    assert entry["ignore"] == ["*.lock"]
    assert [r["root"] for r in service.status(port)["repos"]] == [str(repo.resolve())]

    with pytest.raises(service.ServiceError, match="agent"):
        service.connect_repo(port, repo, HivelaneConfig(room="TEAM1", agent="vim", name="ada", root=repo))
    with pytest.raises(service.ServiceError, match="no room"):
        service.connect_repo(port, repo, HivelaneConfig(name="ada"))

    assert service.disconnect_repo(port, repo) is True
    assert service.disconnect_repo(port, repo) is False

    assert service.stop(port) is True
    assert service.is_running(port) is False
    assert not service.pid_path().exists()
    log = service.log_path().read_text(encoding="utf-8")
    assert "listening on http://127.0.0.1" in log
    assert f"registered {repo.resolve()}" in log


def test_a_daemon_that_dies_on_start_is_reported_with_its_output(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hook_fast,
        "daemon_command",
        lambda port, windowless=None: [sys.executable, "-c", "print('boom: no relay'); raise SystemExit(3)"],
    )
    with pytest.raises(service.ServiceError) as caught:
        service.start_background(port, wait=20.0)
    message = str(caught.value)
    assert "exited with status 3" in message
    assert "boom: no relay" in message


def test_stale_pid_file_is_cleaned_up_without_killing_anything(port: int, home: Path) -> None:
    home.mkdir(parents=True)
    # Our own pid: alive, but it does not hold the port, so it is not "the daemon".
    service.pid_path().write_text(str(os.getpid()), encoding="ascii")
    assert service.stop(port) is True
    assert not service.pid_path().exists()


# --- autostart -------------------------------------------------------------------


@pytest.fixture
def user_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path) -> dict[str, Any]:
    """Home, APPDATA and XDG dirs in tmp; service-manager commands recorded,
    never run."""
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    monkeypatch.setenv("APPDATA", str(user / "AppData" / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user / ".config"))
    commands: list[list[str]] = []
    monkeypatch.setattr(service, "_run", lambda command: (commands.append(command), (True, ""))[1])
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    return {"user": user, "commands": commands}


def test_launchd_agent(user_dirs: dict[str, Any], home: Path) -> None:
    message = service.install_autostart(4601, platform="darwin")

    path = user_dirs["user"] / "Library" / "LaunchAgents" / "dev.hivelane.daemon.plist"
    assert str(path) in message
    plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == "dev.hivelane.daemon"
    assert plist["ProgramArguments"][1:] == ["-m", "hivelane.service", "--port", "4601", "--log-file"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["EnvironmentVariables"] == {"HIVELANE_HOME": str(home)}
    assert ["launchctl", "load", "-w", str(path)] in user_dirs["commands"]

    assert "removed" in service.uninstall_autostart(platform="darwin")
    assert not path.exists()
    assert "no launchd agent" in service.uninstall_autostart(platform="darwin")


def test_systemd_user_unit(user_dirs: dict[str, Any], home: Path) -> None:
    message = service.install_autostart(4601, platform="linux")

    path = user_dirs["user"] / ".config" / "systemd" / "user" / "hivelane.service"
    assert "enabled" in message
    unit = path.read_text(encoding="utf-8")
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_start.endswith('"-m" "hivelane.service" "--port" "4601" "--log-file"')
    assert exec_start.startswith(f'ExecStart="{sys.executable.replace(chr(92), chr(92) * 2)}"')
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert f'Environment="HIVELANE_HOME={str(home).replace(chr(92), chr(92) * 2)}"' in unit
    assert ["systemctl", "--user", "enable", "--now", "hivelane.service"] in user_dirs["commands"]

    service.uninstall_autostart(platform="linux")
    assert not path.exists()
    assert ["systemctl", "--user", "disable", "--now", "hivelane.service"] in user_dirs["commands"]


def test_systemd_unit_without_systemctl_says_how_to_enable(
    user_dirs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    message = service.install_autostart(4601, platform="linux")
    assert "systemctl --user enable --now hivelane.service" in message
    assert user_dirs["commands"] == []


def test_windows_startup_script(user_dirs: dict[str, Any], home: Path) -> None:
    message = service.install_autostart(4601, platform="win32")

    path = (
        user_dirs["user"] / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
        / "Programs" / "Startup" / "hivelane-daemon.vbs"
    )
    assert str(path) in message
    script = path.read_text(encoding="utf-8")
    run_line = next(line for line in script.splitlines() if line.startswith("shell.Run "))
    assert run_line.endswith('-m hivelane.service --port 4601 --log-file", 0, False')
    assert f'shell.Environment("Process")("HIVELANE_HOME") = "{home}"' in script
    # No admin rights and no service manager involved.
    assert user_dirs["commands"] == []

    assert "removed" in service.uninstall_autostart(platform="win32")
    assert not path.exists()
