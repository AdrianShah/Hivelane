"""Daemon side of the relay's wait queues against a stub relay: the queue
position and suggestion in the agent's block message, file_released turning
into "your turn" (and a log line), file_requested turning into a heads-up
for the agent holding the file, `synergo wait`'s POST /wait, and e2e rooms
where every path on the wire is a hash."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection

from synergo import crypto
from synergo.adapters import claude_code
from synergo.daemon import Daemon, RoomConnection
from synergo.protocol import FileRequest, RoomSettings
from synergo.tests.test_daemon_multi_repo import StubRelay, event, isolated, make_repo, wait_for  # noqa: F401

REL = "src/room.py"
SUGGESTION = "try tests/test_room.py (tests for this module) meanwhile, or wait ~12s (they edited it 8s ago)"


class QueueRelay(StubRelay):
    """Answers edit_check with `reply` (block by default, #1 in line) and
    file_request with `on_file_request` frames; push() sends any frame."""

    def __init__(self) -> None:
        super().__init__()
        self.reply: dict[str, Any] = {
            "decision": "block",
            "reason": "sam's cursor edited src/room.py 8s ago",
            "queue_position": 1,
            "suggestion": SUGGESTION,
            "others": [{"user_id": "sam", "agent": "cursor", "seconds_ago": 8.0}],
        }
        self.on_file_request: list[dict[str, Any]] = []
        self.sockets: list[ServerConnection] = []

    async def _handler(self, ws: ServerConnection) -> None:
        self.sockets.append(ws)
        room = (ws.request.path if ws.request else "").rsplit("/", 1)[-1]
        async for raw in ws:
            data = json.loads(raw)
            if data.get("type") == "join":
                self.joins[room].append(data)
                await ws.send(json.dumps({"type": "user_joined", "user_id": str(data.get("name") or "").lower(), "agent": data.get("agent") or "unknown"}))
                continue
            self.frames[room].append(data)
            if data.get("type") == "edit_check":
                await ws.send(json.dumps({
                    "type": "edit_check_response",
                    "request_id": data["request_id"],
                    "file_path": data["file_path"],
                    **self.reply,
                }))
            elif data.get("type") == "file_request":
                for frame in self.on_file_request:
                    await ws.send(json.dumps(frame))

    async def push(self, frame: dict[str, Any]) -> None:
        for ws in self.sockets:
            await ws.send(json.dumps(frame))


async def started(repo: Path, relay: StubRelay) -> Daemon:
    daemon = Daemon(batch_seconds=0.05)
    await daemon.start()
    await daemon.register(repo, "ROOM", relay=relay.url, name="ada")
    await wait_for(lambda: all(r["connected"] for r in daemon.status()["repos"]), "connected")
    return daemon


def session_event(kind: str, repo: Path, session: str, **fields: Any):
    return event(kind, repo, repo / REL, **fields).model_copy(update={"session_id": session})


def test_a_block_names_the_place_in_line_the_suggestion_and_synergo_wait(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> Any:
        async with QueueRelay() as relay:
            daemon = await started(repo, relay)
            ack = await daemon.handle_event(event("pre_edit", repo, repo / REL))
            status = daemon.status()["repos"][0]
            await daemon.aclose()
            return ack, status

    ack, status = asyncio.run(scenario())
    assert (ack.decision, ack.queue_position, ack.suggestion) == ("block", 1, SUGGESTION)
    assert status["queued"] == [{"file_path": REL, "position": 1}]

    rendered = json.loads(claude_code.render_ack(event("pre_edit", repo, repo / REL).model_dump(), ack.model_dump()))
    assert rendered["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Synergo: sam's cursor edited src/room.py 8s ago — you're #1 in line; "
        f"{SUGGESTION}; run `synergo wait src/room.py` to wait your turn"
    )


def test_file_released_is_logged_and_becomes_your_turn_for_the_waiting_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> tuple[Any, Any, Any]:
        async with QueueRelay() as relay:
            daemon = await started(repo, relay)
            blocked = await daemon.handle_event(session_event("pre_edit", repo, "waiting"))
            await relay.push({"type": "file_released", "file_path": REL, "repo_id": "id-repo", "timestamp": 1.0})
            conn = daemon._all_bindings()[0].connection
            await wait_for(lambda: conn._released, "the release")
            relay.reply = {"decision": "allow"}
            other = await daemon.handle_event(session_event("pre_edit", repo, "someone-else"))
            mine = await daemon.handle_event(session_event("pre_edit", repo, "waiting"))
            again = await daemon.handle_event(session_event("pre_edit", repo, "waiting"))
            await daemon.aclose()
            return blocked, other, (mine, again)

    blocked, other, (mine, again) = asyncio.run(scenario())
    assert blocked.decision == "block"
    assert other.decision == "allow"  # not the session that waited
    assert mine.decision == "warn"  # allowed, with a note
    assert mine.reason == f"{REL} was released to you — it's your turn"
    assert again.decision == "allow"  # used up
    assert f"{REL} is free — your turn (you were #1 in line)" in capsys.readouterr().out


def test_file_requested_reaches_the_agent_holding_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / REL).write_text("x = 1\n", encoding="utf-8")

    async def scenario() -> tuple[Any, Any]:
        async with QueueRelay() as relay:
            relay.reply = {"decision": "allow"}
            daemon = await started(repo, relay)
            await daemon.handle_event(session_event("pre_edit", repo, "holder", old_content="x = 1\n"))
            await daemon.handle_event(session_event("post_edit", repo, "holder", new_content="x = 2\n"))
            await relay.push({
                "type": "file_requested", "file_path": REL, "repo_id": "id-repo",
                "by": "priya", "agent": "codex", "timestamp": 1.0,
            })
            conn = daemon._all_bindings()[0].connection
            await wait_for(lambda: conn._session_notes, "the note")
            bystander = await daemon.handle_event(session_event("pre_edit", repo, "bystander"))
            holder = await daemon.handle_event(session_event("pre_edit", repo, "holder"))
            await daemon.aclose()
            return bystander, holder

    bystander, holder = asyncio.run(scenario())
    assert bystander.decision == "allow"
    assert holder.decision == "warn"
    assert holder.reason == f"priya's agent is waiting for {REL} — finish up or hand it off"
    assert f"priya (codex) is waiting for {REL}" in capsys.readouterr().out


def test_wait_returns_on_file_released_or_times_out_with_the_place_in_line(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "src" / "room.py").write_text("x\n", encoding="utf-8")

    async def scenario() -> tuple[Any, Any, Any, list[dict[str, Any]]]:
        async with QueueRelay() as relay:
            daemon = await started(repo, relay)
            await daemon.handle_event(event("pre_edit", repo, repo / REL))  # #1 in line
            # From a subdirectory, with a repo-root-relative path: the form a
            # block message prints.
            waiting = await daemon.wait_for_file(str(repo / "src"), REL, 0.2)
            relay.on_file_request = [
                {"type": "file_released", "file_path": REL, "repo_id": "id-repo", "timestamp": 1.0}
            ]
            free = await daemon.wait_for_file(str(repo / "src"), "room.py", 2.0)
            # Within the grace, asking again answers at once.
            relay.on_file_request = []
            still = await daemon.wait_for_file(str(repo), REL, 0.0)
            frames = relay.of_type("ROOM", "file_request")
            await daemon.aclose()
            return waiting, free, still, frames

    waiting, free, still, frames = asyncio.run(scenario())
    assert waiting == {"file_path": REL, "room": "ROOM", "free": False, "state": "waiting", "queue_position": 1}
    assert free == {"file_path": REL, "room": "ROOM", "free": True, "state": "released"}
    assert still["free"] is True
    assert [(f["file_path"], f["repo_id"]) for f in frames] == [(REL, "id-repo")] * 2


def test_wait_ends_when_one_of_our_checks_passes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> Any:
        async with QueueRelay() as relay:
            daemon = await started(repo, relay)
            waiter = asyncio.create_task(daemon.wait_for_file(str(repo), REL, 5.0))
            # The waiter must be registered before the check that should end it.
            conn = daemon._all_bindings()[0].connection
            await wait_for(lambda: conn._file_waiters, "the waiter to register")
            relay.reply = {"decision": "allow"}
            await daemon.handle_event(event("pre_edit", repo, repo / REL))
            result = await asyncio.wait_for(waiter, 5.0)
            await daemon.aclose()
            return result

    assert asyncio.run(scenario())["state"] == "allowed"


def test_wait_on_an_uncoordinated_or_offline_file_does_not_hold_anyone_up(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def scenario() -> tuple[Any, Any]:
        daemon = Daemon()
        uncoordinated = await daemon.wait_for_file(str(elsewhere), "a.py", 1.0)
        conn = RoomConnection("ROOM", "ada")
        offline = await conn.wait_for_file(REL, "id", 1.0)
        return uncoordinated, offline

    uncoordinated, offline = asyncio.run(scenario())
    assert (uncoordinated["free"], uncoordinated["state"]) == (True, "uncoordinated")
    assert (offline["free"], offline["state"]) == (True, "offline")


# --- e2e rooms ----------------------------------------------------------------------


def e2e_connection() -> tuple[RoomConnection, crypto.RoomKey]:
    key = crypto.derive("correct horse", "ROOM")
    conn = RoomConnection("ROOM", "ada", settings=RoomSettings(e2e=True), e2e_key=key)
    return conn, key


def test_file_request_is_sealed_in_an_e2e_room() -> None:
    conn, key = e2e_connection()
    sealed = conn._seal(FileRequest(file_path=REL, repo_id="id"))
    assert sealed.file_path == key.hash(REL)
    # Keyed in an e2e room (the relay cannot confirm a guessed origin).
    assert sealed.repo_id == key.repo_token("id") != "id"
    kind, envelope = key.open(sealed.e2e)
    assert (kind, envelope["file_path"]) == ("edit_check", REL)


def test_hashed_queue_frames_are_read_back_as_real_paths(capsys: pytest.CaptureFixture[str]) -> None:
    conn, key = e2e_connection()
    hashed = key.hash(REL)
    # A teammate's file_requested: the hash is explained by its blob.
    blob = key.seal("edit_check", {"v": 1, "file_path": REL, "symbols": [], "branch": ""})
    conn._dispatch_frame({
        "type": "file_requested", "file_path": hashed, "repo_id": "id",
        "by": "priya", "agent": "codex", "timestamp": 1.0, "e2e": blob,
    })
    assert conn._take_session_notes("anyone", "id") == (
        f"priya's agent is waiting for {REL} — finish up or hand it off"
    )
    # Our own release: the hash is one we sent ourselves.
    conn._dispatch_frame({"type": "file_released", "file_path": hashed, "repo_id": "id", "timestamp": 1.0})
    assert ("id", REL) in conn._released
    out = capsys.readouterr().out
    assert f"{REL} is free" in out and hashed not in out


def test_the_wait_hint_is_left_out_without_a_path() -> None:
    from synergo.adapters import base

    message = base.ack_message({"decision": "block", "others": []}, "block", None)
    assert message == (
        "a teammate's agent is already working in this file — wait, or pick another file"
    )
    # A malformed queue position is ignored rather than trusted.
    message = base.ack_message(
        {"queue_position": "1", "file_path": "a.py", "suggestion": 3}, "block", None
    )
    assert "in line" not in message and "synergo wait a.py" in message
