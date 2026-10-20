"""Phase 3 decision engine: symbol/line/branch-aware edit checks, plans and
decision feedback."""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

try:
    from server import metrics
    from server.main import _edit_check_reason, app
    from server.models import (
        PROTOCOL_VERSION,
        ActivityMessage,
        AgentKind,
        FileChange,
        PlanMessage,
    )
    from server.room import MAX_TRACKED_DECISIONS, Room, is_always_warn_file
except ImportError:  # Render rootDir=server
    import metrics  # type: ignore[no-redef]
    from main import _edit_check_reason, app  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        PROTOCOL_VERSION,
        ActivityMessage,
        AgentKind,
        FileChange,
        PlanMessage,
    )
    from room import MAX_TRACKED_DECISIONS, Room, is_always_warn_file  # type: ignore[no-redef]

FILE = "server/room.py"


class FakeSocket:
    pass


def _join(room: Room, user_id: str, agent: AgentKind = "cursor") -> None:
    room.add_member(user_id, FakeSocket(), agent)


def _touch(
    room: Room,
    user_id: str,
    timestamp: float,
    file_path: str = FILE,
    symbols: Optional[list[str]] = None,
    line_ranges: Optional[list[tuple[int, int]]] = None,
    branch: str = "",
) -> None:
    room.record_activity(
        ActivityMessage(
            user_id=user_id,
            timestamp=timestamp,
            changes=[
                FileChange(
                    file_path=file_path,
                    diff="@@ -1 +1 @@",
                    symbols=symbols or [],
                    line_ranges=line_ranges or [],
                )
            ],
            agent=room.member_agent(user_id),
            branch=branch,
        )
    )


@pytest.fixture
def room() -> Room:
    room = Room()
    _join(room, "sam")
    _join(room, "priya", "claude-code")
    return room


# --- decision table -----------------------------------------------------------


def test_intersecting_symbols_block(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["Room.check_edit"])
    v = room.evaluate_edit("priya", FILE, 1005.0, symbols=["Room.check_edit", "x"])
    assert (v.decision, v.scope) == ("block", "symbol")
    assert v.peers[0].symbols == ["Room.check_edit"]


def test_disjoint_symbols_warn(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["Room.broadcast"])
    v = room.evaluate_edit("priya", FILE, 1005.0, symbols=["Room.check_edit"])
    assert (v.decision, v.scope) == ("warn", "symbol")


def test_disjoint_symbols_with_overlapping_lines_block(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["Room.broadcast"], line_ranges=[(40, 44)])
    v = room.evaluate_edit(
        "priya", FILE, 1005.0, symbols=["Room.check_edit"], line_ranges=[(44, 46)]
    )
    assert (v.decision, v.scope) == ("block", "lines")


def test_project_config_is_always_warn() -> None:
    from server.room import is_always_warn_file

    assert is_always_warn_file(".hivelane.toml")


def test_imports_symbol_intersects_only_imports(room: Room) -> None:
    # Finding 03-14: an import line does not collide with an unrelated function.
    _touch(room, "sam", 1000.0, symbols=["<imports>", "foo"])
    v = room.evaluate_edit("priya", FILE, 1005.0, symbols=["Room.check_edit"])
    assert (v.decision, v.scope) == ("warn", "symbol")
    v = room.evaluate_edit("priya", FILE, 1005.0, symbols=["<imports>"])
    assert (v.decision, v.scope) == ("block", "symbol")


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        "web/package.json",
        "pyproject.toml",
        "requirements-dev.txt",
        "uv.lock",
        "go.mod",
        "Cargo.toml",
        "tsconfig.app.json",
        ".env.local",
        "Dockerfile",
        ".github/workflows/ci.yml",
    ],
)
def test_always_warn_files_ignore_symbol_disjointness(room: Room, path: str) -> None:
    assert is_always_warn_file(path)
    _touch(room, "sam", 1000.0, file_path=path, symbols=["a"])
    v = room.evaluate_edit("priya", path, 1005.0, symbols=["b"])
    assert (v.decision, v.scope) == ("block", "file")


def test_ordinary_yaml_is_not_always_warn() -> None:
    assert not is_always_warn_file("deploy/app.yml")
    assert not is_always_warn_file("server/room.py")


def test_overlapping_lines_block(room: Room) -> None:
    _touch(room, "sam", 1000.0, line_ranges=[(10, 20)])
    v = room.evaluate_edit("priya", FILE, 1005.0, line_ranges=[(25, 30)])
    assert (v.decision, v.scope) == ("block", "lines")  # within 10 lines


def test_distant_lines_warn(room: Room) -> None:
    _touch(room, "sam", 1000.0, line_ranges=[(10, 20)])
    v = room.evaluate_edit("priya", FILE, 1005.0, line_ranges=[(200, 210)])
    assert (v.decision, v.scope) == ("warn", "lines")


def test_lines_used_when_one_side_lacks_symbols(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["f"], line_ranges=[(10, 20)])
    v = room.evaluate_edit("priya", FILE, 1005.0, line_ranges=[(15, 16)])
    assert (v.decision, v.scope) == ("block", "lines")


def test_no_detail_blocks_at_file_level(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["f"])
    v = room.evaluate_edit("priya", FILE, 1005.0)
    assert (v.decision, v.scope) == ("block", "file")


def test_warn_window_keeps_most_specific_scope(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["f"])
    v = room.evaluate_edit("priya", FILE, 1040.0, symbols=["f"])
    assert (v.decision, v.scope) == ("warn", "symbol")
    _touch(room, "sam", 1000.0, line_ranges=[(1, 2)])
    v = room.evaluate_edit("priya", FILE, 1040.0, line_ranges=[(1, 2)])
    assert (v.decision, v.scope) == ("warn", "lines")


def test_different_branch_never_blocks(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["f"], branch="feat/x")
    v = room.evaluate_edit("priya", FILE, 1002.0, symbols=["f"], branch="main")
    assert (v.decision, v.scope) == ("warn", "branch")
    assert room.evaluate_edit("priya", FILE, 1100.0, branch="main").decision == "allow"


def test_empty_branch_is_same_branch(room: Room) -> None:
    _touch(room, "sam", 1000.0, branch="feat/x")
    assert room.evaluate_edit("priya", FILE, 1002.0).decision == "block"
    _touch(room, "sam", 1000.0, branch="")
    assert room.evaluate_edit("priya", FILE, 1002.0, branch="main").decision == "block"


def test_same_branch_peer_outranks_branch_peer(room: Room) -> None:
    _join(room, "lee")
    _touch(room, "lee", 1001.0, branch="other", symbols=["g"])
    _touch(room, "sam", 1000.0, branch="main", symbols=["f"])
    v = room.evaluate_edit("priya", FILE, 1003.0, branch="main", symbols=["g"])
    assert (v.decision, v.scope) == ("warn", "symbol")
    assert [p.user_id for p in v.peers] == ["sam", "lee"]


def test_find_overlaps_skips_disjoint_symbols(room: Room) -> None:
    _touch(room, "sam", 1000.0, symbols=["f"])
    msg = ActivityMessage(
        user_id="priya",
        timestamp=1005.0,
        changes=[FileChange(file_path=FILE, diff="d", symbols=["g"])],
    )
    assert room.find_overlaps(msg) == []
    msg.changes[0].symbols = ["f"]
    assert len(room.find_overlaps(msg)) == 1
    msg.changes[0].symbols = []
    assert len(room.find_overlaps(msg)) == 1


# --- plans --------------------------------------------------------------------


def _plan(room: Room, user: str, now: float, **kw: Any) -> Any:
    return room.record_plan(PlanMessage(user_id=user, **kw), now, room.member_agent(user))


def test_plan_warns_edit_check(room: Room) -> None:
    _plan(room, "sam", 1000.0, summary="split Room", files=[FILE])
    v = room.evaluate_edit("priya", FILE, 1010.0)
    assert (v.decision, v.scope) == ("warn", "plan")
    assert v.peers[0].kind == "planned"
    assert _edit_check_reason("warn", FILE, v.peers, 1010.0, "plan") == (
        f"sam plans to change {FILE}: split Room"
    )


def test_plan_without_files_matches_by_symbol(room: Room) -> None:
    _plan(room, "sam", 1000.0, files=[], symbols=["f"])
    v = room.evaluate_edit("priya", FILE, 1010.0, symbols=["f"])
    assert v.scope == "plan"


def test_plan_symbols_belong_to_the_plans_files(room: Room) -> None:
    # Finding 03-15: `main` planned in api/server.py says nothing about FILE.
    _plan(room, "sam", 1000.0, files=["other.py"], symbols=["f"])
    v = room.evaluate_edit("priya", FILE, 1010.0, symbols=["f"])
    assert v.decision == "allow"


def test_plan_expires_and_clears_on_leave(room: Room) -> None:
    _plan(room, "sam", 1000.0, files=[FILE], ttl_seconds=60)
    assert room.evaluate_edit("priya", FILE, 1061.0).decision == "allow"
    _plan(room, "sam", 1000.0, files=[FILE])
    room.remove_member("sam")
    assert room.active_plans(1001.0) == []


def test_plan_replaces_previous(room: Room) -> None:
    _plan(room, "sam", 1000.0, files=[FILE])
    _plan(room, "sam", 1001.0, files=["b.py"])
    assert room.evaluate_edit("priya", FILE, 1002.0).decision == "allow"


def test_plan_conflicts(room: Room) -> None:
    _join(room, "lee")
    _touch(room, "sam", 1000.0, file_path="a.py")
    _plan(room, "lee", 1000.0, summary="rename", files=["b.py"])
    mine = _plan(room, "priya", 1005.0, files=["a.py", "b.py", "c.py"])
    got = {(c.file_path, c.user_id, c.kind) for c in room.plan_conflicts(mine, 1005.0)}
    assert got == {("a.py", "sam", "edited"), ("b.py", "lee", "planned")}


def test_ttl_bounds() -> None:
    with pytest.raises(Exception):
        PlanMessage(user_id="x", ttl_seconds=10)


# --- feedback -----------------------------------------------------------------


def test_feedback_counts_one_vote_per_voter() -> None:
    room = Room()
    room.register_decision("d1", "symbol")
    assert room.record_feedback("d1", True, "a").useful_count == 1
    assert room.record_feedback("d1", True, "a") is None
    upd = room.record_feedback("d1", False, "b")
    assert (upd.useful_count, upd.noise_count) == (1, 1)
    assert room.record_feedback("unknown", True, "a") is None


def test_feedback_lru_bound() -> None:
    room = Room()
    for n in range(MAX_TRACKED_DECISIONS + 5):
        room.register_decision(f"d{n}", "file")
    assert room.decision_scope("d0") is None
    assert room.decision_scope(f"d{MAX_TRACKED_DECISIONS + 4}") == "file"


# --- over the wire ------------------------------------------------------------


def _wjoin(ws: Any, name: Optional[str], agent: str = "cursor") -> None:
    ws.send_json(
        {"type": "join", "name": name, "agent": agent, "protocol_version": PROTOCOL_VERSION}
    )


def _frame(ws: Any, type_name: str) -> dict:
    for _ in range(20):
        frame = ws.receive_json()
        if frame.get("type") == type_name:
            return frame
    raise AssertionError(type_name)


def _feed(ws: Any, type_name: str) -> dict:
    for _ in range(20):
        frame = ws.receive_json()
        if frame.get("type") != "hivelane_feed":
            continue
        for event in frame["events"]:
            if event.get("type") == type_name:
                return event
    raise AssertionError(type_name)


def _sample(name: str) -> float:
    for line in metrics.render().splitlines():
        if line.startswith(name + " "):
            return float(line.split()[-1])
    return 0.0


def test_wire_symbol_check_plan_and_feedback() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _wjoin(sam, "sam")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _wjoin(priya, "priya", "claude-code")
            sam.send_json(
                {
                    "type": "activity",
                    "user_id": "sam",
                    "branch": "main",
                    "changes": [
                        {"file_path": FILE, "diff": "d", "symbols": ["Room.check_edit"]}
                    ],
                }
            )
            update = _feed(sam, "activity_update")
            assert update["branch"] == "main"
            assert update["changes"][0]["symbols"] == ["Room.check_edit"]

            priya.send_json(
                {
                    "type": "edit_check",
                    "request_id": "r1",
                    "file_path": FILE,
                    "symbol": "Room.check_edit",
                    "branch": "main",
                }
            )
            resp = _frame(priya, "edit_check_response")
            assert resp["decision"] == "block" and resp["scope"] == "symbol"
            assert len(resp["decision_id"]) == 32
            assert resp["others"][0]["symbols"] == ["Room.check_edit"]
            assert resp["others"][0]["kind"] == "edited"
            assert "is editing Room.check_edit in server/room.py" in resp["reason"]
            decision = _feed(sam, "edit_decision")
            assert decision["decision_id"] == resp["decision_id"]
            assert decision["scope"] == "symbol"

            priya.send_json({"type": "plan", "user_id": "x", "files": [FILE], "summary": "s"})
            conflicts = _frame(priya, "plan_conflicts")["conflicts"]
            assert conflicts[0]["user_id"] == "sam" and conflicts[0]["kind"] == "edited"
            plan = _feed(sam, "plan_update")
            assert plan["user_id"] == "priya" and plan["expires_at"] > plan["timestamp"]

            before = _sample(
                'hivelane_decision_feedback_total{useful="true",scope="symbol"}'
            )
            with client.websocket_connect(f"/ws/{room_id}") as dash:
                _wjoin(dash, None)
                caught_up = _frame(dash, "plan_update")
                assert caught_up["user_id"] == "priya"
                dash.send_json(
                    {"type": "decision_feedback", "decision_id": resp["decision_id"], "useful": True}
                )
                dash.send_json(
                    {"type": "decision_feedback", "decision_id": resp["decision_id"], "useful": True}
                )
                fb = _feed(dash, "decision_feedback_update")
                assert (fb["useful_count"], fb["noise_count"]) == (1, 0)
            after = _sample('hivelane_decision_feedback_total{useful="true",scope="symbol"}')
            assert after - before == 1
            assert _sample('hivelane_edit_checks_total{decision="block",scope="symbol"}') >= 1
