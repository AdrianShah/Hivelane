"""Pending (unpushed) work: the relay keeps each member's latest
pending_changes report for as long as it holds and warns (scope "pending")
when someone else edits one of those files."""

from __future__ import annotations

from typing import Any, Optional

from fastapi.testclient import TestClient

try:
    from server import room as room_module
    from server.main import app
    from server.models import (
        PROTOCOL_VERSION,
        ActivityMessage,
        AgentKind,
        FileChange,
        PendingChanges,
        PendingFile,
    )
    from server.room import Room
    from server.sanitize import MAX_PENDING_FILES, cap_pending
except ImportError:  # Render rootDir=server
    import room as room_module  # type: ignore[no-redef]
    from main import app  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        PROTOCOL_VERSION,
        ActivityMessage,
        AgentKind,
        FileChange,
        PendingChanges,
        PendingFile,
    )
    from room import Room  # type: ignore[no-redef]
    from sanitize import MAX_PENDING_FILES, cap_pending  # type: ignore[no-redef]

FILE = "frontend/Header.tsx"


class FakeSocket:
    """Room only stores member sockets in these tests."""


def _join(room: Room, user_id: str, agent: AgentKind = "cursor") -> None:
    room.add_member(user_id, FakeSocket(), agent)


def _pending(
    room: Room,
    user_id: str,
    now: float,
    files: Optional[list[PendingFile]] = None,
    *,
    repo_id: str = "",
    branch: str = "main",
) -> None:
    room.record_pending(
        PendingChanges(
            user_id=user_id,
            repo_id=repo_id,
            branch=branch,
            base="origin/main",
            files=[PendingFile(path=FILE, since=now - 1500)] if files is None else files,
        ),
        now,
        room.member_agent(user_id),
    )


def test_warns_on_someone_elses_unpushed_file_long_after_the_touch_window() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya", "claude-code")
    _pending(room, "adrian", now=1000.0)

    # Ten minutes later: far past the 60 s touch window.
    verdict = room.evaluate_edit("priya", FILE, 1600.0, branch="main")

    assert verdict.decision == "warn"
    assert verdict.scope == "pending"
    assert [peer.user_id for peer in verdict.peers] == ["adrian"]
    assert verdict.peers[0].kind == "pending"
    # since is 1500 s before the report, the check 600 s after it.
    assert verdict.reason == (
        f"adrian has unpushed changes to {FILE} from 35 min ago; pull or coordinate first"
    )


def test_other_files_and_other_repos_are_not_affected() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0, repo_id="repo-a")

    assert room.evaluate_edit("priya", "other.py", 1010.0, "repo-a").decision == "allow"
    assert room.evaluate_edit("priya", FILE, 1010.0, "repo-b").decision == "allow"
    assert room.evaluate_edit("priya", FILE, 1010.0, "repo-a").scope == "pending"


def test_the_same_person_is_never_warned_about_their_own_work() -> None:
    """adrian's agent lane (adrian-claude) and its daemon's main name are one
    person: own_names / same_chat leave the report out."""
    room = Room()
    _join(room, "adrian")
    _join(room, "adrian-claude", "claude-code")
    _pending(room, "adrian", now=1000.0)

    assert room.evaluate_edit("adrian", FILE, 1010.0).decision == "allow"
    lane = room.evaluate_edit("adrian-claude", FILE, 1010.0, own_names=["adrian", "adrian-claude"])
    assert lane.decision == "allow"
    chat = room.evaluate_edit("adrian-claude", FILE, 1010.0, same_chat=["Adrian"])
    assert chat.decision == "allow"


def test_disjoint_symbols_do_not_warn_shared_ones_do() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0, files=[PendingFile(path=FILE, symbols=["renderNav"], since=900.0)])

    other = room.evaluate_edit("priya", FILE, 1010.0, symbols=["Footer"], branch="main")
    same = room.evaluate_edit("priya", FILE, 1010.0, symbols=["renderNav"], branch="main")
    unknown = room.evaluate_edit("priya", FILE, 1010.0, branch="main")

    assert other.decision == "allow"
    assert same.decision == "warn" and same.scope == "pending"
    assert "(renderNav)" in same.reason
    # The requester could not name symbols: the file decides.
    assert unknown.decision == "warn"


def test_config_files_are_judged_whole() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0, files=[PendingFile(path="package.json", symbols=["deps"])])

    verdict = room.evaluate_edit("priya", "package.json", 1010.0, symbols=["scripts"])

    assert verdict.decision == "warn" and verdict.scope == "pending"


def test_another_branch_says_merge_conflict() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0, branch="feat/nav")

    verdict = room.evaluate_edit("priya", FILE, 1010.0, branch="main")

    assert verdict.scope == "pending"
    assert "on branch feat/nav" in verdict.reason
    assert "conflict at merge" in verdict.reason


def test_hashed_branch_and_symbols_stay_out_of_the_reason() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(
        room,
        "adrian",
        now=1000.0,
        branch="h:" + "a" * 32,
        files=[PendingFile(path="h:" + "f" * 32, symbols=["h:" + "1" * 32], since=1000.0)],
    )

    verdict = room.evaluate_edit("priya", "h:" + "f" * 32, 1010.0, branch="h:" + "b" * 32)

    assert verdict.scope == "pending"
    assert "another branch" in verdict.reason
    assert "h:" + "1" * 32 not in verdict.reason
    assert verdict.peers[0].e2e == ""


def test_live_edits_keep_precedence_and_pending_rides_along() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "sam")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0)
    room.record_activity(
        ActivityMessage(
            user_id="sam",
            timestamp=1005.0,
            changes=[FileChange(file_path=FILE, diff="")],
            agent="cursor",
        )
    )

    blocked = room.evaluate_edit("priya", FILE, 1010.0)
    assert blocked.decision == "block"
    assert [peer.user_id for peer in blocked.peers] == ["sam"]

    warned = room.evaluate_edit("priya", FILE, 1040.0)
    assert warned.decision == "warn"
    assert warned.scope == "file"
    assert [peer.user_id for peer in warned.peers] == ["sam", "adrian"]


def test_a_new_report_replaces_the_last_and_empty_clears_it() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0)
    _pending(room, "adrian", now=1100.0, files=[PendingFile(path="other.py")])

    assert room.evaluate_edit("priya", FILE, 1110.0).decision == "allow"
    assert room.evaluate_edit("priya", "other.py", 1110.0).scope == "pending"

    _pending(room, "adrian", now=1200.0, files=[])
    assert room.evaluate_edit("priya", "other.py", 1210.0).decision == "allow"
    assert room.pending_report_count == 0
    assert room.pending_bytes == 0


def test_disconnect_keeps_the_report_marked_offline_until_it_lapses(monkeypatch: Any) -> None:
    monkeypatch.setattr(room_module, "PENDING_OFFLINE_SECONDS", 3600.0)
    monkeypatch.setattr(room_module.time, "time", lambda: 2000.0)
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0)
    room.remove_member("adrian")

    offline = room.evaluate_edit("priya", FILE, 2500.0)
    assert offline.scope == "pending"
    assert offline.reason.startswith("adrian (offline) has unpushed changes")
    assert [u.online for u in room.pending_updates(2500.0)] == [False]

    lapsed = room.evaluate_edit("priya", FILE, 2000.0 + 3601.0)
    assert lapsed.decision == "allow"
    assert room.pending_updates(2000.0 + 3601.0) == []


def test_reconnecting_makes_the_report_count_as_online_again() -> None:
    room = Room()
    _join(room, "adrian")
    _join(room, "priya")
    _pending(room, "adrian", now=1000.0)
    room.remove_member("adrian")
    _join(room, "adrian")

    verdict = room.evaluate_edit("priya", FILE, 1_000_000.0)

    assert verdict.scope == "pending"
    assert "(offline)" not in verdict.reason
    assert [u.online for u in room.pending_updates(1_000_000.0)] == [True]


def test_reports_are_bounded_per_member_and_per_room(monkeypatch: Any) -> None:
    monkeypatch.setattr(room_module, "MAX_PENDING_REPORTS_PER_ROOM", 3)
    room = Room()
    for name in ("a", "b", "c", "d"):
        _join(room, name)
        _pending(room, name, now=1000.0)
    assert room.pending_report_count == 3
    assert {u.user_id for u in room.pending_updates(1000.0)} == {"b", "c", "d"}

    monkeypatch.setattr(room_module, "MAX_PENDING_REPORTS_PER_ROOM", 256)
    room = Room()
    _join(room, "a")
    for index in range(room_module.MAX_PENDING_REPORTS_PER_MEMBER + 3):
        _pending(room, "a", now=1000.0, repo_id=f"repo-{index}")
    assert room.pending_report_count == room_module.MAX_PENDING_REPORTS_PER_MEMBER


def test_reports_are_bounded_in_bytes(monkeypatch: Any) -> None:
    monkeypatch.setattr(room_module, "MAX_PENDING_BYTES_PER_ROOM", 2500)
    room = Room()
    for name in ("a", "b", "c"):
        _join(room, name)
        _pending(room, name, now=1000.0, files=[PendingFile(path=f"{name}/{i}.py") for i in range(10)])
    assert 0 < room.pending_bytes <= 2500
    assert room.pending_report_count < 3


def test_cap_pending_trims_lists_and_drops_long_paths() -> None:
    message = PendingChanges(
        files=[PendingFile(path=f"f{i}.py", symbols=[f"s{j}" for j in range(100)]) for i in range(MAX_PENDING_FILES + 5)]
        + [PendingFile(path="x" * 5000)],
        branch="b" * 1000,
    )

    cap_pending(message)

    assert len(message.files) == MAX_PENDING_FILES
    assert message.truncated
    assert all(len(f.symbols) <= 32 for f in message.files)
    assert len(message.branch) <= 256


# --- over a real WebSocket ------------------------------------------------------

MAX_FRAMES = 16


def _ws_join(socket: Any, name: Optional[str], agent: str = "cursor") -> None:
    socket.send_json(
        {"type": "join", "name": name, "agent": agent, "protocol_version": PROTOCOL_VERSION}
    )


def _next_frame(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") == type_name:
            return frame
    raise AssertionError(f"never received a top-level {type_name} frame")


def _next_feed_event(socket: Any, type_name: str) -> dict:
    for _ in range(MAX_FRAMES):
        frame = socket.receive_json()
        if frame.get("type") != "hivelane_feed":
            continue
        for event in frame["events"]:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received a {type_name} feed event")


def test_pending_changes_round_trip() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as dashboard:
        _ws_join(dashboard, None)
        with client.websocket_connect(f"/ws/{room_id}") as adrian:
            _ws_join(adrian, "adrian")
            with client.websocket_connect(f"/ws/{room_id}") as priya:
                _ws_join(priya, "priya", "claude-code")
                adrian.send_json(
                    {
                        "type": "pending_changes",
                        "user_id": "spoofed",
                        "branch": "main",
                        "base": "origin/main",
                        "files": [{"path": FILE, "symbols": ["renderNav"], "since": 1.0}],
                    }
                )
                update = _next_feed_event(dashboard, "pending_update")

                priya.send_json(
                    {
                        "type": "edit_check",
                        "request_id": "req-p",
                        "file_path": FILE,
                        "symbols": ["renderNav"],
                        "branch": "main",
                    }
                )
                response = _next_frame(priya, "edit_check_response")

        # A dashboard that connects later is caught up on it.
        with client.websocket_connect(f"/ws/{room_id}") as late:
            _ws_join(late, None)
            caught_up = _next_frame(late, "pending_update")

    assert update["user_id"] == "adrian"
    assert update["files"][0]["path"] == FILE
    assert response["decision"] == "warn"
    assert response["scope"] == "pending"
    assert response["others"][0]["user_id"] == "adrian"
    assert response["others"][0]["kind"] == "pending"
    assert "unpushed changes" in response["reason"]
    assert caught_up["user_id"] == "adrian"
    assert caught_up["online"] is False
