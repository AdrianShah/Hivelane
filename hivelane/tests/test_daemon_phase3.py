"""Daemon side of Phase 3 against a stub relay: symbols / line ranges / branch
on edit_check and activity, the plan round trip (on-time and late
plan_conflicts), and depgraph caller notes from a peer's activity."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

import pytest
from websockets.asyncio.server import ServerConnection

from hivelane import daemon as daemon_module
from hivelane import gitinfo
from hivelane.daemon import Daemon, unified_diff
from hivelane.tests.test_daemon_multi_repo import StubRelay, event, isolated, wait_for  # noqa: F401

SHA = "0123456789abcdef0123456789abcdef01234567"


class PlanRelay(StubRelay):
    """StubRelay that also answers plan frames with `plan_reply` (held until
    `plan_gate` is set, when there is one), and can push frames to connected
    clients."""

    def __init__(self) -> None:
        super().__init__()
        self.plan_reply: dict[str, Any] = {"type": "plan_conflicts", "conflicts": []}
        self.plan_gate: Optional[asyncio.Event] = None
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
                    "type": "edit_check_response", "request_id": data["request_id"],
                    "decision": "allow", "file_path": data["file_path"],
                }))
            elif data.get("type") == "plan":
                if self.plan_gate is not None:
                    await self.plan_gate.wait()
                await ws.send(json.dumps(self.plan_reply))

    async def push(self, frame: dict[str, Any]) -> None:
        for ws in self.sockets:
            await ws.send(json.dumps(frame))


def make_git_repo(path: Path, branch: str = "feature/x") -> Path:
    """A repo whose .git is hand-written: no git subprocess needed."""
    heads = path / ".git" / "refs" / "heads" / "feature"
    heads.mkdir(parents=True)
    (path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    (heads / "x").write_text(SHA + "\n")
    (path / "src").mkdir()
    gitinfo.clear_cache()
    return path.resolve()


async def started(repo: Path, relay: StubRelay, **kwargs: Any) -> Daemon:
    daemon = Daemon(batch_seconds=0.05, **kwargs)
    await daemon.start()
    await daemon.register(repo, "ROOM", relay=relay.url, name="ada")
    await wait_for(lambda: all(r["connected"] for r in daemon.status()["repos"]), "connected")
    return daemon


SOURCE = "import os\n\n\nclass Room:\n    def check_edit(self, path):\n        return path\n\n\ndef other():\n    return 1\n"


def test_edit_check_and_activity_carry_symbols_ranges_and_branch(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    target = repo / "src" / "room.py"

    async def scenario() -> None:
        async with PlanRelay() as relay:
            daemon = await started(repo, relay)
            await daemon.handle_event(event(
                "pre_edit", repo, target, old_content=SOURCE,
                edit_hints=[{"old": "        return path", "new": "        return path.strip()"}],
            ))
            (check,) = relay.of_type("ROOM", "edit_check")
            assert check["symbols"] == ["Room.check_edit"]
            assert check["line_ranges"] == [[6, 6]]
            assert check["branch"] == "feature/x"

            new = SOURCE.replace("return path", "return path.strip()").replace("import os", "import os, sys")
            await daemon.handle_event(event("post_edit", repo, target, new_content=new))
            await wait_for(lambda: relay.of_type("ROOM", "activity"), "activity")
            (activity,) = relay.of_type("ROOM", "activity")
            assert activity["branch"] == "feature/x" and activity["base_commit"] == SHA
            (change,) = activity["changes"]
            assert change["symbols"] == ["<imports>", "Room.check_edit"]
            assert change["line_ranges"] == [[1, 1], [6, 6]]

            # A post_edit whose old content was never seen sends no symbols.
            await daemon.handle_event(event("post_edit", repo, repo / "src" / "b.py", new_content="x = 1\n"))
            await wait_for(lambda: len(relay.of_type("ROOM", "activity")) == 2, "second activity")
            assert relay.of_type("ROOM", "activity")[1]["changes"][0]["symbols"] == []
            await daemon.aclose()

    asyncio.run(scenario())


def test_plan_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        daemon_module, "tracked_files", lambda root, now=None: ["server/room.py", "hivelane/daemon.py", "README.md"]
    )

    async def scenario() -> None:
        async with PlanRelay() as relay:
            relay.plan_reply = {"type": "plan_conflicts", "conflicts": [
                {"file_path": "server/room.py", "user_id": "sam", "agent": "cursor", "kind": "edited",
                 "summary": "split rooms", "seconds_ago": 90},
            ]}
            daemon = await started(repo, relay)
            ack = await daemon.handle_event(event(
                "plan", repo, summary="refactor room.py and hivelane/daemon.py",
                files=["room.py", "./hivelane/daemon.py", "missing.py"], symbols=["Room.check_edit"],
            ))
            (plan,) = relay.of_type("ROOM", "plan")
            assert plan["user_id"] == "ada"
            assert plan["files"] == ["server/room.py", "hivelane/daemon.py"]
            assert plan["symbols"] == ["Room.check_edit"]
            assert plan["branch"] == "feature/x" and plan["repo_id"] == "id-repo"
            assert ack.decision == "warn"
            assert "sam is editing server/room.py" in ack.reason and "1 min ago" in ack.reason

            # No overlaps: a plain allow.
            relay.plan_reply = {"type": "plan_conflicts", "conflicts": []}
            ack = await daemon.handle_event(event("plan", repo, summary="x", files=["README.md"]))
            assert ack.decision == "allow"

            # Nothing resolvable: nothing sent.
            ack = await daemon.handle_event(event("plan", repo, summary="x", files=["nope.py"]))
            assert ack.decision == "allow" and len(relay.of_type("ROOM", "plan")) == 2
            await daemon.aclose()

    asyncio.run(scenario())


def test_late_plan_conflicts_ride_on_the_next_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(daemon_module, "tracked_files", lambda root, now=None: ["server/room.py"])

    async def scenario() -> None:
        async with PlanRelay() as relay:
            relay.plan_gate = asyncio.Event()
            relay.plan_reply = {"type": "plan_conflicts", "conflicts": [
                {"file_path": "server/room.py", "user_id": "kai", "kind": "planned", "seconds_ago": 5},
            ]}
            daemon = await started(repo, relay, edit_check_timeout=0.1)
            ack = await daemon.handle_event(event("plan", repo, summary="x", files=["server/room.py"]))
            assert ack.decision == "allow"  # the reply missed it
            relay.plan_gate.set()  # ...and only now arrives, late
            conn = daemon._connections()[0]
            await wait_for(lambda: conn._session_notes, "late conflicts")
            conn.edit_check_timeout = 1.5
            ack = await daemon.handle_event(event("pre_edit", repo, repo / "server" / "room.py"))
            assert ack.decision == "warn" and "kai plans to edit server/room.py" in ack.reason
            # Surfaced once.
            ack = await daemon.handle_event(event("pre_edit", repo, repo / "server" / "room.py"))
            assert ack.decision == "allow"
            await daemon.aclose()

    asyncio.run(scenario())


def test_peer_signature_change_warns_callers(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    old = "def foo(a):\n    return a\n"
    new = "def foo(a, b):\n    return a + b\n"
    caller = "from pkg.util import foo\n\nfoo(1)\n"

    async def scenario() -> None:
        async with PlanRelay() as relay:
            daemon = await started(repo, relay)
            await relay.push({"type": "hivelane_feed", "events": [{
                "type": "activity_update", "user_id": "sam", "timestamp": time.time() - 180,
                "changes": [{"file_path": "pkg/util.py", "diff": unified_diff("pkg/util.py", old, new)}],
            }]})
            conn = daemon._connections()[0]
            await wait_for(lambda: conn.deps._changes, "peer change recorded")
            ack = await daemon.handle_event(event("pre_edit", repo, repo / "src" / "main.py", old_content=caller))
            assert ack.decision == "warn"
            assert "caller of `foo`" in ack.reason and "sam changed 3 min ago" in ack.reason

            # Our own edit is remembered too.
            await daemon.handle_event(event("pre_edit", repo, repo / "pkg" / "b.py", old_content="def bar(x):\n    pass\n"))
            await daemon.handle_event(event("post_edit", repo, repo / "pkg" / "b.py", new_content="def bar(x, y):\n    pass\n"))
            ack = await daemon.handle_event(
                event("pre_edit", repo, repo / "src" / "c.py", old_content="from pkg import b\n\nb.bar(1)\n")
            )
            assert "caller of `bar`" in ack.reason and "you changed just now" in ack.reason
            await daemon.aclose()

    asyncio.run(scenario())
