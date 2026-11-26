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
    # The reason names the task by its title too.
    assert outcome.reason == f'{API} is reserved by ada\'s task T1 "Fix" (9 min left on its lease)'
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


def test_a_block_reason_leaves_out_a_sealed_title_and_shortens_a_long_one() -> None:
    room = _room("ada", "ben")
    sealed = room.tasks.create("sam", T0, e2e="blob", repo_id="r", files=[API])
    room.tasks.claim("ada", T0, task_id=sealed.task_id, lease_seconds=600)
    verdict = room.evaluate_edit("ben", API, T0 + 60, "r")
    reason = room.queue_edit_check("ben", "cursor", "r", API, verdict, T0 + 60).reason
    assert reason == f"{API} is reserved by ada's task T1 (9 min left on its lease)"

    long = room.tasks.create("sam", T0, title="x" * 150, repo_id="r", files=["b.py"])
    room.tasks.claim("ada", T0, task_id=long.task_id, lease_seconds=600)
    verdict = room.evaluate_edit("ben", "b.py", T0 + 60, "r")
    reason = room.queue_edit_check("ben", "cursor", "r", "b.py", verdict, T0 + 60).reason
    assert '"' + "x" * 79 + '…"' in reason


# --- agent plans, suggestions, room-wide tasks ---------------------------------------


def test_a_plan_step_is_created_claimed_and_reserves_only_while_active() -> None:
    room = _room("ada", "ben")
    board = room.tasks
    step = board.create(
        "ada", T0, title="Wire the API", repo_id="r", claim=True, source="agent_plan",
        lane="ada-claude", lease_seconds=3600,
    )
    assert (step.status, step.claimer, step.source, step.lane, step.active) == (
        "claimed", "ada", "agent_plan", "ada-claude", False,
    )
    assert step.lease_until == T0 + 3600

    # Files attached while inactive reserve nothing yet.
    board.update("ada", T0 + 1, task_id=step.task_id, title=step.title, files=[API], active=False)
    assert room.evaluate_edit("ben", API, T0 + 2, "r").decision == "allow"
    # The agent is on this step now: reserved, and the lease follows the update.
    board.update("ada", T0 + 3, task_id=step.task_id, title=step.title, files=[API], active=True, lease_seconds=600)
    assert step.lease_until == T0 + 603
    assert room.evaluate_edit("ben", API, T0 + 4, "r").scope == "task"
    board.done("ada", T0 + 5, task_id=step.task_id)
    assert step.active is False
    assert room.evaluate_edit("ben", API, T0 + 6, "r").decision == "allow"


def test_update_rules() -> None:
    board = TaskBoard()
    task = board.create("sam", T0, title="Fix", files=[API])
    updated = board.update("sam", T0 + 1, task_id=task.task_id, title="Fix it", files=["a.py", "a.py"])
    assert (updated.title, updated.files, updated.updated_at) == ("Fix it", ["a.py"], T0 + 1)
    # Not claimed: active cannot be set, and nobody's lease changes.
    assert board.update("sam", T0, task_id=task.task_id, title="Fix it", active=True).active is False
    _refused("not_owner", board.update, "ben", T0, task_id=task.task_id, title="mine now")
    _refused("invalid", board.update, "sam", T0, task_id=task.task_id, title=" ")
    board.cancel("sam", T0, task_id=task.task_id)
    _refused("closed", board.update, "sam", T0, task_id=task.task_id, title="again")


def test_claim_next_skips_plan_steps_and_prefers_a_suggestion() -> None:
    board = TaskBoard()
    board.create("ada", T0, title="ada's step", repo_id="r", source="agent_plan")
    for_cy = board.create("sam", T0, title="for cy", repo_id="r", suggested_for="cy")
    anyone = board.create("sam", T0, title="anyone", repo_id="r")
    room_wide = board.create("sam", T0, title="room-wide")

    assert board.claim("cy", T0, next_ready=True, repo_id="r").task_id == for_cy.task_id
    assert board.claim("ben", T0, next_ready=True, repo_id="r").task_id == anyone.task_id
    # A task without a repo is every repo's.
    assert board.claim("ben", T0, next_ready=True, repo_id="r").task_id == room_wide.task_id
    _refused("none_ready", board.claim, "ben", T0, next_ready=True, repo_id="r")
    assert [t.task_id for t in board.list("other")] == [room_wide.task_id]


def test_suggestions_for_someone_else_come_last() -> None:
    board = TaskBoard()
    for_cy = board.create("sam", T0, title="for cy", repo_id="r", suggested_for="cy")
    assert board.claim("ben", T0, next_ready=True, repo_id="r").task_id == for_cy.task_id


def test_a_room_wide_task_reserves_in_every_repo() -> None:
    room = _room("ada", "ben")
    _claimed(room, [API], repo="")
    assert room.evaluate_edit("ben", API, T0, "any-repo").scope == "task"


# --- tasks from dashboards ------------------------------------------------------------

KEY = "k" * 32


def test_a_dashboard_task_is_tagged_and_only_its_dashboard_or_claimer_may_cancel() -> None:
    try:
        from server.tasks import dashboard_tag
    except ImportError:  # Render rootDir=server
        from tasks import dashboard_tag  # type: ignore[no-redef]

    board = TaskBoard()
    task = board.create("adrian", T0, title="Fix login", dashboard_key=KEY, suggested_for="ben")
    assert task.dashboard_tag == dashboard_tag(KEY) and len(task.dashboard_tag) == 16
    assert task.suggested_for == "ben" and task.status == "open"
    assert KEY not in task.model_dump_json()

    # A member who shares the typed name is not its creator.
    _refused("not_owner", board.cancel, "adrian", T0, task_id=task.task_id)
    _refused("not_owner", board.note, "adrian", T0, task_id=task.task_id, text="x")
    _refused("not_owner", board.update, "adrian", T0, task_id=task.task_id, title="x")
    _refused("not_owner", board.cancel_from_dashboard, "x" * 32, T0, task_id=task.task_id)
    _refused("not_owner", board.cancel_from_dashboard, "", T0, task_id=task.task_id)

    cancelled = board.cancel_from_dashboard(KEY, T0 + 1, task_id=task.task_id)
    assert cancelled.status == "cancelled"
    _refused("closed", board.cancel_from_dashboard, KEY, T0, task_id=task.task_id)

    # Once a member claims a dashboard's task, it is the claimer's to cancel.
    other = board.create("adrian", T0, title="Docs", dashboard_key=KEY)
    board.claim("ben", T0, task_id=other.task_id)
    assert board.cancel("ben", T0, task_id=other.task_id).status == "cancelled"


def test_dashboards_may_fill_only_part_of_the_list() -> None:
    board = TaskBoard(max_dashboard_open=2)
    board.create("v", T0, title="a", dashboard_key=KEY)
    board.create("v", T0, title="b", dashboard_key=KEY)
    _refused("limit", board.create, "v", T0, title="c", dashboard_key=KEY)
    # Members are not held back by it.
    assert board.create("sam", T0, title="d").task_id == "T3"


def _main_module() -> Any:
    try:
        from server import main
    except ImportError:  # Render rootDir=server
        import main  # type: ignore[no-redef]
    return main


def test_a_dashboard_creates_and_cancels_its_tasks_over_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_main_module(), "DASHBOARD_TASK_MIN_INTERVAL_SECONDS", 0.0)
    _no_task_rate_limit(monkeypatch)
    client = _client()
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as ben, client.websocket_connect(f"/ws/{room_id}") as dash:
        _join(ben, "ben")
        dash.send_json({"type": "join", "name": None, "role": "spectator"})
        dash.send_json({
            "type": "task", "request_id": "d1", "op": "create", "title": "Fix login",
            "files": [API], "as_name": "Adrian!", "suggested_for": "ben", "dashboard_key": KEY,
        })
        created = _next(dash, "task_reply")
        assert created["ok"], created
        task = created["task"]
        assert (task["created_by"], task["suggested_for"], task["source"]) == ("adrian", "ben", "manual")
        assert task["dashboard_tag"]

        # No name, a short key, or a member-only op: refused.
        dash.send_json({"type": "task", "request_id": "d2", "op": "create", "title": "x", "dashboard_key": KEY})
        assert _next(dash, "task_reply")["reason"] == "invalid"
        dash.send_json(
            {"type": "task", "request_id": "d3", "op": "create", "title": "x", "as_name": "v", "dashboard_key": "short"}
        )
        assert _next(dash, "task_reply")["reason"] == "invalid"
        for op in ("claim", "done", "note", "update", "release", "renew", "list"):
            dash.send_json({"type": "task", "request_id": f"o-{op}", "op": op, "task_id": "T1", "dashboard_key": KEY})
            assert _next(dash, "task_reply")["reason"] == "not_allowed"

        # ben takes it with claim --next (it was suggested for him), then lets go.
        ben.send_json({"type": "task", "request_id": "b1", "op": "claim", "next": True})
        assert _next(ben, "task_reply")["task"]["task_id"] == "T1"
        ben.send_json({"type": "task", "request_id": "b2", "op": "release", "task_id": "T1"})
        assert _next(ben, "task_reply")["ok"]

        dash.send_json({"type": "task", "request_id": "d4", "op": "cancel", "task_id": "T1", "dashboard_key": "z" * 32})
        assert _next(dash, "task_reply")["reason"] == "not_owner"
        dash.send_json({"type": "task", "request_id": "d5", "op": "cancel", "task_id": "T1", "dashboard_key": KEY})
        assert _next(dash, "task_reply")["task"]["status"] == "cancelled"


def test_dashboard_task_changes_are_rate_limited() -> None:
    client = _client()
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as dash:
        dash.send_json({"type": "join", "name": None, "role": "spectator"})
        frame = {"type": "task", "op": "create", "title": "x", "as_name": "v", "dashboard_key": KEY}
        dash.send_json({**frame, "request_id": "r1"})
        assert _next(dash, "task_reply")["ok"]
        dash.send_json({**frame, "request_id": "r2"})
        assert _next(dash, "task_reply")["reason"] == "rate_limited"


def test_a_dashboard_cannot_add_clear_text_tasks_to_an_e2e_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_main_module(), "DASHBOARD_TASK_MIN_INTERVAL_SECONDS", 0.0)
    client = _client()
    room_id = client.post("/rooms", json={"settings": {"e2e": True}}).json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as dash:
        dash.send_json({"type": "join", "name": None, "role": "spectator"})
        base = {"type": "task", "op": "create", "as_name": "v", "dashboard_key": KEY}
        dash.send_json({**base, "request_id": "e1", "title": "readable"})
        assert _next(dash, "task_reply")["reason"] == "invalid"
        dash.send_json({**base, "request_id": "e2", "e2e": "2.sealed", "files": ["h:" + "c" * 32]})
        assert _next(dash, "task_reply")["ok"]
