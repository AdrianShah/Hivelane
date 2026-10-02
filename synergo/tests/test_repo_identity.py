"""Repo identity on the daemon: every clone of one repo must compute the same
repo_id however its remote is spelled, different repos must not, and the git
lookup must stay off the pre_edit hook's per-call path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from synergo import daemon as daemon_module
from synergo.daemon import (
    Daemon,
    compute_repo_id,
    normalize_remote_url,
    repo_id_for,
)
from synergo.events import NormalizedEvent

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The repo caches are process-global; tests must not see each other's."""
    monkeypatch.setattr(daemon_module, "_repo_ids", {})
    monkeypatch.setattr(daemon_module, "_repo_roots", {})
    yield


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(path: Path, origin: str | None = None, commit: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    if commit:
        (path / "README").write_text("x\n", encoding="utf-8")
        _git(path, "add", "README")
        _git(
            path,
            "-c", "user.name=t",
            "-c", "user.email=t@example.com",
            "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", "init",
        )
    return path


def _fake_repo(path: Path) -> Path:
    """A directory repo discovery accepts, without needing git."""
    (path / ".git").mkdir(parents=True)
    return path


def _drain(daemon: Daemon) -> list[dict]:
    messages: list[dict] = []
    for queue in (daemon._priority_outbox, daemon._outbox):
        while not queue.empty():
            messages.append(json.loads(queue.get_nowait()))
    return messages


# --- normalize_remote_url -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:a/b.git",
        "https://github.com/a/b",
        "https://github.com/a/b.git",
        "https://github.com/a/b/",
        "https://user:token@github.com/a/b.git",
        "ssh://git@github.com/a/b.git",
        "ssh://git@github.com:22/a/b.git",
        "git://GitHub.com/a/b.git",
        "HTTPS://GITHUB.COM/a/b",
        "  git@github.com:a/b.git\n",
    ],
)
def test_every_spelling_of_one_remote_normalizes_the_same(url: str) -> None:
    assert normalize_remote_url(url) == "github.com/a/b"


def test_path_case_is_kept() -> None:
    """Only the host is case-insensitive everywhere; some hosts treat the
    path as significant."""
    assert normalize_remote_url("git@github.com:Acme/App.git") == "github.com/Acme/App"


def test_different_repos_stay_different() -> None:
    assert normalize_remote_url("git@github.com:a/b.git") != normalize_remote_url(
        "git@github.com:a/c.git"
    )
    assert normalize_remote_url("git@github.com:a/b.git") != normalize_remote_url(
        "git@gitlab.com:a/b.git"
    )


def test_local_paths_are_not_mistaken_for_scp_addresses() -> None:
    assert normalize_remote_url("/srv/git/b.git") == "srv/git/b"
    assert normalize_remote_url("C:\\repos\\b.git") == "C:/repos/b"


def test_empty_remote_normalizes_to_empty() -> None:
    assert normalize_remote_url("") == ""


# --- compute_repo_id (real git) -----------------------------------------------


@needs_git
def test_ssh_and_https_clones_share_a_repo_id(tmp_path: Path) -> None:
    ssh = _git_repo(tmp_path / "ssh", origin="git@github.com:a/b.git")
    https = _git_repo(tmp_path / "https", origin="https://github.com/a/b")

    expected = hashlib.sha256(b"github.com/a/b").hexdigest()[:16]
    assert compute_repo_id(ssh) == compute_repo_id(https) == expected


@needs_git
def test_different_origins_get_different_ids(tmp_path: Path) -> None:
    one = _git_repo(tmp_path / "one", origin="git@github.com:a/b.git")
    two = _git_repo(tmp_path / "two", origin="git@github.com:a/c.git")

    assert compute_repo_id(one) != compute_repo_id(two)


@needs_git
def test_without_origin_the_root_commit_identifies_the_repo(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "local", commit=True)
    root_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert compute_repo_id(repo) == root_commit[:16]


@needs_git
def test_repo_without_origin_or_commits_has_no_id(tmp_path: Path) -> None:
    assert compute_repo_id(_git_repo(tmp_path / "empty")) == ""


def test_missing_git_means_no_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(daemon_module.subprocess, "run", no_git)

    assert compute_repo_id(_fake_repo(tmp_path / "r")) == ""


# --- caching and the hook path --------------------------------------------------


def test_repo_id_is_computed_once_per_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def fake(root: Path) -> str:
        calls.append(root)
        return "feedfacecafebeef"

    monkeypatch.setattr(daemon_module, "compute_repo_id", fake)
    root = _fake_repo(tmp_path / "r").resolve()

    for _ in range(5):
        assert repo_id_for(root) == "feedfacecafebeef"

    assert calls == [root]


def test_pre_edit_does_not_shell_out_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def fake(root: Path) -> str:
        calls.append(root)
        return "feedfacecafebeef"

    monkeypatch.setattr(daemon_module, "compute_repo_id", fake)
    repo = _fake_repo(tmp_path / "r")

    async def scenario() -> list[dict]:
        daemon = Daemon("TEAM42", "Ada", port=4697, edit_check_timeout=0.01)
        daemon.connected = True
        for n in range(4):
            await daemon.handle_event(
                NormalizedEvent(
                    kind="pre_edit",
                    session_id="s1",
                    cwd=str(repo),
                    file_path=str(repo / f"f{n}.py"),
                )
            )
        return _drain(daemon)

    messages = asyncio.run(scenario())

    assert len(calls) == 1
    assert [m["repo_id"] for m in messages] == ["feedfacecafebeef"] * 4


def test_messages_carry_the_repo_id_of_their_own_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon serving agents in two repos labels each message by its repo,
    and never mixes their changes in one activity message."""
    ids = {"one": "1111111111111111", "two": "2222222222222222"}
    monkeypatch.setattr(daemon_module, "compute_repo_id", lambda root: ids[root.name])
    one = _fake_repo(tmp_path / "one")
    two = _fake_repo(tmp_path / "two")

    async def scenario() -> list[dict]:
        daemon = Daemon("TEAM42", "Ada", port=4697, batch_seconds=0.05)
        await daemon.handle_event(
            NormalizedEvent(kind="intent", session_id="s1", cwd=str(one), prompt="go")
        )
        for repo in (one, two):
            await daemon.handle_event(
                NormalizedEvent(
                    kind="post_edit",
                    session_id="s1",
                    cwd=str(repo),
                    file_path=str(repo / "src" / "main.py"),
                    old_content="a\n",
                    new_content="b\n",
                )
            )
        await asyncio.sleep(0.2)
        return _drain(daemon)

    messages = asyncio.run(scenario())

    intent = next(m for m in messages if m["type"] == "intent")
    activities = [m for m in messages if m["type"] == "activity"]
    assert intent["repo_id"] == ids["one"]
    assert sorted((a["repo_id"], len(a["changes"])) for a in activities) == [
        (ids["one"], 1),
        (ids["two"], 1),
    ]
    assert {a["changes"][0]["file_path"] for a in activities} == {"src/main.py"}


def test_outside_a_repo_the_id_is_empty_without_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if daemon_module.repo_root(str(tmp_path)) is not None:
        pytest.skip("the temp directory is itself inside a git repo")

    def boom(root: Path) -> str:
        raise AssertionError("git must not run outside a repo")

    monkeypatch.setattr(daemon_module, "compute_repo_id", boom)

    async def scenario() -> list[dict]:
        daemon = Daemon("TEAM42", "Ada", port=4697)
        await daemon.handle_event(
            NormalizedEvent(kind="intent", session_id="s1", cwd=str(tmp_path), prompt="go")
        )
        return _drain(daemon)

    assert asyncio.run(scenario())[0]["repo_id"] == ""


# --- --root -------------------------------------------------------------------


def test_root_override_keys_paths_on_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)

    async def scenario() -> str:
        daemon = Daemon("TEAM42", "Ada", port=4697, root=str(project))
        ack = await daemon.handle_event(
            NormalizedEvent(
                kind="pre_edit",
                session_id="s1",
                cwd=str(project / "pkg"),
                file_path=str(project / "pkg" / "mod.py"),
            )
        )
        return ack.file_path or ""

    assert asyncio.run(scenario()) == "pkg/mod.py"


def test_root_override_inside_another_repo_does_not_inherit_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--root under, say, a dotfiles repo in $HOME must not pick up that repo's
    identity: teammates' dotfiles differ, so they would never match."""
    monkeypatch.setattr(daemon_module, "compute_repo_id", lambda root: "dotfiles00000000")
    home = _fake_repo(tmp_path / "home")
    project = home / "project"
    project.mkdir()

    async def scenario() -> list[dict]:
        daemon = Daemon("TEAM42", "Ada", port=4697, root=str(project))
        await daemon.handle_event(
            NormalizedEvent(kind="intent", session_id="s1", cwd=str(project), prompt="go")
        )
        return _drain(daemon)

    assert asyncio.run(scenario())[0]["repo_id"] == ""
