"""Relay frames are a network boundary: a bad one must not kill the daemon."""

from __future__ import annotations

import asyncio

from hivelane.daemon import Daemon


def _daemon() -> Daemon:
    return Daemon("ABC123", "adrian", agent="claude-code", server="ws://x/ws", port=4600)


def test_unparseable_frame_is_ignored() -> None:
    daemon = _daemon()
    daemon._handle_frame("not json at all")
    assert daemon.fatal is None


def test_malformed_known_frame_is_ignored() -> None:
    """A garbage reply must not resolve the check waiting on that request id
    (the hook would act on it); the well-formed reply that follows does."""

    async def scenario() -> tuple[bool, bool, str]:
        daemon = _daemon()
        future = asyncio.get_running_loop().create_future()
        daemon._pending["r1"] = future
        daemon._handle_frame(
            '{"type": "edit_check_response", "request_id": "r1", "decision": "nonsense"}'
        )
        resolved_by_garbage = future.done()
        still_pending = "r1" in daemon._pending
        daemon._handle_frame(
            '{"type": "edit_check_response", "request_id": "r1", "decision": "block",'
            ' "file_path": "a.py", "reason": "sam is editing a.py"}'
        )
        assert daemon.fatal is None
        return resolved_by_garbage, still_pending, future.result().decision

    resolved_by_garbage, still_pending, decision = asyncio.run(scenario())
    assert not resolved_by_garbage
    assert still_pending
    assert decision == "block"


def test_non_object_frame_is_ignored() -> None:
    daemon = _daemon()
    daemon._handle_frame("[1, 2, 3]")
    assert daemon.fatal is None


def test_user_left_removes_the_peer() -> None:
    daemon = _daemon()
    daemon._handle_frame('{"type":"hivelane_feed","events":[{"type":"user_joined","user_id":"sam"}]}')
    assert sorted(daemon._peers) == ["sam"]
    daemon._handle_frame('{"type":"hivelane_feed","events":[{"type":"user_left","user_id":"sam"}]}')
    assert sorted(daemon._peers) == []


def test_bare_user_joined_frame_records_the_peer() -> None:
    """Join catch-up arrives unbatched, not inside a hivelane_feed frame."""
    daemon = _daemon()
    daemon._handle_frame('{"type":"user_joined","user_id":"sam","agent":"cursor"}')
    assert sorted(daemon._peers) == ["sam"]
    daemon._handle_frame('{"type":"user_left","user_id":"sam"}')
    assert sorted(daemon._peers) == []


def test_own_name_is_never_recorded_as_a_peer() -> None:
    daemon = _daemon()
    daemon._handle_frame('{"type":"user_joined","user_id":"adrian"}')
    assert sorted(daemon._peers) == []  # daemon is itself named adrian


def test_rate_limited_frame_is_not_fatal_and_sets_retry_after() -> None:
    """Throttling is the relay asking us to wait, not refusing us: the daemon
    must back off rather than exit, and must not keep the limiter pinned."""
    daemon = _daemon()
    daemon._handle_frame(
        '{"type":"rate_limited","reason":"Too many attempts","retry_after":45}'
    )
    assert daemon.fatal is None
    assert daemon.retry_after == 45.0
