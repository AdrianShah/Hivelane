"""Daemon liveness reporting and the non-git warning.

`connected` alone cannot tell a live relay link from a half-open socket the OS
has not noticed is dead, so status also says when the relay was last heard
from; and a daemon started outside git has to say that its path keys will not
line up across teammates' directories."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hivelane import daemon as daemon_module
from hivelane.daemon import Daemon


def _daemon(**kwargs: object) -> Daemon:
    return Daemon("TEAM42", "Ada", port=4696, **kwargs)  # type: ignore[arg-type]


def test_status_reports_no_contact_before_connecting() -> None:
    status = _daemon().status()

    assert status["last_relay_contact"] is None
    assert status["connected_since"] is None


def test_any_relay_frame_counts_as_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_module.time, "time", lambda: 1234.5)
    daemon = _daemon()

    daemon._handle_frame('{"type":"user_joined","user_id":"sam"}')

    assert daemon.status()["last_relay_contact"] == 1234.5


def test_even_an_unparseable_frame_proves_the_link_is_alive() -> None:
    daemon = _daemon()
    daemon._handle_frame("not json")
    assert daemon.last_relay_contact is not None


def test_a_successful_send_counts_as_contact() -> None:
    class Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    async def scenario() -> tuple[list[str], float | None]:
        daemon = _daemon()
        socket = Socket()
        daemon._outbox.put_nowait('{"type":"intent"}')
        daemon._outbox_ready.set()
        drain = asyncio.create_task(daemon._drain(socket))
        # Wait for the send itself (and the contact it records), not a delay.
        deadline = asyncio.get_running_loop().time() + 5.0
        while not (socket.sent and daemon.last_relay_contact is not None):
            assert asyncio.get_running_loop().time() < deadline, "the drain never sent"
            await asyncio.sleep(0.001)
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        return socket.sent, daemon.last_relay_contact

    sent, contact = asyncio.run(scenario())
    assert sent == ['{"type":"intent"}']
    assert contact is not None


def test_status_reports_the_root_override(tmp_path: Path) -> None:
    status = _daemon(root=str(tmp_path)).status()
    assert status["root"] == str(tmp_path.resolve())


# --- non-git warning ------------------------------------------------------------


def test_warns_outside_git_without_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(daemon_module, "_repo_roots", {})
    if daemon_module.repo_root(str(tmp_path)) is not None:
        pytest.skip("the temp directory is itself inside a git repo")
    monkeypatch.chdir(tmp_path)

    assert _daemon().warn_if_unanchored() is True

    err = capsys.readouterr().err
    assert "not inside a git repository" in err
    assert "--root" in err


def test_no_warning_with_an_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _daemon(root=str(tmp_path)).warn_if_unanchored() is False
    assert capsys.readouterr().err == ""


def test_no_warning_inside_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(daemon_module, "_repo_roots", {})
    (tmp_path / ".git").mkdir()
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")

    assert _daemon().warn_if_unanchored() is False
    assert capsys.readouterr().err == ""
