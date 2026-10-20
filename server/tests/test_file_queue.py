"""Per-file wait queues (server/file_queue.py) and how the relay works them:
ordering, dedupe, bounds, expiry, release on lapse and on leave, the grace
hand-off, third parties waiting behind the line, claims, and that no state
waits forever."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

try:
    from server.file_queue import FileQueues, QueueEntry, QueueKey
    from server.models import ActivityMessage, FileChange, PlanMessage, RoomSettings
    from server.room import EditVerdict, Room
except ImportError:  # Render rootDir=server
    from file_queue import FileQueues, QueueEntry, QueueKey  # type: ignore[no-redef]
    from models import ActivityMessage, FileChange, PlanMessage, RoomSettings  # type: ignore[no-redef]
    from room import EditVerdict, Room  # type: ignore[no-redef]

KEY: QueueKey = ("repo", "server/room.py")
WARN = 60.0
BLOCK = 20.0
T0 = 1_000_000.0


def _sweep(
    queues: FileQueues,
    now: float,
    holders: Optional[dict[QueueKey, list[str]]] = None,
    active: Optional[set[str]] = None,
    settled: Optional[set[str]] = None,
) -> list[tuple[str, str, str]]:
    """Run a sweep with fixed holders; notices as (kind, to, requester)."""
    holders = holders or {}

    def blockers(key: QueueKey, entry: QueueEntry, _now: float) -> list[str]:
        found = list(holders.get(key, []))
        found.extend(queues.claimers(key, exclude=entry.user_id))
        return found

    notices = queues.sweep(
        now,
        warn_window=WARN,
        block_window=BLOCK,
        blockers=blockers,
        settled=lambda key, user, at: user in (settled or set()),
        is_active=lambda user: active is None or user in active,
    )
    return [(n.kind, n.to_user, n.entry.user_id) for n in notices]


# --- FileQueues: the bookkeeping -------------------------------------------------


def test_the_line_is_first_come_first_served() -> None:
    queues = FileQueues()
    assert queues.enqueue(KEY, "ben", "cursor", T0) == 1
    assert queues.enqueue(KEY, "cy", "codex", T0 + 1) == 2
    assert queues.enqueue(KEY, "dee", "codex", T0 + 2) == 3
    assert [e.user_id for e in queues.queue(KEY)] == ["ben", "cy", "dee"]
    assert queues.position(KEY, "cy") == 2
    assert queues.position(KEY, "nobody") == 0
    assert queues.head(KEY).user_id == "ben"  # type: ignore[union-attr]


def test_asking_again_keeps_your_place_and_restarts_the_expiry() -> None:
    queues = FileQueues()
    queues.enqueue(KEY, "ben", "cursor", T0)
    queues.enqueue(KEY, "cy", "codex", T0)
    assert queues.enqueue(KEY, "ben", "cursor", T0 + 50, symbols=["Room.send"], e2e="blob") == 1
    entry = queues.entry(KEY, "ben")
    assert entry is not None and entry.refreshed_at == T0 + 50
    assert entry.symbols == ["Room.send"] and entry.e2e == "blob"
    assert len(queues.queue(KEY)) == 2

    # cy never asked again: gone after the warn window; ben was refreshed.
    _sweep(queues, T0 + WARN + 1, holders={KEY: ["ana"]})
    assert [e.user_id for e in queues.queue(KEY)] == ["ben"]


def test_lines_and_the_table_of_lines_are_bounded() -> None:
    queues = FileQueues(max_length=2, max_files=2)
    assert queues.enqueue(KEY, "a", "cursor", T0) == 1
    assert queues.enqueue(KEY, "b", "cursor", T0) == 2
    assert queues.enqueue(KEY, "c", "cursor", T0) == 0  # line full
    assert queues.enqueue(KEY, "a", "cursor", T0) == 1  # but a member already in it stays
    assert queues.enqueue(("repo", "two.py"), "a", "cursor", T0) == 1
    assert queues.enqueue(("repo", "three.py"), "a", "cursor", T0) == 0  # too many files
    assert queues.enqueue(("repo", "x" * 5000), "a", "cursor", T0) == 0  # absurd path


def test_entries_expire_after_the_warn_window() -> None:
    queues = FileQueues()
    queues.enqueue(KEY, "ben", "cursor", T0)
    _sweep(queues, T0 + WARN, holders={KEY: ["ana"]})
    assert queues.position(KEY, "ben") == 1
    _sweep(queues, T0 + WARN + 0.5, holders={KEY: ["ana"]})
    assert not queues
    assert queues.keys() == []


def test_holders_hear_once_per_requester_and_the_head_is_released_when_they_lapse() -> None:
    queues = FileQueues()
    queues.enqueue(KEY, "ben", "cursor", T0)
    queues.enqueue(KEY, "cy", "codex", T0)

    assert _sweep(queues, T0, holders={KEY: ["ana"]}) == [("requested", "ana", "ben")]
    assert _sweep(queues, T0 + 1, holders={KEY: ["ana"]}) == []  # told once
    # A second holder joins in: only it is new.
    assert _sweep(queues, T0 + 2, holders={KEY: ["ana", "dan"]}) == [("requested", "dan", "ben")]

    # Every holder lapsed: the head (only) is released, once.
    assert _sweep(queues, T0 + 3) == [("released", "ben", "ben")]
    assert _sweep(queues, T0 + 4) == []
    assert queues.head(KEY).released_at == T0 + 3  # type: ignore[union-attr]


def test_a_holder_coming_back_withdraws_the_release() -> None:
    queues = FileQueues()
    queues.enqueue(KEY, "ben", "cursor", T0)
    assert _sweep(queues, T0) == [("released", "ben", "ben")]
    _sweep(queues, T0 + 2, holders={KEY: ["ana"]})
    assert queues.head(KEY).released_at is None  # type: ignore[union-attr]
    # It frees up again later: released again (a fresh grace).
    assert _sweep(queues, T0 + 30) == [("released", "ben", "ben")]


def test_an_unused_release_passes_to_the_next_in_line_after_the_grace() -> None:
    queues = FileQueues(grace_seconds=10)
    queues.enqueue(KEY, "ben", "cursor", T0)
    queues.enqueue(KEY, "cy", "codex", T0)
    assert _sweep(queues, T0 + 1) == [("released", "ben", "ben")]
    assert _sweep(queues, T0 + 11) == []  # still inside ben's grace
    # Grace over: ben makes way and cy is released in the same pass.
    assert _sweep(queues, T0 + 11.5) == [("released", "cy", "cy")]
    assert [e.user_id for e in queues.queue(KEY)] == ["cy"]


def test_members_who_leave_lose_their_place_and_their_claims() -> None:
    queues = FileQueues()
    queues.enqueue(KEY, "ben", "cursor", T0)
    queues.enqueue(KEY, "cy", "codex", T0)
    queues.enqueue(KEY, "dee", "cursor", T0)
    queues.take(KEY, "dee", T0)  # dee (in line) passed while others wait: a claim
    assert queues.claimers(KEY) == {"dee": T0}

    queues.remove_user("ben")
    queues.remove_user("dee")
    assert [e.user_id for e in queues.queue(KEY)] == ["cy"]
    assert queues.claimers(KEY) == {}

    # A member that vanished without a goodbye (a dead socket) is dropped by
    # the sweep instead.
    queues.enqueue(KEY, "eve", "cursor", T0)
    _sweep(queues, T0 + 1, holders={KEY: ["ana"]}, active={"eve", "ana"})
    assert [e.user_id for e in queues.queue(KEY)] == ["eve"]


def test_taking_the_file_leaves_a_claim_only_while_others_wait() -> None:
    queues = FileQueues()
    queues.enqueue(KEY, "ben", "cursor", T0)
    queues.take(KEY, "ben", T0 + 1)
    assert not queues  # nobody else waited: no line, no claim

    queues.enqueue(KEY, "ben", "cursor", T0)
    queues.enqueue(KEY, "cy", "codex", T0)
    queues.take(KEY, "ben", T0 + 1)
    assert queues.claimers(KEY) == {"ben": T0 + 1}
    # The claim keeps cy waiting (and tells ben once) ...
    assert _sweep(queues, T0 + 2) == [("requested", "ben", "cy")]
    # ... until ben's own touch arrives (the touch then does the blocking) ...
    _sweep(queues, T0 + 3, settled={"ben"}, holders={KEY: ["ben"]})
    assert queues.claimers(KEY) == {}
    # ... or the block window passes with no edit at all.
    queues.take(KEY, "dee", T0 + 4)
    _sweep(queues, T0 + 4 + BLOCK + 1)
    assert queues.claimers(KEY) == {}


def test_nothing_waits_forever() -> None:
    """Crossed lines (each holds the file the other wants), claims and unused
    releases all run out on their own once the holders stop editing."""
    queues = FileQueues(grace_seconds=10)
    one, two = ("repo", "one.py"), ("repo", "two.py")
    queues.enqueue(one, "ben", "cursor", T0)  # ben waits for ana's one.py
    queues.enqueue(two, "ana", "cursor", T0)  # ana waits for ben's two.py
    queues.enqueue(one, "cy", "codex", T0)
    queues.take(two, "dee", T0)

    # Both are blocked, so neither edits: their touches lapse after the block
    # window, which is when the caller stops reporting them as holders.
    _sweep(queues, T0 + 1, holders={one: ["ana"], two: ["ben"]})
    released = _sweep(queues, T0 + BLOCK + 1)
    assert ("released", "ben", "ben") in released
    assert ("released", "ana", "ana") in released
    # Nobody takes anything: every grace lapses, the line drains to empty.
    for step in range(1, 10):
        _sweep(queues, T0 + BLOCK + 1 + step * 11)
    assert not queues


# --- Room: the relay's decisions ------------------------------------------------


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("type") == kind]


FILE = "server/room.py"


def make_room(*names: str, block: float = BLOCK, warn: float = WARN) -> tuple[Room, dict[str, FakeSocket]]:
    room = Room(RoomSettings(block_window_seconds=block, warn_window_seconds=warn))
    sockets = {}
    for name in names:
        sockets[name] = FakeSocket()
        room.add_member(name, sockets[name], "cursor")  # type: ignore[arg-type]
    return room, sockets


def touch(room: Room, user: str, at: float, path: str = FILE, repo: str = "", **change: Any) -> None:
    room.record_activity(
        ActivityMessage(
            user_id=user,
            timestamp=at,
            changes=[FileChange(file_path=path, diff="@@", **change)],
            repo_id=repo,
        )
    )


def check(room: Room, user: str, at: float, path: str = FILE, repo: str = "", **kwargs: Any):
    """What handle_edit_check does: sweep, decide, queue, suggest."""
    asyncio.run(room.tick_file_queues(at))
    verdict = room.evaluate_edit(user, path, at, repo)
    outcome = room.queue_edit_check(user, "codex", repo, path, verdict, at, **kwargs)
    asyncio.run(room.deliver_queue_notices(outcome.notices))
    suggestion = room.edit_suggestion(outcome.decision, user, repo, path, at, outcome.wait)
    return outcome, suggestion


def tick(room: Room, at: float) -> None:
    asyncio.run(room.tick_file_queues(at))


def test_a_blocked_check_queues_and_tells_the_holder() -> None:
    room, sockets = make_room("ana", "ben", "cy")
    touch(room, "ana", T0)

    outcome, suggestion = check(room, "ben", T0 + 5)
    assert outcome.decision == "block"
    assert outcome.queue_position == 1
    assert suggestion == "wait ~15s (they edited it 5s ago)"
    [requested] = sockets["ana"].of_type("file_requested")
    assert requested["by"] == "ben"
    assert requested["agent"] == "codex"
    assert requested["file_path"] == FILE

    # A retry keeps ben's place and does not re-notify ana.
    outcome, _ = check(room, "ben", T0 + 6)
    assert outcome.queue_position == 1
    assert len(sockets["ana"].of_type("file_requested")) == 1

    # A third agent lines up behind ben, and ana hears about cy too.
    outcome, _ = check(room, "cy", T0 + 7)
    assert (outcome.decision, outcome.queue_position) == ("block", 2)
    assert [f["by"] for f in sockets["ana"].of_type("file_requested")] == ["ben", "cy"]


def test_the_head_is_released_when_the_holder_lapses_and_takes_the_file() -> None:
    room, sockets = make_room("ana", "ben", "cy")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)
    check(room, "cy", T0 + 2)

    tick(room, T0 + BLOCK + 0.5)
    [released] = sockets["ben"].of_type("file_released")
    assert released["file_path"] == FILE
    assert not sockets["cy"].of_type("file_released")

    # cy is not blocked by any touch now, but ben is next in line.
    outcome, suggestion = check(room, "cy", T0 + BLOCK + 2)
    assert (outcome.decision, outcome.scope, outcome.queue_position) == ("block", "file", 2)
    assert outcome.reason.startswith("ben is next in line for server/room.py")
    assert [peer.user_id for peer in outcome.peers] == ["ben"]
    assert suggestion == "wait ~9s (ben is next in line)"

    # ben takes it: allowed, out of line, and cy (still waiting) is held back
    # by ben's claim until ben's edit shows up.
    # (A warn, not an allow: ana's edit is still inside the warn window.)
    outcome, _ = check(room, "ben", T0 + BLOCK + 3)
    assert outcome.decision == "warn"
    assert room.file_queues.position(("", FILE), "ben") == 0
    outcome, _ = check(room, "cy", T0 + BLOCK + 4)
    assert outcome.decision == "block"
    assert outcome.reason.startswith("ben just took server/room.py from the wait queue")
    # ben's edit lands: from here its touch blocks cy the ordinary way.
    touch(room, "ben", T0 + BLOCK + 5)
    outcome, _ = check(room, "cy", T0 + BLOCK + 6)
    assert outcome.decision == "block" and outcome.reason == ""
    assert outcome.queue_position == 1
    # ... and lapses like any touch.
    tick(room, T0 + 2 * BLOCK + 6)
    assert sockets["cy"].of_type("file_released")


def test_an_unused_release_is_handed_to_the_next_in_line() -> None:
    room, sockets = make_room("ana", "ben", "cy")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)
    check(room, "cy", T0 + 2)
    tick(room, T0 + BLOCK + 1)
    assert sockets["ben"].of_type("file_released")
    tick(room, T0 + BLOCK + 1 + 10.5)  # ben's grace is over
    assert sockets["cy"].of_type("file_released")
    outcome, _ = check(room, "cy", T0 + BLOCK + 13)
    assert outcome.decision == "warn"  # passes (ana's old edit only warns)
    # ben lost its turn; cy took the file with nobody left waiting, so no claim
    # holds ben back either (until cy's edit arrives and blocks as usual).
    outcome, _ = check(room, "ben", T0 + BLOCK + 13.5)
    assert outcome.decision == "warn"
    assert not room.file_queues


def test_the_holder_leaving_releases_the_file_at_once() -> None:
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)
    room.remove_member("ana")
    tick(room, T0 + 2)
    assert sockets["ben"].of_type("file_released")


def test_a_requester_leaving_gives_up_its_place() -> None:
    room, sockets = make_room("ana", "ben", "cy")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)
    check(room, "cy", T0 + 2)
    room.remove_member("ben")
    assert room.file_queues.position(("", FILE), "cy") == 1
    tick(room, T0 + BLOCK + 3)
    assert sockets["cy"].of_type("file_released")


def test_a_returning_holder_blocks_the_released_head_again() -> None:
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0)
    check(room, "ben", T0 + 1)
    tick(room, T0 + BLOCK + 1)
    touch(room, "ana", T0 + BLOCK + 2)  # ana was not done after all
    outcome, _ = check(room, "ben", T0 + BLOCK + 3)
    assert (outcome.decision, outcome.queue_position) == ("block", 1)
    head = room.file_queues.head(("", FILE))
    assert head is not None and head.released_at is None


def test_symbol_level_checks_are_queued_and_released_at_their_own_granularity() -> None:
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0, symbols=["Room.send"])
    outcome, _ = check(room, "ben", T0 + 1, symbols=["Room.send"])
    assert outcome.decision == "block"
    # A different function in the same file is not held up by the line.
    outcome, _ = check(room, "ben", T0 + 2)  # file level: ana's touch blocks
    verdict = room.evaluate_edit("ben", FILE, T0 + 3, "", symbols=["Room.other"])
    other = room.queue_edit_check("ben", "codex", "", FILE, verdict, T0 + 3, symbols=["Room.other"])
    assert other.decision == "warn"


def test_file_request_on_a_free_file_is_released_at_once() -> None:
    room, sockets = make_room("ana", "ben")
    notices = room.queue_file_request("ben", "codex", "", FILE, T0)
    asyncio.run(room.deliver_queue_notices(notices))
    [released] = sockets["ben"].of_type("file_released")
    assert released["file_path"] == FILE
    # Asking again while released: told again (cheap, survives a lost frame).
    asyncio.run(room.deliver_queue_notices(room.queue_file_request("ben", "codex", "", FILE, T0 + 1)))
    assert len(sockets["ben"].of_type("file_released")) == 2
    # Meanwhile the file is promised to ben.
    outcome, _ = check(room, "ana", T0 + 2)
    assert outcome.decision == "block" and outcome.queue_position == 2


def test_file_request_on_a_held_file_queues_and_notifies() -> None:
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0)
    asyncio.run(room.deliver_queue_notices(room.queue_file_request("ben", "codex", "", FILE, T0 + 1)))
    assert room.file_queues.position(("", FILE), "ben") == 1
    assert [f["by"] for f in sockets["ana"].of_type("file_requested")] == ["ben"]
    assert not sockets["ben"].of_type("file_released")
    tick(room, T0 + BLOCK + 2)
    assert sockets["ben"].of_type("file_released")


def test_queues_are_per_repository() -> None:
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0, repo="one")
    outcome, _ = check(room, "ben", T0 + 1, repo="one")
    assert outcome.queue_position == 1
    outcome, _ = check(room, "ben", T0 + 1, repo="two")
    assert (outcome.decision, outcome.queue_position) == ("allow", 0)


def test_hashed_paths_and_blobs_flow_through_untouched() -> None:
    """e2e rooms: the relay queues on the hash, copies the requester's blob
    onto the holder's file_requested, and offers no path suggestions."""
    hashed = "h:" + "a" * 32
    room, sockets = make_room("ana", "ben")
    touch(room, "ana", T0, path=hashed)
    outcome, suggestion = check(room, "ben", T0 + 2, path=hashed, e2e="BLOB")
    assert outcome.queue_position == 1
    assert suggestion == "wait ~18s (they edited it 2s ago)"
    [requested] = sockets["ana"].of_type("file_requested")
    assert (requested["file_path"], requested["e2e"]) == (hashed, "BLOB")
    tick(room, T0 + BLOCK + 1)
    [released] = sockets["ben"].of_type("file_released")
    assert (released["file_path"], released["e2e"]) == (hashed, "BLOB")


# --- suggestions -----------------------------------------------------------------


def test_suggests_this_modules_tests_first() -> None:
    room, _ = make_room("ana", "ben")
    for path in ("server/room.py", "server/tests/test_room.py", "server/main.py"):
        room.remember_path("", path)
    touch(room, "ana", T0)
    _, suggestion = check(room, "ben", T0 + 8)
    assert suggestion == (
        "try server/tests/test_room.py (tests for this module) meanwhile, "
        "or wait ~12s (they edited it 8s ago)"
    )


def test_suggests_a_free_neighbour_but_never_a_busy_file() -> None:
    room, _ = make_room("ana", "ben", "cy")
    for path in ("server/a.py", "server/b.py", "server/package.json", "docs/x.md"):
        room.remember_path("", path)
    touch(room, "ana", T0)
    touch(room, "cy", T0, path="server/a.py")  # cy is in a.py
    _, suggestion = check(room, "ben", T0 + 1)
    assert suggestion.startswith("try server/b.py meanwhile, or wait ~")


def test_suggests_the_next_file_of_your_own_plan() -> None:
    room, _ = make_room("ana", "ben")
    touch(room, "ana", T0)
    room.record_plan(
        PlanMessage(user_id="ben", files=["server/room.py", "docs/queue.md"], summary="queues"),
        T0,
    )
    _, suggestion = check(room, "ben", T0 + 1)
    assert suggestion.startswith("try docs/queue.md (next in your plan) meanwhile")


def test_warn_suggestions_offer_an_alternative_only() -> None:
    room, _ = make_room("ana", "ben")
    room.remember_path("", "server/tests/test_room.py")
    touch(room, "ana", T0)
    outcome, suggestion = check(room, "ben", T0 + BLOCK + 5)  # past block: a warn
    assert outcome.decision == "warn"
    assert suggestion == (
        "server/tests/test_room.py (tests for this module) is free if you'd rather stay clear"
    )
    # Nothing to offer, nothing said.
    room2, _ = make_room("ana", "ben")
    touch(room2, "ana", T0)
    assert check(room2, "ben", T0 + BLOCK + 5)[1] == ""


@pytest.mark.parametrize(
    "path,test",
    [
        ("src/widget.ts", "src/widget.test.ts"),
        ("pkg/store.go", "pkg/store_test.go"),
        ("lib/parser.rb", "spec/parser_spec.rb"),
    ],
)
def test_test_files_across_ecosystems(path: str, test: str) -> None:
    try:
        from server import suggest
    except ImportError:  # Render rootDir=server
        import suggest  # type: ignore[no-redef]

    assert suggest.pick_alternative(path, [test, "unrelated.py"], set()) == (test, "tests")
    # A test file is not offered its own tests.
    assert suggest.pick_alternative(test, [path], set()) != (path, "tests")


def test_allow_has_no_suggestion() -> None:
    room, _ = make_room("ana", "ben")
    outcome, suggestion = check(room, "ben", T0)
    assert (outcome.decision, suggestion) == ("allow", "")


def test_the_verdict_type_is_unchanged_for_rooms_without_lines() -> None:
    room, _ = make_room("ana")
    verdict = EditVerdict("warn", [], "intent")
    outcome = room.queue_edit_check("ana", "codex", "", FILE, verdict, T0)
    assert (outcome.decision, outcome.scope, outcome.queue_position) == ("warn", "intent", 0)
    assert not room.file_queues


# --- over the wire ------------------------------------------------------------------


def _ws_join(socket: Any, name: str) -> None:
    try:
        from server.models import PROTOCOL_VERSION
    except ImportError:  # Render rootDir=server
        from models import PROTOCOL_VERSION  # type: ignore[no-redef]

    socket.send_json(
        {"type": "join", "name": name, "agent": "cursor", "protocol_version": PROTOCOL_VERSION}
    )


def _ws_next(socket: Any, kind: str, limit: int = 20) -> dict[str, Any]:
    """Next top-level frame of this kind (queue frames are never batched)."""
    for _ in range(limit):
        frame = socket.receive_json()
        if frame.get("type") == kind:
            return frame
    raise AssertionError(f"never received a top-level {kind}")


def _ws_feed_event(socket: Any, kind: str, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        frame = socket.receive_json()
        for event in frame.get("events", []) if frame.get("type") == "synergo_feed" else []:
            if event.get("type") == kind:
                return event
    raise AssertionError(f"never received a {kind} feed event")


def _ws_room(client: Any, block: float, warn: float) -> str:
    response = client.post(
        "/rooms", json={"settings": {"block_window_seconds": block, "warn_window_seconds": warn}}
    )
    return response.json()["room_id"]


def test_the_relay_works_the_line_on_its_own() -> None:
    """Real sockets: ben's blocked check is answered with its place in line
    and a suggestion, ana hears ben is waiting, and once ana's edit ages past
    the block window the room's own sweeper sends ben file_released without
    anyone sending another frame."""
    from fastapi.testclient import TestClient

    try:
        from server.main import app
    except ImportError:  # Render rootDir=server
        from main import app  # type: ignore[no-redef]

    client = TestClient(app)
    room_id = _ws_room(client, block=1.0, warn=5.0)
    with client.websocket_connect(f"/ws/{room_id}") as ana:
        _ws_join(ana, "ana")
        with client.websocket_connect(f"/ws/{room_id}") as ben:
            _ws_join(ben, "ben")
            ana.send_json(
                {"type": "activity", "user_id": "ana", "changes": [{"file_path": FILE, "diff": "@@"}]}
            )
            _ws_feed_event(ana, "activity_update")

            ben.send_json({"type": "edit_check", "request_id": "r1", "file_path": FILE})
            response = _ws_next(ben, "edit_check_response")
            requested = _ws_next(ana, "file_requested")
            decision = _ws_feed_event(ben, "edit_decision")
            released = _ws_next(ben, "file_released")

    assert response["decision"] == "block"
    assert response["queue_position"] == 1
    assert response["suggestion"].startswith("wait ~1s (they edited it")
    assert (requested["by"], requested["file_path"]) == ("ben", FILE)
    assert (decision["queue_position"], decision["suggestion"]) == (1, response["suggestion"])
    assert released["file_path"] == FILE
    assert released["timestamp"] >= requested["timestamp"]


def test_file_request_over_the_wire_and_release_when_the_holder_leaves() -> None:
    from fastapi.testclient import TestClient

    try:
        from server.main import app
    except ImportError:  # Render rootDir=server
        from main import app  # type: ignore[no-redef]

    client = TestClient(app)
    room_id = _ws_room(client, block=60.0, warn=120.0)
    with client.websocket_connect(f"/ws/{room_id}") as ben:
        _ws_join(ben, "ben")
        with client.websocket_connect(f"/ws/{room_id}") as ana:
            _ws_join(ana, "ana")
            ana.send_json(
                {"type": "activity", "user_id": "ana", "changes": [{"file_path": FILE, "diff": "@@"}]}
            )
            _ws_feed_event(ana, "activity_update")
            ben.send_json({"type": "file_request", "file_path": FILE})
            requested = _ws_next(ana, "file_requested")
        # ana disconnected: the file is ben's at once, long before 60 s.
        released = _ws_next(ben, "file_released")

    assert requested["by"] == "ben"
    assert released["file_path"] == FILE
