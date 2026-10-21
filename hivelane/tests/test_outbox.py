"""Outbox ordering and retention: edit_check is on a blocking hook, and a
payload must survive a connection drop mid-send."""

from __future__ import annotations

import asyncio
import json


from hivelane.daemon import Daemon
from hivelane.protocol import ActivityMessage, EditCheckRequest, FileChange


def _daemon() -> Daemon:
    return Daemon("TEAM42", "Ada", port=4698)


def _activity(n: int) -> ActivityMessage:
    return ActivityMessage(
        user_id="Ada", changes=[FileChange(file_path=f"f{n}.py", diff="d")]
    )


def test_edit_check_overtakes_a_backlog_of_activity() -> None:
    async def scenario() -> dict:
        daemon = _daemon()
        for n in range(50):
            daemon._send(_activity(n))
        daemon._send(EditCheckRequest(request_id="r1", file_path="server/room.py"))
        return json.loads(await daemon._next_payload())

    assert asyncio.run(scenario())["type"] == "edit_check"


def test_inflight_payload_is_resent_after_a_drop() -> None:
    async def scenario() -> tuple[str, str, bool]:
        daemon = _daemon()
        daemon._send(_activity(1))
        payload = await daemon._next_payload()
        daemon._inflight = payload  # send began, connection died mid-flight
        again = await daemon._next_payload()
        daemon._inflight = None
        drained = False
        try:
            await asyncio.wait_for(daemon._next_payload(), timeout=0.05)
        except asyncio.TimeoutError:
            drained = True
        return payload, again, drained

    payload, again, drained = asyncio.run(scenario())
    assert again == payload
    assert drained, "the retained payload must not also remain queued"


def test_next_payload_wakes_on_a_later_send() -> None:
    async def scenario() -> dict:
        daemon = _daemon()

        async def send_soon() -> None:
            await asyncio.sleep(0.01)
            daemon._send(_activity(7))

        asyncio.create_task(send_soon())
        return json.loads(await asyncio.wait_for(daemon._next_payload(), timeout=1.0))

    assert asyncio.run(scenario())["type"] == "activity"
