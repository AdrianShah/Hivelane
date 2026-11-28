"""Current branch and HEAD commit of a repo, read from .git directly.

No git subprocess: the daemon asks on every edit check. Handles worktrees and
submodules (where `.git` is a file pointing at the real git dir, with refs in
the shared `commondir`), detached HEAD, loose and packed refs. Cached per repo
root and re-read when `HEAD`'s mtime changes, checked at most every
CHECK_INTERVAL seconds.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

CHECK_INTERVAL = 2.0
REPO_ID_LENGTH = 16
LOOKUP_KEY_PREFIX = "hivelane-lookup:"

# scp-like git address: [user@]host:path. The host must be longer than one
# character so a Windows drive path (C:\repos\x) is not mistaken for one.
_SCP_LIKE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^@/:\\]{2,}):(?P<path>[^\\].*)$")


@dataclass(frozen=True)
class GitHead:
    branch: Optional[str]  # None when detached
    commit: Optional[str]  # None in a repo with no commits


def git_dir(root: Path) -> Optional[Path]:
    dot_git = root / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                target = Path(text[len("gitdir:") :].strip())
                if not target.is_absolute():
                    target = (root / target).resolve()
                return target
    except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
        return None
    return None


def common_dir(gdir: Path) -> Path:
    try:
        text = (gdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
        return gdir
    path = Path(text)
    return path if path.is_absolute() else (gdir / path).resolve()


def _resolve_ref(gdir: Path, cdir: Path, ref: str, depth: int = 0) -> Optional[str]:
    if depth > 5:
        return None
    for base in (gdir, cdir):
        try:
            text = (base / ref).read_text(encoding="utf-8").strip()
        except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
            continue
        if text.startswith("ref:"):
            return _resolve_ref(gdir, cdir, text[4:].strip(), depth + 1)
        return text or None
    try:
        with open(cdir / "packed-refs", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(("#", "^")):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
        pass
    return None


REFTABLE_HEAD = "ref: refs/heads/.invalid"


def _git_cli_head(root: Path) -> Optional[GitHead]:
    def run(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=3, check=False,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        out = done.stdout.strip()
        return out if done.returncode == 0 and out else None

    commit = run("rev-parse", "--verify", "-q", "HEAD")
    branch = run("symbolic-ref", "-q", "--short", "HEAD")
    return GitHead(branch=branch, commit=commit)


def read_head(root: Path) -> Optional[GitHead]:
    gdir = git_dir(root)
    if gdir is None:
        return None
    try:
        head = (gdir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
        return None
    cdir = common_dir(gdir)
    if head == REFTABLE_HEAD or (cdir / "reftable").is_dir():
        # git >= 2.45 with extensions.refStorage=reftable: refs live in a
        # binary table, and HEAD is a stub. Ask git (rare, and cached).
        return _git_cli_head(root)
    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        return GitHead(branch=branch, commit=_resolve_ref(gdir, cdir, ref))
    return GitHead(branch=None, commit=head or None)


class _Entry:
    __slots__ = ("head", "mtime", "checked")

    def __init__(self, head: Optional[GitHead], mtime: float, checked: float) -> None:
        self.head = head
        self.mtime = mtime
        self.checked = checked


_cache: dict[str, _Entry] = {}
_lock = threading.Lock()


def _head_mtime(root: Path) -> float:
    gdir = git_dir(root)
    if gdir is None:
        return -1.0
    total = 0.0
    try:
        total = os.stat(gdir / "HEAD").st_mtime_ns
        try:  # reftable: every ref update rewrites tables.list
            total += os.stat(common_dir(gdir) / "reftable" / "tables.list").st_mtime_ns
        except OSError:
            pass
        head = (gdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            # A commit moves the branch ref, not HEAD.
            ref = head[4:].strip()
            for base in (gdir, common_dir(gdir)):
                try:
                    total += os.stat(base / ref).st_mtime_ns
                    break
                except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
                    continue
    except (OSError, ValueError):  # ValueError: a non-UTF-8 ref file
        pass
    return float(total)


def head_for(root: Optional[Path], now: Optional[float] = None) -> Optional[GitHead]:
    """Cached GitHead for root (None outside a repo)."""
    if root is None:
        return None
    key = str(root)
    now = time.monotonic() if now is None else now
    with _lock:
        entry = _cache.get(key)
        if entry is not None and now - entry.checked < CHECK_INTERVAL:
            return entry.head
    mtime = _head_mtime(root)
    with _lock:
        entry = _cache.get(key)
        if entry is not None and entry.mtime == mtime:
            entry.checked = now
            return entry.head
    head = read_head(root)
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = _Entry(head, mtime, now)
    return head


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def normalize_remote_url(url: str) -> str:
    """Reduce a git remote URL to host/path so every clone compares equal."""
    url = url.strip()
    if not url:
        return ""
    host = ""
    path = url
    if "://" in url:
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path
    else:
        match = _SCP_LIKE.match(url)
        if match:
            host = match["host"].lower()
            path = match["path"]
    path = path.replace("\\", "/").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")].rstrip("/")
    return f"{host}/{path}" if host else path


def _run_git(root: Path, *args: str) -> str:
    """stdout of a git command run in root, or "" on failure."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=3,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def is_tracked(root: Path, rel: str) -> Optional[bool]:
    """Whether git tracks root/rel, so every checkout and linked worktree of
    the repo has it; None when git cannot say (not a repo, no git)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel],
            capture_output=True, text=True, timeout=3, check=False,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return True
    return False if "did not match" in done.stderr else None


def compute_repo_id(root: Path) -> str:
    """Identity of the repository at root, shared by every clone of it."""
    origin = normalize_remote_url(_run_git(root, "remote", "get-url", "origin"))
    if origin:
        return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:REPO_ID_LENGTH]
    roots = _run_git(root, "rev-list", "--max-parents=0", "HEAD").split()
    if roots:
        return min(roots)[:REPO_ID_LENGTH]
    return ""


def repo_lookup_key(repo_id: str) -> str:
    """One-way relay lookup key derived from a repository id.

    Uses a domain-separated SHA-256 digest instead of the raw remote URL (or
    the daemon's per-room wire id) so the relay can match clones without ever
    seeing the repository address.
    """
    return hashlib.sha256(f"{LOOKUP_KEY_PREFIX}{repo_id}".encode("utf-8")).hexdigest()
