"""Prompt blame (hivelane/provenance.py) against real temporary git repos:
the daemon journals edits, the installed post-commit hook attaches them as
notes, `hivelane blame` reads them back, and amend / rebase / partial commits
behave as docs/prompt-blame.md says. No relay anywhere."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from hivelane import cli, githook, mcp_server, provenance
from hivelane.daemon import Daemon
from hivelane.events import NormalizedEvent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def dead_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """A git that ignores the developer's config, and a PATH whose `hivelane`
    is this checkout's (the installed hooks run it)."""
    home = tmp_path / "git-home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_AUTHOR_NAME": "Ada Lovelace",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_NAME": "Ada Lovelace",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
        "HIVELANE_PORT": str(dead_port()),
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def git(root: Path, *args: str, stdin: str | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, input=stdin, check=False)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}{done.stdout}"
    return done.stdout


def make_repo(path: Path, hooks: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("hello\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    if hooks:
        githook.install(path)
    return path.resolve()


def ev(kind: str, root: Path, **fields: Any) -> NormalizedEvent:
    return NormalizedEvent(kind=kind, agent="claude-code", session_id=fields.pop("session_id", "s1"),  # type: ignore[arg-type]
                           cwd=str(root), **fields)


class Agent:
    """Drives a real Daemon (single-room mode, relay unreachable) the way
    the Claude Code hooks do: prompt, pre_edit with the old content,
    post_edit with the new."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.daemon = Daemon("ROOM", "ada", server=f"ws://127.0.0.1:{dead_port()}/ws", root=str(root),
                             edit_check_timeout=0.05, batch_seconds=0.01)

    def run(self, *events: NormalizedEvent) -> None:
        async def go() -> None:
            for event in events:
                await self.daemon.handle_event(event)

        asyncio.run(go())

    def prompt(self, text: str, **kw: Any) -> None:
        self.run(ev("intent", self.root, prompt=text, **kw))

    def write(self, rel: str, content: str, **kw: Any) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text() if path.exists() else ""
        pre = ev("pre_edit", self.root, file_path=str(path), old_content=old, **kw)
        path.write_text(content)
        post = ev("post_edit", self.root, file_path=str(path), new_content=content, **kw)
        self.run(pre, post)


def commit(root: Path, message: str, *paths: str) -> str:
    git(root, "add", *(paths or ["-A"]))
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def note(root: Path, rev: str = "HEAD") -> list[dict[str, Any]]:
    done = subprocess.run(["git", "notes", f"--ref={provenance.NOTES_REF}", "show", rev], cwd=root,
                          capture_output=True, text=True, check=False)
    return provenance.parse_note(done.stdout) if done.returncode == 0 else []


def who(root: Path, target: str) -> list[dict[str, Any]]:
    path, first, last = provenance.parse_target(target)
    return provenance.blame(root, path, first, last)


CODE_V1 = "def fetch(url):\n    return http_get(url)\n"
CODE_V2 = "def fetch(url, retries=3):\n    return http_get(url)\n"


# --- the whole loop --------------------------------------------------------------


def test_commit_gets_a_note_and_blame_reads_it_back(git_env: dict[str, str], tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt(f"make fetch retry with backoff, token {SECRET}")
    agent.run(ev("plan", root, steps=[{"content": "Add retry to fetch", "status": "in_progress"}]))
    agent.write("app.py", CODE_V1)
    sha = commit(root, "add fetch")

    entries = note(root, sha)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["file"] == "app.py" and entry["agent"] == "claude-code" and entry["user"] == "ada"
    assert entry["task"] == "Add retry to fetch"
    assert "retry with backoff" in entry["prompt"]
    assert SECRET not in json.dumps(entry)  # redacted before it was ever written
    assert SECRET not in provenance.journal_path(Path(git(root, "rev-parse", "--absolute-git-dir").strip())).read_text()

    groups = who(root, "app.py:1-2")
    assert [(g["start"], g["end"]) for g in groups] == [(1, 2)]
    g = groups[0]
    assert g["commit"] == sha and g["author"] == "Ada Lovelace" and g["match"] == "line"
    assert g["provenance"]["task"] == "Add retry to fetch"

    # The CLI, text and JSON.
    os.chdir(root)
    assert cli.main(["blame", "app.py:2"]) == 0
    text = capsys.readouterr().out
    assert "app.py:2" in text and "claude-code for ada" in text and "Add retry to fetch" in text
    assert cli.main(["blame", "app.py", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["provenance"]["agent"] == "claude-code"

    # A second commit by the same agent attributes only its own lines to it.
    agent.prompt("thread retries through fetch")
    agent.write("app.py", CODE_V2)
    sha2 = commit(root, "retries")
    lines = {ln: g for g in who(root, "app.py") for ln in range(g["start"], g["end"] + 1)}
    assert lines[1]["commit"] == sha2 and "thread retries" in lines[1]["provenance"]["prompt"]
    assert lines[2]["commit"] == sha and "backoff" in lines[2]["provenance"]["prompt"]


def test_human_lines_are_not_claimed_by_the_agent(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("write fetch")
    agent.write("app.py", CODE_V1)
    # The person adds a line by hand before committing.
    (root / "app.py").write_text(CODE_V1 + "print('hand written line')\n")
    commit(root, "fetch")
    groups = who(root, "app.py")
    by_line = {ln: g for g in groups for ln in range(g["start"], g["end"] + 1)}
    assert by_line[1]["provenance"] is not None
    assert by_line[3]["provenance"] is None


def test_uncommitted_lines_come_from_the_journal(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("draft the parser")
    agent.write("parse.py", "def parse(text):\n    return text.split()\n")
    groups = who(root, "parse.py")
    assert groups[0]["uncommitted"] and groups[0]["commit"] is None
    assert "draft the parser" in groups[0]["provenance"]["prompt"]


def test_a_partial_commit_leaves_the_rest_for_the_next_one(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("two modules")
    agent.write("a.py", "alpha_value = 1\n")
    agent.write("b.py", "beta_value = 2\n")
    commit(root, "a only", "a.py")
    assert {e["file"] for e in note(root)} == {"a.py"}
    commit(root, "b too", "b.py")
    assert {e["file"] for e in note(root)} == {"b.py"}
    # Nothing is attached twice.
    git(root, "commit", "-q", "--allow-empty", "-m", "empty")
    assert note(root) == []


# --- rewriting history -----------------------------------------------------------


def test_amend_keeps_the_note_and_adds_new_edits(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("first version")
    agent.write("app.py", CODE_V1)
    old = commit(root, "fetch")
    agent.prompt("second thoughts")
    agent.write("util.py", "def helper_function():\n    return 42\n")
    git(root, "add", "util.py")
    git(root, "commit", "-q", "--amend", "-m", "fetch and helper")
    new = git(root, "rev-parse", "HEAD").strip()
    assert new != old
    assert {e["file"] for e in note(root, new)} == {"app.py", "util.py"}
    assert who(root, "app.py:1")[0]["provenance"]["prompt"] == "first version"


def test_rebase_carries_notes_to_the_rewritten_commits(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    git(root, "checkout", "-q", "-b", "feature")
    agent = Agent(root)
    agent.prompt("feature work")
    agent.write("feature.py", "feature_flag = True\n")
    before = commit(root, "feature")
    git(root, "checkout", "-q", "main")
    (root / "other.py").write_text("unrelated_change = 1\n")
    commit(root, "main moves on")
    git(root, "checkout", "-q", "feature")
    git(root, "rebase", "-q", "main")
    after = git(root, "rev-parse", "HEAD").strip()
    assert after != before
    assert [e["file"] for e in note(root, after)] == ["feature.py"]
    assert who(root, "feature.py:1")[0]["provenance"]["prompt"] == "feature work"


def test_squash_merges_the_notes(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("one")
    agent.write("one.py", "first_module = 1\n")
    one = commit(root, "one")
    agent.prompt("two")
    agent.write("two.py", "second_module = 2\n")
    two = commit(root, "two")
    git(root, "reset", "--soft", "HEAD~2")
    # A soft reset plus commit is not a rewrite git reports; amend is.
    git(root, "commit", "-q", "-m", "squashed")
    head = git(root, "rev-parse", "HEAD").strip()
    assert note(root, head) == []  # documented: use rebase -i / amend to keep notes
    provenance.rewrite(root, [(one, head), (two, head)])
    assert {e["file"] for e in note(root, head)} == {"one.py", "two.py"}


# --- settings and privacy ------------------------------------------------------------


def test_blame_prompts_false_stores_no_prompt_text(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    (root / ".hivelane.toml").write_text("blame_prompts = false\n")
    agent = Agent(root)
    agent.prompt("a private prompt about acquisitions")
    agent.run(ev("plan", root, summary="secret plan", steps=[{"content": "Step", "status": "in_progress"}]))
    agent.write("app.py", CODE_V1)
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    assert "acquisitions" not in provenance.journal_path(git_dir).read_text()
    commit(root, "fetch", "app.py")
    entry = note(root)[0]
    assert "prompt" not in entry and "plan" not in entry
    assert entry["task"] == "Step" and entry["agent"] == "claude-code"


def test_turning_prompts_off_later_strips_them_at_commit(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("was recorded")
    agent.write("app.py", CODE_V1)
    (root / ".hivelane.toml").write_text("blame_prompts = false\n")
    commit(root, "fetch", "app.py")
    assert "prompt" not in note(root)[0]


def test_excerpts_are_capped(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    (root / ".hivelane.toml").write_text("blame_excerpt_chars = 40\n")
    agent = Agent(root)
    agent.prompt("word " * 500)
    agent.write("app.py", CODE_V1)
    commit(root, "fetch", "app.py")
    prompt = note(root)[0]["prompt"]
    assert len(prompt) <= 40 and prompt.endswith(provenance.ELLIPSIS)


def test_blame_false_records_nothing(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    (root / ".hivelane.toml").write_text("blame = false\n")
    agent = Agent(root)
    agent.prompt("nothing to see")
    agent.write("app.py", CODE_V1)
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    assert not provenance.journal_path(git_dir).exists()


def test_the_more_private_setting_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hivelane.config import ConfigFileError, load_config, personal_config_path

    root = tmp_path / "proj"
    root.mkdir()
    (root / ".hivelane.toml").write_text("blame_prompts = false\nblame_excerpt_chars = 100\n")
    personal = personal_config_path()
    personal.parent.mkdir(parents=True, exist_ok=True)
    personal.write_text("blame_prompts = true\nblame_excerpt_chars = 500\n")
    config = load_config(root)
    assert config.blame_prompts is False and config.blame_excerpt_chars == 100
    (root / ".hivelane.toml").write_text("blame_excerpt_chars = 99999\n")
    with pytest.raises(ConfigFileError):
        load_config(root)


def test_notes_never_leave_without_an_explicit_push(git_env: dict[str, str], tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    root = make_repo(tmp_path / "repo")
    git(root, "remote", "add", "origin", str(remote))
    agent = Agent(root)
    agent.prompt("shared work")
    agent.write("app.py", CODE_V1)
    sha = commit(root, "fetch")
    git(root, "push", "-q", "origin", "main")
    assert "refs/notes/hivelane" not in git(remote, "for-each-ref")

    assert "pushed" in provenance.push(root)
    assert "refs/notes/hivelane" in git(remote, "for-each-ref")

    # A teammate's clone: no notes until they fetch them.
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    assert note(clone, sha) == []
    assert "merged" in provenance.fetch(clone)
    assert note(clone, sha)[0]["prompt"] == "shared work"

    # Both sides annotate the same commit: fetch unions the entries.
    extra = {"v": 1, "id": "teammate0000001", "at": 1.0, "file": "app.py", "agent": "codex", "h": []}
    provenance.write_note(provenance.Repo.open(clone), sha, note(clone, sha) + [extra])
    git(clone, "push", "-q", "origin", "refs/notes/hivelane")
    local_only = {"v": 1, "id": "local0000000001", "at": 2.0, "file": "app.py", "agent": "cursor", "h": []}
    provenance.write_note(provenance.Repo.open(root), sha, note(root, sha) + [local_only])
    provenance.fetch(root)
    assert {e["id"] for e in note(root, sha)} >= {"teammate0000001", "local0000000001"}
    assert len(note(root, sha)) == 3


# --- hooks: chaining and failing open -------------------------------------------------


def test_hooks_chain_to_existing_ones(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo", hooks=False)
    hooks = githook.hooks_dir(root)
    hooks.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "chain.log"
    for name in ("post-commit", "post-rewrite"):
        script = hooks / name
        script.write_text(f'#!/bin/sh\necho "{name} $* $(cat)" >> "{log}"\n')
        script.chmod(0o755)
    out = githook.install(root)
    assert out.count("chains") == 2
    assert githook.installed(root) == {"pre-commit": True, "post-commit": True, "post-rewrite": True}

    agent = Agent(root)
    agent.prompt("chained")
    agent.write("app.py", CODE_V1)
    old = commit(root, "fetch")
    git(root, "commit", "-q", "--amend", "-m", "fetch (amended)")
    new = git(root, "rev-parse", "HEAD").strip()
    text = log.read_text()
    assert "post-commit" in text
    assert f"post-rewrite amend {old} {new}" in text  # the previous hook got the same stdin
    assert note(root, new)  # and ours still ran

    githook.uninstall(root)
    assert not githook.is_ours(hooks / "post-commit")
    assert "post-commit" in (hooks / "post-commit").read_text()


def test_a_broken_hivelane_never_breaks_a_commit(git_env: dict[str, str], tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repo(tmp_path / "repo")
    bad = tmp_path / "badbin"
    bad.mkdir()
    script = bad / "hivelane"
    script.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    script.chmod(0o755)
    # Only the post hooks may be broken here; pre-commit's own fail-open is
    # covered in test_fallbacks.
    (githook.hooks_dir(root) / "pre-commit").unlink()
    monkeypatch.setenv("PATH", os.pathsep.join([str(bad), os.environ["PATH"]]))
    (root / "x.py").write_text("value = 1\n")
    commit(root, "still commits")
    git(root, "commit", "-q", "--amend", "-m", "and amends")


def test_garbage_in_the_journal_is_skipped(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("real edit")
    agent.write("app.py", CODE_V1)
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    with open(provenance.journal_path(git_dir), "a") as handle:
        handle.write("{not json\n[1,2]\n{\"id\": 5}\n")
    commit(root, "fetch")
    assert len(note(root)) == 1


def test_hook_entrypoints_fail_open_outside_a_repo(tmp_path: Path) -> None:
    assert provenance.post_commit(tmp_path) == 0
    assert provenance.post_rewrite(tmp_path, "abc def\n") == 0
    assert cli.main(["githook", "post-commit"]) == 0


def test_git_calls_are_time_boxed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout") or 0)

    monkeypatch.setattr(provenance.subprocess, "run", slow)
    with pytest.raises(provenance.GitError, match="did not finish"):
        provenance.run_git(tmp_path, "status", timeout=0.01)
    assert provenance.post_commit(tmp_path) == 0


# --- context tracking ------------------------------------------------------------------


def test_subagents_get_their_delegated_task_as_prompt(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("parent prompt")
    agent.run(
        ev("delegate", root, prompt="write the tests for fetch", summary="tests", subagent_type="Tester"),
        ev("subagent_start", root, subagent_id="sub1", subagent_type="Tester"),
    )
    agent.write("test_app.py", "def test_fetch():\n    assert fetch_ok()\n", subagent_id="sub1", subagent_type="Tester")
    commit(root, "tests")
    entry = note(root)[0]
    assert entry["prompt"] == "tests: write the tests for fetch"
    assert entry["subagent"] == "Tester"


def test_partial_plan_updates_track_the_active_step() -> None:
    recorder = provenance.Recorder()
    base = {"agent": "claude-code", "session_id": "s", "cwd": "/"}
    recorder.observe(NormalizedEvent(kind="plan", plan_partial=True, steps=[{"id": "1", "content": "Write parser", "status": "pending"}], **base))
    recorder.observe(NormalizedEvent(kind="plan", plan_partial=True, steps=[{"id": "1", "content": "", "status": "in_progress"}], **base))
    ctx = recorder._merged(NormalizedEvent(kind="post_edit", **base))
    assert ctx.task == "Write parser"
    recorder.observe(NormalizedEvent(kind="plan", plan_partial=True, steps=[{"id": "1", "content": "", "status": "completed"}], **base))
    assert recorder._merged(NormalizedEvent(kind="post_edit", **base)).task == ""


# --- pieces ----------------------------------------------------------------------------------


def test_parse_target() -> None:
    assert provenance.parse_target("a.py") == ("a.py", None, None)
    assert provenance.parse_target("a.py:12") == ("a.py", 12, 12)
    assert provenance.parse_target("src/a.py:3-9") == ("src/a.py", 3, 9)
    assert provenance.parse_target("C:\\src\\a.py") == ("C:\\src\\a.py", None, None)
    with pytest.raises(ValueError):
        provenance.parse_target("a.py:9-3")


def test_generic_lines_have_no_key() -> None:
    assert provenance.line_key("}") is None and provenance.line_key("   ") is None
    assert provenance.line_key("  return x") == provenance.line_key("return x")


def test_compaction_drops_attached_and_old_records(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    for i, at in enumerate([1.0, 1e12, 1e12]):
        provenance.append_record(git_dir, {"id": f"r{i}", "file": "a", "at": at})
    provenance.mark_attached(git_dir, ["r1"], {"r0", "r1", "r2"})
    provenance.compact_journal(git_dir, now=1e12)
    assert [r["id"] for r in provenance.read_journal(git_dir)] == ["r2"]
    if os.name == "posix":
        assert provenance.journal_path(git_dir).stat().st_mode & 0o077 == 0


def test_mcp_why_code(git_env: dict[str, str], tmp_path: Path) -> None:
    root = make_repo(tmp_path / "repo")
    agent = Agent(root)
    agent.prompt("explain me later")
    agent.write("app.py", CODE_V1)
    commit(root, "fetch")
    server = mcp_server.McpServer(mcp_server.DaemonClient(dead_port(), timeout=0.2), cwd=str(root))
    text = server.why_code({"path": "app.py", "start_line": 1, "end_line": 2})
    assert "explain me later" in text and "claude-code" in text
    assert "No agent provenance" in server.why_code({"path": "README.md"})
