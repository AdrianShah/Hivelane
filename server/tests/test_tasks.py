"""Room tasks: the board's bookkeeping, file reservations in the decision
engine, and the task / task_reply / task_update wire round trip."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

try:
    from server.models import PROTOCOL_VERSION, RoomSettings
    from server.room import Room
    from server.tasks import MAX_LEASE_SECONDS, MIN_LEASE_SECONDS, TaskBoard, TaskError
except ImportError:  # Render rootDir=server
    from models import PROTOCOL_VERSION, RoomSettings  # type: ignore[no-redef]
    from room import Room  # type: ignore[no-redef]
    from tasks import MAX_LEASE_SECONDS, MIN_LEASE_SECONDS, TaskBoard, TaskError  # type: ignore[no-redef]

T0 = 1_000_000.0
API = "server/api.py"


def _refused(reason: str, call: Any, *args: Any, **kwargs: Any) -> None:
    with pytest.raises(TaskError) as info:
        call(*args, **kwargs)
    assert info.value.reason == reason


# --- the board -----------------------------------------------------------------


def test_create_claim_done_and_ids() -> None:
    board = TaskBoard()
    first = board.create("sam", T0, title="Add rate limiting", files=[API, API, "server/limits.py"])
    second = board.create("sam", T0, title="Docs")
    assert (first.task_id, second.task_id) == ("T1", "T2")
    assert first.files == [API, "server/limits.py"]

    claimed = board.claim("ada", T0 + 1, task_id="t1", lease_seconds=600)
    assert (claimed.status, claimed.claimer, claimed.lease_until) == ("claimed", "ada", T0 + 601)

    done = board.done("ada", T0 + 2, task_id="1", text="PR #12")
    assert done.status == "done" and done.lease_until is None
    assert [(n.kind, n.text) for n in done.notes] == [("done", "PR #12")]
    assert [t.task_id for t in board.list()] == ["T2"]
    assert [t.task_id for t in board.list(include_closed=True)] == ["T1", "T2"]


def test_dependencies_gate_claims_and_next_takes_the_oldest_ready() -> None:
    board = TaskBoard()
    fix = board.create("sam", T0, title="Fix", repo_id="r")
    review = board.create("sam", T0, title="Review", repo_id="r", depends_on=["T1"])
    other_repo = board.create("sam", T0, title="Elsewhere", repo_id="x")

    _refused("not_ready", board.claim, "ada", T0, task_id=review.task_id)
    assert board.claim("ada", T0, next_ready=True, repo_id="r").task_id == fix.task_id
    _refused("none_ready", board.claim, "ben", T0, next_ready=True, repo_id="r")

    board.done("ada", T0 + 1, task_id=fix.task_id)
    assert board.claim("ben", T0 + 2, next_ready=True, repo_id="r").task_id == review.task_id
    assert board.claim("cy", T0, next_ready=True, repo_id="x").task_id == other_repo.task_id
    _refused("not_found", board.create, "sam", T0, title="x", depends_on=["T99"])


def test_only_the_claimer_renews_releases_or_completes() -> None:
    board = TaskBoard()
    task = board.create("sam", T0, title="Fix")
    board.claim("ada", T0, task_id=task.task_id)
    _refused("claimed", board.claim, "ben", T0, task_id=task.task_id)
    for op in (board.renew, board.release, board.done):
        _refused("not_claimer", op, "ben", T0, task_id=task.task_id)
    _refused("not_owner", board.note, "ben", T0, task_id=task.task_id, text="hi")
    _refused("not_owner", board.cancel, "ben", T0, task_id=task.task_id)

    # The creator may note and cancel; claiming again is a renewal.
    board.note("sam", T0, task_id=task.task_id, text="ping")
    assert board.claim("ada", T0 + 5, task_id=task.task_id, lease_seconds=60).lease_until == T0 + 65
    board.release("ada", T0 + 6, task_id=task.task_id)
    assert (task.status, task.claimer) == ("open", None)
    board.cancel("sam", T0 + 7, task_id=task.task_id, text="not needed")
    assert task.status == "cancelled"
    _refused("closed", board.cancel, "sam", T0, task_id=task.task_id)


def test_leases_lapse_are_clamped_and_renew_on_the_claimers_edits() -> None:
    board = TaskBoard()
    task = board.create("sam", T0, title="Fix", repo_id="r", files=[API])
    board.claim("ada", T0, task_id=task.task_id, lease_seconds=1)
    assert task.lease_until == T0 + MIN_LEASE_SECONDS
    board.renew("ada", T0, task_id=task.task_id, lease_seconds=10**9)
    assert task.lease_until == T0 + MAX_LEASE_SECONDS

    board.renew("ada", T0, task_id=task.task_id, lease_seconds=600)
    # Someone else's edit, another file or another repo: no renewal.
    assert board.renew_on_edit(["ben"], "r", API, T0 + 300) == []
    assert board.renew_on_edit(["ada"], "r", "other.py", T0 + 300) == []
    assert board.renew_on_edit(["ada"], "x", API, T0 + 300) == []
    assert task.lease_until == T0 + 600
    # The claimer (or one of its lanes' own names) editing it: extended and announced.
    assert board.renew_on_edit(["ada-claude", "ADA"], "r", API, T0 + 300) == [task]
    assert task.lease_until == T0 + 900
    # Again a moment later: extended, but not worth another broadcast.
    assert board.renew_on_edit(["ada"], "r", API, T0 + 310) == []
    assert task.lease_until == T0 + 910

    assert board.sweep(T0 + 909) == []
    assert board.sweep(T0 + 910) == [task]
    assert (task.status, task.claimer) == ("open", None)


def test_limits() -> None:
    board = TaskBoard(max_open=2, max_closed=1, max_notes=2)
    board.create("sam", T0, title="a")
    board.create("sam", T0, title="b")
    _refused("limit", board.create, "sam", T0, title="c")
    _refused("invalid", TaskBoard().create, "sam", T0, title="  ")
    # An e2e task has no clear title, only its blob.
    assert TaskBoard().create("sam", T0, e2e="blob").e2e == "blob"

    for i in range(3):
        board.note("sam", T0 + i, task_id="T1", text=f"n{i}")
    assert [n.text for n in board.get("T1").notes] == ["n1", "n2"]

    board.cancel("sam", T0 + 10, task_id="T1")
    board.cancel("sam", T0 + 11, task_id="T2")
    # Only the most recently closed task is kept.
    assert [t.task_id for t in board.list(include_closed=True)] == ["T2"]


# --- reservations in the decision engine -----------------------------------------


def _room(*names: str) -> Room:
    room = Room(RoomSettings(block_window_seconds=20, warn_window_seconds=60))

    class Sock:
        async def send_json(self, payload: dict) -> None:
            pass

    for name in names:
        room.add_member(name, Sock(), "claude-code")  # type: ignore[arg-type]
    return room


def _claimed(room: Room, files: list[str], repo: str = "r", claimer: str = "ada") -> Any:
    task = room.tasks.create("sam", T0, title="Fix", repo_id=repo, files=files)
    return room.tasks.claim(claimer, T0, task_id=task.task_id, lease_seconds=600)


def test_a_reserved_file_blocks_a_teammate_with_scope_task() -> None:
    room = _room("ada", "ben")
    task = _claimed(room, [API])
    verdict = room.evaluate_edit("ben", API, T0 + 60, "r")
    assert (verdict.decision, verdict.scope, verdict.task_id) == ("block", "task", task.task_id)
    assert verdict.peers[0].user_id == "ada" and verdict.peers[0].e2e == ""

    outcome = room.queue_edit_check("ben", "cursor", "r", API, verdict, T0 + 60)
    assert (outcome.decision, outcome.scope, outcome.queue_position) == ("block", "task", 0)
    assert outcome.reason == f"{API} is reserved by ada's task T1 (9 min left on its lease)"
    # Not queued: a task can hold its files for hours.
    assert not room.file_queues


def test_reservations_exempt_the_claimers_own_names_and_chat() -> None:
    room = _room("ada", "ada-claude", "ben")
    _claimed(room, [API])
    assert room.evaluate_edit("ada", API, T0, "r").decision == "allow"
    assert room.evaluate_edit("ada-claude", API, T0, "r", own_names=["ada", "ada-claude"]).decision == "allow"
    assert room.evaluate_edit("ada-claude", API, T0, "r", same_chat=["ada"]).decision == "allow"
    # Without the hint, ada's second chat is just another member.
    assert room.evaluate_edit("ada-claude", API, T0, "r").scope == "task"


def test_reservations_are_scoped_by_repo_file_and_lease() -> None:
    room = _room("ada", "ben")
    task = _claimed(room, [API])
    assert room.evaluate_edit("ben", API, T0, "other-repo").decision == "allow"
    assert room.evaluate_edit("ben", "server/other.py", T0, "r").decision == "allow"
    assert room.evaluate_edit("ben", API, task.lease_until, "r").decision == "allow"
    room.tasks.done("ada", T0, task_id=task.task_id)
    assert room.evaluate_edit("ben", API, T0, "r").decision == "allow"


def test_hashed_files_still_match_in_e2e_rooms() -> None:
    hashed = "h:" + "a" * 32
    room = _room("ada", "ben")
    _claimed(room, [hashed], repo="h:" + "b" * 32)
    assert room.evaluate_edit("ben", hashed, T0, "h:" + "b" * 32).scope == "task"


def test_a_live_touch_still_wins_over_a_reservation() -> None:
    try:
        from server.models import ActivityMessage, FileChange
    except ImportError:  # Render rootDir=server
        from models import ActivityMessage, FileChange  # type: ignore[no-redef]

    room = _room("ada", "ben", "cy")
    _claimed(room, [API])
    room.record_activity(
        ActivityMessage(user_id="cy", timestamp=T0, changes=[FileChange(file_path=API, diff="@@")], repo_id="r")
    )
    verdict = room.evaluate_edit("ben", API, T0 + 1, "r")
    assert verdict.decision == "block" and verdict.scope != "task"
    assert verdict.peers[0].user_id == "cy"


def test_the_sweeper_reopens_lapsed_tasks_for_dashboards() -> None:
    room = _room("ada")
    task = _claimed(room, [API])
    expired = asyncio.run(room.sweep_tasks(task.lease_until))
    assert expired == [task] and task.status == "open"


# --- over the wire -----------------------------------------------------------------


def _client() -> Any:
    from fastapi.testclient import TestClient

    try:
        from server.main import app
    except ImportError:  # Render rootDir=server
        from main import app  # type: ignore[no-redef]
    return TestClient(app)


def _no_task_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from server import main
    except ImportError:  # Render rootDir=server
        import main  # type: ignore[no-redef]
    monkeypatch.setattr(main, "TASK_MIN_INTERVAL_SECONDS", 0.0)


def test_task_requests_are_rate_limited_per_member() -> None:
    client = _client()
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as ada:
        _join(ada, "ada")
        ada.send_json({"type": "task", "request_id": "a", "op": "create", "title": "one"})
        assert _next(ada, "task_reply")["ok"]
        ada.send_json({"type": "task", "request_id": "b", "op": "create", "title": "two"})
        assert _next(ada, "task_reply")["reason"] == "rate_limited"
        # Listing is never limited.
        ada.send_json({"type": "task", "request_id": "c", "op": "list"})
        assert len(_next(ada, "task_reply")["tasks"]) == 1


def _join(socket: Any, name: str) -> None:
    socket.send_json({"type": "join", "name": name, "agent": "cursor", "protocol_version": PROTOCOL_VERSION})


def _next(socket: Any, kind: str, limit: int = 30) -> dict[str, Any]:
    for _ in range(limit):
        frame = socket.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"never received {kind}")


def _feed(socket: Any, kind: str, limit: int = 30, seen: list[str] | None = None, until: Any = None) -> dict[str, Any]:
    """The first event of this kind (matching `until`, if given), whether it
    comes bare or inside a feed batch; every event read is noted in seen."""
    for _ in range(limit):
        frame = socket.receive_json()
        events = frame.get("events", []) if frame.get("type") == "hivelane_feed" else [frame]
        found = None
        for event in events:
            if seen is not None:
                seen.append(str(event.get("type")))
            if found is None and event.get("type") == kind and (until is None or until(event)):
                found = event
        if found is not None:
            return found
    raise AssertionError(f"never received {kind}")


def test_task_round_trip_reservation_and_dashboard_only_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    _no_task_rate_limit(monkeypatch)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as ada, client.websocket_connect(f"/ws/{room_id}") as ben:
        _join(ada, "ada")
        _join(ben, "ben")
        ben.send_json({"type": "task", "request_id": "c1", "op": "create", "title": "Fix the API", "files": [API]})
        created = _next(ben, "task_reply")
        assert created["ok"] and created["task"]["task_id"] == "T1"

        ada.send_json({"type": "task", "request_id": "c2", "op": "claim", "task_id": "T1"})
        claimed = _next(ada, "task_reply")
        assert claimed["task"]["claimer"] == "ada"

        ben.send_json({"type": "edit_check", "request_id": "e1", "file_path": API})
        blocked = _next(ben, "edit_check_response")
        assert (blocked["decision"], blocked["scope"]) == ("block", "task")
        assert "reserved by ada's task T1" in blocked["reason"]

        ben.send_json({"type": "task", "request_id": "c3", "op": "done", "task_id": "T1"})
        assert _next(ben, "task_reply")["reason"] == "not_claimer"

        # A dashboard joining now gets the task in its catch-up.
        with client.websocket_connect(f"/ws/{room_id}") as dash:
            dash.send_json({"type": "join", "name": None, "role": "spectator"})
            caught_up = _feed(dash, "task_update")
            assert caught_up["task"]["status"] == "claimed"

            ada.send_json({"type": "task", "request_id": "c4", "op": "done", "task_id": "T1", "text": "PR 7"})
            assert _next(ada, "task_reply")["task"]["status"] == "done"
            # Feed events queued before the dashboard joined may follow its
            # catch-up (older snapshots: the dashboard keeps the newest by
            # updated_at), so read until the done update.
            live = _feed(dash, "task_update", until=lambda e: e["task"]["status"] == "done")
            assert live["task"]["notes"][-1]["text"] == "PR 7"

            # Members never get task_update: everything ben receives up to
            # the next ordinary broadcast is checked.
            ada.send_json({"type": "intent", "user_id": "ada", "prompt": "next thing"})
            seen: list[str] = []
            _feed(ben, "intent_update", seen=seen)
            assert "task_update" not in seen

        ben.send_json({"type": "edit_check", "request_id": "e2", "file_path": API})
        assert _next(ben, "edit_check_response")["decision"] == "allow"

        ben.send_json({"type": "task", "request_id": "c5", "op": "list", "include_closed": True})
        listed = _next(ben, "task_reply")
        assert [t["task_id"] for t in listed["tasks"]] == ["T1"]


def test_an_invalid_task_request_is_still_answered() -> None:
    client = _client()
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as ada:
        _join(ada, "ada")
        ada.send_json({"type": "task", "request_id": "x1", "op": "explode"})
        reply = _next(ada, "task_reply")
        assert (reply["ok"], reply["reason"]) == (False, "invalid")
