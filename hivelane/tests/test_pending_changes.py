"""Pending (unpushed) work on the daemon side: git collection
(hivelane/pending.py), the pending_changes report a connection sends (only
when it changed, sealed in an e2e room) and the pull hint."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hivelane import daemon as daemon_module
from hivelane import gitinfo
from hivelane import pending as pending_work
from hivelane.daemon import Daemon, IgnoreRules, RoomConnection
from hivelane.events import NormalizedEvent

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


PY_SOURCE = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"


@pytest.fixture
def cloned(tmp_path: Path) -> tuple[Path, Path]:
    """(remote, clone): a bare origin with one pushed commit on main, and a
    clone tracking origin/main."""
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text(PY_SOURCE)
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "one")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(remote))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    gitinfo.clear_cache()
    return remote, clone


# --- parsing ---------------------------------------------------------------------


def test_parse_status_keeps_new_paths_and_skips_untracked_dirs() -> None:
    output = " M a.py\0R  new.py\0old.py\0?? notes.txt\0?? build/\0D  gone.py\0"
    assert pending_work.parse_status(output) == ["a.py", "new.py", "notes.txt", "gone.py"]


def test_parse_log_takes_each_files_oldest_commit() -> None:
    output = "\x00300\n\na.py\nb.py\n\x00200\n\na.py\n"
    assert pending_work.parse_log(output) == {"a.py": 200.0, "b.py": 300.0}


def test_parse_diff_ranges_reads_new_file_positions() -> None:
    output = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -2 +2,2 @@ def alpha():\n-x\n+y\n+z\n@@ -9,3 +10,0 @@\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
    )
    assert pending_work.parse_diff_ranges(output) == {"app.py": [(2, 3), (10, 10)]}


# --- scanning a real repo ------------------------------------------------------------


@needs_git
def test_scan_reports_uncommitted_and_unpushed_files_with_symbols(cloned: tuple[Path, Path]) -> None:
    _, clone = cloned
    (clone / "app.py").write_text(PY_SOURCE.replace("return 2", "return 3"))
    git(clone, "commit", "-q", "-am", "beta three")
    (clone / "README.md").write_text("hello again\n")
    (clone / "new.txt").write_text("draft\n")

    snapshot = pending_work.PendingScanner().scan(clone, branch="main")

    assert snapshot is not None
    assert snapshot.base == "origin/main"
    assert snapshot.has_upstream
    files = {path: names for path, _, names in snapshot.files}
    assert set(files) == {"app.py", "README.md", "new.txt"}
    assert files["app.py"] == ["beta"]
    assert all(since > 0 for _, since, _ in snapshot.files)
    assert snapshot.behind == 0


@needs_git
def test_a_clean_pushed_repo_has_nothing_pending(cloned: tuple[Path, Path]) -> None:
    _, clone = cloned
    snapshot = pending_work.PendingScanner().scan(clone, branch="main")
    assert snapshot is not None and snapshot.files == []


@needs_git
def test_without_an_upstream_the_default_branch_merge_base_is_used(cloned: tuple[Path, Path]) -> None:
    _, clone = cloned
    git(clone, "checkout", "-q", "-b", "feature")  # no upstream configured
    (clone / "README.md").write_text("feature work\n")
    git(clone, "commit", "-q", "-am", "feature")

    snapshot = pending_work.PendingScanner().scan(clone, branch="feature")

    assert snapshot is not None
    assert snapshot.base == "origin/main"
    assert not snapshot.has_upstream
    assert [path for path, _, _ in snapshot.files] == ["README.md"]


@needs_git
def test_a_repo_with_no_remote_still_reports_uncommitted_work(tmp_path: Path) -> None:
    root = tmp_path / "solo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "one")
    (root / "a.txt").write_text("b\n")

    snapshot = pending_work.PendingScanner().scan(root, branch="main")

    assert snapshot is not None
    assert snapshot.base == ""
    assert [path for path, _, _ in snapshot.files] == ["a.txt"]


def test_outside_a_repo_there_is_no_snapshot(tmp_path: Path) -> None:
    assert pending_work.PendingScanner().scan(tmp_path) is None


@needs_git
def test_first_seen_time_is_kept_across_scans_and_reset_once_clean(cloned: tuple[Path, Path]) -> None:
    _, clone = cloned
    clock = [5_000_000_000.0]
    scanner = pending_work.PendingScanner(clock=lambda: clock[0])
    (clone / "README.md").write_text("x\n")
    first = scanner.scan(clone)
    assert first is not None
    since = first.files[0][1]

    os.utime(clone / "README.md", None)  # edited again: still pending since then
    second = scanner.scan(clone)
    assert second is not None and second.files[0][1] == since

    git(clone, "checkout", "--", "README.md")
    assert scanner.scan(clone).files == []  # type: ignore[union-attr]


@needs_git
def test_ignored_files_and_the_cap(cloned: tuple[Path, Path]) -> None:
    _, clone = cloned
    for n in range(5):
        (clone / f"f{n}.txt").write_text("x\n")
    (clone / "secret.env").write_text("x\n")

    snapshot = pending_work.PendingScanner().scan(
        clone, ignore=IgnoreRules(["*.env"]).matches, max_files=3
    )

    assert snapshot is not None
    assert len(snapshot.files) == 3
    assert snapshot.truncated
    assert all(not path.endswith(".env") for path, _, _ in snapshot.files)


@needs_git
def test_upstream_commits_not_pulled_are_noticed_after_a_fetch(
    cloned: tuple[Path, Path], tmp_path: Path
) -> None:
    remote, clone = cloned
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "app.py").write_text(PY_SOURCE + "\n\ndef gamma():\n    return 4\n")
    git(other, "commit", "-q", "-am", "gamma")
    git(other, "push", "-q", "origin", "main")
    git(clone, "fetch", "-q")

    snapshot = pending_work.PendingScanner().scan(clone, branch="main")

    assert snapshot is not None
    assert snapshot.behind == 1
    assert snapshot.upstream_files == ["app.py"]


# --- the report a connection sends -------------------------------------------------


def _live(conn: RoomConnection) -> RoomConnection:
    conn.connected = True
    conn.connected_since = 1000.0
    return conn


def _snapshot(*files: tuple[str, float, list[str]], branch: str = "main") -> pending_work.PendingSnapshot:
    return pending_work.PendingSnapshot(branch=branch, base="origin/main", has_upstream=True, files=list(files))


def _sent(conn: RoomConnection) -> list[dict]:
    out = []
    while not conn._outbox.empty():
        out.append(json.loads(conn._outbox.get_nowait()))
    return out


def test_report_is_sent_only_when_it_changes() -> None:
    async def scenario() -> None:
        conn = _live(RoomConnection("TEAM42", "adrian"))
        snap = _snapshot(("web/Header.tsx", 100.0, ["renderNav"]))

        assert conn.report_pending("repo", snap, now=0.0)
        frames = _sent(conn)
        assert [f["type"] for f in frames] == ["pending_changes"]
        assert frames[0]["files"] == [{"path": "web/Header.tsx", "symbols": ["renderNav"], "since": 100.0}]
        assert frames[0]["base"] == "origin/main"

        assert not conn.report_pending("repo", snap, now=10.0)
        # A refresh after a while, and at once on a new link.
        assert conn.report_pending("repo", snap, now=10.0 + daemon_module.PENDING_REFRESH_SECONDS)
        conn.connected_since = 2000.0
        assert conn.report_pending("repo", snap, now=11.0 + daemon_module.PENDING_REFRESH_SECONDS)
        # Everything pushed: an empty report clears the relay's copy.
        assert conn.report_pending("repo", _snapshot(), now=12.0 + daemon_module.PENDING_REFRESH_SECONDS)
        assert _sent(conn)[-1]["files"] == []

    asyncio.run(scenario())


def test_nothing_to_report_sends_nothing_and_offline_sends_nothing() -> None:
    async def scenario() -> None:
        conn = _live(RoomConnection("TEAM42", "adrian"))
        assert not conn.report_pending("repo", _snapshot(), now=0.0)
        offline = RoomConnection("TEAM42", "adrian")
        assert not offline.report_pending("repo", _snapshot(("a.py", 1.0, [])), now=0.0)
        assert _sent(conn) == [] and _sent(offline) == []

    asyncio.run(scenario())


def test_e2e_report_hashes_names_and_seals_the_real_ones() -> None:
    crypto = pytest.importorskip("hivelane.crypto")
    pytest.importorskip("cryptography")
    key = crypto.derive("correct horse battery staple pending", "TEAM42")

    async def scenario() -> None:
        conn = _live(RoomConnection("TEAM42", "adrian", e2e_key=key))
        snap = _snapshot(("web/Header.tsx", 5.0, ["renderNav"]), ("package.json", 6.0, ["deps"]), branch="feat/x")
        assert conn.report_pending("repo", snap, now=0.0)
        frame = _sent(conn)[0]
        assert "web/Header.tsx" not in json.dumps({k: v for k, v in frame.items() if k != "e2e"})
        assert frame["files"][0]["path"] == key.hash("web/Header.tsx")
        assert frame["files"][0]["symbols"] == [key.hash("renderNav")]
        # Config files: nothing finer than the file for the relay.
        assert frame["files"][1]["symbols"] == []
        assert frame["branch"] == key.hash("feat/x")
        kind, envelope = key.open(frame["e2e"])
        assert kind == "pending"
        assert envelope["files"][0] == {"path": "web/Header.tsx", "symbols": ["renderNav"]}
        assert envelope["branch"] == "feat/x"

    asyncio.run(scenario())


def test_an_oversized_report_slims_down_instead_of_vanishing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_module, "MAX_FRAME_BYTES", 6000)

    async def scenario() -> None:
        conn = _live(RoomConnection("TEAM42", "adrian"))
        files = [(f"dir/file_{n:03}.py", 1.0, [f"symbol_{i}" for i in range(10)]) for n in range(150)]
        assert conn.report_pending("repo", _snapshot(*files), now=0.0)
        frame = _sent(conn)[0]
        assert len(json.dumps(frame)) <= 6000
        assert frame["truncated"]
        assert 0 < len(frame["files"]) < 150

    asyncio.run(scenario())


def test_pull_note_once_per_file_and_upstream_state() -> None:
    conn = RoomConnection("TEAM42", "adrian")
    behind = pending_work.PendingSnapshot(base="origin/main", behind=2, upstream_files=["app.py"])
    conn.report_pending("repo", behind)

    note = conn.pull_note("repo", "app.py")
    assert note is not None and "origin/main has 2 commits" in note and "app.py" in note
    assert conn.pull_note("repo", "app.py") is None
    assert conn.pull_note("repo", "other.py") is None

    conn.report_pending("repo", pending_work.PendingSnapshot(base="origin/main", behind=3, upstream_files=["app.py"]))
    assert conn.pull_note("repo", "app.py") is not None
    conn.report_pending("repo", pending_work.PendingSnapshot(base="origin/main"))
    assert conn.pull_note("repo", "app.py") is None


# --- the daemon's scan -----------------------------------------------------------------


@needs_git
def test_daemon_scan_reports_each_live_repo(cloned: tuple[Path, Path]) -> None:
    _, clone = cloned
    (clone / "README.md").write_text("changed\n")

    async def scenario() -> list[dict]:
        daemon = Daemon("TEAM42", "adrian", port=4697, root=str(clone))
        conn = _live(daemon.connection)
        assert await daemon.scan_pending() == 1
        assert await daemon.scan_pending() == 0  # unchanged
        return _sent(conn)

    frames = asyncio.run(scenario())
    assert [f["type"] for f in frames] == ["pending_changes"]
    assert [f["path"] for f in frames[0]["files"]] == ["README.md"]
    assert frames[0]["branch"] == "main"


@needs_git
def test_post_edit_asks_for_an_early_scan_and_pre_edit_gets_the_pull_hint(
    cloned: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, clone = cloned
    monkeypatch.setattr(daemon_module, "PENDING_ENABLED", True)

    async def scenario() -> tuple[bool, str]:
        daemon = Daemon("TEAM42", "adrian", port=4696, root=str(clone))
        daemon._pending_task = asyncio.create_task(asyncio.sleep(3600))
        try:
            await daemon.handle_event(
                NormalizedEvent(
                    agent="claude-code", kind="post_edit", session_id="s", cwd=str(clone),
                    file_path=str(clone / "README.md"), old_content="hello\n", new_content="hi\n",
                )
            )
            asked = daemon._pending_due is not None
            daemon.connection.report_pending(
                daemon.connection.repo_id or await daemon.connection.repo_id_for_cwd(str(clone), daemon._default),  # type: ignore[arg-type]
                pending_work.PendingSnapshot(base="origin/main", behind=1, upstream_files=["README.md"]),
            )
            ack = await daemon.handle_event(
                NormalizedEvent(
                    agent="claude-code", kind="pre_edit", session_id="s", cwd=str(clone),
                    file_path=str(clone / "README.md"), old_content="hi\n",
                )
            )
            return asked, ack.reason
        finally:
            daemon._pending_task.cancel()

    asked, reason = asyncio.run(scenario())
    assert asked
    assert "you have not pulled that change README.md" in reason
