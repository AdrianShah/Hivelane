"""Room.check_edit policy: the pre-edit decision an agent's hook depends on."""

from __future__ import annotations

from typing import Optional

try:
    from server.models import ActivityMessage, AgentKind, FileChange
    from server.room import Room
except ImportError:  # Render rootDir=server
    from models import ActivityMessage, AgentKind, FileChange
    from room import Room

FILE = "server/room.py"


class FakeSocket:
    """Stand-in member socket: Room only stores it and calls send_json on
    broadcast, which these tests never trigger."""


def _join(room: Room, user_id: str, agent: AgentKind = "cursor") -> None:
    room.add_member(user_id, FakeSocket(), agent)


def _touch(
    room: Room,
    user_id: str,
    timestamp: float,
    file_path: str = FILE,
    prompt: Optional[str] = None,
) -> None:
    room.record_activity(
        ActivityMessage(
            user_id=user_id,
            timestamp=timestamp,
            prompt=prompt,
            changes=[FileChange(file_path=file_path, diff="@@ -1 +1 @@")],
            agent=room.member_agent(user_id),
        )
    )


def test_blocks_inside_the_block_window() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    _touch(room, "sam", timestamp=1000.0)

    decision, peers = room.check_edit("priya", FILE, now=1008.0)

    assert decision == "block"
    assert [peer.user_id for peer in peers] == ["sam"]


def test_warns_between_block_and_conflict_windows() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    _touch(room, "sam", timestamp=1000.0)

    decision, peers = room.check_edit("priya", FILE, now=1045.0)

    assert decision == "warn"
    assert [peer.user_id for peer in peers] == ["sam"]


def test_allows_past_the_conflict_window() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    _touch(room, "sam", timestamp=1000.0)

    decision, peers = room.check_edit("priya", FILE, now=1090.0)

    assert decision == "allow"
    assert peers == []


def test_allows_when_the_only_toucher_disconnected() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    _touch(room, "sam", timestamp=1000.0)
    room.remove_member("sam")

    decision, peers = room.check_edit("priya", FILE, now=1002.0)

    assert decision == "allow"
    assert peers == []


def test_peers_are_returned_most_recent_first() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "alex")
    _join(room, "priya", "claude-code")
    _touch(room, "sam", timestamp=1000.0)
    _touch(room, "alex", timestamp=1005.0)

    decision, peers = room.check_edit("priya", FILE, now=1010.0)

    assert decision == "block"
    assert [peer.user_id for peer in peers] == ["alex", "sam"]


def test_intent_mentioning_the_file_escalates_allow_to_warn() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    room.record_intent("sam", "refactor the Room broadcast", timestamp=1000.0)

    decision, peers = room.check_edit("priya", FILE, now=1002.0)

    assert decision == "warn"
    assert [peer.user_id for peer in peers] == ["sam"]
    assert peers[0].prompt == "refactor the Room broadcast"


def test_intent_with_the_full_basename_escalates() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    room.record_intent("sam", "split server/room.py in two", timestamp=1000.0)

    decision, _ = room.check_edit("priya", FILE, now=1002.0)

    assert decision == "warn"


def test_unrelated_intent_still_allows() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    room.record_intent("sam", "tidy up the dashboard header", timestamp=1000.0)

    decision, peers = room.check_edit("priya", FILE, now=1002.0)

    assert decision == "allow"
    assert peers == []


def test_disconnecting_clears_intent() -> None:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    room.record_intent("sam", "refactor the room module", timestamp=1000.0)
    room.remove_member("sam")

    assert room.latest_intents() == []
    assert room.check_edit("priya", FILE, now=1002.0)[0] == "allow"


def test_intent_does_not_consume_the_activity_rate_limit_budget() -> None:
    """A prompt is followed by edits within milliseconds; the intent must not
    cause that first activity to be dropped."""
    room = Room()
    assert room.is_activity_rate_limited("adrian", 0.5, channel="intent") is False
    assert room.is_activity_rate_limited("adrian", 0.5, channel="activity") is False
    assert room.is_activity_rate_limited("adrian", 0.5, channel="activity") is True


def test_stale_intent_no_longer_escalates() -> None:
    """An intent has to expire: otherwise one morning prompt naming a file warns
    about it for the rest of the session."""
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    room.record_intent("sam", "refactor the room module", timestamp=1000.0)

    assert room.check_edit("priya", FILE, now=1000.0 + 60)[0] == "warn"
    assert room.check_edit("priya", FILE, now=1000.0 + 7200)[0] == "allow"


def test_old_touches_are_swept() -> None:
    """A long-lived room must not keep an entry for every file ever touched."""
    room = Room()
    _join(room, "sam")
    for n in range(50):
        _touch(room, "sam", timestamp=1000.0, file_path=f"src/f{n}.py")
    assert len(room._file_activity) == 50

    _touch(room, "sam", timestamp=5000.0, file_path="src/recent.py")

    assert list(room._file_activity) == ["src/recent.py"]


def test_pruning_keeps_touches_still_inside_the_window() -> None:
    """The sweep must not reach a touch a comparison can still see."""
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    _touch(room, "sam", timestamp=1000.0, file_path="src/old.py")
    _touch(room, "sam", timestamp=1010.0)
    _touch(room, "priya", timestamp=1065.0, file_path="src/other.py")

    assert "src/old.py" not in room._file_activity
    assert FILE in room._file_activity
    assert room.check_edit("priya", FILE, now=1066.0)[0] == "warn"
