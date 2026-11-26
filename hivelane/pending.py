"""Which files the local user changed but has not pushed yet.

The relay's touch window is about a minute: it knows who is typing now, not
whose work is still unmerged. This module answers the second question from
cheap, bounded git calls in one repo:

    pending = uncommitted working-tree changes (git status)
            + files of the commits ahead of the upstream branch
              (or, with no upstream, of the merge-base with the default
              branch: origin/HEAD, origin/main, origin/master)

with, where it is affordable, the definitions each file's unpushed changes
touch (one `git diff -U0 <merge-base>` for the hunk positions, the daemon's
own symbol extraction for the names; cached per file version). A file is
"pending since" its oldest unpushed commit, or since the daemon first saw it
changed (its mtime on first sight).

It also notices when the upstream branch has commits the local branch does
not (someone pushed, and a fetch brought them in), and which files those
commits change: the daemon turns an edit of one of them into a pull hint.
No fetch is ever run: that needs the network and credentials, and is the
user's (or their IDE's autofetch's) business.

Every git call has a timeout and every list a cap; any failure degrades to
less information (files without symbols, no base), never to an exception.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from hivelane import symbols

GIT_TIMEOUT = 3.0
# Files per report (the relay keeps the first 200 of a longer list too).
MAX_FILES = 200
# Commits ahead of the base read for file names; older ones still count via
# the diff below, they just do not set `since`.
MAX_COMMITS = 500
# Symbol extraction: at most this many files parsed per scan (cache misses),
# and none when the unpushed diff is bigger than this (a huge refactor is
# reported file by file).
MAX_SYMBOL_FILES = 60
MAX_DIFF_BYTES = 2 * 1024 * 1024
SYMBOL_CACHE_SIZE = 512
MAX_SYMBOLS_PER_FILE = 32
# Upstream files a pull hint can name.
MAX_UPSTREAM_FILES = 500

# Remote branches only: a local main says nothing about what teammates have.
_DEFAULT_BASES = ("origin/main", "origin/master", "upstream/main", "upstream/master")


@dataclass
class PendingSnapshot:
    """One scan's answer. files: (path, since, symbols), oldest first."""

    branch: str = ""
    base: str = ""
    # True when base is the branch's configured upstream (a pull fetches it).
    has_upstream: bool = False
    files: list[tuple[str, float, list[str]]] = field(default_factory=list)
    truncated: bool = False
    # Commits the base has that HEAD does not, and the files they change.
    behind: int = 0
    upstream_files: list[str] = field(default_factory=list)

    def signature(self) -> tuple:
        """What a report says, minus timestamps: equal signatures need not
        be sent again."""
        return (
            self.branch,
            self.base,
            self.truncated,
            tuple((path, tuple(names)) for path, _, names in self.files),
        )


Runner = Callable[..., Optional[str]]


def run_git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> Optional[str]:
    """stdout of `git -C root args`, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace")


def _first_line(text: Optional[str]) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def find_base(root: Path, git: Runner = run_git) -> tuple[str, bool]:
    """(ref the local work is measured against, whether it is the branch's
    upstream). ("", False) when there is nothing to compare with."""
    upstream = _first_line(git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"))
    if upstream:
        return upstream, True
    remote_head = _first_line(git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"))
    if remote_head:
        return remote_head, False
    for candidate in _DEFAULT_BASES:
        if git(root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}") is not None:
            return candidate, False
    return "", False


def parse_status(output: str) -> list[str]:
    """Paths from `git status --porcelain=v1 -z` (renames: the new path)."""
    paths: list[str] = []
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # the source path follows
        if path.endswith("/"):
            continue  # an untracked directory: nothing to name
        paths.append(path)
    return paths


def parse_log(output: str) -> dict[str, float]:
    """path -> commit time of its oldest commit in `git log --format=%x00%ct
    --name-only` output (newest commit first)."""
    oldest: dict[str, float] = {}
    when = 0.0
    for line in output.splitlines():
        if line.startswith("\0"):
            try:
                when = float(line[1:].strip())
            except ValueError:
                when = 0.0
            continue
        path = line.strip()
        if path:
            oldest[path] = when  # later lines are older commits
    return oldest


def parse_diff_ranges(output: str) -> dict[str, list[tuple[int, int]]]:
    """path -> new-file line ranges from `git diff -U0` hunk headers. A pure
    deletion maps to the line before it (or line 1)."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: Optional[str] = None
    for line in output.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
            continue
        if current is None or not line.startswith("@@"):
            continue
        try:
            new = line.split("+", 1)[1].split(" ", 1)[0]
            start_text, _, count_text = new.partition(",")
            start = int(start_text)
            count = int(count_text) if count_text else 1
        except (IndexError, ValueError):
            continue
        end = start + count - 1 if count > 0 else start
        ranges.setdefault(current, []).append((max(1, start), max(1, end)))
    return ranges


class PendingScanner:
    """Scans one repo; remembers when each file was first seen pending and
    the symbols of each file version, so a rescan costs a few git calls."""

    def __init__(self, git: Runner = run_git, clock: Callable[[], float] = time.time) -> None:
        self._git = git
        self._clock = clock
        self._first_seen: dict[str, float] = {}
        self._symbols: OrderedDict[tuple[str, int, int, tuple], list[str]] = OrderedDict()

    def scan(
        self,
        root: Path,
        branch: str = "",
        ignore: Optional[Callable[[str], bool]] = None,
        max_files: int = MAX_FILES,
    ) -> Optional[PendingSnapshot]:
        """The repo's pending work, or None when root is not a git work tree."""
        git = self._git
        status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
        if status is None:
            return None
        now = self._clock()
        snapshot = PendingSnapshot(branch=branch)
        base, has_upstream = find_base(root, git)
        merge_base = _first_line(git(root, "merge-base", "HEAD", base)) if base else ""
        if base and not merge_base:
            base = ""  # unrelated histories, or no commits yet
        snapshot.base, snapshot.has_upstream = base, has_upstream and bool(base)

        committed: dict[str, float] = {}
        if merge_base:
            log = git(
                root, "log", f"--max-count={MAX_COMMITS}", "--format=%x00%ct", "--name-only",
                "--no-renames", f"{merge_base}..HEAD",
            )
            committed = parse_log(log or "")
            behind = _first_line(git(root, "rev-list", "--count", f"HEAD..{base}"))
            snapshot.behind = int(behind) if behind.isdigit() else 0
            if snapshot.behind:
                upstream = git(root, "diff", "--name-only", "--no-renames", merge_base, base)
                snapshot.upstream_files = [
                    p for p in (upstream or "").splitlines() if p
                ][:MAX_UPSTREAM_FILES]

        dirty = parse_status(status)
        seen: dict[str, float] = {}
        for path in dirty:
            if path in seen:
                continue
            first = self._first_seen.get(path)
            if first is None:
                try:
                    first = min(now, os.stat(root / path).st_mtime)
                except OSError:
                    first = now
            seen[path] = first
        # Forget files no longer dirty (committed, reverted): a file dirtied
        # again later is pending from then.
        self._first_seen = dict(seen)
        for path, when in committed.items():
            seen[path] = min(seen.get(path, when), when) if when else seen.get(path, now)

        paths = [p for p in seen if not (ignore and ignore(p))]
        paths.sort(key=lambda p: (seen[p], p))
        if len(paths) > max_files:
            snapshot.truncated = True
            paths = paths[:max_files]
        names = self._symbols_for(root, merge_base, paths, set(dirty))
        snapshot.files = [(p, seen[p], names.get(p, [])) for p in paths]
        return snapshot

    def _symbols_for(
        self, root: Path, merge_base: str, paths: list[str], dirty: set[str]
    ) -> dict[str, list[str]]:
        """path -> definitions its unpushed changes touch, for the paths the
        symbol extractor knows a language for; best effort."""
        wanted = [p for p in paths if symbols.language_for(p)]
        if not wanted:
            return {}
        against = merge_base or "HEAD"
        diff = self._git(
            root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", against, "--", *wanted,
        )
        if diff is None or len(diff) > MAX_DIFF_BYTES:
            return {}
        ranges = parse_diff_ranges(diff)
        out: dict[str, list[str]] = {}
        parsed = 0
        for path in wanted:
            hunks = ranges.get(path)
            if not hunks:
                continue  # untracked or deleted: judged as a whole file
            full = root / path
            try:
                stat = full.stat()
            except OSError:
                continue
            if stat.st_size > symbols.MAX_SOURCE_BYTES:
                continue
            key = (path, stat.st_mtime_ns, stat.st_size, tuple(hunks))
            cached = self._symbols.get(key)
            if cached is None:
                if parsed >= MAX_SYMBOL_FILES:
                    continue
                parsed += 1
                try:
                    source = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                try:
                    cached = symbols.symbols_for_ranges(path, source, hunks)[:MAX_SYMBOLS_PER_FILE]
                except Exception:  # noqa: BLE001 - an extractor bug must not lose the report
                    cached = []
                self._symbols[key] = cached
                while len(self._symbols) > SYMBOL_CACHE_SIZE:
                    self._symbols.popitem(last=False)
            out[path] = list(cached)
        return out
