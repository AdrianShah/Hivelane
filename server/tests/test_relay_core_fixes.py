"""Regression tests for the relay-core review (findings 03-* and the relay
items of 04-*): delivery that no slow socket can stall, merged touches,
per-repo activity limits, bounded memory, room lifetime, the join handshake
and the plan correlation id."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Iterator
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server import file_queue, main, metrics, room as room_module, room_manager as rm_module
from server.file_queue import FileQueues
from server.limits import TokenBucket
from server.main import _edit_check_reason, app
from server.models import (
    PROTOCOL_VERSION,
    ActivityMessage,
    FileChange,
    PlanConflict,
    PlanMessage,
    RoomSettings,
)
from server.room import FileTouch, Room, feed_frames, fit_e2e, ranges_near
from server.room_manager import RoomManager
from server.store import MemoryRoomStore

FILE = "server/room.py"


# --- helpers -----------------------------------------------------------------


class FakeSocket:
    """A member/spectator socket that records frames; `delay` makes it slow."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.sent: list[Any] = []
        self.closed: Optional[int] = None

    async def send_text(self, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(json.loads(text))

    async def send_json(self, payload: Any) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = code


def _activity(user: str, ts: float, path: str = FILE, repo: str = "", **change: Any) -> ActivityMessage:
    return ActivityMessage(
        user_id=user,
        timestamp=ts,
        repo_id=repo,
        changes=[FileChange(file_path=path, diff="@@", **change)],
    )


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


@pytest.fixture(autouse=True)
def _fresh_counters() -> Iterator[None]:
    metrics.registry.reset()
    yield
    metrics.registry.reset()


def _join(socket: Any, name: Optional[str]) -> None:
    socket.send_json(
        {"type": "join", "name": name, "agent": "cursor", "protocol_version": PROTOCOL_VERSION}
    )


def _next_event(socket: Any, type_name: str, frames: int = 30) -> dict:
    for _ in range(frames):
        frame = socket.receive_json()
        events = frame["events"] if frame.get("type") == "synergo_feed" else [frame]
        for event in events:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received {type_name}")


# --- 03-1 / 03-9 / 03-43 / 04-7 / perf #1: delivery -----------------------------


def test_a_slow_socket_does_not_stall_broadcast_or_the_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_module, "SEND_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(room_module, "BROADCAST_FLUSH_SECONDS", 0.01)

    async def scenario() -> None:
        room = Room()
        fast, slow, dash = FakeSocket(), FakeSocket(delay=30), FakeSocket(delay=30)
        room.add_member("alice", fast)
        room.add_member("bob", slow)
        room.add_spectator(dash)
        await room.broadcast(room.policy_update())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await room.broadcast(room.policy_update())  # never waits on a socket
        assert time.monotonic() - started < 0.05
        await asyncio.sleep(0.5)
        # Both feeds reached the fast socket; the slow ones were dropped once
        # (bounded by the send timeout), not waited on per frame.
        assert len(fast.sent) == 2
        assert slow.closed == room_module.WS_CLOSE_TOO_SLOW
        # 03-9: membership is left to bob's own handler, never popped by name.
        assert room.is_taken("bob")
        assert room.spectator_count == 0

    asyncio.run(scenario())


def test_feeds_are_serialized_once_and_split_under_the_frame_budget() -> None:
    events = [{"type": "x", "blob": "a" * 300} for _ in range(10)]
    frames = feed_frames(events, budget=1000)
    assert len(frames) > 1
    assert all(len(frame.encode()) <= 1000 for frame in frames)
    decoded = [event for frame in frames for event in json.loads(frame)["events"]]
    assert decoded == events
    assert json.loads(feed_frames(events, budget=0)[0]) == {"type": "synergo_feed", "events": events}


def test_catch_up_holds_feeds_until_the_snapshot_is_out() -> None:
    async def scenario() -> None:
        room = Room()
        dash = FakeSocket()
        room.add_spectator(dash)
        room.begin_catch_up(dash)
        await room._deliver({"type": "newer"})
        assert dash.sent == []
        await room.send(dash, room.policy_update())  # the snapshot
        await room.finish_catch_up(dash)
        assert [frame["type"] for frame in dash.sent] == ["room_policy_update", "newer"]
        await room._deliver({"type": "live"})
        assert dash.sent[-1] == {"type": "live"}

    asyncio.run(scenario())


# --- 03-3: merged touches ------------------------------------------------------------


def test_a_peers_touch_keeps_earlier_symbols_inside_the_window() -> None:
    room = Room(RoomSettings(block_window_seconds=20, warn_window_seconds=60))
    room.add_member("alice", FakeSocket())
    room.record_activity(_activity("alice", 1000.0, symbols=["foo"]))
    room.record_activity(_activity("alice", 1005.0, symbols=["bar"]))
    verdict = room.evaluate_edit("bob", FILE, 1006.0, symbols=["foo"])
    assert (verdict.decision, verdict.scope) == ("block", "symbol")
    # Each symbol is judged by its own age: foo is past the block window here.
    verdict = room.evaluate_edit("bob", FILE, 1022.0, symbols=["foo"])
    assert (verdict.decision, verdict.scope) == ("warn", "symbol")
    verdict = room.evaluate_edit("bob", FILE, 1022.0, symbols=["bar"])
    assert (verdict.decision, verdict.scope) == ("block", "symbol")


def test_a_batch_without_symbols_makes_the_touch_file_level() -> None:
    room = Room()
    room.add_member("alice", FakeSocket())
    room.record_activity(_activity("alice", 1000.0, symbols=["foo"]))
    room.record_activity(_activity("alice", 1001.0))
    verdict = room.evaluate_edit("bob", FILE, 1002.0, symbols=["bar"])
    assert (verdict.decision, verdict.scope) == ("block", "file")


def test_line_ranges_merge_and_keep_their_own_age() -> None:
    room = Room(RoomSettings(block_window_seconds=20, warn_window_seconds=60))
    room.add_member("alice", FakeSocket())
    room.record_activity(_activity("alice", 1000.0, line_ranges=[(10, 12)]))
    room.record_activity(_activity("alice", 1005.0, line_ranges=[(200, 210)]))
    verdict = room.evaluate_edit("bob", FILE, 1006.0, line_ranges=[(11, 11)])
    assert (verdict.decision, verdict.scope) == ("block", "lines")


# --- 03-4: activity is never dropped ----------------------------------------------------


def test_back_to_back_activity_for_two_repos_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        room = Room()
        room.add_member("alice", FakeSocket())
        room.add_member("bob", FakeSocket())
        for repo, path in (("repoA", "a.py"), ("repoB", "b.py"), ("repoB", "c.py")):
            await main.handle_activity(
                room,
                "alice",
                {"type": "activity", "user_id": "x", "repo_id": repo,
                 "changes": [{"file_path": path, "diff": "@@"}]},
            )
        for repo, path in (("repoA", "a.py"), ("repoB", "b.py"), ("repoB", "c.py")):
            verdict = room.evaluate_edit("bob", path, time.time(), repo_id=repo)
            assert verdict.decision == "block", (repo, path)
        # The throttled third one is coalesced and still broadcast.
        assert ("alice", "repoB") in room.deferred_activity
        await asyncio.sleep(main.ACTIVITY_MIN_INTERVAL_SECONDS + 0.2)
        assert ("alice", "repoB") not in room.deferred_activity
        assert any(u.repo_id == "repoB" and u.changes[0].file_path == "c.py"
                   for u in room.latest_updates())

    asyncio.run(scenario())


def test_rate_limit_is_per_repo_and_monotonic() -> None:
    room = Room()
    assert room.is_activity_rate_limited("a", 0.5, scope="r1") is False
    assert room.is_activity_rate_limited("a", 0.5, scope="r2") is False
    assert room.is_activity_rate_limited("a", 0.5, scope="r1") is True


# --- 03-2 / 03-30: Gemini off the receive loop ----------------------------------------


def test_gemini_captions_do_not_block_the_receive_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_caption(message: ActivityMessage) -> str:
        await asyncio.sleep(0.3)
        return "a real caption"

    monkeypatch.setattr(main, "_captions_enabled", lambda: True)
    monkeypatch.setattr(main, "generate_caption", slow_caption)

    async def scenario() -> None:
        room = Room()
        room.add_member("alice", FakeSocket())
        started = time.monotonic()
        await main.handle_activity(
            room, "alice",
            {"type": "activity", "user_id": "alice", "changes": [{"file_path": "a.py", "diff": "@@"}]},
        )
        assert time.monotonic() - started < 0.1
        await asyncio.sleep(0.5)
        assert [u.caption for u in room.latest_updates()] == ["a real caption"]

    asyncio.run(scenario())


def test_gemini_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MAX_GEMINI_IN_FLIGHT", 1)
    monkeypatch.setattr(main, "_gemini_in_flight", 0)
    assert main._take_gemini_slot() is True
    assert main._take_gemini_slot() is False
    main._release_gemini_slot()
    assert main._take_gemini_slot() is True
    main._release_gemini_slot()


# --- 03-5: outbound size ------------------------------------------------------------------


def test_derived_e2e_copies_are_trimmed_to_the_frame_budget() -> None:
    conflicts = [
        PlanConflict(file_path=f"h:{i}", user_id="alice", kind="edited", seconds_ago=1, e2e="b" * 200_000)
        for i in range(50)
    ]
    main._fit_plan_conflicts(conflicts, "p1")
    frame = main.PlanConflicts(conflicts=conflicts, request_id="p1").model_dump_json()
    assert len(frame) <= room_module.OUTBOUND_FRAME_BUDGET
    assert conflicts[0].e2e  # the most relevant keep theirs
    assert len(conflicts) == 50
    assert fit_e2e([], 10) == 0


# --- 03-6 / 04-3: bounded touch memory -----------------------------------------------------


def test_touch_table_is_bounded_per_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_module, "MAX_TOUCHES_PER_ROOM", 10)
    room = Room()
    room.add_member("alice", FakeSocket())
    for i in range(50):
        room.record_activity(_activity("alice", 1000.0 + i, path=f"f{i}.py"))
    assert room.touch_count == 10
    assert room.recent_other_touches("f49.py", "bob", 1050.0)
    assert not room.recent_other_touches("f0.py", "bob", 1050.0)


def test_touch_bytes_are_bounded_and_long_paths_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(room_module, "MAX_TOUCH_BYTES_PER_ROOM", 50_000)
    monkeypatch.setattr(room_module, "MAX_TRACKED_PATH_CHARS", 100)
    room = Room()
    for i in range(100):
        room.record_activity(
            ActivityMessage(
                user_id="alice", timestamp=1000.0,
                changes=[FileChange(file_path=f"f{i}.py", diff="x" * 9000)],
            )
        )
    assert room.touch_bytes <= 50_000
    room.record_activity(_activity("alice", 1000.0, path="p" * 500))
    assert not room.recent_other_touches("p" * 500, "bob", 1000.0, active_only=False)


def test_plans_and_intents_are_kept_per_repo() -> None:
    room = Room()
    room.add_member("sam", FakeSocket())
    room.record_plan(PlanMessage(user_id="sam", files=["server/x.py"], repo_id="A"), 1000.0)
    room.record_plan(PlanMessage(user_id="sam", files=["docs/y.md"], repo_id="B"), 1000.0)
    assert room.evaluate_edit("bob", "server/x.py", 1001.0, repo_id="A").scope == "plan"
    assert room.evaluate_edit("bob", "docs/y.md", 1001.0, repo_id="B").scope == "plan"
    room.record_intent("sam", "fix main.py", 1000.0, repo_id="A")
    room.record_intent("sam", "fix other.py", 1000.0, repo_id="B")
    assert len(room.latest_intents()) == 2
    room.remove_member("sam")
    assert room.latest_intents() == [] and room.active_plans(1001.0) == []


# --- 03-7 / 03-13 / 04-2 / 04-23: room lifetime ---------------------------------------------


def test_cleanup_grace_runs_from_the_latest_departure() -> None:
    async def scenario() -> None:
        manager = RoomManager(store=MemoryRoomStore())
        code = manager.create_room()
        manager.schedule_cleanup(code, 0.3)
        await asyncio.sleep(0.2)
        manager.schedule_cleanup(code, 0.3)  # someone came back and left again
        assert manager.cleanup_timer_count == 1
        await asyncio.sleep(0.2)
        assert manager.get(code) is not None  # the first timer did not fire
        await asyncio.sleep(0.2)
        assert manager.live(code) is None and manager.get(code) is None

    asyncio.run(scenario())


def test_a_room_nobody_joins_is_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rm_module, "UNCLAIMED_ROOM_TTL_SECONDS", 0.1)

    async def scenario() -> None:
        manager = RoomManager(store=MemoryRoomStore())
        code = manager.create_room()
        await asyncio.sleep(0.25)
        assert manager.get(code) is None

    asyncio.run(scenario())


def test_a_room_in_use_when_the_timer_fires_is_kept() -> None:
    async def scenario() -> None:
        manager = RoomManager(store=MemoryRoomStore())
        code = manager.create_room()
        room = manager.live(code)
        assert room is not None
        room.connecting = 1  # 03-12: mid-handshake or waiting for approval
        manager.schedule_cleanup(code, 0.05)
        await asyncio.sleep(0.15)
        assert manager.live(code) is room

    asyncio.run(scenario())


def test_pending_joiners_keep_a_room_from_being_empty() -> None:
    async def scenario() -> None:
        room = Room()
        assert room.is_empty
        pending = room.open_pending("carol")
        assert pending is not None and not room.is_empty

    asyncio.run(scenario())


# --- 03-8 / 03-21 / 04-10: identity ---------------------------------------------------------


def test_a_replaced_connection_cannot_evict_its_successor() -> None:
    room = Room()
    old, new = FakeSocket(), FakeSocket()
    room.add_member("alice", old)
    room.add_member("alice", new)  # reconnected
    assert room.remove_member("alice", old) is False
    assert room.is_taken("alice") and room.member_socket("alice") is new
    assert room.remove_member("alice", new) is True
    assert not room.is_taken("alice")


def test_the_name_is_taken_as_soon_as_the_handshake_passes(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as first:
        _join(first, "alice")
        first.send_json({"type": "edit_check", "request_id": "r", "file_path": "a.py"})
        _next_event(first, "edit_check_response")
        with client.websocket_connect(f"/ws/{room_id}") as second:
            _join(second, "alice")
            assert _next_event(second, "name_rejected")["reason"] == "Name taken"
        room = relay.live(room_id)
        assert room is not None and room.is_taken("alice")


# --- 03-17 / 04-9: handshake bounds -----------------------------------------------------------


def test_a_socket_that_never_joins_is_closed(relay: RoomManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "HANDSHAKE_TIMEOUT_SECONDS", 0.3)
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as idle:
        with pytest.raises(WebSocketDisconnect) as closed:
            idle.receive_json()
    assert closed.value.code == main.WS_CLOSE_HANDSHAKE_TIMEOUT
    room = relay.live(room_id)
    assert room is not None and room.connecting == 0


def test_too_many_invalid_joins_close_the_socket(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as noisy:
        for _ in range(main.MAX_HANDSHAKE_ATTEMPTS):
            _join(noisy, "!!!")
            assert _next_event(noisy, "name_rejected")
        _join(noisy, "!!!")
        with pytest.raises(WebSocketDisconnect) as closed:
            noisy.receive_json()
    assert closed.value.code == main.WS_CLOSE_HANDSHAKE_TIMEOUT


# --- 03-10: plan correlation -------------------------------------------------------------------


def test_every_plan_gets_one_reply_carrying_its_request_id(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json({"type": "plan", "user_id": "sam", "files": ["a.py"], "request_id": "p1"})
        sam.send_json({"type": "plan", "user_id": "sam", "files": ["b.py"], "request_id": "p2"})
        sam.send_json({"type": "plan", "user_id": "sam", "files": "nope", "request_id": "p3"})
        replies = [_next_event(sam, "plan_conflicts") for _ in range(3)]
    assert [(r["request_id"], r["reason"]) for r in replies] == [
        ("p1", ""),
        ("p2", "rate_limited"),
        ("p3", "invalid"),
    ]


# --- 03-11: edit checks have their own budget ---------------------------------------------------


def test_a_throttled_edit_check_is_answered_with_warn(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json({"type": "edit_check", "request_id": "r0", "file_path": "a.py"})
        _next_event(sam, "edit_check_response")
        room = relay.live(room_id)
        assert room is not None
        room._edit_check_quotas["sam"] = TokenBucket(rate_per_minute=1)
        room._edit_check_quotas["sam"].take()
        sam.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
        reply = _next_event(sam, "edit_check_response")
    assert (reply["request_id"], reply["decision"]) == ("r1", "warn")


# --- small fixes --------------------------------------------------------------------------------


def test_edit_reasons_describe_real_edits_without_a_diff() -> None:
    peer = FileTouch(user_id="alice", file_path=FILE, diff="", prompt=None, timestamp=997.0,
                     agent="cursor", symbols=["foo"])
    reason = _edit_check_reason("block", FILE, [peer], 1000.0, "symbol")
    assert reason == f"alice's cursor is editing foo in {FILE} (3s ago)"


def test_only_a_member_in_line_takes_a_claim() -> None:
    queues = FileQueues()
    key = ("", FILE)
    queues.enqueue(key, "b", "cursor", 1000.0)
    queues.take(key, "c", 1000.0)  # c never queued
    assert queues.claimers(key) == {}
    assert file_queue.CLAIM_MAX_SECONDS < 20


def test_intent_mentions_match_whole_words() -> None:
    room = Room()
    room.add_member("sam", FakeSocket())
    room.record_intent("sam", "fix the mapping and make it happy", 1000.0)
    assert room.evaluate_edit("bob", "src/map.py", 1001.0).decision == "allow"
    room.record_intent("sam", "fix map.py please", 1000.0)
    assert room.evaluate_edit("bob", "src/map.py", 1001.0).scope == "intent"


def test_pending_joins_are_bounded_per_address() -> None:
    async def scenario() -> None:
        room = Room()
        assert room.open_pending("a", ip="1.2.3.4") is not None
        assert room.open_pending("b", ip="1.2.3.4") is not None
        assert room.open_pending("c", ip="1.2.3.4") is None
        assert room.open_pending("d", ip="5.6.7.8") is not None

    asyncio.run(scenario())


def test_ranges_near_matches_the_brute_force_answer() -> None:
    def brute(a: list, b: list, p: int) -> bool:
        return any(x <= d + p and c <= y + p for x, y in a for c, d in b)

    rng = random.Random(3)
    for _ in range(2000):
        a = [tuple(sorted((rng.randint(1, 300), rng.randint(1, 300)))) for _ in range(rng.randint(0, 6))]
        b = [tuple(sorted((rng.randint(1, 300), rng.randint(1, 300)))) for _ in range(rng.randint(0, 6))]
        p = rng.choice([0, 10])
        assert ranges_near(a, b, p) == brute(a, b, p), (a, b, p)


def test_readyz_reports_version_and_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "RELAY_COMMIT", "abc123")
    body = TestClient(app).get("/readyz").json()
    assert body["commit"] == "abc123"
    assert isinstance(body["version"], str) and body["version"]


# --- relay-infra follow-ups -------------------------------------------------------------------


class _UnreadableStore(MemoryRoomStore):
    def get_record(self, room_id: str) -> Any:
        from server.store import StoreReadError

        raise StoreReadError("down")


def test_an_unreadable_registry_refuses_instead_of_reviving(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.store import StoreReadError

    store = _UnreadableStore()
    store.add("ABCDEFGH", time.time())
    manager = RoomManager(store=store)
    with pytest.raises(StoreReadError):
        manager.get("ABCDEFGH")
    monkeypatch.setattr(main, "room_manager", manager)
    with TestClient(app).websocket_connect("/ws/ABCDEFGH") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1013
    assert manager.live("ABCDEFGH") is None


def test_eviction_notifies_listeners() -> None:
    manager = RoomManager(store=MemoryRoomStore())
    code = manager.create_room()
    seen: list[str] = []
    manager.evict_listeners.append(seen.append)
    manager.cleanup_if_empty(code)
    assert seen == [code]


def test_gemini_slots_are_capped_per_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "MAX_GEMINI_IN_FLIGHT_PER_ROOM", 1)
    monkeypatch.setattr(main, "_gemini_in_flight", 0)
    busy, other = Room(), Room()
    assert main._take_gemini_slot(busy) is True
    assert main._take_gemini_slot(busy) is False
    assert main._take_gemini_slot(other) is True
    main._release_gemini_slot(busy)
    main._release_gemini_slot(other)
    assert busy.gemini_in_flight == 0 and main._gemini_in_flight == 0
