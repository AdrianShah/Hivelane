"""`hivelane task ...`: each command becomes one POST /tasks to the daemon,
replies print as short lines, refusals exit 1, and `claim --wait` retries
until a task is ready."""

from __future__ import annotations

import time
from typing import Any

import pytest

from hivelane import cli, tasks_cli
from hivelane.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("machine")


def scripted(m: Machine, replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    m.service.running = True

    def request(port: int, method: str, path: str, body: Any = None, timeout: float = 0.0) -> Any:
        assert (method, path) == ("POST", "/tasks")
        sent.append(body)
        return replies[min(len(sent), len(replies)) - 1]

    m.service._request = request
    return sent


def _task(**fields: Any) -> dict[str, Any]:
    base = {
        "task_id": "T1", "title": "Add rate limiting", "files": ["server/api.py"], "depends_on": [],
        "status": "open", "created_by": "sam", "created_at": 1.0, "updated_at": 1.0, "notes": [],
    }
    return {**base, **fields}


def test_add_sends_files_and_dependencies(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    sent = scripted(machine, [{"ok": True, "task": _task()}])
    assert cli.main(["task", "add", "Add rate limiting", "--files", "server/api.py, server/limits.py", "--after", "T0"]) == 0
    assert sent[0]["op"] == "create"
    assert sent[0]["files"] == ["server/api.py", "server/limits.py"]
    assert sent[0]["depends_on"] == ["T0"]
    assert sent[0]["cwd"]
    assert "created T1: Add rate limiting" in capsys.readouterr().out


def test_list_and_mine(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    lease = time.time() + 25 * 60
    tasks = [_task(), _task(task_id="T2", status="claimed", claimer="ada", lease_until=lease, title="Docs", files=[])]
    scripted(machine, [{"ok": True, "me": "ada", "tasks": tasks}])
    assert cli.main(["task", "list"]) == 0
    out = capsys.readouterr().out
    assert "T1  open      Add rate limiting  (server/api.py)" in out
    assert "T2  claimed   Docs [ada, 2" in out
    assert cli.main(["task", "list", "--mine", "--port", "4600"]) == 0
    out = capsys.readouterr().out
    assert "T2" in out and "T1" not in out


def test_claim_next_waits_until_one_is_ready(
    machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks_cli, "CLAIM_RETRY_SECONDS", 0.0)
    claimed = _task(status="claimed", claimer="ada", lease_until=time.time() + 1800)
    sent = scripted(machine, [{"ok": False, "reason": "none_ready"}, {"ok": False, "reason": "none_ready"}, {"ok": True, "task": claimed}])
    assert cli.main(["task", "claim", "--next", "--wait", "1m", "--lease", "45m"]) == 0
    assert len(sent) == 3
    assert all(body["next"] is True and body["lease_seconds"] == 2700 for body in sent)
    out = capsys.readouterr().out
    assert out.count("nothing ready yet") == 1
    assert "claimed T1" in out and "reserved for you: server/api.py" in out


def test_refusals_exit_1_with_a_reason(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    scripted(machine, [{"ok": False, "reason": "not_claimer"}])
    assert cli.main(["task", "done", "t3", "PR #4"]) == 1
    assert "only the agent that claimed T3 can do that" in capsys.readouterr().err
    assert cli.main(["task", "claim"]) == 2


def test_without_a_daemon_it_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["task", "list"]) == 1
    assert "run `hivelane up` first" in capsys.readouterr().err


@pytest.mark.parametrize("raw,seconds", [("90", 90), ("90s", 90), ("30m", 1800), ("1.5h", 5400), ("1d", 86400)])
def test_durations(raw: str, seconds: float) -> None:
    assert tasks_cli.duration(raw) == seconds


def test_tasks_cli_never_imports_the_cli_back() -> None:
    # hivelane.cli imports tasks_cli (and mcp_server imports it too); an
    # import the other way would close a cycle (CodeQL py/cyclic-import).
    import ast
    from pathlib import Path

    tree = ast.parse(Path(tasks_cli.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not {name for name in imported if name and name.startswith("hivelane")}, imported


def test_add_for_a_teammate_and_list_shows_plans_and_suggestions(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    sent = scripted(machine, [{"ok": True, "task": _task(suggested_for="ben")}])
    assert cli.main(["task", "add", "Fix login", "--for", "ben"]) == 0
    assert sent[0]["suggested_for"] == "ben"
    capsys.readouterr()

    lease = time.time() + 600
    tasks = [
        _task(suggested_for="ben"),
        _task(task_id="T2", status="claimed", claimer="ada", lease_until=lease, title="Wire it",
              source="agent_plan", lane="ada-claude", active=True),
    ]
    scripted(machine, [{"ok": True, "me": "ada", "tasks": tasks}])
    assert cli.main(["task", "list"]) == 0
    out = capsys.readouterr().out
    assert "[for ben]" in out
    assert "[step of ada-claude's plan, current step]" in out


class _Gh:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, "boom"


def test_import_github_adds_new_labelled_issues_once(
    machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    issues = [
        {"number": 7, "title": "Login loops", "body": "steps", "url": "u7"},
        {"number": 8, "title": "Dark mode", "body": "", "url": "u8"},
    ]
    commands: list[list[str]] = []
    monkeypatch.setattr(tasks_cli.shutil, "which", lambda name: "gh")
    monkeypatch.setattr(
        tasks_cli.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or _Gh(json.dumps(issues))
    )
    listed = {"ok": True, "tasks": [_task(title="#7 Login loops")]}
    sent = scripted(machine, [listed, {"ok": True, "task": _task(task_id="T2")}])
    assert cli.main(["task", "import-github", "--label", "ready"]) == 0
    assert commands[0][:5] == ["gh", "issue", "list", "--label", "ready"]
    creates = [b for b in sent if b["op"] == "create"]
    assert [c["title"] for c in creates] == ["#8 Dark mode"]
    assert creates[0]["body"] == "u8"
    assert "imported 1 issue labelled ready (1 already on the list)" in capsys.readouterr().out


def test_import_github_without_gh_says_so(
    machine: Machine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks_cli.shutil, "which", lambda name: None)
    scripted(machine, [{"ok": True}])
    assert cli.main(["task", "import-github", "--label", "x"]) == 1
    assert "GitHub CLI" in capsys.readouterr().err
