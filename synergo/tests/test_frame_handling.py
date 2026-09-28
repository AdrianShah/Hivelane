"""Relay frames are a network boundary: a bad one must not kill the daemon."""

from __future__ import annotations

from synergo.daemon import Daemon


def _daemon() -> Daemon:
    return Daemon("ABC123", "adrian", agent="claude-code", server="ws://x/ws", port=4600)


def test_unparseable_frame_is_ignored() -> None:
    daemon = _daemon()
    daemon._handle_frame("not json at all")
    assert daemon.fatal is None


def test_malformed_known_frame_is_ignored() -> None:
    daemon = _daemon()
    daemon._handle_frame('{"type": "edit_check_response", "decision": "nonsense"}')
    assert daemon.fatal is None


def test_non_object_frame_is_ignored() -> None:
    daemon = _daemon()
    daemon._handle_frame("[1, 2, 3]")
    assert daemon.fatal is None


def test_user_left_removes_the_peer() -> None:
    daemon = _daemon()
    daemon._handle_frame('{"type":"synergo_feed","events":[{"type":"user_joined","user_id":"sam"}]}')
    assert sorted(daemon._peers) == ["sam"]
    daemon._handle_frame('{"type":"synergo_feed","events":[{"type":"user_left","user_id":"sam"}]}')
    assert sorted(daemon._peers) == []


def test_bare_user_joined_frame_records_the_peer() -> None:
    """Join catch-up arrives unbatched, not inside a synergo_feed frame."""
    daemon = _daemon()
    daemon._handle_frame('{"type":"user_joined","user_id":"sam","agent":"cursor"}')
    assert sorted(daemon._peers) == ["sam"]
    daemon._handle_frame('{"type":"user_left","user_id":"sam"}')
    assert sorted(daemon._peers) == []


def test_own_name_is_never_recorded_as_a_peer() -> None:
    daemon = _daemon()
    daemon._handle_frame('{"type":"user_joined","user_id":"adrian"}')
    assert sorted(daemon._peers) == []  # daemon is itself named adrian
