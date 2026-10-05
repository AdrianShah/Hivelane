"""Current branch and HEAD commit of a repo, read from .git directly.

No git subprocess: the daemon asks on every edit check. Handles worktrees and
submodules (where `.git` is a file pointing at the real git dir, with refs in
the shared `commondir`), detached HEAD, loose and packed refs. Cached per repo
root and re-read when `HEAD`'s mtime changes, checked at most every
CHECK_INTERVAL seconds.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CHECK_INTERVAL = 2.0


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
    except OSError:
        return None
    return None


def common_dir(gdir: Path) -> Path:
    try:
        text = (gdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return gdir
    path = Path(text)
    return path if path.is_absolute() else (gdir / path).resolve()


def _resolve_ref(gdir: Path, cdir: Path, ref: str, depth: int = 0) -> Optional[str]:
    if depth > 5:
        return None
    for base in (gdir, cdir):
        try:
            text = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
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
    except OSError:
        pass
    return None


def read_head(root: Path) -> Optional[GitHead]:
    gdir = git_dir(root)
    if gdir is None:
        return None
    try:
        head = (gdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    cdir = common_dir(gdir)
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
        head = (gdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            # A commit moves the branch ref, not HEAD.
            ref = head[4:].strip()
            for base in (gdir, common_dir(gdir)):
                try:
                    total += os.stat(base / ref).st_mtime_ns
                    break
                except OSError:
                    continue
    except OSError:
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
