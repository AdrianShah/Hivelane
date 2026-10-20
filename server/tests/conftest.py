"""Shared fixtures. Keeps the suite offline: no Gemini key, no network calls."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import anyio.from_thread
import pytest
from starlette import testclient

try:
    from server import limits
    from server import room as room_module
except ImportError:  # Render rootDir=server
    import limits
    import room as room_module  # type: ignore[no-redef]

# The feed debounce the suite runs with. The production value (0.5 s) is what
# most server tests spent their time waiting on; tests that check the debounce
# itself, or ordering across a real flush window, opt out with
# @pytest.mark.real_feed_debounce.
TEST_BROADCAST_FLUSH_SECONDS = 0.02


@pytest.fixture(autouse=True)
def _fast_feed_debounce(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("real_feed_debounce") is None:
        monkeypatch.setattr(
            room_module, "BROADCAST_FLUSH_SECONDS", TEST_BROADCAST_FLUSH_SECONDS
        )


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


@pytest.fixture(autouse=True)
def _one_event_loop_per_test(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every TestClient session in a test on one shared event loop.

    Starlette >= 1.0 gives each websocket_connect its own blocking portal (its
    own loop and thread) unless the client is entered as a context manager.
    The relay fans a broadcast out from whichever socket's handler produced
    it, so with a loop per socket the send to another socket waits forever on
    a stream that loop does not own. Uvicorn runs the relay on one loop, so
    the tests should too.
    """
    with anyio.from_thread.start_blocking_portal() as portal:

        @contextlib.contextmanager
        def shared_portal(self: testclient.TestClient) -> Iterator[object]:
            yield self.portal if self.portal is not None else portal

        monkeypatch.setattr(testclient.TestClient, "_portal_factory", shared_portal)
        yield
