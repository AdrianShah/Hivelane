"""Shared test isolation for the client package."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_hivelane_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Hooks and CLI commands run in-process (and in subprocesses, which
    inherit the environment) must never read or write the developer's real
    ~/.hivelane (token, hook_state, room keys). Tests that care about the home
    directory still set their own HIVELANE_HOME on top of this."""
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path_factory.mktemp("hivelane-home")))


@pytest.fixture(autouse=True)
def _no_ambient_room_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own HIVELANE_ROOM_KEY must not decide what an e2e test
    sees (it overrides every personal room_keys entry)."""
    monkeypatch.delenv("HIVELANE_ROOM_KEY", raising=False)


@pytest.fixture(autouse=True)
def _hivelane_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed hooks run bare `hivelane hook <id>` only when hivelane is on
    PATH (else an absolute command). The suite usually runs as
    .venv/Scripts/python without the venv on PATH; pin the common case so
    install tests do not depend on the machine. test_hook_install covers the
    fallback explicitly."""
    from hivelane.adapters import base

    monkeypatch.setattr(base, "hivelane_on_path", lambda: True)
