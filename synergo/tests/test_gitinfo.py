"""synergo.gitinfo: branch and HEAD read from .git files, against real repos
(branch switch, detached HEAD, packed refs, linked worktree) and the cache."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from synergo import gitinfo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "one")
    gitinfo.clear_cache()
    return root


def test_branch_and_commit(repo: Path) -> None:
    head = gitinfo.read_head(repo)
    assert head == gitinfo.GitHead(branch="main", commit=git(repo, "rev-parse", "HEAD"))


def test_branch_switch_and_new_commit_refresh_the_cache(repo: Path) -> None:
    assert gitinfo.head_for(repo, now=0.0).branch == "main"
    git(repo, "checkout", "-q", "-b", "feature")
    # Within the check interval the cached answer stands.
    assert gitinfo.head_for(repo, now=1.0).branch == "main"
    assert gitinfo.head_for(repo, now=10.0).branch == "feature"
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two")
    assert gitinfo.head_for(repo, now=20.0).commit == git(repo, "rev-parse", "HEAD")


def test_detached_head(repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")
    assert gitinfo.read_head(repo) == gitinfo.GitHead(branch=None, commit=sha)


def test_packed_refs(repo: Path) -> None:
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "pack-refs", "--all")
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    assert gitinfo.read_head(repo) == gitinfo.GitHead(branch="main", commit=sha)


def test_linked_worktree(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    assert (other / ".git").is_file()
    head = gitinfo.read_head(other)
    assert head is not None and head.branch == "side"
    assert head.commit == git(repo, "rev-parse", "HEAD")


def test_no_repo_and_unborn_branch(tmp_path: Path) -> None:
    assert gitinfo.read_head(tmp_path) is None
    assert gitinfo.head_for(None) is None
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "trunk")
    assert gitinfo.read_head(fresh) == gitinfo.GitHead(branch="trunk", commit=None)
