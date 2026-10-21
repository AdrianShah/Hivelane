"""`hivelane relay`: a relay (and the bundled dashboard) for a LAN, run in-process."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from hivelane import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT = 30.0


def test_relay_help_lists_its_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["relay", "--help"])

    assert exited.value.code == 0
    out = capsys.readouterr().out
    for option in ("--host", "--port", "--state", "--dashboard"):
        assert option in out
    assert "hivelane[relay]" in out


def test_relay_without_the_extra_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "relay_missing_module", lambda: "dotenv")

    assert cli.main(["relay"]) == 1

    err = capsys.readouterr().err
    assert 'pip install "hivelane[relay]"' in err
    assert "dotenv" in err


def test_relay_rejects_a_dashboard_without_index_html(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["relay", "--dashboard", str(tmp_path)]) == 2
    assert "no index.html" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("bind", "lan", "expected"),
    [
        ("0.0.0.0", "192.168.1.23", "192.168.1.23"),
        ("0.0.0.0", None, "localhost"),
        ("10.0.0.5", "192.168.1.23", "10.0.0.5"),
        ("127.0.0.1", "192.168.1.23", "127.0.0.1"),
        ("fd00::5", None, "[fd00::5]"),
    ],
)
def test_relay_public_host(bind: str, lan: str | None, expected: str) -> None:
    assert cli.relay_public_host(bind, lan) == expected


def test_the_lan_join_line_carries_insecure_for_plaintext_relays() -> None:
    """A ws:// relay on another machine is refused without --insecure, so the
    printed line must work as pasted; a loopback relay needs no opt-in."""
    assert cli.relay_join_command("ws://192.168.1.23:8000/ws") == (
        "hivelane up --relay ws://192.168.1.23:8000/ws --insecure"
    )
    assert cli.relay_join_command("ws://127.0.0.1:8000/ws") == "hivelane up --relay ws://127.0.0.1:8000/ws"


@pytest.mark.parametrize(
    "routed, expected",
    [
        ("10.0.0.7", "10.0.0.7"),
        ("192.168.1.20", "192.168.1.20"),
        ("127.0.0.1", None),
        ("127.0.1.1", None),  # Debian's hostname alias
        ("0.0.0.0", None),
        (OSError("network is unreachable"), None),
    ],
)
def test_lan_address_is_never_loopback(
    monkeypatch: pytest.MonkeyPatch, routed: object, expected: object
) -> None:
    """The probe socket is faked, so this checks the filtering rather than
    whatever network the test machine happens to have."""

    class Probe:
        def __init__(self, *args: object) -> None:
            self.closed = False

        def connect(self, address: object) -> None:
            if isinstance(routed, OSError):
                raise routed

        def getsockname(self) -> tuple[object, int]:
            return routed, 54321

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(socket, "socket", Probe)

    assert cli.lan_address() == expected


def test_the_source_checkouts_dashboard_build_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build = tmp_path / "dist"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setenv(cli.RELAY_STATIC_ENV, str(build))

    assert cli.find_dashboard_build(None) == build.resolve()
    assert cli.find_dashboard_build(str(tmp_path / "elsewhere")) == (tmp_path / "elsewhere").resolve()


# --- a real relay in a subprocess ------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RelayProcess:
    def __init__(self, process: subprocess.Popen[str], port: int, log: Path) -> None:
        self.process = process
        self.port = port
        self.log = log

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def relay_process(tmp_path: Path) -> Iterator[RelayProcess]:
    build = tmp_path / "dist"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html><title>bundled</title>", encoding="utf-8")
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HIVELANE_", "CORS_", "FLY_", "GEMINI_"))
    }
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    log = tmp_path / "relay.log"
    with log.open("w", encoding="utf-8") as sink:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from hivelane.cli import main; sys.exit(main())",
                "relay",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--state",
                str(tmp_path / "relay.db"),
                "--dashboard",
                str(build),
                "--log-level",
                "warning",
            ],
            cwd=tmp_path,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            text=True,
        )
    relay = RelayProcess(process, port, log)
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"hivelane relay exited early ({process.returncode}):\n{relay.output()}")
            try:
                if httpx.get(f"{relay.url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail(f"hivelane relay did not answer /health:\n{relay.output()}")
        yield relay
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_relay_serves_the_api_and_the_bundled_dashboard(relay_process: RelayProcess, tmp_path: Path) -> None:
    health = httpx.get(f"{relay_process.url}/health", timeout=5)
    assert health.json()["status"] == "ok"

    created = httpx.post(f"{relay_process.url}/rooms", timeout=5)
    assert created.status_code == 200
    assert (tmp_path / "relay.db").exists()

    page = httpx.get(f"{relay_process.url}/r/{created.json()['room_id']}", timeout=5)
    assert page.status_code == 200
    assert "bundled" in page.text

    out = relay_process.output()
    assert f"hivelane up --relay ws://127.0.0.1:{relay_process.port}/ws" in out
    assert "only this machine can connect" in out
