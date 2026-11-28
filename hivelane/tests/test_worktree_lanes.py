"""Agents in git worktrees and their subagents, as the room sees them.

The agent a repo's connection stands for is learned from the first hook
(never configured); a session in a linked worktree gets a lane named after the
worktree; a subagent's lane names the chat it belongs to. All of it is display
metadata on join (or a member_update), so a dashboard can draw the tree.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from hivelane.daemon import Daemon, RoomConnection, lane_name, lane_suffix, linked_worktree
from hivelane.protocol import RoomSettings
from hivelane.tests.test_agent_lanes import MemberRelay, event, isolated, make_repo, wait_for

__all__ = ["isolated"]  # the autouse fixture, imported so it applies here too


def make_worktree(path: Path, project: str) -> Path:
    """A linked worktree: .git is a file pointing into the main repository."""
    (path / "src").mkdir(parents=True)
    (path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    (path / ".hivelane.toml").write_text(project, encoding="utf-8")
    return path.resolve()


def test_linked_worktree_is_told_apart_from_a_checkout(tmp_path: Path) -> None:
    main = make_repo(tmp_path / "app")
    tree = make_worktree(tmp_path / "ghost-merge", 'room = "R"\n')
    assert linked_worktree(main) == ""
    assert linked_worktree(tree) == "ghost-merge"
    assert linked_worktree(None) == ""
    assert linked_worktree(tmp_path / "missing") == ""


def test_worktree_sessions_are_named_after_the_worktree() -> None:
    assert lane_name("ada", lane_suffix("claude-code", None, None, "ghost-merge")) == "ada-ghost-merge"
    # A subagent keeps its type and id even inside a worktree.
    assert lane_name("ada", lane_suffix("claude-code", "agent-abc123", "Explore", "x")) == "ada-explore-c123"
    # Nothing usable in the worktree name: the agent, as before.
    assert lane_suffix("claude-code", None, None, "///") == "claude"
    assert len(lane_name("Bartholomew", lane_suffix("codex", None, None, "outcome-calibration"))) <= 20


def test_worktree_name_never_leaves_an_e2e_room() -> None:
    plain = RoomConnection("R", "ada")
    plain.worktree = "ghost-merge"
    assert json.loads(plain._join_payload())["worktree"] == "ghost-merge"
    sealed = RoomConnection("R", "ada", settings=RoomSettings(e2e=True))
    sealed.worktree = "ghost-merge"
    assert "worktree" not in json.loads(sealed._join_payload())
    # Empty metadata is left off the join entirely.
    assert not {"parent", "subagent_type", "worktree"} & set(json.loads(RoomConnection("R", "ada")._join_payload()))


def test_room_sees_the_agent_worktrees_and_subagents(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MemberRelay() as relay:
            project = f'room = "ROOMW"\nrelay = "{relay.url}"\n'
            main = make_repo(tmp_path / "app", project=project)
            tree = make_worktree(tmp_path / "worktrees" / "ghost-merge", project)
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()

            # The first main session says which agent the repo's connection
            # stands for: on its join if it is not in yet, else a member_update.
            await daemon.handle_event(event("pre_edit", main, main / "src" / "a.py", session_id="chat1"))
            await wait_for(lambda: "ada" in relay.members(), "the repo's connection")

            def label() -> str:
                updates = relay.of_type("ada", "member_update")
                return str(updates[-1]["agent"] if updates else relay.joins[0]["agent"])

            await wait_for(lambda: label() == "claude-code", "the relabel")

            # A session in a linked worktree: its own lane, named after it.
            await daemon.handle_event(event("pre_edit", tree, tree / "src" / "b.py", session_id="chat2"))
            await wait_for(lambda: "ada-ghost-merge" in relay.members(), "the worktree lane")
            joined = next(j for j in relay.joins if j["name"] == "ada-ghost-merge")
            assert (joined["agent"], joined["worktree"]) == ("claude-code", "ghost-merge")
            assert "parent" not in joined

            # Its subagent: a lane that names the worktree chat as its parent.
            await daemon.handle_event(
                event(
                    "pre_edit", tree, tree / "src" / "c.py", session_id="chat2",
                    subagent_id="agent-9f31", subagent_type="Explore",
                )
            )
            await wait_for(lambda: "ada-explore-9f31" in relay.members(), "the subagent lane")
            sub = next(j for j in relay.joins if j["name"] == "ada-explore-9f31")
            assert (sub["parent"], sub["subagent_type"], sub["worktree"]) == ("ada-ghost-merge", "Explore", "ghost-merge")

            # A subagent of the main-checkout chat belongs to the repo's connection.
            await daemon.handle_event(
                event(
                    "pre_edit", main, main / "src" / "d.py", session_id="chat1",
                    subagent_id="agent-77aa", subagent_type="Plan",
                )
            )
            await wait_for(lambda: "ada-plan-77aa" in relay.members(), "the main chat's subagent")
            plan = next(j for j in relay.joins if j["name"] == "ada-plan-77aa")
            assert plan["parent"] == "ada" and "worktree" not in plan
            await daemon.aclose()

    asyncio.run(scenario())
