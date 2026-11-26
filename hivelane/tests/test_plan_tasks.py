"""Agents' plans as room tasks: the adapters' plan steps, PlanTasks keeping
one task per step (dedupe across re-sent lists, status mapping, files of the
step in progress), and the daemon handing plan events over without waiting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from hivelane.adapters import claude_code, codex
from hivelane.daemon import Daemon
from hivelane.events import NormalizedEvent
from hivelane.plan_tasks import ACTIVE_LEASE_SECONDS, IDLE_LEASE_SECONDS, MAX_STEPS, PlanTasks

KEY = ("claude-code", "s1", "")


class FakeRelay:
    """A RoomConnection stand-in: answers task requests like the relay's
    TaskBoard would, and records them."""

    name = "ada"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.fail: dict[str, str] = {}

    async def task_request(self, op: str, repo_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append((op, fields))
        if op in self.fail:
            return {"ok": False, "reason": self.fail.pop(op)}
        if op == "create":
            task_id = f"T{len(self.tasks) + 1}"
            task = {"task_id": task_id, "repo_id": repo_id, "status": "claimed" if fields.get("claim") else "open", **fields}
            self.tasks[task_id] = task
            return {"ok": True, "task": task}
        task = self.tasks.get(fields.get("task_id", ""))
        if task is None:
            return {"ok": False, "reason": "not_found"}
        if op == "done":
            task["status"] = "done"
        elif op == "cancel":
            task["status"] = "cancelled"
        elif op == "update":
            task.update(fields)
        return {"ok": True, "task": task}

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _todos(*items: tuple[str, str]) -> list[dict[str, str]]:
    return [{"content": c, "status": s} for c, s in items]


def _run(plans: PlanTasks, *steps: Any) -> None:
    async def scenario() -> None:
        for step in steps:
            step()
            await plans.idle()

    asyncio.run(scenario())


# --- adapters ------------------------------------------------------------------------


def _claude(tool: str, tool_input: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {
        "hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/repo",
        "tool_name": tool, "tool_input": tool_input, **extra,
    }
    event = claude_code.normalize_payload(payload)
    assert event is not None
    return event


def test_todowrite_carries_every_step_with_its_status() -> None:
    event = _claude("TodoWrite", {"todos": [
        {"content": "Read the  code", "status": "completed", "activeForm": "Reading"},
        {"content": "Add the limiter", "status": "in_progress"},
        {"content": "Write tests", "status": "pending", "id": "3"},
        {"content": "   ", "status": "pending"},
        "junk",
    ]})
    assert event["steps"] == [
        {"content": "Read the code", "status": "completed"},
        {"content": "Add the limiter", "status": "in_progress"},
        {"content": "Write tests", "status": "pending", "id": "3"},
    ]
    assert event["plan_partial"] is None
    # The plan text (for overlap checks) still leaves finished steps out.
    assert event["summary"] == "- Add the limiter\n- Write tests"
    NormalizedEvent(**event)


def test_task_tools_send_single_steps() -> None:
    created = _claude("TaskCreate", {"subject": "Add the limiter", "description": "..."},
                      tool_response={"task": {"id": "4", "subject": "Add the limiter"}})
    assert created["steps"] == [{"content": "Add the limiter", "status": "pending", "id": "4"}]
    assert created["plan_partial"] is True and created["summary"] is None
    # The id from the response text when it is not structured.
    texty = _claude("TaskCreate", {"subject": "Docs"}, tool_response="Task #12 created successfully")
    assert texty["steps"][0]["id"] == "12"

    updated = _claude("TaskUpdate", {"taskId": "4", "status": "in_progress"})
    assert updated["steps"] == [{"content": "", "status": "in_progress", "id": "4"}]
    deleted = _claude("TaskUpdate", {"taskId": "4", "status": "deleted"})
    assert deleted["steps"][0]["status"] == "removed"
    renamed = _claude("TaskUpdate", {"taskId": "4", "subject": "Add a limiter"})
    assert renamed["steps"] == [{"content": "Add a limiter", "status": "", "id": "4"}]


def test_a_task_create_without_an_id_is_not_a_step() -> None:
    payload = {
        "hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/repo",
        "tool_name": "TaskCreate", "tool_input": {"subject": "x"}, "tool_response": {},
    }
    assert claude_code.normalize_payload(payload) is None


def test_claude_code_hooks_include_the_task_tools() -> None:
    matcher = claude_code.hook_settings()["PostToolUse"][0]["matcher"].split("|")
    assert {"TodoWrite", "TaskCreate", "TaskUpdate"} <= set(matcher)


def test_codex_update_plan_becomes_plan_steps() -> None:
    events = codex.normalize_payload({
        "hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/repo", "tool_name": "update_plan",
        "tool_input": {"explanation": "x", "plan": [
            {"step": "Add the limiter", "status": "in_progress"},
            {"step": "Write tests", "status": "pending"},
        ]},
    })
    assert len(events) == 1 and events[0]["kind"] == "plan"
    assert [s["content"] for s in events[0]["steps"]] == ["Add the limiter", "Write tests"]
    assert "update_plan" in codex.hook_settings()["PostToolUse"][0]["matcher"]
    assert codex.normalize_payload({
        "hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/repo", "tool_name": "update_plan",
        "tool_input": {"plan": []},
    }) == []


# --- PlanTasks -------------------------------------------------------------------------


def test_a_plan_becomes_claimed_tasks_and_a_resent_list_updates_them() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(plans, lambda: plans.submit(relay, KEY, "repo", "ada-claude", _todos(
        ("Add the limiter", "in_progress"), ("Write tests", "pending"),
    )))
    creates = [f for op, f in relay.calls if op == "create"]
    assert [c["title"] for c in creates] == ["Add the limiter", "Write tests"]
    for create in creates:
        assert (create["claim"], create["source"], create["lane"]) == (True, "agent_plan", "ada-claude")
    assert [(c["active"], c["lease_seconds"]) for c in creates] == [
        (True, ACTIVE_LEASE_SECONDS), (False, IDLE_LEASE_SECONDS),
    ]

    # The same list again: nothing to do.
    relay.calls.clear()
    _run(plans, lambda: plans.submit(relay, KEY, "repo", "ada-claude", _todos(
        ("Add the limiter", "in_progress"), ("Write tests", "pending"),
    )))
    assert relay.calls == []

    # First step done, second started, a third added, nothing duplicated.
    _run(plans, lambda: plans.submit(relay, KEY, "repo", "ada-claude", _todos(
        ("Add the limiter", "completed"), ("Write tests", "in_progress"), ("Update docs", "pending"),
    )))
    assert relay.ops() == ["done", "claim", "update", "create"]
    assert relay.tasks["T1"]["status"] == "done"
    assert relay.tasks["T2"]["active"] is True
    assert relay.tasks["T3"]["title"] == "Update docs"
    assert len(relay.tasks) == 3


def test_removed_steps_are_cancelled_and_reworded_ones_keep_their_task() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(plans, lambda: plans.submit(relay, KEY, "r", "ada-claude", _todos(
        ("One", "pending"), ("Two", "pending"), ("Three", "pending"),
    )))
    relay.calls.clear()
    # "Two" reworded in place, "Three" gone.
    _run(plans, lambda: plans.submit(relay, KEY, "r", "ada-claude", _todos(("One", "pending"), ("Two, better", "pending"))))
    assert relay.ops() == ["update", "cancel"]
    assert relay.tasks["T2"]["title"] == "Two, better"
    assert relay.tasks["T3"]["status"] == "cancelled"
    assert [s.title for s in plans.plan(relay, KEY).steps] == ["One", "Two, better"]


def test_a_reopened_step_gets_a_new_task() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(
        plans,
        lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "completed"))),
        lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "pending"))),
    )
    assert relay.ops() == ["create", "done", "create"]


def test_partial_steps_update_by_the_agents_id() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(
        plans,
        lambda: plans.submit(relay, KEY, "r", "l", [{"content": "One", "status": "pending", "id": "1"}], partial=True),
        lambda: plans.submit(relay, KEY, "r", "l", [{"content": "Two", "status": "pending", "id": "2"}], partial=True),
        lambda: plans.submit(relay, KEY, "r", "l", [{"content": "", "status": "in_progress", "id": "1"}], partial=True),
        lambda: plans.submit(relay, KEY, "r", "l", [{"content": "", "status": "removed", "id": "2"}], partial=True),
    )
    # A partial update never removes the steps it leaves out.
    assert relay.ops() == ["create", "create", "claim", "update", "cancel"]
    assert relay.tasks["T1"]["active"] is True and relay.tasks["T1"]["title"] == "One"


def test_files_edited_during_a_step_are_attached_to_it() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(
        plans,
        lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "completed"), ("Two", "in_progress"))),
        lambda: plans.note_edit(relay, KEY, "server/api.py"),
        lambda: plans.note_edit(relay, KEY, "server/api.py"),
        # A subagent's edit counts for its parent's current step.
        lambda: plans.note_edit(relay, ("claude-code", "s1", "sub-1"), "server/limits.py"),
        # Another session's edit does not.
        lambda: plans.note_edit(relay, ("claude-code", "s2", ""), "other.py"),
    )
    assert relay.tasks["T2"]["files"] == ["server/api.py", "server/limits.py"]
    assert relay.tasks["T1"].get("files") == []
    updates = [f for op, f in relay.calls if op == "update"]
    assert len(updates) == 2


def test_no_step_in_progress_attaches_nothing() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(
        plans,
        lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "pending"))),
        lambda: plans.note_edit(relay, KEY, "a.py"),
    )
    assert relay.ops() == ["create"]


def test_a_full_room_list_is_not_retried_until_the_step_changes() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    relay.fail["create"] = "limit"
    _run(
        plans,
        lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "pending"))),
        lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "pending"))),
    )
    assert relay.ops() == ["create"]
    _run(plans, lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "in_progress"))))
    assert relay.ops() == ["create", "create"]


def test_a_task_the_relay_forgot_is_made_again() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(plans, lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "pending"))))
    relay.tasks.clear()  # a relay restart
    _run(plans, lambda: plans.submit(relay, KEY, "r", "l", _todos(("One renamed", "pending"))))
    assert relay.ops() == ["create", "update", "create"]
    assert relay.tasks["T1"]["title"] == "One renamed"


def test_a_lapsed_claim_is_taken_back_to_finish_it() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(plans, lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "pending"))))
    relay.fail["done"] = "not_claimer"
    _run(plans, lambda: plans.submit(relay, KEY, "r", "l", _todos(("One", "completed"))))
    assert relay.ops() == ["create", "done", "claim", "done"]
    assert relay.tasks["T1"]["status"] == "done"


def test_long_plans_are_capped() -> None:
    relay, plans = FakeRelay(), PlanTasks()
    _run(plans, lambda: plans.submit(relay, KEY, "r", "l", _todos(*[(f"s{i}", "pending") for i in range(MAX_STEPS + 5)])))
    assert relay.ops().count("create") == MAX_STEPS


def test_plan_step_requests_are_sealed_in_an_e2e_room() -> None:
    from hivelane import crypto
    from hivelane.daemon import RoomConnection
    from hivelane.protocol import RoomSettings, TaskRequest

    if not crypto.available():
        pytest.skip("needs the e2e extra (cryptography)")
    key = crypto.derive("correct horse battery", "ROOM")
    conn = RoomConnection("ROOM", "ada", settings=RoomSettings(e2e=True), e2e_key=key)
    request = TaskRequest(
        request_id="r", op="update", task_id="T1", title="Add the limiter", files=["server/api.py"],
        active=True, source="agent_plan", lane="ada-claude",
    )
    sealed = conn._seal(request)
    assert (sealed.title, sealed.files, sealed.active, sealed.lane) == ("", [key.hash("server/api.py")], True, "ada-claude")
    kind, envelope = key.open(sealed.e2e)
    assert (kind, envelope["title"], envelope["files"]) == ("task", "Add the limiter", ["server/api.py"])


def test_agents_are_told_how_to_grab_the_next_task() -> None:
    from hivelane import mcp_server

    assert "next task" in mcp_server.AGENTS_MD and "`next: true`" in mcp_server.AGENTS_MD
    claim = next(t for t in mcp_server.TOOLS if t["name"] == "task_claim")
    assert "grab" in claim["description"]


# --- the daemon hands plans over without waiting ------------------------------------


def _daemon(tmp_path: Path, **kwargs: Any) -> Daemon:
    return Daemon(room="ROOM", name="ada", root=str(tmp_path), **kwargs)


def test_the_daemon_mirrors_a_plan_event_into_plan_tasks(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    seen: list[tuple[Any, ...]] = []
    daemon.plan_tasks.submit = lambda conn, key, repo, lane, steps, partial=False: seen.append(  # type: ignore[method-assign]
        ("plan", key, lane, steps, partial)
    )
    daemon.plan_tasks.note_edit = lambda conn, key, rel: seen.append(("edit", key, rel))  # type: ignore[method-assign]
    plan = NormalizedEvent(
        kind="plan", agent="claude-code", session_id="s1", cwd=str(tmp_path), tool="TodoWrite",
        steps=[{"content": "One", "status": "in_progress"}],
    )
    edit = NormalizedEvent(
        kind="post_edit", agent="claude-code", session_id="s1", cwd=str(tmp_path),
        file_path=str(tmp_path / "src" / "a.py"), new_content="x",
    )

    async def scenario() -> None:
        await daemon.handle_event(plan)
        await daemon.handle_event(edit)

    asyncio.run(scenario())
    assert seen[0] == ("plan", KEY, "ada", [{"content": "One", "status": "in_progress"}], False)
    assert seen[1] == ("edit", KEY, "src/a.py")


def test_plan_tasks_off_in_the_config_sends_nothing(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    assert daemon._default is not None
    daemon._default.plan_tasks = False
    seen: list[Any] = []
    daemon.plan_tasks.submit = lambda *a, **k: seen.append(a)  # type: ignore[method-assign]
    event = NormalizedEvent(
        kind="plan", agent="claude-code", session_id="s1", cwd=str(tmp_path),
        steps=[{"content": "One", "status": "pending"}],
    )
    asyncio.run(daemon.handle_event(event))
    assert seen == []


def test_plan_tasks_is_a_project_and_personal_setting(tmp_path: Path) -> None:
    from hivelane.config import ConfigFileError, load_config

    (tmp_path / ".hivelane.toml").write_text('room = "ROOM"\nplan_tasks = false\n', encoding="utf-8")
    assert load_config(tmp_path).plan_tasks is False
    (tmp_path / ".hivelane.toml").write_text('room = "ROOM"\n', encoding="utf-8")
    assert load_config(tmp_path).plan_tasks is True
    (tmp_path / ".hivelane.toml").write_text('room = "ROOM"\nplan_tasks = "no"\n', encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_config(tmp_path)


def test_a_registration_carries_plan_tasks(tmp_path: Path) -> None:
    from hivelane.config import HivelaneConfig
    from hivelane.daemon import RepoRegistration
    from hivelane.service import registration

    body = registration(tmp_path, HivelaneConfig(room="ROOM", plan_tasks=False))
    assert RepoRegistration(**json.loads(json.dumps(body))).plan_tasks is False
