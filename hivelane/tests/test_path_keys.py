"""Path keys must be identical for every agent in the room, whatever their cwd."""

from __future__ import annotations

from pathlib import Path

from hivelane.daemon import repo_relative_path, repo_root


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "room.py").write_text("x\n", encoding="utf-8")
    return tmp_path


def test_same_file_keys_identically_from_different_cwds(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    target = str(root / "server" / "room.py")

    from_root = repo_relative_path(target, str(root))
    from_subdir = repo_relative_path(target, str(root / "server"))

    assert from_root == from_subdir == "server/room.py"


def test_worktree_git_file_is_recognised(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    assert repo_root(str(tmp_path)) == tmp_path.resolve()


def test_outside_a_repo_falls_back_to_cwd_relative(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    assert repo_relative_path(str(tmp_path / "a.py"), str(tmp_path)) == "a.py"
