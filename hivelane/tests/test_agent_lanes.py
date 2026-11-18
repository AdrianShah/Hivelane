"""Several agents per person: subagents, parallel chats and different tools
each get a lane (their own relay member), and repos are found from the edited
file even when the agent was started in a folder above the repo.

The stub relay here keeps frames per member and blocks an edit_check on a
file another member already checked, which is enough to see who the relay
would think is colliding with whom.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from hivelane import daemon as daemon_module
from hivelane import hook_fast
from hivelane.adapters import claude_code
from hivelane.daemon import Daemon, find_nested_project, lane_name, lane_suffix
from hivelane.events import NormalizedEvent

TIMEOUT = 5.0


class MemberRelay:
    """Records each member's join and frames by name. An edit_check on a
    file some other member checked first is blocked, naming that member."""

    def __init__(self) -> None:
        self.joins: list[dict[str, Any]] = []
        self.frames: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.holders: dict[str, str] = {}
        self.port = 0
        self._server: Any = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def members(self) -> list[str]:
        return [str(join.get("name")) for join in self.joins]

    def of_type(self, member: str, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames[member] if frame.get("type") == kind]

    async def _handler(self, ws: ServerConnection) -> None:
        name = ""
        async for raw in ws:
            data = json.loads(raw)
            if data.get("type") == "join":
                name = str(data.get("name") or "").lower()
                self.joins.append(data)
                await ws.send(json.dumps({"type": "user_joined", "user_id": name, "agent": data.get("agent")}))
                continue
            self.frames[name].append(data)
            if data.get("type") == "edit_check":
                path = data["file_path"]
                holder = self.holders.setdefault(path, name)
                blocked = holder != name
                await ws.send(
                    json.dumps(
                        {
                            "type": "edit_check_response",
                            "request_id": data["request_id"],
                            "decision": "block" if blocked else "allow",
                            "file_path": path,
                            "others": [{"user_id": holder, "agent": "claude-code", "seconds_ago": 1.0}] if blocked else [],
                            "reason": f"{holder} is in {path}" if blocked else "",
                        }
                    )
                )

    async def __aenter__(self) -> MemberRelay:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()


async def wait_for(predicate: Callable[[], Any], what: str, timeout: float = TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def event(kind: str, cwd: Path, file_path: Path | None = None, **fields: Any) -> NormalizedEvent:
    fields.setdefault("agent", "claude-code")
    fields.setdefault("session_id", "chat1")
    return NormalizedEvent(
        kind=kind,  # type: ignore[arg-type]
        cwd=str(cwd),
        file_path=str(file_path) if file_path else None,
        **fields,
    )


def make_repo(path: Path, project: str | None = None) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / "src").mkdir()
    if project is not None:
        (path / ".hivelane.toml").write_text(project, encoding="utf-8")
    return path.resolve()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HIVELANE_NAME", raising=False)
    monkeypatch.delenv("HIVELANE_RELAY", raising=False)
    monkeypatch.setattr(daemon_module, "_repo_ids", {})
    monkeypatch.setattr(daemon_module, "_repo_roots", {})
    monkeypatch.setattr(daemon_module, "compute_repo_id", lambda root: f"id-{root.name}")
    monkeypatch.setattr(daemon_module, "default_name", lambda root=None: "ada")
    yield


# --- the Claude Code adapter -------------------------------------------------------


def test_subagent_tool_events_carry_the_subagent(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    payload = {
        "hook_event_name": "PreToolUse", "session_id": "s", "cwd": str(tmp_path),
        "agent_id": "agent-abc123", "agent_type": "Explore",
        "tool_name": "Edit", "tool_input": {"file_path": str(target), "old_string": "a", "new_string": "b"},
    }
    (event_,) = claude_code.ADAPTER.normalize(payload)
    assert event_["kind"] == "pre_edit"
    assert event_["subagent_id"] == "agent-abc123"
    assert event_["subagent_type"] == "Explore"
    NormalizedEvent(**event_)

    main = dict(payload)
    del main["agent_id"], main["agent_type"]
    (plain,) = claude_code.ADAPTER.normalize(main)
    assert plain["subagent_id"] is None


def test_agent_tool_launch_is_a_delegate_event(tmp_path: Path) -> None:
    for tool in ("Agent", "Task"):
        payload = {
            "hook_event_name": "PreToolUse", "session_id": "s", "cwd": str(tmp_path), "tool_name": tool,
            "tool_input": {"prompt": "find every caller of parse()", "description": "Find callers", "subagent_type": "Explore"},
        }
        (event_,) = claude_code.ADAPTER.normalize(payload)
        assert event_["kind"] == "delegate"
        assert event_["prompt"] == "find every caller of parse()"
        assert event_["summary"] == "Find callers"
        assert event_["subagent_type"] == "Explore"
        NormalizedEvent(**event_)
        # Its PostToolUse (the subagent finished) is not interesting.
        assert claude_code.ADAPTER.normalize({**payload, "hook_event_name": "PostToolUse"}) == []


def test_subagent_start_and_stop_events(tmp_path: Path) -> None:
    common = {"session_id": "s", "cwd": str(tmp_path), "agent_id": "agent-9", "agent_type": "Plan"}
    (start,) = claude_code.ADAPTER.normalize({**common, "hook_event_name": "SubagentStart"})
    (stop,) = claude_code.ADAPTER.normalize({**common, "hook_event_name": "SubagentStop"})
    assert (start["kind"], stop["kind"]) == ("subagent_start", "subagent_stop")
    assert start["subagent_id"] == stop["subagent_id"] == "agent-9"
    # Without an agent_id there is no subagent to speak of.
    assert claude_code.ADAPTER.normalize({"hook_event_name": "SubagentStart", "session_id": "s", "cwd": "x"}) == []


def test_hook_settings_cover_subagents() -> None:
    settings = claude_code.hook_settings()
    assert {"SubagentStart", "SubagentStop"} <= set(settings)
    matchers = [group["matcher"] for group in settings["PreToolUse"]]
    assert matchers == [claude_code.EDIT_MATCHER, claude_code.DELEGATE_MATCHER]


# --- names -----------------------------------------------------------------------


def test_lane_names_fit_the_relay() -> None:
    assert lane_name("Ada", lane_suffix("cursor", None, None)) == "ada-cursor"
    assert lane_name("ada", lane_suffix("claude-code", None, None)) == "ada-claude"
    sub = lane_name("ada", lane_suffix("claude-code", "agent-abc123", "Explore"))
    assert sub == "ada-explore-c123"
    long = lane_name("Bartholomew Longname", lane_suffix("claude-code", "agent-ffff", "security-reviewer"))
    assert len(long) <= 20 and long.endswith("-security-ffff")
    assert all(ch.isalnum() or ch in "-_" for ch in long)


# --- finding the repo from outside it ----------------------------------------------


def test_find_nested_project(tmp_path: Path) -> None:
    outer = tmp_path / "MONGODB"
    inner = make_repo(outer / "MONGODB", project='room = "ROOMN"\n')
    (outer / "node_modules" / "pkg").mkdir(parents=True)
    (outer / "node_modules" / "pkg" / ".hivelane.toml").write_text('room = "X"\n', encoding="utf-8")
    assert find_nested_project(str(outer)) == inner
    assert hook_fast.find_nested_project_file(str(outer)) == str(inner / ".hivelane.toml")
    # Two candidates: which one a cwd-only event means would be a guess.
    make_repo(outer / "other", project='room = "ROOMO"\n')
    assert find_nested_project(str(outer)) is None
    assert hook_fast.find_nested_project_file(str(outer)) is None


def test_agent_started_above_its_repo_is_still_coordinated(tmp_path: Path) -> None:
    """The chat started in MONGODB/, the project (and its .hivelane.toml)
    ended up in MONGODB/MONGODB/: the edits route by file, and the prompt
    sent before any edit is not lost."""
    outer = tmp_path / "MONGODB"
    outer.mkdir()

    async def scenario() -> None:
        async with MemberRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            # The intent arrives before the project exists at all.
            await daemon.handle_event(event("intent", outer, prompt="build the mongo api"))
            inner = make_repo(outer / "MONGODB", project=f'room = "ROOMN"\nrelay = "{relay.url}"\n')
            ack =await daemon.handle_event(event("pre_edit", outer, inner / "src" / "db.py"))
            assert ack.file_path == "src/db.py"
            await wait_for(lambda: daemon.status().get("connected"), "the adopted repo's link")
            await daemon.handle_event(event("pre_edit", outer, inner / "src" / "db.py"))
            await wait_for(lambda: relay.of_type("ada", "edit_check"), "the edit check")
            await wait_for(lambda: relay.of_type("ada", "intent"), "the held intent")
            assert relay.of_type("ada", "intent")[0]["prompt"] == "build the mongo api"
            # A later prompt from the outer cwd goes to the same repo.
            await daemon.handle_event(event("intent", outer, prompt="now add tests"))
            await wait_for(lambda: len(relay.of_type("ada", "intent")) == 2, "the second intent")
            await daemon.aclose()

    asyncio.run(scenario())


def test_cwd_only_event_finds_the_single_nested_project(tmp_path: Path) -> None:
    outer = tmp_path / "work"
    outer.mkdir()

    async def scenario() -> None:
        async with MemberRelay() as relay:
            make_repo(outer / "app", project=f'room = "ROOMN"\nrelay = "{relay.url}"\n')
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.handle_event(event("intent", outer, prompt="hello", session_id="fresh"))
            await wait_for(lambda: relay.of_type("ada", "intent"), "the intent")
            await daemon.aclose()

    asyncio.run(scenario())


# --- lanes -------------------------------------------------------------------------


def registered(daemon: Daemon, root: Path, relay: MemberRelay, agent: str = "claude-code") -> Any:
    return daemon.register(root, "ROOM", relay=relay.url, name="ada", agent=agent)  # type: ignore[arg-type]


def test_parallel_agents_each_get_a_lane(tmp_path: Path) -> None:
    """Two chats and a Cursor session on one machine are three relay
    members, so the relay sees (and blocks) their collisions."""
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with MemberRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await registered(daemon, repo, relay)
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the primary")

            first = await daemon.handle_event(event("pre_edit", repo, repo / "src" / "a.py"))
            assert first.decision == "allow"
            other_chat = await daemon.handle_event(
                event("pre_edit", repo, repo / "src" / "a.py", session_id="chat2")
            )
            assert other_chat.decision == "block"
            assert other_chat.others[0].user_id == "ada"
            cursor = await daemon.handle_event(
                event("pre_edit", repo, repo / "src" / "b.py", agent="cursor", session_id="conv")
            )
            assert cursor.decision == "allow"

            assert relay.members() == ["ada", "ada-claude", "ada-cursor"]
            agents = {join["name"]: join["agent"] for join in relay.joins}
            assert agents["ada-cursor"] == "cursor"
            assert relay.of_type("ada-claude", "edit_check")[0]["file_path"] == "src/a.py"
            lanes = {lane["name"] for lane in daemon.status()["repos"][0]["lanes"]}
            assert lanes == {"ada", "ada-claude", "ada-cursor"}
            await daemon.aclose()

    asyncio.run(scenario())


def test_first_session_takes_the_main_lane_whatever_its_tool(tmp_path: Path) -> None:
    """The repo's config names the team's agent, not this person's: a lone
    Cursor user in a claude-code repo is still just `ada`, not `ada-cursor`."""
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with MemberRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await registered(daemon, repo, relay, agent="claude-code")
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the primary")
            await daemon.handle_event(
                event("pre_edit", repo, repo / "src" / "a.py", agent="cursor", session_id="conv")
            )
            await wait_for(lambda: relay.of_type("ada", "edit_check"), "the check on the main lane")
            assert relay.members() == ["ada"]
            await daemon.aclose()

    asyncio.run(scenario())


def test_subagent_gets_a_lane_with_its_task_as_intent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with MemberRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await registered(daemon, repo, relay)
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the primary")

            await daemon.handle_event(event("intent", repo, prompt="refactor the parser"))
            await daemon.handle_event(
                event("delegate", repo, prompt="find every caller of parse()", summary="Find callers", subagent_type="Explore")
            )
            sub = {"subagent_id": "agent-abc123", "subagent_type": "Explore"}
            await daemon.handle_event(event("subagent_start", repo, **sub))
            await wait_for(lambda: relay.of_type("ada-explore-c123", "intent"), "the subagent's intent")
            assert relay.of_type("ada-explore-c123", "intent")[0]["prompt"] == "Find callers: find every caller of parse()"
            assert [f["prompt"] for f in relay.of_type("ada", "intent")] == ["refactor the parser"]

            # Parent edits a file, then hands it to the subagent: a hand-off,
            # not a collision.
            assert (await daemon.handle_event(event("pre_edit", repo, repo / "src" / "p.py"))).decision == "allow"
            handoff = await daemon.handle_event(event("pre_edit", repo, repo / "src" / "p.py", **sub))
            assert handoff.decision == "allow"
            assert relay.of_type("ada-explore-c123", "edit_check")

            # Two subagents of one chat in one file: a warning, never a block.
            sibling = {"subagent_id": "agent-def456", "subagent_type": "general-purpose"}
            await daemon.handle_event(event("pre_edit", repo, repo / "src" / "q.py", **sub))
            clash = await daemon.handle_event(event("pre_edit", repo, repo / "src" / "q.py", **sibling))
            assert clash.decision == "warn"
            assert "ada-explore-c123" in clash.reason

            # Another chat in the parent's file still gets the relay's block.
            stranger = await daemon.handle_event(
                event("pre_edit", repo, repo / "src" / "p.py", session_id="chat2")
            )
            assert stranger.decision == "block"

            # The subagent finishes: its lane closes after the grace period.
            await daemon.handle_event(event("subagent_stop", repo, **sub))
            daemon._sweep_lanes(now=10**9)
            names = {lane["name"] for lane in daemon.status()["repos"][0]["lanes"]}
            assert "ada-explore-c123" not in names
            await daemon.aclose()

    asyncio.run(scenario())


def test_lanes_can_be_turned_off(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with MemberRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            daemon.lanes_enabled = False
            await daemon.start()
            await registered(daemon, repo, relay)
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the primary")
            await daemon.handle_event(event("pre_edit", repo, repo / "src" / "a.py", session_id="x"))
            await daemon.handle_event(event("pre_edit", repo, repo / "src" / "a.py", agent="cursor", session_id="y"))
            assert relay.members() == ["ada"]
            await daemon.aclose()

    asyncio.run(scenario())
