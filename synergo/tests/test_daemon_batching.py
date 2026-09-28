"""Daemon translation: post_edit debouncing, diffing and the pre_edit cache."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from synergo.daemon import Daemon, relative_path
from synergo.events import NormalizedEvent


def make_daemon() -> Daemon:
    return Daemon("TEAM42", "Ada", agent="claude-code", port=4699, batch_seconds=0.05)


def drain(daemon: Daemon) -> list[dict]:
    """Priority first, matching the order the real drain sends in."""
    messages: list[dict] = []
    for queue in (daemon._priority_outbox, daemon._outbox):
        while not queue.empty():
            messages.append(json.loads(queue.get_nowait()))
    return messages


def test_post_edits_batch_into_one_activity(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    target = tmp_path / "server" / "room.py"

    async def scenario() -> list[dict]:
        daemon = make_daemon()
        for path, old, new in (
            (target, "a = 1\n", "a = 2\n"),
            (tmp_path / "server" / "relay.py", "", "print('hi')\n"),
        ):
            await daemon.handle_event(
                NormalizedEvent(
                    kind="post_edit",
                    agent="claude-code",
                    session_id="s1",
                    cwd=cwd,
                    file_path=str(path),
                    old_content=old,
                    new_content=new,
                )
            )
        await asyncio.sleep(0.2)
        return drain(daemon)

    messages = asyncio.run(scenario())

    assert len(messages) == 1
    activity = messages[0]
    assert activity["type"] == "activity"
    assert activity["user_id"] == "Ada"
    assert activity["agent"] == "claude-code"

    paths = [change["file_path"] for change in activity["changes"]]
    assert paths == ["server/room.py", "server/relay.py"]

    diff = next(c["diff"] for c in activity["changes"] if c["file_path"] == "server/room.py")
    assert diff.splitlines() == [
        "--- a/server/room.py",
        "+++ b/server/room.py",
        "@@ -1 +1 @@",
        "-a = 1",
        "+a = 2",
    ]


def test_pre_edit_cache_feeds_the_later_diff(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    target = tmp_path / "pkg" / "mod.py"

    async def scenario() -> list[dict]:
        daemon = make_daemon()
        await daemon.handle_event(
            NormalizedEvent(
                kind="pre_edit",
                agent="claude-code",
                session_id="s1",
                cwd=cwd,
                file_path=str(target),
                old_content="value = 1\n",
            )
        )
        await daemon.handle_event(
            NormalizedEvent(
                kind="post_edit",
                agent="claude-code",
                session_id="s1",
                cwd=cwd,
                file_path=str(target),
                new_content="value = 2\n",
            )
        )
        await asyncio.sleep(0.2)
        return drain(daemon)

    messages = asyncio.run(scenario())

    assert len(messages) == 1
    change = messages[0]["changes"][0]
    assert change["file_path"] == "pkg/mod.py"
    assert "-value = 1" in change["diff"]
    assert "+value = 2" in change["diff"]


def test_post_edit_without_cache_is_a_whole_file_diff(tmp_path: Path) -> None:
    async def scenario() -> list[dict]:
        daemon = make_daemon()
        await daemon.handle_event(
            NormalizedEvent(
                kind="post_edit",
                agent="claude-code",
                session_id="fresh",
                cwd=str(tmp_path),
                file_path=str(tmp_path / "new.py"),
                new_content="one\ntwo\n",
            )
        )
        await asyncio.sleep(0.2)
        return drain(daemon)

    change = asyncio.run(scenario())[0]["changes"][0]
    assert change["file_path"] == "new.py"
    assert "+one" in change["diff"]
    assert "+two" in change["diff"]


def test_pre_edit_fails_open_while_disconnected(tmp_path: Path) -> None:
    async def scenario() -> str:
        daemon = make_daemon()
        ack = await daemon.handle_event(
            NormalizedEvent(
                kind="pre_edit",
                agent="claude-code",
                session_id="s1",
                cwd=str(tmp_path),
                file_path=str(tmp_path / "x.py"),
            )
        )
        return ack.decision

    assert asyncio.run(scenario()) == "allow"


def test_pre_edit_times_out_to_allow(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, list[dict]]:
        daemon = make_daemon()
        daemon.connected = True
        daemon.edit_check_timeout = 0.05
        ack = await daemon.handle_event(
            NormalizedEvent(
                kind="pre_edit",
                agent="claude-code",
                session_id="s1",
                cwd=str(tmp_path),
                file_path=str(tmp_path / "server" / "x.py"),
            )
        )
        return ack.decision, drain(daemon)

    decision, messages = asyncio.run(scenario())
    assert decision == "allow"
    assert messages[0]["type"] == "edit_check"
    assert messages[0]["file_path"] == "server/x.py"


def test_intent_becomes_an_intent_message(tmp_path: Path) -> None:
    async def scenario() -> list[dict]:
        daemon = make_daemon()
        await daemon.handle_event(
            NormalizedEvent(
                kind="intent",
                agent="claude-code",
                session_id="s1",
                cwd=str(tmp_path),
                prompt="split the relay out of app.py",
            )
        )
        return drain(daemon)

    messages = asyncio.run(scenario())
    assert messages[0]["type"] == "intent"
    assert messages[0]["user_id"] == "Ada"
    assert messages[0]["prompt"] == "split the relay out of app.py"


def test_relative_path_falls_back_to_absolute_outside_cwd() -> None:
    assert relative_path("/etc/hosts", "/home/ada/proj") == "/etc/hosts"


def test_old_content_cache_is_bounded(tmp_path: Path) -> None:
    from synergo.daemon import OLD_CONTENT_CACHE_SIZE

    async def scenario() -> int:
        daemon = make_daemon()
        for index in range(OLD_CONTENT_CACHE_SIZE + 50):
            daemon._remember_old("s1", f"f{index}.py", "x")
        return len(daemon._old_content)

    assert asyncio.run(scenario()) == OLD_CONTENT_CACHE_SIZE
