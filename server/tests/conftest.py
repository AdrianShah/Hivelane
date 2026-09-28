"""Shared fixtures. Keeps the suite offline: no Gemini key, no network calls."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

try:
    from server import limits
except ImportError:  # Render rootDir=server
    import limits


@pytest.fixture(autouse=True)
def _no_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_environment() -> Iterator[None]:
    """Restore os.environ afterwards so config-related env edits cannot leak
    into the next test."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> Iterator[None]:
    """The limiters are process-global, so every test starts with a full budget
    and leaves nothing behind."""
    limits.room_creation_limiter.reset()
    limits.ws_attempt_limiter.reset()
    yield
    limits.room_creation_limiter.reset()
    limits.ws_attempt_limiter.reset()
