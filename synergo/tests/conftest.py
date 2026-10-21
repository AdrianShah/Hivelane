"""Shared test isolation for the client package."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_room_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own SYNERGO_ROOM_KEY must not decide what an e2e test
    sees (it overrides every personal room_keys entry)."""
    monkeypatch.delenv("SYNERGO_ROOM_KEY", raising=False)
