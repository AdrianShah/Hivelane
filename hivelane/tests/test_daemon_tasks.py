"""Room tasks on the daemon side: task requests are sealed in e2e rooms and
replies are opened for display, task_reply frames answer their waiters, and a
block on a task-reserved file never suggests waiting in line."""

from __future__ import annotations

import asyncio

import pytest

from hivelane import crypto
from hivelane.adapters import base
from hivelane.daemon import RoomConnection
from hivelane.protocol import RoomSettings, Task, TaskNote, TaskRequest

REL = "server/api.py"

needs_crypto = pytest.mark.skipif(not crypto.available(), reason="needs the e2e extra (cryptography)")


def _e2e() -> tuple[RoomConnection, crypto.RoomKey]:
    key = crypto.derive("correct horse battery", "ROOM")
    return RoomConnection("ROOM", "ada", settings=RoomSettings(e2e=True), e2e_key=key), key


@needs_crypto
def test_task_requests_are_sealed_and_replies_opened_in_an_e2e_room() -> None:
    conn, key = _e2e()
    request = TaskRequest(request_id="r", op="create", title="Add rate limiting", body="per user", files=[REL], repo_id="id")
    sealed = conn._seal(request)
    assert (sealed.title, sealed.body, sealed.text) == ("", "", "")
    assert sealed.files == [key.hash(REL)]
    assert sealed.repo_id == key.repo_token("id") != "id"
    kind, envelope = key.open(sealed.e2e)
    assert (kind, envelope["title"], envelope["files"]) == ("task", "Add rate limiting", [REL])

    # The relay's copy, as a teammate's daemon gets it back in a task_reply.
    note_blob = key.seal("task", {"v": 1, "title": "", "body": "", "text": "PR #12", "files": []})
    on_relay = Task(
        task_id="T1", repo_id=sealed.repo_id, files=sealed.files, created_by="sam",
        created_at=1.0, updated_at=2.0, e2e=sealed.e2e, status="done",
        notes=[TaskNote(user_id="ada", timestamp=2.0, kind="done", e2e=note_blob)],
    )
    teammate, _ = _e2e()
    shown = teammate._open_task(on_relay)
    assert (shown.title, shown.body, shown.files, shown.e2e) == ("Add rate limiting", "per user", [REL], "")
    assert shown.notes[0].text == "PR #12"


def test_a_list_request_with_nothing_to_hide_carries_no_blob() -> None:
    if not crypto.available():
        pytest.skip("needs the e2e extra (cryptography)")
    conn, _ = _e2e()
    assert conn._seal(TaskRequest(request_id="r", op="list")).e2e == ""


def test_task_reply_answers_its_waiter() -> None:
    async def scenario() -> None:
        conn = RoomConnection("ROOM", "ada")
        future = asyncio.get_running_loop().create_future()
        conn._task_waiters["r1"] = future
        conn._dispatch_frame({"type": "task_reply", "request_id": "r1", "ok": False, "reason": "none_ready"})
        reply = await asyncio.wait_for(future, 1)
        assert (reply.ok, reply.reason) == (False, "none_ready")
        assert "r1" not in conn._task_waiters

    asyncio.run(scenario())


def test_an_offline_connection_answers_task_requests_at_once() -> None:
    conn = RoomConnection("ROOM", "ada")
    assert asyncio.run(conn.task_request("list", "id")) == {"ok": False, "reason": "offline"}


def test_a_task_block_points_at_the_task_list_not_the_wait_queue() -> None:
    ack = {
        "decision": "block",
        "file_path": REL,
        "reason": f"{REL} is reserved by sam's task T3 (12 min left on its lease)",
        "suggestion": "pick other work: `hivelane task list` shows open tasks",
        "scope": "task",
        "others": [],
    }
    message = base.ack_message(ack, "block", REL)
    assert "reserved by sam's task T3" in message
    assert "hivelane task list" in message
    assert "hivelane wait" not in message
    # Any other block still offers the wait queue.
    assert "hivelane wait" in base.ack_message({**ack, "scope": "symbol"}, "block", REL)
