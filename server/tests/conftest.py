"""Shared fixtures. Keeps the suite offline: no Gemini key, no network calls."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
