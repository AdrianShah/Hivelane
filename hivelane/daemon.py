"""The Hivelane daemon: one asyncio process per machine that is both a relay
room member for every repo it coordinates and a loopback HTTP endpoint for the
agent hooks running in those repos.

Each repo, keyed by its root path, is bound to a RoomConnection: one WebSocket
to one relay room under one display name. Hooks only report their cwd, so the
HTTP side routes every event to the repo containing that cwd, registering a
repo on first sight when it carries a `.hivelane.toml`.

`Daemon(room, name)` is the single-room form behind `hivelane join`: one
connection that every event falls through to whatever its cwd, as before
there was more than one repo to serve. `Daemon()` is the per-machine form
behind `hivelane serve` / the background service.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import functools
import getpass
import hashlib
import hmac
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from websockets.asyncio.client import connect as ws_connect

from hivelane import __version__, auth, crypto, gitinfo, redact, symbols
from hivelane.depgraph import DepTracker
from hivelane.config import (
    ConfigFileError,
    HivelaneConfig,
    load_config,
    room_key_env_name,
    transport_problem,
    unconfirmed_project_optouts,
)
from hivelane.events import EventAck, NormalizedEvent
from hivelane.protocol import (
    AGENT_KINDS,
    E2E_MAX_CHARS,
    PROTOCOL_VERSION,
    ActivityMessage,
    AgentKind,
    EditCheckRequest,
    EditCheckResponse,
    FileChange,
    FileRequest,
    IntentMessage,
    JoinDecision,
    JoinMessage,
    PeerTouch,
    PlanConflicts,
    PlanMessage,
    RoomPolicy,
    RoomSettings,
)

from hivelane.constants import DEFAULT_SERVER

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 4600

BATCH_SECONDS = 1.5
EDIT_CHECK_TIMEOUT = 1.5
MAX_BACKOFF = 30.0
SHUTDOWN_GRACE = 5.0
# How long a stopping connection waits for its outbox to reach the relay
# before closing the socket anyway.
FLUSH_GRACE = 1.0
OLD_CONTENT_CACHE_SIZE = 512
# Total characters of whole-file contents the pre_edit cache may hold; files
# larger than symbols.MAX_SOURCE_BYTES are not cached at all (they would get
# neither symbols nor a diff anyway).
OLD_CONTENT_BUDGET = 32 * 1024 * 1024

# Size caps, kept under the relay's (server/config.py) so it never has to cut
# or refuse what we send. A frame over the relay's 512 KB closes the socket
# (1009), so frames are kept well under it.
MAX_FRAME_BYTES = 400 * 1024
# One file's diff (the relay keeps 64 KB); longer ones are truncated, with
# TRUNCATED_MARKER appended.
MAX_DIFF_BYTES = 60 * 1024
# A prompt or plan summary (the relay keeps 4 KB).
MAX_TEXT_BYTES = 4 * 1024
# Files per activity message (the relay keeps 50).
MAX_FILES_PER_MESSAGE = 50
# Above this, old+new content is not diffed at all (difflib is roughly
# quadratic): the change goes out path-only, still detected as an overlap.
MAX_DIFF_INPUT_BYTES = 512 * 1024
TRUNCATED_MARKER = chr(10) + "[... truncated by hivelane]"
# The relay drops a member's activity, intent or plan sent less than 0.5 s
# after the previous one of the same kind (server/main.py
# ACTIVITY_MIN_INTERVAL_SECONDS); sends of those kinds are spaced by this.
PACED_KINDS = ("activity", "intent", "plan")
SEND_SPACING = 0.6
# A connection only counts as stable (and the reconnect backoff resets) after
# being admitted for this long; a relay that accepts then drops is backed off.
STABLE_SECONDS = 30.0
BACKOFF_JITTER = 0.2
REPO_ROOT_CACHE_SIZE = 64
ROUTE_CACHE_SIZE = 256
# Ordinary (non-priority) messages kept for the relay while it is unreachable,
# per room connection; the oldest is dropped beyond it. HIVELANE_OUTBOX_LIMIT
# overrides it, 0 meaning unlimited (a long outage then grows memory).
DEFAULT_OUTBOX_LIMIT = 500


def _outbox_limit(raw: Optional[str]) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_OUTBOX_LIMIT
    return value if value >= 0 else DEFAULT_OUTBOX_LIMIT


OUTBOX_LIMIT = _outbox_limit(os.environ.get("HIVELANE_OUTBOX_LIMIT"))
# Hex characters of repo identity sent on the wire: 64 bits is ample to keep
# the handful of repos in one room apart, and short enough to read in logs.
REPO_ID_LENGTH = 16
GIT_TIMEOUT = 3.0
# A cwd with no project config is re-checked at most this often, so repos that
# never opted in do not pay a filesystem walk (and a TOML parse) on every edit,
# while one that just ran `hivelane init` is picked up within half a minute.
NEGATIVE_CACHE_SECONDS = 30.0
# An auto-registered repo whose room turned it away (room not created yet,
# name taken by a session that has since left) is retried after this long.
FATAL_RETRY_SECONDS = 300.0
# Auto-derived display names are capped at the relay's own limit (it strips
# spaces and lowercases, then rejects anything over 20 characters), so a long
# git user.name degrades to a truncated name rather than a rejected one.
NAME_MAX_LENGTH = 20
# Symbol extraction on content larger than this runs in a worker thread.
INLINE_SYMBOLS_BYTES = 40_000
# `git ls-files` (plan candidate resolution) is re-run at most this often.
LS_FILES_SECONDS = 60.0
MAX_PLAN_FILES = 50
# h:-token -> real name memory for an e2e room (own sends, teammates' blobs).
E2E_NAME_CACHE_SIZE = 4096
# Headroom under the relay's blob limit for JSON and base64 overhead.
# v2 seal pads the plaintext (powers of two to 16 KiB, then 16 KiB steps),
# adding up to 16 KiB before base64 (~21.8 K characters).
E2E_BLOB_BUDGET = E2E_MAX_CHARS - 24 * 1024
# Diffs inside an envelope that would not fit are cut to this many characters.
E2E_DIFF_FALLBACK_CHARS = 4000
# A join request the owner has not answered is forgotten after this long (the
# relay gives up on it well before).
JOIN_REQUEST_TTL = 900.0
# Wait queues. Mirrors server/file_queue.py RELEASE_GRACE_SECONDS: how long
# the relay keeps a released file for us before offering it to the next in
# line, so a file_released older than this no longer means "go".
RELEASE_GRACE = 10.0
# A file_released note is still shown on the session's next pre_edit for this long.
RELEASE_NOTE_TTL = 60.0
# Files whose queue state (our place in line, a release) is remembered.
QUEUE_MEMORY = 256
# One POST /wait holds at most this long; `hivelane wait` polls in slices.
WAIT_SLICE_MAX = 30.0
_HASH_TOKEN = re.compile(r"h:[0-9a-f]{32}")

# Mirrors server/room.py _ALWAYS_WARN_BASENAMES: files the relay always judges
# at file level. In an e2e room it only sees a hashed path and cannot tell, so
# the daemon sends such a file's edits with no symbols or line ranges, which
# makes the relay's decision file-level for it too.
_ALWAYS_WARN_BASENAMES = (
    "package.json",
    "pyproject.toml",
    "requirements*.txt",
    "*.lock",
    "go.mod",
    "Cargo.toml",
    "tsconfig*.json",
    ".env*",
    "Dockerfile",
    ".hivelane.toml",
)


def is_always_warn_file(file_path: str) -> bool:
    import fnmatch

    parts = file_path.replace("\\", "/").split("/")
    basename = parts[-1]
    if any(fnmatch.fnmatchcase(basename, pattern) for pattern in _ALWAYS_WARN_BASENAMES):
        return True
    return ".github" in parts[:-1] and basename.endswith((".yml", ".yaml"))


def unified_diff(file_path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=2,
    )
    return "".join(lines).strip()


def truncate_utf8(text: str, limit: int, marker: str = TRUNCATED_MARKER) -> str:
    """text cut to at most limit UTF-8 bytes (marker included), on a
    character boundary; unchanged when it already fits."""
    if limit <= 0 or len(text) * 4 <= limit:
        return text
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    room = max(0, limit - len(marker.encode("utf-8")))
    return data[:room].decode("utf-8", "ignore") + marker


def clean_text(text: Optional[str]) -> Optional[str]:
    """text with lone surrogates (a JS agent's half-cut emoji) replaced, so it
    can be encoded: one unencodable prompt must not poison every later send."""
    if not text:
        return text
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return text


def capped_diff(file_path: str, old: str, new: str, redactor: Optional[redact.Redactor] = None) -> str:
    """unified_diff, redacted, then truncated to MAX_DIFF_BYTES; skipped ("")
    above MAX_DIFF_INPUT_BYTES of input. Redaction runs before the cut, so a
    secret straddling it cannot slip past half-matched. Blocking."""
    if len(old) + len(new) > MAX_DIFF_INPUT_BYTES:
        return ""
    diff = unified_diff(file_path, old, new)
    if diff and redactor is not None:
        # Secrets out, and a sensitive file's diff withheld entirely; the
        # path still goes, so overlaps on it are still detected.
        diff = redactor.diff(file_path, diff)
    return truncate_utf8(diff, MAX_DIFF_BYTES)


def _post_edit_work(
    rel: str,
    old: str,
    new: str,
    known_old: bool,
    share_diffs: bool,
    redactor: redact.Redactor,
    deps: DepTracker,
    repo_id: str,
) -> tuple[list[str], list[tuple[int, int]], str]:
    """(symbols, line ranges, diff) of a finished edit. Without a known old
    version every line would count as changed, which says nothing, so no
    symbols are sent; without share_diffs the content stays here (the path
    still goes out, so overlaps are still detected)."""
    names: list[str] = []
    ranges: list[tuple[int, int]] = []
    if known_old:
        names, ranges = change_symbols(rel, old, new)
        deps.record_local(rel, old, new, repo_id)
    diff = capped_diff(rel, old, new, redactor) if share_diffs else ""
    return names, ranges, diff


def relative_path(file_path: str, cwd: str) -> str:
    path = Path(file_path)
    try:
        return path.relative_to(Path(cwd)).as_posix()
    except ValueError:
        return path.as_posix()


_repo_roots: dict[str, Optional[Path]] = {}


def repo_root(start: str) -> Optional[Path]:
    """Nearest ancestor containing .git, or None outside a repo.

    Walked on the filesystem rather than shelled out to git: pre_edit is on the
    agent's blocking hook path. .git is a file in worktrees, so existence is the
    test, not is_dir.
    """
    if start in _repo_roots:
        return _repo_roots[start]
    root: Optional[Path] = None
    try:
        current = Path(start).resolve() if start else Path.cwd()
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                root = candidate
                break
    except OSError:
        root = None
    if len(_repo_roots) > REPO_ROOT_CACHE_SIZE:
        _repo_roots.clear()
    _repo_roots[start] = root
    return root


def repo_relative_path(
    file_path: str, cwd: str, root: Optional[Path] = None
) -> str:
    """Path key shared by every agent in the room.

    Keyed on the repo root, not the agent's cwd: teammates run their agents from
    different directories, and a cwd-relative key makes the same file look like
    two files, so no overlap is ever detected. An explicit root (the daemon's
    --root, or a registered repo's root) replaces discovery, which is the only
    shared anchor outside git.
    """
    if root is None:
        root = repo_root(cwd)
    if root is not None:
        try:
            return Path(file_path).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            pass
    return relative_path(file_path, cwd)


# --- repo identity -------------------------------------------------------------

# scp-like git address: [user@]host:path. The host must be longer than one
# character so a Windows drive path (C:\repos\x) is not mistaken for one.
_SCP_LIKE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^@/:\\]{2,}):(?P<path>[^\\].*)$")


def normalize_remote_url(url: str) -> str:
    """Reduce a git remote URL to host/path so every way of spelling one
    repository compares equal.

    Teammates clone the same repo over ssh and https, with and without
    credentials or a trailing .git; all of those must yield one identity, or
    the relay would treat one repo as two and never report their overlaps.
    Scheme, credentials and port are dropped and the host is lowercased; the
    path keeps its case, since some hosts treat it as significant.
    """
    url = url.strip()
    if not url:
        return ""
    host = ""
    path = url
    if "://" in url:
        parts = urlsplit(url)
        # .hostname drops credentials and port, and lowercases.
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


def _git(root: Path, *args: str) -> str:
    """stdout of a git command run in root, or "" on any failure (git missing,
    not a repo, no such remote, no commits yet)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
            check=False,
            # A daemon started without a console must not flash one per call.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


_ls_files: dict[str, tuple[float, list[str]]] = {}


def tracked_files(root: Optional[Path], now: Optional[float] = None) -> list[str]:
    """`git ls-files` of root (repo-relative, "/"-separated), cached for
    LS_FILES_SECONDS. [] outside a repo or when git fails."""
    if root is None:
        return []
    now = time.monotonic() if now is None else now
    key = str(root)
    cached = _ls_files.get(key)
    if cached is not None and now - cached[0] < LS_FILES_SECONDS:
        return cached[1]
    files = [line for line in _git(root, "ls-files").splitlines() if line]
    if len(_ls_files) > REPO_ROOT_CACHE_SIZE:
        _ls_files.clear()
    _ls_files[key] = (now, files)
    return files


def resolve_plan_files(candidates: Iterable[str], tracked: list[str]) -> list[str]:
    """Tracked paths a plan's path-like tokens name: an exact repo-relative
    path, or a path suffix ("room.py" -> "server/room.py", at most 3 matches).
    With no tracked list (not a git repo) candidates pass through as given."""
    out: list[str] = []
    tracked_set = set(tracked)
    for raw in candidates:
        cand = raw.replace("\\", "/")
        while cand.startswith("./"):
            cand = cand[2:]
        if not cand or cand.startswith("/") or ".." in cand.split("/"):
            continue
        if not tracked or cand in tracked_set:
            matches = [cand]
        else:
            matches = [t for t in tracked if t.endswith("/" + cand)][:3]
        for match in matches:
            if match not in out:
                out.append(match)
        if len(out) >= MAX_PLAN_FILES:
            break
    return out[:MAX_PLAN_FILES]


def edit_symbols(
    rel: str, old: Optional[str], hints: Optional[list[dict[str, str]]]
) -> tuple[list[str], list[tuple[int, int]]]:
    """Symbols and line ranges a pending edit is predicted to touch, from its
    old/new string hints against the file's current content."""
    if not old or not hints or len(old) > symbols.MAX_SOURCE_BYTES:
        return [], []
    ranges: list[tuple[int, int]] = []
    for hint in hints:
        ranges.extend(symbols.predict_ranges(old, hint.get("old") or ""))
    ranges = symbols.merge_ranges(ranges)
    if not ranges:
        return [], []
    return symbols.symbols_for_ranges(rel, old, ranges), ranges


def change_symbols(rel: str, old: str, new: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Symbols and new-file line ranges a finished edit changed."""
    if max(len(old), len(new)) > symbols.MAX_SOURCE_BYTES:
        return [], []
    ranges = symbols.changed_line_ranges(old, new)
    if not ranges:
        return [], []
    return symbols.symbols_for_ranges(rel, new, ranges), ranges[:200]


def compute_repo_id(root: Path) -> str:
    """Identity of the repository at root, shared by every clone of it.

    The normalized origin URL is hashed (the URL itself may name a private
    project, and only equality matters to the relay). Without an origin, the
    root commit is used: every clone of a history shares it. A repo with
    neither, or a directory that is not a repo at all, yields "", which the
    relay compares equal only to other empty ids, the pre-repo_id behaviour.

    Shells out to git, so callers go through repo_id_for's cache.
    """
    origin = normalize_remote_url(_git(root, "remote", "get-url", "origin"))
    if origin:
        return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:REPO_ID_LENGTH]
    roots = _git(root, "rev-list", "--max-parents=0", "HEAD").split()
    if roots:
        # A history can have several roots (merged unrelated histories); the
        # smallest is a choice every clone makes identically.
        return min(roots)[:REPO_ID_LENGTH]
    return ""


# str(root) -> (repo_id, the git metadata signature it was computed under).
_repo_ids: dict[str, tuple[str, tuple[int, ...]]] = {}


def _identity_signature(root: Path) -> tuple[int, ...]:
    """mtimes of the git files a repo identity derives from: .git/config
    (`git remote add origin` after `hivelane up` changes the id) and HEAD's
    reflog (the first commit gives an origin-less repo its id). A stat or
    two, cheap enough for the hook path."""
    git = root / ".git"
    out: list[int] = []
    for path in (git / "config", git / "logs" / "HEAD", git):
        try:
            out.append(path.stat().st_mtime_ns)
        except OSError:
            out.append(0)
    return tuple(out)


def cached_repo_id(root: Optional[Path]) -> Optional[str]:
    """The cached id for root, "" for no root, or None if not computed yet
    (or the repo's remote / first commit changed since)."""
    if root is None:
        return ""
    cached = _repo_ids.get(str(root))
    if cached is None:
        return None
    if cached[1] != _identity_signature(root):
        return None
    return cached[0]


def repo_id_for(root: Optional[Path]) -> str:
    """compute_repo_id, once per root until its git identity files change.

    The pre_edit hook path cannot afford a git subprocess per call, but a repo
    that gains an origin under a running daemon (`git init`, `hivelane up`,
    then `git remote add origin`) must switch to the id its teammates' clones
    compute, or the relay sees two repos and reports no overlaps.
    """
    if root is None:
        return ""
    cached = cached_repo_id(root)
    if cached is not None:
        return cached
    signature = _identity_signature(root)
    # Only a directory that is itself a repo root gets an identity. An explicit
    # --root that is not one must not inherit an enclosing repo's (a dotfiles
    # repo in $HOME, say): teammates would then get different ids for the same
    # project and stop seeing each other's overlaps.
    repo_id = compute_repo_id(root) if (root / ".git").exists() else ""
    if len(_repo_ids) > REPO_ROOT_CACHE_SIZE:
        _repo_ids.clear()
    _repo_ids[str(root)] = (repo_id, signature)
    return repo_id


# --- display names ---------------------------------------------------------------

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 _-]")
_RELAY_UNSAFE = re.compile(r"[^a-z0-9_-]")


def sanitize_name(raw: str) -> str:
    """A display name built from letters, digits, space, - and _ only.

    Applied to names we derive (git user.name, the OS login), never to one the
    user chose: those go to the relay as typed, and its verdict is reported.
    """
    name = " ".join(_NAME_UNSAFE.sub("", raw or "").split())
    return name[:NAME_MAX_LENGTH].strip()


def relay_user_id(name: str) -> str:
    """The user_id the relay will know `name` by (server/sanitize.py strips
    and lowercases), so the daemon can recognise itself in peer lists."""
    return _RELAY_UNSAFE.sub("", name.strip().lower())


def default_name(root: Optional[Path] = None) -> Optional[str]:
    """Display name for a repo whose config names nobody: git's user.name as
    seen from the repo (so per-repo identities win), else the OS login."""
    name = sanitize_name(_git(root or Path.cwd(), "config", "user.name"))
    if name:
        return name
    try:
        login = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name is an ordinary outcome
        login = ""
    return sanitize_name(login) or None


def discover_repo(cwd: str) -> Optional[tuple[HivelaneConfig, str]]:
    """The project config and display name for an unregistered cwd, or None
    when nothing there says which room to join.

    Blocking (filesystem walk, TOML parse, maybe `git config`): the daemon
    runs it in a worker thread.
    """
    try:
        config = load_config(cwd)
    except ConfigFileError as exc:
        print(f"hivelane: not coordinating {cwd}: {exc}", file=sys.stderr)
        return None
    if not config.room or config.root is None:
        return None
    # A repo the daemon finds on its own may be a fresh clone: its committed
    # file must not be able to turn off transport security or redaction for
    # data leaving this machine. `hivelane up` in the repo is the opt-in.
    optouts = unconfirmed_project_optouts(config)
    if optouts:
        print(
            f"hivelane: not coordinating {config.root}: its .hivelane.toml sets "
            f"{' and '.join(optouts)}; run `hivelane up` there to accept that",
            file=sys.stderr,
        )
        return None
    name = config.name or default_name(config.root)
    if not name:
        print(
            f"hivelane: not coordinating {config.root}: no display name "
            "(set `name` in ~/.hivelane/config.toml or HIVELANE_NAME)",
            file=sys.stderr,
        )
        return None
    return config, name


# --- ignore globs ----------------------------------------------------------------


def _glob_regex(pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                i += 2
                if pattern.startswith("/", i):
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = i + 1
            if end < n and pattern[end] in "!^":
                end += 1
            if end < n and pattern[end] == "]":
                end += 1
            end = pattern.find("]", end)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = pattern[i + 1 : end]
                if body and body[0] in "!^":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


class IgnoreRules:
    """Files a repo keeps out of coordination entirely (generated code,
    lockfiles, vendored trees): no edit_check before them, no activity after.

    Matched against the repo-relative POSIX path with the gitignore flavour
    people already write globs in: `*` and `?` stay within one path segment,
    `**` spans any number of them (and `**/` also matches none), a pattern
    without a slash matches at any depth, and a pattern that matches a
    directory covers everything under it.
    """

    def __init__(self, patterns: Iterable[str] = ()) -> None:
        kept: list[str] = []
        bodies: list[str] = []
        for raw in patterns:
            pattern = raw.strip().replace("\\", "/")
            if not pattern or pattern.startswith("#"):
                continue
            kept.append(raw.strip())
            anchored = "/" in pattern.rstrip("/")
            pattern = pattern.strip("/")
            if pattern.startswith("./"):
                pattern = pattern[2:]
            if not pattern:
                continue
            body = _glob_regex(pattern)
            bodies.append(body if anchored else f"(?:.*/)?{body}")
        self.patterns: tuple[str, ...] = tuple(kept)
        self._regex = (
            re.compile("|".join(f"(?:{body})" for body in bodies)) if bodies else None
        )

    def __bool__(self) -> bool:
        return self._regex is not None

    def matches(self, rel: str) -> bool:
        if self._regex is None:
            return False
        parts = rel.replace("\\", "/").split("/")
        # The file itself, then each directory above it.
        return any(
            self._regex.fullmatch("/".join(parts[:end]))
            for end in range(len(parts), 0, -1)
        )


# --- one room ----------------------------------------------------------------------


async def _close_quietly(socket: Any) -> None:
    try:
        await asyncio.wait_for(socket.close(), timeout=2.0)
    except Exception:  # noqa: BLE001 - best effort
        pass


class RelayGone(Exception):
    """The relay link a hook's question went out on dropped before the answer
    came: the hook fails open at once instead of waiting out its timeout."""


@dataclass(eq=False)
class RepoBinding:
    """How events from one repo are handled, and which connection carries them.

    `root` is the path-key anchor; None (single-room mode without --root)
    means each event's git root is discovered from its cwd.
    """

    root: Optional[Path]
    connection: RoomConnection
    ignore: IgnoreRules
    share_diffs: bool = True
    # Registered on first sight from a .hivelane.toml rather than explicitly.
    auto: bool = False
    # What it was registered with, so re-registering it unchanged is a no-op.
    spec: tuple[Any, ...] = ()
    # Secret redaction for what this repo sends (`redact`, `redact_patterns`).
    redactor: redact.Redactor = field(default_factory=redact.Redactor)


class RoomConnection:
    """One WebSocket to one relay room under one name: the relay socket loop,
    outboxes, pending edit_checks, batches, old-content cache and peers.

    Every repo bound to the same relay, room, name and agent shares one
    connection, because the relay admits a name only once per room; messages
    still carry each repo's own repo_id and path keys.
    """

    def __init__(
        self,
        room: str,
        name: str,
        *,
        agent: AgentKind = "claude-code",
        server: str = DEFAULT_SERVER,
        batch_seconds: float = BATCH_SECONDS,
        edit_check_timeout: float = EDIT_CHECK_TIMEOUT,
        settings: Optional[RoomSettings] = None,
        rename_hint: str = "Re-run with a different --name.",
        e2e_key: Optional[crypto.RoomKey] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        self.room = room
        self.name = name
        self.agent: AgentKind = agent
        self.server = server
        self.batch_seconds = batch_seconds
        self.edit_check_timeout = edit_check_timeout
        # Team conflict windows from the repo's .hivelane.toml, sent on join.
        self.settings = settings
        self.rename_hint = rename_hint
        self._self_ids = {name, relay_user_id(name)}
        # End-to-end encryption: the team's .hivelane.toml says e2e, and this
        # is the key derived from the room passphrase (None: not configured,
        # which is fatal rather than a fallback to cleartext).
        self.e2e_required = bool(settings is not None and settings.e2e)
        self.e2e_key = e2e_key
        # sha256 of the passphrase e2e_key came from (repos sharing this
        # connection must agree on it).
        self.key_fingerprint: Optional[str] = None
        # h:-token -> the name it hashes, learned from our own sends and from
        # teammates' decrypted blobs, so relay reasons read in real names.
        self._names: OrderedDict[str, str] = OrderedDict()
        # e2e repo tokens we sent -> our repo_id (_wire_repo_id).
        self._repo_tokens: dict[str, str] = {}
        # user_id -> that teammate's latest decrypted task (intent or plan).
        self._peer_tasks: dict[str, str] = {}
        # Room ownership (the token POST /rooms returned to whoever created
        # it): an owner socket is asked to approve joins.
        self.owner_token = owner_token
        # Set while the relay holds this join for an owner's approval.
        self.awaiting_approval: Optional[dict[str, Any]] = None
        # Join requests waiting on us as an owner: request_id -> details.
        self.join_requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # The room's join policy as last broadcast (None: never heard).
        self.require_approval: Optional[bool] = None

        # Admitted to the room: set on the relay's first ordinary frame after
        # our join (it always sends the joiner its own user_joined), not when
        # the join was merely written, so a join about to be refused or held
        # never reads as connected; cleared on fatal and on disconnect.
        self.connected = False
        self.fatal: Optional[str] = None
        self.fatal_at: Optional[float] = None
        # Set from a relay rate_limited frame; overrides the next backoff.
        self.retry_after = 0.0
        # Unix seconds of the last frame received from, or successfully sent
        # to, the relay. `connected` alone cannot tell a live link from a
        # half-open socket the OS has not noticed is dead yet.
        self.last_relay_contact: Optional[float] = None
        # Unix seconds the current relay connection was established, or None.
        self.connected_since: Optional[float] = None
        # Identity of the repo this connection was opened for, sent on join
        # (re-read from repo_root before every connect: a remote added since
        # changes it).
        self.repo_id = ""
        self.repo_root: Optional[Path] = None

        self._stop = asyncio.Event()
        self._closed = False
        self._task: Optional[asyncio.Task[None]] = None
        self._socket: Any = None
        # edit_check sits on the agent's blocking hook and gives up after
        # edit_check_timeout, so it must not queue behind a backlog of activity.
        self._priority_outbox: asyncio.Queue[str] = asyncio.Queue()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._outbox_ready = asyncio.Event()
        # Held across a send so a connection drop mid-send re-sends rather than
        # silently losing the payload.
        self._inflight: Optional[str] = None
        self._pending: dict[str, asyncio.Future[EditCheckResponse]] = {}
        # request_id -> the repo-relative path asked about (the reply names it
        # by hash in an e2e room).
        self._pending_paths: dict[str, str] = {}
        # edit_checks whose hook gave up (timed out, link dropped): not sent
        # if still queued (a late one has side effects on the relay's queues).
        self._abandoned: OrderedDict[str, None] = OrderedDict()
        self._peers: set[str] = set()
        self._old_content: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._old_content_chars = 0
        # Kind -> monotonic time of its last send, for SEND_SPACING.
        self._last_sent: dict[str, float] = {}

        # (repo_id, path key) -> latest change. Keyed by repo as well so one
        # flush can split into one activity message per repo.
        self._batch: dict[tuple[str, str], FileChange] = {}
        self._batch_deadline = 0.0
        self._batch_task: Optional[asyncio.Task[None]] = None
        self._last_prompt: Optional[str] = None
        # Local signature-change memory for "you're editing a caller" notes.
        self.deps = DepTracker()
        # repo_id -> (branch, HEAD commit) seen at the last post_edit.
        self._heads: dict[str, tuple[str, str]] = {}
        # Plan events waiting for the relay's plan_conflicts reply, in the
        # order sent: request_id -> (session_id, future, monotonic sent-at).
        # A relay that echoes request_id is paired exactly; one that does not
        # is answered first-in-first-out.
        self._plan_waiters: OrderedDict[
            str, tuple[str, asyncio.Future[PlanConflicts], float]
        ] = OrderedDict()
        # session_id -> notes (plan conflicts, someone waiting for a file we
        # hold) to surface on that session's next pre_edit ack. "*:<repo_id>"
        # holds notes for whichever session in that repo comes first.
        self._session_notes: dict[str, str] = {}
        # Wait queues (server/file_queue.py). (repo_id, path key) -> our place
        # in line for a teammate's file: {"position", "sessions", "since"}.
        self._queued: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        # (repo_id, path key) -> a file_released not yet used: {"at", "sessions"}.
        self._released: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        # (repo_id, path key) -> the session that last edited it through us,
        # i.e. who a teammate's file_requested is about.
        self._last_editor: OrderedDict[tuple[str, str], str] = OrderedDict()
        # `hivelane wait` callers: (repo_id, path key) -> futures resolved with
        # how the file came free ("released" or "allowed").
        self._file_waiters: dict[tuple[str, str], list[asyncio.Future[str]]] = {}

    @property
    def ws_url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.room}"

    @property
    def live(self) -> bool:
        """Joined and admitted: worth asking the relay before an edit."""
        return self.connected and self.awaiting_approval is None and self.fatal is None

    # --- end-to-end encryption -----------------------------------------------

    def _hash(self, value: str) -> str:
        """value's h:-token (remembered, to turn relay reasons back into names)."""
        key = self.e2e_key
        if key is None or not value or value == symbols.IMPORTS:
            return value
        token = key.hash(value)
        self._remember_name(token, value)
        return token

    def _remember_name(self, token: str, value: str) -> None:
        self._names.pop(token, None)
        self._names[token] = value
        while len(self._names) > E2E_NAME_CACHE_SIZE:
            self._names.popitem(last=False)

    def _wire_repo_id(self, repo_id: str) -> str:
        """repo_id as sent: in an e2e room a keyed token (equality kept, so
        clones still group; a guessed origin URL can no longer be confirmed)."""
        key = self.e2e_key
        token_of = getattr(key, "repo_token", None)
        if key is None or not repo_id or token_of is None:
            return repo_id
        token = token_of(repo_id)
        self._repo_tokens[token] = repo_id
        while len(self._repo_tokens) > REPO_ROOT_CACHE_SIZE:
            self._repo_tokens.pop(next(iter(self._repo_tokens)))
        return token

    def _local_repo_id(self, wire: str) -> str:
        """A repo_id from the relay as our own id, when it is one of our tokens."""
        return self._repo_tokens.get(wire, wire)

    def reveal(self, text: Optional[str]) -> Optional[str]:
        """text with every h:-token we know replaced by its real name."""
        if not text or self.e2e_key is None or "h:" not in text:
            return text
        return _HASH_TOKEN.sub(lambda m: self._names.get(m.group(0), m.group(0)), text)

    def _learn(self, envelope: dict[str, Any]) -> None:
        """Remember the hashes of every name in a decrypted envelope."""
        key = self.e2e_key
        if key is None:
            return
        def listed(value: Any) -> list[Any]:
            return value if isinstance(value, list) else []

        names: list[Any] = []
        for change in listed(envelope.get("changes")):
            if isinstance(change, dict):
                names.append(change.get("file_path"))
                names.extend(listed(change.get("symbols")))
        names.append(envelope.get("file_path"))
        names.extend(listed(envelope.get("files")))
        names.extend(listed(envelope.get("symbols")))
        names.append(envelope.get("branch"))
        for name in names:
            if isinstance(name, str) and name and name != symbols.IMPORTS:
                self._remember_name(key.hash(name), name)

    def open_blob(self, blob: str) -> Optional[tuple[str, dict[str, Any]]]:
        """(type, envelope) of a teammate's blob, learning its names; None when
        there is no key or it does not open (a teammate with the wrong key)."""
        if not blob or self.e2e_key is None:
            return None
        try:
            opened = self.e2e_key.open(blob)
        except (crypto.DecryptError, crypto.E2EUnavailable) as exc:
            print(f"hivelane: room {self.room}: cannot decrypt a teammate's message ({exc})", file=sys.stderr)
            return None
        self._learn(opened[1])
        return opened

    def _seal(
        self, message: ActivityMessage | IntentMessage | EditCheckRequest | PlanMessage | FileRequest
    ) -> ActivityMessage | IntentMessage | EditCheckRequest | PlanMessage | FileRequest:
        """The e2e form of message: names hashed, content emptied, the real
        values in its blob. The relay still matches overlaps (equal names hash
        equal) but can read nothing."""
        key = self.e2e_key
        assert key is not None
        if isinstance(message, FileRequest):
            # An edit_check-shaped envelope: the relay copies it onto the
            # file_requested a holder gets, whose daemon opens it like any
            # edit_check blob (no new envelope type for dashboards to learn).
            rel = message.file_path
            blob = key.seal("edit_check", {"v": 1, "file_path": rel, "symbols": [], "branch": ""})
            return message.model_copy(
                update={"file_path": self._hash(rel), "e2e": blob, "repo_id": self._wire_repo_id(message.repo_id)}
            )
        if isinstance(message, IntentMessage):
            blob = key.seal("intent", {"v": 1, "prompt": message.prompt})
            return message.model_copy(
                update={"prompt": "", "e2e": blob, "repo_id": self._wire_repo_id(message.repo_id)}
            )
        if isinstance(message, EditCheckRequest):
            rel = message.file_path
            names, ranges = list(message.symbols), list(message.line_ranges)
            blob = key.seal(
                "edit_check",
                {"v": 1, "file_path": rel, "symbols": names, "branch": message.branch},
            )
            if is_always_warn_file(rel):
                # The relay cannot see this is a manifest/lockfile: give it
                # nothing finer than the file to judge by.
                names, ranges = [], []
            return message.model_copy(
                update={
                    "file_path": self._hash(rel),
                    "symbol": None,
                    "symbols": [self._hash(s) for s in names],
                    "line_ranges": ranges,
                    "branch": self._hash(message.branch),
                    "e2e": blob,
                    "repo_id": self._wire_repo_id(message.repo_id),
                }
            )
        if isinstance(message, PlanMessage):
            blob = key.seal(
                "plan",
                {
                    "v": 1,
                    "summary": message.summary,
                    "files": list(message.files),
                    "symbols": list(message.symbols),
                    "branch": message.branch,
                },
            )
            return message.model_copy(
                update={
                    "summary": "",
                    "files": [self._hash(f) for f in message.files],
                    "symbols": [self._hash(s) for s in message.symbols],
                    "branch": self._hash(message.branch),
                    "e2e": blob,
                    "repo_id": self._wire_repo_id(message.repo_id),
                }
            )
        # ActivityMessage
        blob = _seal_activity(key, message)
        changes = []
        for change in message.changes:
            warn_only = is_always_warn_file(change.file_path)
            changes.append(
                FileChange(
                    file_path=self._hash(change.file_path),
                    diff="",
                    symbols=[] if warn_only else [self._hash(s) for s in change.symbols],
                    line_ranges=[] if warn_only else list(change.line_ranges),
                )
            )
        return message.model_copy(
            update={
                "prompt": None,
                "changes": changes,
                "branch": self._hash(message.branch),
                "base_commit": self._hash(message.base_commit),
                "e2e": blob,
                "repo_id": self._wire_repo_id(message.repo_id),
            }
        )

    def _open_peer(self, peer: PeerTouch) -> PeerTouch:
        """A relay PeerTouch with the teammate's real task, symbols and branch."""
        opened = self.open_blob(peer.e2e)
        envelope = opened[1] if opened else {}
        task = envelope.get("prompt") or envelope.get("summary") or self._peer_tasks.get(peer.user_id)
        update: dict[str, Any] = {
            "symbols": [self._names.get(s, s) for s in peer.symbols],
            "branch": self._names.get(peer.branch, peer.branch),
            # Opened: the agent's ack needs none of it, and a few teammates'
            # blobs could otherwise push the ack past the hook's size cap.
            "e2e": "",
        }
        if task and not peer.prompt:
            update["prompt"] = str(task)
        return peer.model_copy(update=update)

    def _open_response(self, response: EditCheckResponse, rel: str) -> EditCheckResponse:
        others = [self._open_peer(peer) for peer in response.others]
        return response.model_copy(
            update={
                "file_path": rel,
                "others": others,
                "reason": self.reveal(response.reason) or "",
                "suggestion": self.reveal(response.suggestion) or "",
            }
        )

    def _open_plan_conflicts(self, conflicts: PlanConflicts) -> PlanConflicts:
        opened = []
        for conflict in conflicts.conflicts:
            found = self.open_blob(conflict.e2e)
            envelope = found[1] if found else {}
            summary = envelope.get("summary") or envelope.get("prompt") or conflict.summary
            opened.append(
                conflict.model_copy(
                    update={
                        "file_path": self._names.get(conflict.file_path, conflict.file_path),
                        "summary": str(summary or ""),
                    }
                )
            )
        return conflicts.model_copy(update={"conflicts": opened})

    def _open_feed_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """A feed event (dict) with a teammate's blob opened: real paths and
        diffs for depgraph, and their latest task remembered."""
        blob = event.get("e2e")
        if not blob or self.e2e_key is None or not isinstance(blob, str):
            return event
        user_id = event.get("user_id")
        if isinstance(user_id, str) and user_id in self._self_ids:
            return event
        opened = self.open_blob(blob)
        if opened is None:
            return event
        kind, envelope = opened
        task = envelope.get("prompt") or envelope.get("summary")
        if isinstance(user_id, str) and isinstance(task, str) and task:
            self._peer_tasks[user_id] = task[:500]
            while len(self._peer_tasks) > 256:
                self._peer_tasks.pop(next(iter(self._peer_tasks)))
        if kind == "activity" and isinstance(envelope.get("changes"), list):
            return {**event, "changes": envelope["changes"], "prompt": envelope.get("prompt")}
        return event

    @property
    def queued_events(self) -> int:
        return self._outbox.qsize() + self._priority_outbox.qsize() + len(self._batch)

    def _has_backlog(self) -> bool:
        return (
            self._inflight is not None
            or not self._outbox.empty()
            or not self._priority_outbox.empty()
        )

    # --- event handling ------------------------------------------------------

    @staticmethod
    def _root_for(cwd: str, binding: RepoBinding) -> Optional[Path]:
        return binding.root if binding.root is not None else repo_root(cwd)

    async def repo_id_for_cwd(self, cwd: str, binding: RepoBinding) -> str:
        """repo_id for an event's cwd, so one connection serving agents in
        several repos still labels each message with the right one.

        Cached per root after the first lookup; that first lookup runs git in
        a worker thread so it never stalls the event loop other hooks share.
        """
        root = self._root_for(cwd, binding)
        cached = cached_repo_id(root)
        if cached is not None:
            return cached
        return await asyncio.to_thread(repo_id_for, root)

    async def handle_event(self, event: NormalizedEvent, binding: RepoBinding) -> EventAck:
        repo_id = await self.repo_id_for_cwd(event.cwd, binding)
        if event.kind == "intent":
            return self._handle_intent(event, repo_id, binding.redactor)
        root = self._root_for(event.cwd, binding)
        if event.kind == "plan":
            return await self._handle_plan(event, root, binding, repo_id)
        if not event.file_path:
            return EventAck()
        rel = repo_relative_path(event.file_path, event.cwd, binding.root)
        if binding.ignore.matches(rel):
            return EventAck(file_path=rel)
        head = gitinfo.head_for(root)
        if event.kind == "pre_edit":
            branch = (head.branch or "") if head else ""
            ack = await self._handle_pre_edit(event, rel, repo_id, branch=branch)
            for note in (
                self._release_note(repo_id, rel, event.session_id, ack),
                self.deps.note_for(rel, event.old_content, repo_id),
                self._take_session_notes(event.session_id, repo_id),
            ):
                if note:
                    ack = with_note(ack, note)
            return ack
        if head is not None:
            self._heads[repo_id] = (head.branch or "", head.commit or "")
        self._remember_bounded(self._last_editor, (repo_id, rel), event.session_id)
        return await self._handle_post_edit(
            event, rel, repo_id, binding.share_diffs, binding.redactor
        )

    def _handle_intent(
        self, event: NormalizedEvent, repo_id: str = "", redactor: Optional[redact.Redactor] = None
    ) -> EventAck:
        if not event.prompt:
            return EventAck()
        # Redacted (and capped, and made encodable) before it is remembered:
        # the prompt rides on every later activity message too, so one huge
        # or unencodable prompt would otherwise break each of those as well.
        prompt = (redactor or redact.DEFAULT).text(clean_text(event.prompt)) or ""
        prompt = truncate_utf8(prompt, MAX_TEXT_BYTES)
        if self._send(IntentMessage(user_id=self.name, prompt=prompt, repo_id=repo_id)):
            self._last_prompt = prompt
        return EventAck()

    async def _handle_plan(
        self, event: NormalizedEvent, root: Optional[Path], binding: RepoBinding, repo_id: str
    ) -> EventAck:
        """Send the agent's plan; the relay answers with the planned files
        teammates are editing or planning. Those come back on this ack as
        context (adapters never turn a plan ack into a permission decision);
        a reply that misses the ack is kept for the session's next ack."""
        summary = (event.summary or "").strip()
        if not self.live or (not summary and not event.files):
            return EventAck()
        summary = truncate_utf8(binding.redactor.text(clean_text(summary)) or "", MAX_TEXT_BYTES)
        tracked = await asyncio.to_thread(tracked_files, root) if root is not None else []
        files = [
            f for f in resolve_plan_files(event.files or [], tracked) if not binding.ignore.matches(f)
        ]
        if not files and not event.symbols:
            return EventAck()
        head = gitinfo.head_for(root)
        future: asyncio.Future[PlanConflicts] = asyncio.get_running_loop().create_future()
        request_id = uuid.uuid4().hex
        fields: dict[str, Any] = {
            "user_id": self.name,
            "summary": summary[:2000],
            "files": files,
            "symbols": [clean_text(s) or "" for s in (event.symbols or [])][:50],
            "repo_id": repo_id,
            "branch": (head.branch or "") if head else "",
        }
        if "request_id" in PlanMessage.model_fields:
            # The relay echoes it on plan_conflicts, so replies pair exactly
            # even when one is never sent (rate-limited, invalid plan).
            fields["request_id"] = request_id
        self._prune_plan_waiters()
        self._plan_waiters[request_id] = (event.session_id, future, time.monotonic())
        if not self._send(PlanMessage(**fields)):
            self._plan_waiters.pop(request_id, None)
            return EventAck()
        try:
            conflicts = await asyncio.wait_for(asyncio.shield(future), timeout=self.edit_check_timeout)
        except asyncio.TimeoutError:
            future.cancel()  # tells _on_plan_conflicts to keep the note for later
            return EventAck()
        except RelayGone:
            return EventAck()
        note = plan_conflict_note(conflicts)
        return EventAck(decision="warn", reason=note) if note else EventAck()

    async def _handle_pre_edit(
        self, event: NormalizedEvent, rel: str, repo_id: str = "", branch: str = ""
    ) -> EventAck:
        if event.old_content is not None:
            self._remember_old(event.session_id, rel, event.old_content)

        if not self.live:
            return EventAck(file_path=rel)

        old = event.old_content
        if old is not None and event.edit_hints and len(old) > INLINE_SYMBOLS_BYTES:
            names, ranges = await asyncio.to_thread(edit_symbols, rel, old, event.edit_hints)
        else:
            names, ranges = edit_symbols(rel, old, event.edit_hints)
        request_id = uuid.uuid4().hex
        future: asyncio.Future[EditCheckResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._pending_paths[request_id] = rel
        sent = self._send(
            EditCheckRequest(
                request_id=request_id,
                file_path=rel,
                repo_id=repo_id,
                symbols=names,
                line_ranges=ranges,
                branch=branch,
            )
        )
        if not sent:
            self._pending.pop(request_id, None)
            self._pending_paths.pop(request_id, None)
            return EventAck(file_path=rel)
        try:
            response = await asyncio.wait_for(future, timeout=self.edit_check_timeout)
        except (asyncio.TimeoutError, RelayGone):
            self._pending.pop(request_id, None)
            self._pending_paths.pop(request_id, None)
            self._abandon(request_id)
            return EventAck(file_path=rel)
        key = (repo_id, rel)
        if response.decision == "block":
            if response.queue_position > 0:
                queued = self._queued.get(key) or {"sessions": set(), "since": time.time()}
                queued["sessions"].add(event.session_id)
                queued["position"] = response.queue_position
                self._remember_bounded(self._queued, key, queued)
        else:
            # Ours now (or never contested): out of line, and anyone running
            # `hivelane wait` on it can stop.
            self._queued.pop(key, None)
            self._resolve_file_waiters(key, "allowed")
        return EventAck(
            decision=response.decision,
            reason=response.reason,
            file_path=response.file_path or rel,
            others=response.others,
            queue_position=response.queue_position,
            suggestion=response.suggestion,
        )

    # --- wait queues -----------------------------------------------------------

    @staticmethod
    def _remember_bounded(table: OrderedDict, key: Any, value: Any) -> None:
        table.pop(key, None)
        table[key] = value
        while len(table) > QUEUE_MEMORY:
            table.popitem(last=False)

    def _add_session_note(self, session_id: str, note: str) -> None:
        existing = self._session_notes.get(session_id)
        if existing and note in existing:
            return
        self._session_notes[session_id] = f"{existing}; {note}" if existing else note
        while len(self._session_notes) > 256:
            self._session_notes.pop(next(iter(self._session_notes)))

    def _take_session_notes(self, session_id: str, repo_id: str) -> Optional[str]:
        notes = [
            note
            for note in (
                self._session_notes.pop(session_id, None),
                self._session_notes.pop(f"*:{repo_id}", None),
            )
            if note
        ]
        return "; ".join(notes) or None

    def _release_note(
        self, repo_id: str, rel: str, session_id: str, ack: EventAck
    ) -> Optional[str]:
        """The "it's your turn" note for the first pre_edit of a file the relay released
        to us, by a session that was waiting for it (any session, when the
        release answered a `hivelane wait`). Used up either way; a block means
        a holder came back, and the relay's answer stands."""
        key = (repo_id, rel)
        released = self._released.get(key)
        if released is None:
            return None
        sessions = released["sessions"]
        if sessions and session_id not in sessions:
            return None
        self._released.pop(key, None)
        if ack.decision == "block" or time.time() - released["at"] > RELEASE_NOTE_TTL:
            return None
        return f"{rel} was released to you, it's your turn"

    def _resolve_file_waiters(self, key: tuple[str, str], how: str) -> None:
        for future in self._file_waiters.pop(key, []):
            if not future.done():
                future.set_result(how)

    def _frame_path(self, data: dict[str, Any]) -> str:
        """A queue frame's file_path as a repo-relative path: in an e2e room
        it is a hash, which our own sends (or the requester's blob) explain."""
        path = str(data.get("file_path") or "")
        if self.e2e_key is not None:
            blob = data.get("e2e")
            if isinstance(blob, str) and blob:
                self.open_blob(blob)
            path = self._names.get(path, path)
        return path

    def _on_file_released(self, data: dict[str, Any]) -> None:
        rel = self._frame_path(data)
        if not rel:
            return
        key = (self._local_repo_id(str(data.get("repo_id") or "")), rel)
        queued = self._queued.pop(key, None)
        sessions = set(queued["sessions"]) if queued else set()
        self._remember_bounded(self._released, key, {"at": time.time(), "sessions": sessions})
        self._resolve_file_waiters(key, "released")
        place = f" (you were #{queued['position']} in line)" if queued and queued.get("position") else ""
        print(f"hivelane: room {self.room}: {rel} is free, your turn{place}")

    def _on_file_requested(self, data: dict[str, Any]) -> None:
        rel = self._frame_path(data)
        if not rel:
            return
        repo_id = self._local_repo_id(str(data.get("repo_id") or ""))
        by = str(data.get("by") or "a teammate")
        agent = str(data.get("agent") or "unknown")
        session = self._last_editor.get((repo_id, rel))
        self._add_session_note(
            session or f"*:{repo_id}",
            f"{by}'s agent is waiting for {rel}, finish up or hand it off",
        )
        print(f"hivelane: room {self.room}: {by} ({agent}) is waiting for {rel}")

    async def wait_for_file(self, rel: str, repo_id: str, timeout: float) -> dict[str, Any]:
        """One slice of `hivelane wait`: ask the relay for the file (it answers
        file_released when the file is, or becomes, ours to take) and wait up
        to timeout for that, or for one of our edit checks on it to pass.

        Every slice re-sends the file_request: the relay keeps our place and
        restarts its expiry, and a request lost to a reconnect is renewed."""
        key = (repo_id, rel)
        result: dict[str, Any] = {"file_path": rel, "room": self.room}
        if not self.live:
            # Fail open, like every hook: edits are not checked right now.
            return {**result, "free": True, "state": "offline"}
        released = self._released.get(key)
        if released is not None and time.time() - released["at"] <= RELEASE_GRACE:
            return {**result, "free": True, "state": "released"}
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._file_waiters.setdefault(key, []).append(future)
        self._send(FileRequest(file_path=rel, repo_id=repo_id))
        try:
            state = await asyncio.wait_for(asyncio.shield(future), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            queued = self._queued.get(key) or {}
            return {
                **result,
                "free": False,
                "state": "waiting",
                "queue_position": int(queued.get("position") or 0),
            }
        finally:
            waiters = self._file_waiters.get(key)
            if waiters is not None and future in waiters:
                waiters.remove(future)
                if not waiters:
                    del self._file_waiters[key]
        return {**result, "free": True, "state": state}

    async def _handle_post_edit(
        self,
        event: NormalizedEvent,
        rel: str,
        repo_id: str = "",
        share_diffs: bool = True,
        redactor: Optional[redact.Redactor] = None,
    ) -> EventAck:
        old = event.old_content
        known_old = old is not None or (event.session_id, rel) in self._old_content
        if old is None:
            old = self._forget_old((event.session_id, rel)) or ""
        else:
            self._forget_old((event.session_id, rel))
        new = event.new_content or ""
        # Which definitions changed. Without a known old version every line
        # would count as changed, which says nothing, so nothing is sent.
        if old == new:
            return EventAck(file_path=rel)
        # Symbols, the diff and its redaction are CPU work (difflib is about
        # quadratic): above a small size it all runs in a worker thread, so a
        # big file never stalls the loop every other hook and the relay
        # keepalive share. Content past the caps goes out path-only.
        work = functools.partial(
            _post_edit_work,
            rel,
            old,
            new,
            known_old,
            share_diffs,
            redactor or redact.DEFAULT,
            self.deps,
            repo_id,
        )
        if max(len(old), len(new)) > INLINE_SYMBOLS_BYTES:
            names, ranges, diff = await asyncio.to_thread(work)
        else:
            names, ranges, diff = work()
        previous = self._batch.get((repo_id, rel))
        if previous is not None:
            # Two edits to one file inside a batch: report both edits' symbols.
            names = list(dict.fromkeys([*previous.symbols, *names]))[: symbols.MAX_SYMBOLS]
        self._batch[(repo_id, rel)] = FileChange(
            file_path=rel, diff=diff, symbols=names, line_ranges=ranges
        )
        self._schedule_flush()
        return EventAck(file_path=rel)

    def _remember_old(self, session_id: str, rel: str, content: str) -> None:
        key = (session_id, rel)
        self._forget_old(key)
        if len(content) > symbols.MAX_SOURCE_BYTES:
            # Too big for symbols, and its diff would be skipped too: caching
            # it only costs memory (a pre_edit with no post_edit never frees it).
            return
        self._old_content[key] = content
        self._old_content_chars += len(content)
        while self._old_content and (
            len(self._old_content) > OLD_CONTENT_CACHE_SIZE
            or self._old_content_chars > OLD_CONTENT_BUDGET
        ):
            _, dropped = self._old_content.popitem(last=False)
            self._old_content_chars -= len(dropped)

    def _forget_old(self, key: tuple[str, str]) -> Optional[str]:
        content = self._old_content.pop(key, None)
        if content is not None:
            self._old_content_chars -= len(content)
        return content

    # --- post_edit batching --------------------------------------------------

    def _schedule_flush(self) -> None:
        self._batch_deadline = time.monotonic() + self.batch_seconds
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._flush_when_idle())

    async def _flush_when_idle(self) -> None:
        while True:
            remaining = self._batch_deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        self.flush_batch()

    def flush_batch(self) -> None:
        """Send the pending changes, one activity message per repo: repo_id is
        per message, and a path only means something next to its repo."""
        if not self._batch:
            return
        by_repo: dict[str, list[FileChange]] = {}
        for (repo_id, _), change in self._batch.items():
            by_repo.setdefault(repo_id, []).append(change)
        self._batch.clear()
        prompt = self._last_prompt
        budget = MAX_FRAME_BYTES - 4096 - len((prompt or "").encode("utf-8", "replace")) * 2
        for repo_id, changes in by_repo.items():
            branch, commit = self._heads.get(repo_id, ("", ""))
            for chunk in _chunk_changes(changes, budget):
                self._send(
                    ActivityMessage(
                        user_id=self.name,
                        prompt=prompt,
                        changes=chunk,
                        agent=self.agent,
                        repo_id=repo_id,
                        branch=branch,
                        base_commit=commit,
                    )
                )

    # --- relay socket --------------------------------------------------------

    def _send(
        self,
        message: ActivityMessage
        | IntentMessage
        | EditCheckRequest
        | PlanMessage
        | FileRequest
        | JoinDecision
        | RoomPolicy,
    ) -> bool:
        """Queue message for the relay; False when it cannot go (an e2e room
        without a key, or a message that cannot be encoded or made to fit a
        frame). Never raises: a caller's batch must not die with one message."""
        sealable = (ActivityMessage, IntentMessage, EditCheckRequest, PlanMessage, FileRequest)
        if self.e2e_required and self.e2e_key is None and isinstance(message, sealable):
            # An e2e room without a key: never fall back to cleartext.
            return False
        try:
            payload = self._encode(message)
        except Exception as exc:  # noqa: BLE001 - one bad message, not the connection
            print(
                f"hivelane: room {self.room}: not sending a {getattr(message, 'type', '?')} message ({exc!r})",
                file=sys.stderr,
            )
            return False
        if payload is None:
            print(
                f"hivelane: room {self.room}: not sending a {getattr(message, 'type', '?')} message "
                f"larger than {MAX_FRAME_BYTES} bytes",
                file=sys.stderr,
            )
            return False
        # A plan ack, like an edit_check, has an agent hook waiting on it; an
        # owner's decision has a teammate waiting at the door; a file_request
        # has `hivelane wait` (and so an agent) waiting on it.
        if isinstance(message, (EditCheckRequest, PlanMessage, FileRequest, JoinDecision, RoomPolicy)):
            self._priority_outbox.put_nowait(payload)
        else:
            if OUTBOX_LIMIT > 0 and self._outbox.qsize() >= OUTBOX_LIMIT:
                self._outbox.get_nowait()
            self._outbox.put_nowait(payload)
        self._outbox_ready.set()
        return True

    def _encode(self, message: Any) -> Optional[str]:
        """message as the JSON frame to send (sealed in an e2e room), or None
        when even its slimmest form is over MAX_FRAME_BYTES."""
        sealable = (ActivityMessage, IntentMessage, EditCheckRequest, PlanMessage, FileRequest)
        sealed = self._seal(message) if self.e2e_key is not None and isinstance(message, sealable) else message
        payload = sealed.model_dump_json()
        if len(payload) <= MAX_FRAME_BYTES // 4 or len(payload.encode("utf-8")) <= MAX_FRAME_BYTES:
            return payload
        if isinstance(message, ActivityMessage):
            # Path-only, with a short prompt: overlaps are still detected.
            slim = message.model_copy(
                update={
                    "prompt": truncate_utf8(message.prompt or "", 500) or None,
                    "changes": [c.model_copy(update={"diff": ""}) for c in message.changes],
                }
            )
            sealed = self._seal(slim) if self.e2e_key is not None else slim
            payload = sealed.model_dump_json()
            if len(payload.encode("utf-8")) <= MAX_FRAME_BYTES:
                return payload
        return None

    def _join_payload(self) -> str:
        payload = JoinMessage(
            name=self.name,
            agent=self.agent,
            repo_id=self._wire_repo_id(self.repo_id),
            settings=self.settings,
            owner_token=self.owner_token,
        ).model_dump(mode="json")
        settings = payload.get("settings")
        if isinstance(settings, dict) and not settings.get("e2e"):
            # Only ever switch e2e on. A teammate on an older .hivelane.toml
            # (no e2e line) must not turn the room's encryption flag off for
            # everyone; the relay keeps the room's flag when the join omits it.
            settings.pop("e2e", None)
        if (
            isinstance(settings, dict)
            and self.settings is not None
            and "history_hours" not in self.settings.model_fields_set
        ):
            # Sent only when the .hivelane.toml sets it: the relay then keeps
            # the room's history setting (and what it has kept) as it was.
            settings.pop("history_hours", None)
        if payload.get("owner_token") is None:
            payload.pop("owner_token", None)
        return json.dumps(payload, separators=(",", ":"))

    # --- room ownership ------------------------------------------------------

    def decide_join(self, request_id: str, approve: bool) -> bool:
        """Answer a pending join request as the room's owner. False when this
        connection has no such request (or is not an owner)."""
        if not self.owner_token or request_id not in self.join_requests:
            return False
        self.join_requests.pop(request_id, None)
        self._send(JoinDecision(request_id=request_id, approve=approve))
        return True

    def set_policy(self, require_approval: bool) -> bool:
        if not self.owner_token:
            return False
        self._send(RoomPolicy(require_approval=require_approval))
        return True

    def pending_join_requests(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        for request_id, request in list(self.join_requests.items()):
            if now - request["received_at"] > JOIN_REQUEST_TTL:
                del self.join_requests[request_id]
        return list(self.join_requests.values())

    def _e2e_problem(self) -> Optional[str]:
        """Why this e2e room cannot be joined, or None."""
        if not self.e2e_required:
            return None
        if self.e2e_key is None:
            return (
                f"room {self.room} is end-to-end encrypted (e2e = true in .hivelane.toml) but no "
                "room key is configured on this machine. Get the key from a teammate and run "
                f"`hivelane key set {self.room}` (or set {room_key_env_name(self.room)}), then `hivelane up` again"
            )
        if not crypto.available():
            return f"room {self.room} is end-to-end encrypted, which needs the e2e extra: {crypto.INSTALL_HINT}"
        return None

    def start(self) -> asyncio.Task[None]:
        """Start the relay loop on the running event loop (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._relay_loop(), name=f"relay:{self.room}")
            self._task.add_done_callback(self._relay_loop_done)
        return self._task

    def _relay_loop_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self.fatal is None:
            self._set_fatal(f"internal error in the relay connection: {exc!r}")

    def _set_fatal(self, message: str) -> None:
        self.fatal = message
        self.fatal_at = time.monotonic()
        # Not connected from this moment, though the socket still closes.
        self.connected = False
        self._fail_open_waiters()

    def fail(self, message: str) -> None:
        """Go fatal now, closing the socket if one is open (a frame-driven
        fatal ends the receive loop by itself; this one may not get a frame)."""
        self._set_fatal(message)
        socket = self._socket
        if socket is not None:
            asyncio.get_running_loop().create_task(_close_quietly(socket))

    def _fail_open_waiters(self) -> None:
        """Answer every hook still waiting on the relay with "allow" now
        rather than at its timeout: the link they were asked over is gone."""
        for request_id, future in list(self._pending.items()):
            self._abandon(request_id)
            if not future.done():
                future.set_exception(RelayGone())
        self._pending.clear()
        self._pending_paths.clear()
        for _session, plan_future, _at in self._plan_waiters.values():
            if not plan_future.done():
                plan_future.set_exception(RelayGone())
        self._plan_waiters.clear()

    async def _relay_loop(self) -> None:
        problem = self._e2e_problem()
        if problem:
            self._set_fatal(problem)
            return
        backoff = 1.0
        while not self._stop.is_set():
            admitted_since: Optional[float] = None
            try:
                if self.repo_root is not None:
                    # A remote added (or a first commit made) since the last
                    # join changes the identity teammates' clones compute.
                    self.repo_id = await asyncio.to_thread(repo_id_for, self.repo_root)
                async with ws_connect(self.ws_url) as socket:
                    self._socket = socket
                    # A fresh join: any approval wait belonged to the old socket,
                    # and so did our places in the relay's wait queues.
                    self.awaiting_approval = None
                    self._queued.clear()
                    self._released.clear()
                    await socket.send(self._join_payload())
                    # Not `connected` yet: that waits for the relay's first
                    # ordinary frame (_admit), so a join it is about to refuse
                    # or hold never reads as connected.
                    self.last_relay_contact = time.time()
                    drain = asyncio.create_task(self._drain(socket))
                    try:
                        async for raw in socket:
                            self._handle_frame(raw)
                            if self.connected_since is not None:
                                admitted_since = self.connected_since
                            if self.fatal:
                                break
                    finally:
                        drain.cancel()
                        await asyncio.gather(drain, return_exceptions=True)
            except websockets.exceptions.InvalidURI as exc:
                self._set_fatal(f"invalid relay URL {self.server!r}: {exc}")
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                if self.connected:
                    print(
                        f"hivelane: relay connection lost for room {self.room} ({exc})",
                        file=sys.stderr,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bug must cost a reconnect, not the room
                print(
                    f"hivelane: room {self.room}: internal error in the relay connection ({exc!r}); reconnecting",
                    file=sys.stderr,
                )
            finally:
                self._socket = None
                self.connected = False
                self.connected_since = None
                # Hooks still waiting on the dropped link fail open now.
                self._fail_open_waiters()

            if self.fatal or self._stop.is_set():
                return
            if admitted_since is not None and time.time() - admitted_since >= STABLE_SECONDS:
                # Only a link that held reset the backoff: a relay (or proxy)
                # that admits and then drops at once must not get hammered.
                backoff = 1.0
            if self.retry_after > 0:
                backoff = max(backoff, self.retry_after)
                self.retry_after = 0.0
            # Jittered, so a relay restart is not met by every daemon at once.
            delay = backoff * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
            print(
                f"hivelane: reconnecting to room {self.room} in {delay:.0f}s",
                file=sys.stderr,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _admit(self) -> None:
        """The relay answered our join with room traffic: we are in."""
        if self.connected or self._socket is None or self.fatal is not None:
            return
        self.connected = True
        self.connected_since = time.time()
        print(f"hivelane: connected to room {self.room} as {self.name}")

    def _not_before(self, payload: str) -> float:
        """Monotonic time payload may go out: the relay silently drops an
        activity, intent or plan sent within 0.5 s of the previous one of its
        kind, so those are spaced SEND_SPACING apart (a reconnect's backlog
        then arrives whole instead of mostly dropped)."""
        kind = _payload_kind(payload)
        if kind not in PACED_KINDS:
            return 0.0
        last = self._last_sent.get(kind)
        return 0.0 if last is None else last + SEND_SPACING

    def _is_stale(self, payload: str) -> bool:
        """An edit_check whose hook already gave up: sending it late would
        still queue us behind (or claim) a file teammates wait for."""
        if _payload_kind(payload) != "edit_check":
            return False
        try:
            request_id = json.loads(payload).get("request_id")
        except (ValueError, AttributeError):
            return False
        return isinstance(request_id, str) and request_id in self._abandoned

    def _abandon(self, request_id: str) -> None:
        self._abandoned[request_id] = None
        while len(self._abandoned) > QUEUE_MEMORY:
            self._abandoned.popitem(last=False)

    async def _next_payload(self) -> str:
        """Next payload to send: any retained in-flight one, then priority, then bulk.

        A queue whose head is not due yet (SEND_SPACING) is skipped until it
        is, keeping each queue's order. The ready event is cleared before the
        queues are polled so a put landing during the poll either shows up in
        the poll or leaves the event set.
        """
        while True:
            if self._inflight is not None:
                return self._inflight
            self._outbox_ready.clear()
            now = time.monotonic()
            wake: Optional[float] = None
            for queue in (self._priority_outbox, self._outbox):
                while not queue.empty():
                    head = _peek(queue)
                    if head is not None and self._is_stale(head):
                        queue.get_nowait()
                        continue
                    due = self._not_before(head) if head is not None else 0.0
                    if due > now:
                        wake = due if wake is None else min(wake, due)
                        break
                    return queue.get_nowait()
            if wake is None:
                await self._outbox_ready.wait()
            else:
                try:
                    await asyncio.wait_for(self._outbox_ready.wait(), timeout=wake - now)
                except asyncio.TimeoutError:
                    pass

    async def _drain(self, socket: Any) -> None:
        while True:
            payload = await self._next_payload()
            self._inflight = payload
            await socket.send(payload)
            self._inflight = None
            kind = _payload_kind(payload)
            if kind in PACED_KINDS:
                self._last_sent[kind] = time.monotonic()
            self.last_relay_contact = time.time()

    async def close(self, grace: float = SHUTDOWN_GRACE) -> None:
        """Stop for good: flush debounced edits, give the outbox a moment to
        reach the relay, then close the socket and end the relay loop."""
        if self._closed:
            if self._task is not None:
                await asyncio.gather(self._task, return_exceptions=True)
            return
        self._closed = True
        self._stop.set()
        self.flush_batch()
        if self._batch_task is not None:
            self._batch_task.cancel()
        task = self._task
        if task is None:
            return
        if self._socket is None:
            # Connecting or backing off: there is nothing to deliver over a
            # socket that does not exist, so do not wait out a connect timeout.
            task.cancel()
        else:
            deadline = time.monotonic() + min(grace, FLUSH_GRACE)
            while self._socket is not None and self._has_backlog() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            socket = self._socket
            if socket is not None:
                try:
                    await asyncio.wait_for(socket.close(), timeout=min(grace, 2.0))
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass
        done, _ = await asyncio.wait({task}, timeout=grace)
        if not done:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _handle_frame(self, raw: str | bytes) -> None:
        """Dispatch one relay frame.

        A bad frame must not kill the connection: every hook would then fail
        open and the user would lose coordination with no signal that it
        happened.
        """
        # Any frame, even one we cannot parse, proves the link is alive.
        self.last_relay_contact = time.time()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print("hivelane: ignoring unparseable relay frame", file=sys.stderr)
            return
        if not isinstance(data, dict):
            return
        try:
            self._dispatch_frame(data)
        except ValidationError as exc:
            print(f"hivelane: ignoring malformed relay frame: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - see above: one frame, never the link
            kind = data.get("type")
            print(
                f"hivelane: ignoring a relay frame that could not be handled "
                f"({kind if isinstance(kind, str) else '?'}: {exc!r})",
                file=sys.stderr,
            )

    def _prune_plan_waiters(self) -> None:
        """Forget plans whose reply is long overdue (the relay sends none for a
        rate-limited or invalid plan), so the FIFO fallback stays in step and
        the table stays small."""
        cutoff = time.monotonic() - max(self.edit_check_timeout * 4, 30.0)
        for request_id, (_session, future, sent_at) in list(self._plan_waiters.items()):
            if sent_at < cutoff:
                self._plan_waiters.pop(request_id, None)
                if not future.done():
                    future.cancel()
        while len(self._plan_waiters) > 64:
            _, (_session, future, _at) = self._plan_waiters.popitem(last=False)
            if not future.done():
                future.cancel()

    def _on_plan_conflicts(self, conflicts: PlanConflicts, request_id: Optional[str] = None) -> None:
        """Pair a reply with its plan: by the request_id the relay echoes, or
        (a relay that does not) the oldest plan still waiting. One whose plan
        ack is still waiting goes on that ack; otherwise it is kept as a note
        for the session's next pre_edit ack."""
        if request_id:
            waiter = self._plan_waiters.pop(request_id, None)
            if waiter is None:
                return
        else:
            self._prune_plan_waiters()
            if not self._plan_waiters:
                return
            _, waiter = self._plan_waiters.popitem(last=False)
        session_id, future, _sent_at = waiter
        if self.e2e_key is not None:
            conflicts = self._open_plan_conflicts(conflicts)
        if not future.done():
            future.set_result(conflicts)
            return
        note = plan_conflict_note(conflicts)
        if note:
            self._add_session_note(session_id, note)

    def _note_peer_activity(self, event: dict[str, Any]) -> None:
        """Remember signature changes in a teammate's diffs (depgraph notes)."""
        user_id = event.get("user_id")
        if not isinstance(user_id, str) or user_id in self._self_ids:
            return
        at = event.get("timestamp")
        repo_id = self._local_repo_id(str(event.get("repo_id") or ""))
        for change in event.get("changes") or []:
            if isinstance(change, dict) and change.get("diff") and change.get("file_path"):
                self.deps.record_peer_diff(
                    user_id,
                    str(change["file_path"]),
                    str(change["diff"]),
                    at if isinstance(at, (int, float)) else None,
                    repo_id,
                )

    def _note_peers(self, event: dict[str, Any]) -> None:
        user_id = event.get("user_id")
        if not isinstance(user_id, str):
            user_id = None
        if event.get("type") == "user_left":
            if user_id:
                self._peers.discard(user_id)
            return
        if user_id and user_id not in self._self_ids:
            self._peers.add(user_id)
        users = event.get("users")
        for other in users if isinstance(users, list) else []:
            if isinstance(other, str) and other and other not in self._self_ids:
                self._peers.add(other)

    def _on_join_request(self, data: dict[str, Any]) -> None:
        """Someone waits at the door of a room we own: keep it for `hivelane
        status` / `hivelane room approve`, and say so in the log (one plain
        line: the background daemon's console is a log file)."""
        request_id = str(data.get("request_id") or "")
        if not request_id:
            return
        name = str(data.get("name") or "someone")
        agent = str(data.get("agent") or "unknown")
        self.join_requests[request_id] = {
            "request_id": request_id,
            "name": name,
            "agent": agent,
            "timestamp": data.get("timestamp"),
            "received_at": time.time(),
        }
        while len(self.join_requests) > 64:
            self.join_requests.popitem(last=False)
        print(
            f"hivelane: room {self.room}: {name} ({agent}) asks to join; "
            f"approve with `hivelane room approve {request_id}` "
            f"or deny with `hivelane room deny {request_id}`"
        )

    def _note_room_settings(self, event: dict[str, Any]) -> None:
        """A teammate's .hivelane.toml turned e2e on while ours says nothing of
        it: stop rather than keep sending them cleartext."""
        if event.get("type") != "room_settings" or not event.get("e2e"):
            return
        if self.e2e_required:
            return
        self._set_fatal(
            f"room {self.room} is end-to-end encrypted (set by {event.get('set_by') or 'a teammate'}), "
            "but this repo's .hivelane.toml does not say `e2e = true`. Pull the latest "
            ".hivelane.toml (and get the room key: `hivelane key set "
            f"{self.room}`), then run `hivelane up` again"
        )

    def _dispatch_frame(self, data: dict[str, Any]) -> None:
        kind = data.get("type")

        if kind == "join_pending":
            self.awaiting_approval = {
                "request_id": str(data.get("request_id") or ""),
                "reason": str(data.get("reason") or ""),
                "since": time.time(),
            }
            print(
                f"hivelane: room {self.room}: waiting for an owner to approve this join"
                + (f" ({data['reason']})" if data.get("reason") else "")
            )
            return
        if kind not in _NOT_ADMISSION:
            if self.awaiting_approval is not None:
                # Anything else after join_pending is the room's normal
                # traffic: an owner let us in.
                print(f"hivelane: room {self.room}: join approved")
                self.awaiting_approval = None
            # The relay's first ordinary frame (it sends the joiner its own
            # user_joined) is what admits us.
            self._admit()

        if kind == "join_request":
            self._on_join_request(data)
            return

        if kind == "room_policy_update":
            self.require_approval = bool(data.get("require_approval"))
            return

        if kind == "file_released":
            self._on_file_released(data)
            return

        if kind == "file_requested":
            self._on_file_requested(data)
            return

        if kind == "edit_check_response":
            response = EditCheckResponse.model_validate(data)
            rel = self._pending_paths.pop(response.request_id, None)
            if self.e2e_key is not None and rel is not None:
                response = self._open_response(response, rel)
            self._peers.update(other.user_id for other in response.others)
            future = self._pending.pop(response.request_id, None)
            if future is not None and not future.done():
                future.set_result(response)
            return

        if kind == "plan_conflicts":
            echoed = data.get("request_id")
            self._on_plan_conflicts(
                PlanConflicts.model_validate(data), echoed if isinstance(echoed, str) else None
            )
            return

        if kind == "hivelane_feed":
            for event in data.get("events") or []:
                if not isinstance(event, dict):
                    continue
                self._note_peers(event)
                self._note_room_settings(event)
                event = self._open_feed_event(event)
                if event.get("type") == "activity_update":
                    self._note_peer_activity(event)
            return

        self._note_room_settings(data)
        data = self._open_feed_event(data)
        if kind == "activity_update":
            self._note_peer_activity(data)

        # Join catch-up and other point-to-point events arrive unbatched, so the
        # same bookkeeping has to run on a bare frame as on a feed event.
        self._note_peers(data)

        if kind == "rate_limited":
            # Not fatal: the relay is throttling, not refusing. Honour its window
            # so reconnecting does not keep the limiter pinned.
            try:
                self.retry_after = min(max(float(data.get("retry_after") or 0), 0.0), 3600.0)
            except (TypeError, ValueError):
                self.retry_after = 0.0
            print(
                f"hivelane: relay is throttling connections ({data.get('reason', '')})",
                file=sys.stderr,
            )
            return

        if kind == "version_rejected":
            self._set_fatal(
                f"{data.get('reason', 'this hivelane build is too old for the relay')}\n"
                f"(relay speaks protocol {data.get('server_protocol', '?')}, "
                f"needs at least {data.get('min_supported', '?')}; "
                f"this build speaks {PROTOCOL_VERSION})"
            )
            return

        if kind == "name_rejected":
            self._set_fatal(
                f"the relay rejected the name {self.name!r}: {data.get('reason', '')}\n"
                f"{self.rename_hint}"
            )
        elif kind == "room_rejected":
            self._set_fatal(
                f"the relay rejected room {self.room!r}: {data.get('reason', '')}\n"
                "Check the room code, or have a teammate create the room first."
            )


# Relay frames that answer a join without admitting it.
_NOT_ADMISSION = frozenset(
    {"join_pending", "room_rejected", "name_rejected", "version_rejected", "rate_limited"}
)


def _payload_kind(payload: str) -> str:
    """The "type" of a frame we serialised (pydantic writes it first)."""
    prefix = '{"type":"'
    if payload.startswith(prefix):
        end = payload.find('"', len(prefix))
        if end > 0:
            return payload[len(prefix) : end]
    try:
        kind = json.loads(payload).get("type")
    except (ValueError, AttributeError):
        return ""
    return kind if isinstance(kind, str) else ""


def _peek(queue: asyncio.Queue[str]) -> Optional[str]:
    """The next item of queue without taking it (None if it cannot tell)."""
    items = getattr(queue, "_queue", None)
    try:
        return items[0] if items else None
    except (TypeError, IndexError, KeyError):
        return None


def _chunk_changes(changes: list[FileChange], budget: int) -> list[list[FileChange]]:
    """changes split into activity-message-sized groups: at most
    MAX_FILES_PER_MESSAGE files, and roughly budget bytes of JSON each (a
    frame over the relay's cap closes the socket and loses the batch)."""
    chunks: list[list[FileChange]] = []
    current: list[FileChange] = []
    size = 0
    budget = max(budget, 16 * 1024)
    for change in changes:
        # JSON escaping (newlines, quotes) roughly doubles a diff at worst.
        cost = 256 + len(change.file_path) + 64 * len(change.symbols) + 2 * len(change.diff.encode("utf-8", "replace"))
        if current and (len(current) >= MAX_FILES_PER_MESSAGE or size + cost > budget):
            chunks.append(current)
            current, size = [], 0
        current.append(change)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def _seal_activity(key: crypto.RoomKey, message: ActivityMessage) -> str:
    """The activity blob, cut down until it fits the relay's blob limit:
    whole diffs, then truncated ones, then paths and symbols alone."""

    def envelope(limit: Optional[int], prompt: Optional[str]) -> dict[str, Any]:
        return {
            "v": 1,
            "prompt": prompt,
            "branch": message.branch,
            "base_commit": message.base_commit,
            "changes": [
                {
                    "file_path": change.file_path,
                    "diff": change.diff if limit is None else change.diff[:limit],
                    "symbols": list(change.symbols),
                }
                for change in message.changes
            ],
        }

    blob = ""
    for limit, prompt in (
        (None, message.prompt),
        (E2E_DIFF_FALLBACK_CHARS, message.prompt),
        (0, (message.prompt or "")[:500] or None),
    ):
        blob = key.seal("activity", envelope(limit, prompt))
        if len(blob) <= E2E_BLOB_BUDGET:
            return blob
    # Still over (a great many files or symbols): paths alone. Never a blob
    # past the relay's limit, which would drop the whole activity.
    bare = {
        "v": 1,
        "prompt": None,
        "branch": message.branch,
        "base_commit": message.base_commit,
        "changes": [{"file_path": c.file_path, "diff": "", "symbols": []} for c in message.changes],
    }
    blob = key.seal("activity", bare)
    return blob if len(blob) <= E2E_BLOB_BUDGET else ""


def with_note(ack: EventAck, note: str) -> EventAck:
    """ack with a local warn-level note: allow becomes warn, an existing
    warn/block keeps its decision and gains the note."""
    reason = f"{ack.reason}; {note}" if ack.reason else note
    decision = "warn" if ack.decision == "allow" else ack.decision
    return ack.model_copy(update={"decision": decision, "reason": reason})


def plan_conflict_note(conflicts: PlanConflicts) -> str:
    """One line naming who is editing / planning which of the plan's files."""
    parts: list[str] = []
    for conflict in conflicts.conflicts[:5]:
        verb = "is editing" if conflict.kind == "edited" else "plans to edit"
        ago = int(conflict.seconds_ago)
        when = f"{ago // 60} min ago" if ago >= 60 else f"{ago}s ago"
        # A teammate's summary is quoted as theirs: it cannot close our quotes.
        summary = conflict.summary[:80].replace('"', "'") if conflict.summary else ""
        task = f' for "{summary}"' if summary else ""
        parts.append(f"{conflict.user_id} {verb} {conflict.file_path}{task} ({when})")
    if not parts:
        return ""
    text = "your plan overlaps teammates' work: " + "; ".join(parts)
    extra = len(conflicts.conflicts) - len(parts)
    if extra > 0:
        text += f" (and {extra} more)"
    return text


# --- the per-machine daemon -------------------------------------------------------


class RepoRegistration(BaseModel):
    """POST /repos body: everything a repo's config says about coordinating it."""

    root: str
    room: str
    relay: str = DEFAULT_SERVER
    name: Optional[str] = None
    agent: AgentKind = "claude-code"
    settings: Optional[RoomSettings] = None
    ignore: list[str] = []
    share_diffs: bool = True
    redact: bool = True
    redact_patterns: list[str] = []
    # The e2e room passphrase (settings.e2e rooms), resolved by the CLI from
    # HIVELANE_ROOM_KEY or the personal config. Loopback and token-guarded;
    # never echoed back by /status.
    room_key: Optional[str] = None
    # The room's owner token, if this machine created the room.
    owner_token: Optional[str] = None


class RepoRef(BaseModel):
    root: str


class JoinAnswer(BaseModel):
    """POST /rooms/decide body: an owner's answer to a join request."""

    request_id: str
    approve: bool


class PolicyChange(BaseModel):
    """POST /rooms/policy body: an owner's join policy for root's room."""

    root: str
    require_approval: bool


class WaitRequest(BaseModel):
    """POST /wait body (`hivelane wait`): wait up to timeout seconds for
    file_path (absolute, cwd-relative, or repo-relative) to be free."""

    cwd: str
    file_path: str
    timeout: float = Field(default=5.0, ge=0, le=WAIT_SLICE_MAX)


_UNROUTED = object()

# Single-room compatibility: on Daemon(room, name) these read and write the one
# connection, as they did when the daemon was that connection.
_DELEGATED = (
    "room",
    "name",
    "agent",
    "connected",
    "fatal",
    "retry_after",
    "last_relay_contact",
    "connected_since",
    "repo_id",
    "ws_url",
    "_priority_outbox",
    "_outbox",
    "_outbox_ready",
    "_inflight",
    "_pending",
    "_peers",
    "_old_content",
    "_batch",
    "_last_prompt",
    "_send",
    "_next_payload",
    "_drain",
    "_handle_frame",
    "_dispatch_frame",
    "_note_peers",
    "_remember_old",
    "flush_batch",
)


def _reject_cross_origin(request: Request) -> None:
    """Browsers send Origin on every cross-site POST (and on DNS-rebound
    ones); hooks and the CLI never do. Without this any web page could shut
    the daemon down or point it at a room of its choosing."""
    if request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="cross-origin requests are not accepted")


_TOKEN_HEADER_KEY = auth.TOKEN_HEADER.lower().encode("ascii")


class LocalGuard:
    """ASGI middleware in front of every daemon route.

    1. Host must be 127.0.0.1:<port> or localhost:<port>. A DNS-rebinding page
       reaches the loopback socket under its own hostname, and this is the one
       header it cannot forge (421 otherwise).
    2. Every route except `GET /health` needs X-Hivelane-Token equal to the
       install's token (constant-time compare; 401 otherwise). On a mismatch
       the token file is re-read once, so a token regenerated on disk while the
       daemon runs (a deleted HIVELANE_HOME, say) heals instead of locking
       every client out until a restart.

    Plain ASGI rather than @app.middleware: BaseHTTPMiddleware costs a task
    and a memory stream per request, and every edit's hook waits on this one.
    """

    def __init__(self, app: Any, daemon: "Daemon") -> None:
        self.app = app
        self.daemon = daemon

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        host = b""
        presented = b""
        for name, value in scope.get("headers") or ():
            if name == b"host":
                host = value
            elif name == _TOKEN_HEADER_KEY:
                presented = value
        if host.lower() not in self.daemon.allowed_hosts():
            await _plain_reply(send, 421, "this daemon only answers requests for 127.0.0.1 or localhost")
            return
        if scope.get("path") == "/health" and scope.get("method") in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        if not self.daemon.token_matches(presented):
            await _plain_reply(
                send,
                401,
                f"missing or wrong {auth.TOKEN_HEADER} header (the token is in {auth.token_path()})",
            )
            return
        await self.app(scope, receive, send)


async def _plain_reply(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class Daemon:
    """The loopback HTTP endpoint for agent hooks, routing each event by its
    cwd to the repo it belongs to.

    Daemon(room, name, ...) is single-room mode (`hivelane join`): one
    connection that every event falls through to whatever its cwd, with the
    old exit-on-fatal behaviour. Daemon() is multi-repo mode (`hivelane serve`):
    repos are registered over POST /repos or on first sight of a .hivelane.toml,
    and a repo the relay turns away is reported in /status, not fatal to the
    daemon. Explicit registrations work in both modes.
    """

    def __init__(
        self,
        room: Optional[str] = None,
        name: Optional[str] = None,
        *,
        agent: AgentKind = "claude-code",
        server: str = DEFAULT_SERVER,
        port: int = DEFAULT_PORT,
        batch_seconds: float = BATCH_SECONDS,
        edit_check_timeout: float = EDIT_CHECK_TIMEOUT,
        root: Optional[str] = None,
        settings: Optional[dict[str, float] | RoomSettings] = None,
        ignore: Iterable[str] = (),
        share_diffs: bool = True,
        auto_register: Optional[bool] = None,
        token: Optional[str] = None,
        room_key: Optional[str] = None,
        owner_token: Optional[str] = None,
        redact: bool = True,
        redact_patterns: Iterable[str] = (),
    ) -> None:
        if (room is None) != (name is None):
            raise ValueError("room and name go together: both for single-room mode, neither for multi-repo")
        self.port = port
        self.server = server
        # The secret every HTTP caller presents (hivelane.auth). None until the
        # daemon serves: run() loads or creates HIVELANE_HOME/token then, so
        # tests driving handle_event directly never touch the file.
        self.token: Optional[str] = token
        self._token_pinned = token is not None
        self._token_path: Optional[Path] = None
        self._timing = {"batch_seconds": batch_seconds, "edit_check_timeout": edit_check_timeout}
        # Explicit project root (--root). Replaces git discovery for path keys:
        # outside a repo it is the only anchor teammates can share.
        self.root: Optional[Path] = Path(root).expanduser().resolve() if root else None

        self._default: Optional[RepoBinding] = None
        if room is not None and name is not None:
            room_settings = _room_settings(settings)
            connection = RoomConnection(
                room,
                name,
                agent=agent,
                server=server,
                batch_seconds=batch_seconds,
                edit_check_timeout=edit_check_timeout,
                settings=room_settings,
                e2e_key=_room_key(room_settings, room_key, room),
                owner_token=owner_token,
            )
            self._default = RepoBinding(
                root=self.root,
                connection=connection,
                ignore=IgnoreRules(ignore),
                share_diffs=share_diffs,
                redactor=_redactor(redact, redact_patterns),
            )
        # Single-room mode sends everything to its room, as it always has; only
        # the per-machine daemon adopts repos from their .hivelane.toml.
        self.auto_register = (self._default is None) if auto_register is None else auto_register

        self._bindings: dict[Path, RepoBinding] = {}
        self._route_cache: dict[str, Any] = {}
        self._negative: dict[str, float] = {}
        # Roots explicitly disconnected: never re-adopted from their config
        # until registered again, or `hivelane disconnect` would not stick.
        self._disabled: set[Path] = set()
        self._discovering: dict[str, asyncio.Task[Optional[RepoBinding]]] = {}
        # Where single-room mode was started, for display in /status.
        self._anchor: Optional[Path] = None

        self._stopping = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        self.app = self._build_app()

    # --- single-room compatibility ---------------------------------------------

    @property
    def connection(self) -> RoomConnection:
        """The single-room connection (Daemon(room, name) only)."""
        if self._default is None:
            raise AttributeError("a multi-repo daemon has no single connection")
        return self._default.connection

    @property
    def batch_seconds(self) -> float:
        if self._default is not None:
            return self._default.connection.batch_seconds
        return self._timing["batch_seconds"]

    @batch_seconds.setter
    def batch_seconds(self, value: float) -> None:
        self._timing["batch_seconds"] = value
        if self._default is not None:
            self._default.connection.batch_seconds = value

    @property
    def edit_check_timeout(self) -> float:
        if self._default is not None:
            return self._default.connection.edit_check_timeout
        return self._timing["edit_check_timeout"]

    @edit_check_timeout.setter
    def edit_check_timeout(self, value: float) -> None:
        self._timing["edit_check_timeout"] = value
        if self._default is not None:
            self._default.connection.edit_check_timeout = value

    # --- local HTTP ----------------------------------------------------------

    def allowed_hosts(self) -> tuple[bytes, ...]:
        """Host header values LocalGuard accepts (lowercase)."""
        port = str(self.port).encode("ascii")
        return (b"127.0.0.1:" + port, b"localhost:" + port)

    def ensure_token(self) -> str:
        if self.token is None:
            # Pinned at first use: the file this daemon trusts does not move
            # if HIVELANE_HOME changes under it later.
            self._token_path = auth.token_path()
            self.token = auth.ensure_token(self._token_path)
        return self.token

    def current_token(self) -> str:
        """The token to prove: the one on disk, so a token regenerated while
        the daemon runs heals here too (as in token_matches)."""
        token = self.ensure_token()
        if self._token_pinned:
            return token
        fresh = auth.read_token(self._token_path)
        if fresh and fresh != token:
            print("hivelane: the token file changed on disk; using the new token")
            self.token = fresh
            return fresh
        return token

    def token_matches(self, presented: bytes) -> bool:
        """Constant-time check of a request's X-Hivelane-Token."""
        expected = self.ensure_token().encode("ascii")
        if presented and hmac.compare_digest(presented, expected):
            return True
        if not presented or self._token_pinned:
            return False
        fresh = auth.read_token(self._token_path)
        if fresh is None or fresh == self.token:
            return False
        if hmac.compare_digest(presented, fresh.encode("ascii")):
            print("hivelane: the token file changed on disk; using the new token")
            self.token = fresh
            return True
        return False

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="hivelane-daemon", docs_url=None, redoc_url=None)
        app.add_middleware(LocalGuard, daemon=self)

        @app.get("/health")
        async def health(nonce: Optional[str] = None) -> dict[str, Any]:
            # Unauthenticated, so it says nothing beyond "a hivelane daemon is
            # here": enough for `is_running` and for a client to tell a token
            # problem from a dead port. With a nonce it also proves this is the
            # install's daemon (auth.daemon_proof), so clients never hand their
            # token or content to another process on this port.
            body: dict[str, Any] = {"ok": True, "service": "hivelane-daemon"}
            if nonce is not None and auth.NONCE_RE.fullmatch(nonce):
                body["proof"] = auth.daemon_proof(self.current_token(), nonce)
            return body

        @app.get("/status")
        async def status() -> dict[str, Any]:
            return self.status()

        @app.post("/events")
        async def events(event: NormalizedEvent) -> EventAck:
            return await self.handle_event(event)

        @app.post("/repos")
        async def add_repo(body: RepoRegistration, request: Request) -> dict[str, Any]:
            _reject_cross_origin(request)
            try:
                binding = await self.register(
                    body.root,
                    body.room,
                    relay=body.relay,
                    name=body.name,
                    agent=body.agent,
                    settings=body.settings,
                    ignore=body.ignore,
                    share_diffs=body.share_diffs,
                    redact=body.redact,
                    redact_patterns=body.redact_patterns,
                    room_key=body.room_key,
                    owner_token=body.owner_token,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            return self._binding_status(binding)

        @app.post("/repos/remove")
        async def remove_repo(body: RepoRef, request: Request) -> dict[str, Any]:
            _reject_cross_origin(request)
            return {"removed": await self.unregister(body.root)}

        @app.post("/rooms/decide")
        async def decide(body: JoinAnswer, request: Request) -> dict[str, Any]:
            _reject_cross_origin(request)
            for conn in self._connections():
                if body.request_id in conn.join_requests:
                    if not conn.decide_join(body.request_id, body.approve):
                        break
                    return {"sent": True, "room": conn.room, "approve": body.approve}
            raise HTTPException(
                status_code=404,
                detail=f"no pending join request {body.request_id!r} in a room this machine owns "
                "(see `hivelane status`; requests expire)",
            )

        @app.post("/rooms/policy")
        async def policy(body: PolicyChange, request: Request) -> dict[str, Any]:
            _reject_cross_origin(request)
            binding = await self.binding_for(body.root)
            if binding is None:
                raise HTTPException(status_code=404, detail=f"{body.root} is not connected to a room")
            conn = binding.connection
            if not conn.owner_token:
                raise HTTPException(
                    status_code=409,
                    detail=f"this machine has no owner token for room {conn.room} "
                    "(only the machine that created the room holds one)",
                )
            if not conn.live:
                raise HTTPException(status_code=409, detail=f"room {conn.room} is not connected right now")
            conn.set_policy(body.require_approval)
            return {"sent": True, "room": conn.room, "require_approval": body.require_approval}

        @app.post("/wait")
        async def wait(body: WaitRequest, request: Request) -> dict[str, Any]:
            _reject_cross_origin(request)
            return await self.wait_for_file(body.cwd, body.file_path, body.timeout)

        @app.post("/shutdown")
        async def shutdown(request: Request) -> dict[str, Any]:
            _reject_cross_origin(request)
            # After this response is on its way, not before.
            asyncio.get_running_loop().call_soon(self.stop)
            return {"stopping": True, "pid": os.getpid()}

        return app

    # --- hivelane wait -------------------------------------------------------------

    @staticmethod
    def _wait_path(file_path: str, cwd: str, binding: RepoBinding) -> str:
        """The path key for a path typed at a shell: absolute, relative to the
        cwd when that names a file, else relative to the repo root (the form
        block messages print, so it works from any directory of the repo)."""
        candidate = Path(file_path)
        if not candidate.is_absolute():
            local = Path(cwd) / candidate
            if local.exists():
                candidate = local
            else:
                root = binding.root if binding.root is not None else repo_root(cwd)
                if root is None:
                    return relative_path(str(local), cwd)
                candidate = root / candidate
        return repo_relative_path(str(candidate), cwd, binding.root)

    async def wait_for_file(self, cwd: str, file_path: str, timeout: float) -> dict[str, Any]:
        binding = await self.binding_for(cwd)
        if binding is None:
            return {"free": True, "state": "uncoordinated", "file_path": file_path}
        rel = self._wait_path(file_path, cwd, binding)
        if binding.ignore.matches(rel):
            return {"free": True, "state": "ignored", "file_path": rel}
        conn = binding.connection
        repo_id = await conn.repo_id_for_cwd(cwd, binding)
        return await conn.wait_for_file(rel, repo_id, timeout)

    # --- status ----------------------------------------------------------------

    def _all_bindings(self) -> list[RepoBinding]:
        bindings = list(self._bindings.values())
        if self._default is not None:
            bindings.insert(0, self._default)
        return bindings

    def _connections(self) -> list[RoomConnection]:
        seen: dict[int, RoomConnection] = {}
        for binding in self._all_bindings():
            seen.setdefault(id(binding.connection), binding.connection)
        return list(seen.values())

    def _binding_repo_id(self, binding: RepoBinding) -> str:
        if binding is self._default:
            # The identity it joined with (run() resolves it from its cwd).
            return binding.connection.repo_id
        return cached_repo_id(binding.root) or ""

    def _binding_status(self, binding: RepoBinding) -> dict[str, Any]:
        conn = binding.connection
        root = binding.root if binding.root is not None else self._anchor
        return {
            "root": str(root) if root is not None else None,
            "room": conn.room,
            "relay": conn.server,
            "server": conn.ws_url,
            "name": conn.name,
            "agent": conn.agent,
            "connected": conn.live,
            "last_relay_contact": conn.last_relay_contact,
            "connected_since": conn.connected_since,
            "peers": sorted(conn._peers),
            "queued_events": conn.queued_events,
            "repo_id": self._binding_repo_id(binding),
            "fatal": conn.fatal,
            # What the team's file set (history_hours only when it says so).
            "settings": (
                conn.settings.model_dump(exclude_unset=True)
                if conn.settings is not None
                else None
            ),
            "ignore": list(binding.ignore.patterns),
            "share_diffs": binding.share_diffs,
            "auto": binding.auto,
            "redact": binding.redactor.enabled,
            # Whether, never what: the key and the owner token stay in here.
            "e2e": conn.e2e_required,
            "e2e_key": conn.e2e_key is not None,
            "owner": bool(conn.owner_token),
            "require_approval": conn.require_approval,
            "awaiting_approval": dict(conn.awaiting_approval) if conn.awaiting_approval else None,
            "join_requests": [
                {k: v for k, v in request.items() if k != "received_at"}
                for request in conn.pending_join_requests()
            ],
            # Teammates' files this connection is waiting in line for.
            "queued": [
                {"file_path": rel, "position": queued.get("position", 0)}
                for (_, rel), queued in conn._queued.items()
            ],
        }

    def status(self) -> dict[str, Any]:
        bindings = self._all_bindings()
        result: dict[str, Any] = {
            "version": __version__,
            "pid": os.getpid(),
            "port": self.port,
            "mode": "single-room" if self._default is not None else "multi-repo",
            "repos": [self._binding_status(binding) for binding in bindings],
        }
        if len(bindings) == 1:
            # The pre-multi-repo shape, for callers written against one room.
            binding = bindings[0]
            conn = binding.connection
            result.update(
                {
                    "connected": conn.live,
                    "room": conn.room,
                    "name": conn.name,
                    "agent": conn.agent,
                    "server": conn.ws_url,
                    "peers": sorted(conn._peers),
                    "queued_events": conn.queued_events,
                    "last_relay_contact": conn.last_relay_contact,
                    "connected_since": conn.connected_since,
                    "root": str(binding.root) if binding.root is not None else None,
                    "repo_id": self._binding_repo_id(binding),
                    "fatal": conn.fatal,
                }
            )
        return result

    # --- routing ---------------------------------------------------------------

    def _lookup_root(self, cwd: str) -> Optional[Path]:
        try:
            current = Path(cwd).resolve()
        except (OSError, ValueError):
            return None
        for candidate in (current, *current.parents):
            if candidate in self._bindings:
                return candidate
        return None

    def _registered_for(self, cwd: str) -> Optional[RepoBinding]:
        """The deepest registered repo containing cwd (cached per cwd string:
        hooks from one agent repeat the same cwd on every event)."""
        if not self._bindings or not cwd:
            return None
        key = self._route_cache.get(cwd, _UNROUTED)
        if key is _UNROUTED:
            key = self._lookup_root(cwd)
            if len(self._route_cache) > ROUTE_CACHE_SIZE:
                self._route_cache.clear()
            self._route_cache[cwd] = key
        return self._bindings.get(key) if key is not None else None

    async def binding_for(self, cwd: str) -> Optional[RepoBinding]:
        """The repo an event from cwd belongs to, registering it from its
        .hivelane.toml if need be; None means nobody coordinates it."""
        binding = self._registered_for(cwd)
        if binding is not None:
            conn = binding.connection
            if not (
                binding.auto
                and conn.fatal_at is not None
                and time.monotonic() - conn.fatal_at > FATAL_RETRY_SECONDS
            ):
                return binding
            # Turned away a while ago; the room may exist by now.
            await self._unbind(binding)
        if self._default is not None:
            return self._default
        if not self.auto_register or not cwd:
            return None
        return await self._discover(cwd)

    async def _discover(self, cwd: str) -> Optional[RepoBinding]:
        expiry = self._negative.get(cwd)
        if expiry is not None:
            if expiry > time.monotonic():
                return None
            del self._negative[cwd]
        task = self._discovering.get(cwd)
        if task is None:
            task = asyncio.create_task(self._discover_and_register(cwd))
            self._discovering[cwd] = task
            task.add_done_callback(lambda _t, cwd=cwd: self._discovering.pop(cwd, None))
        # Shielded: a hook that gives up must not cancel the registration the
        # next hook from the same repo will want.
        return await asyncio.shield(task)

    def _remember_unconfigured(self, cwd: str) -> None:
        if len(self._negative) > ROUTE_CACHE_SIZE:
            self._negative.clear()
        self._negative[cwd] = time.monotonic() + NEGATIVE_CACHE_SECONDS

    async def _discover_and_register(self, cwd: str) -> Optional[RepoBinding]:
        found = await asyncio.to_thread(discover_repo, cwd)
        if found is None:
            self._remember_unconfigured(cwd)
            return None
        config, name = found
        assert config.root is not None and config.room is not None
        key = config.root.resolve()
        if key in self._disabled:
            self._remember_unconfigured(cwd)
            return None
        existing = self._bindings.get(key)
        if existing is not None:
            self._route_cache.clear()
            return existing
        settings: Optional[RoomSettings]
        try:
            settings = RoomSettings.model_validate(config.room_settings())
        except ValidationError as exc:
            # The team's windows are out of the relay's bounds: coordinate
            # under the room's current windows rather than not at all.
            print(f"hivelane: ignoring settings in {config.root}: {exc}", file=sys.stderr)
            settings = None
        agent = config.agent if config.agent in AGENT_KINDS else "unknown"
        if transport_problem(config.relay, config.insecure):
            print(
                f"hivelane: not coordinating {config.root}: {transport_problem(config.relay, config.insecure)}",
                file=sys.stderr,
            )
            self._remember_unconfigured(cwd)
            return None
        try:
            return await self.register(
                key,
                config.room,
                relay=config.relay,
                name=name,
                agent=agent,  # type: ignore[arg-type]
                settings=settings,
                ignore=config.ignore,
                share_diffs=config.share_diffs,
                redact=config.redact,
                redact_patterns=config.redact_patterns,
                # Never the bare HIVELANE_ROOM_KEY of whoever started the
                # daemon: it is one room's key, and this may be another room.
                room_key=config.room_key(generic_env=False),
                owner_token=config.owner_token(),
                auto=True,
            )
        except ValueError as exc:
            print(f"hivelane: not coordinating {config.root}: {exc}", file=sys.stderr)
            self._remember_unconfigured(cwd)
            return None

    # --- registration ------------------------------------------------------------

    def _shared_connection(
        self, server: str, room: str, name: str, agent: str
    ) -> Optional[RoomConnection]:
        """A live connection the relay already knows this name by, in this room."""
        ws_url = f"{server.rstrip('/')}/{room}"
        for conn in self._connections():
            if (
                conn.ws_url == ws_url
                and conn.name == name
                and conn.agent == agent
                and conn.fatal is None
                and not conn._closed
            ):
                return conn
        return None

    async def register(
        self,
        root: str | Path,
        room: str,
        *,
        relay: str = DEFAULT_SERVER,
        name: Optional[str] = None,
        agent: AgentKind = "claude-code",
        settings: Optional[dict[str, Any] | RoomSettings] = None,
        ignore: Iterable[str] = (),
        share_diffs: bool = True,
        auto: bool = False,
        redact: bool = True,
        redact_patterns: Iterable[str] = (),
        room_key: Optional[str] = None,
        owner_token: Optional[str] = None,
    ) -> RepoBinding:
        """Coordinate the repo at root in room (replacing any earlier
        registration of it). Raises ValueError for an unusable registration.

        `room_key` is the e2e passphrase (used only when settings say e2e);
        `owner_token` marks this machine as the room's owner on join."""
        try:
            key = Path(root).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise ValueError(f"bad repo root {root!r}: {exc}") from None
        room = (room or "").strip()
        if not room:
            raise ValueError("a room is required")
        if agent not in AGENT_KINDS:
            raise ValueError(f"unknown agent {agent!r}")
        try:
            room_settings = _room_settings(settings)
        except ValidationError as exc:
            raise ValueError(f"invalid settings: {exc}") from None
        relay = (relay or DEFAULT_SERVER).strip()
        if not name:
            name = await asyncio.to_thread(default_name, key)
            if not name:
                raise ValueError("no display name: set `name` in ~/.hivelane/config.toml")
        ignore = tuple(ignore)
        redact_patterns = tuple(redact_patterns)
        redactor = _redactor(redact, redact_patterns)
        # Off the event loop: the v2 KDF stretches the passphrase (~0.5 s
        # the first time per room), and every hook shares this loop.
        e2e_key = await asyncio.to_thread(_room_key, room_settings, room_key, room)
        # Warm the per-root identity cache off the event loop, so neither the
        # join nor the first hook from this repo waits on git.
        repo_id = await asyncio.to_thread(repo_id_for, key)

        spec = (
            relay,
            room,
            name,
            agent,
            room_settings.model_dump_json() if room_settings is not None else None,
            ignore,
            share_diffs,
            redact,
            redact_patterns,
            # Fingerprints, so a changed key or token re-registers; the
            # secrets themselves are kept only on the connection.
            hashlib.sha256(room_key.encode("utf-8")).hexdigest() if room_key else None,
            hashlib.sha256(owner_token.encode("utf-8")).hexdigest() if owner_token else None,
        )
        existing = self._bindings.get(key)
        if existing is not None and existing.spec == spec and existing.connection.fatal is None:
            # Re-running `hivelane connect` must not bounce a healthy connection.
            existing.auto = existing.auto and auto
            return existing
        if existing is not None:
            await self._unbind(existing)
        self._disabled.discard(key)

        key_fingerprint = (
            hashlib.sha256(room_key.encode("utf-8")).hexdigest() if e2e_key is not None and room_key else None
        )
        conn = self._shared_connection(relay, room, name, agent)
        if conn is not None:
            # One socket per name per room: a repo joining it brings what it
            # knows (e2e, a key for the same room, an owner token) to the
            # shared link. Encryption is per room, so it is only ever turned
            # on here: one repo's e2e = true covers every repo on the link,
            # never cleartext because another repo registered first.
            if (
                key_fingerprint is not None
                and conn.key_fingerprint is not None
                and conn.key_fingerprint != key_fingerprint
            ):
                raise ValueError(
                    f"room {room} is already joined on this machine with a different room key; "
                    f"run `hivelane key set {room}` with the team's key so every repo uses the same one"
                )
            if conn.e2e_key is None and e2e_key is not None:
                conn.e2e_key = e2e_key
                conn.key_fingerprint = key_fingerprint
            if room_settings is not None and room_settings.e2e and not conn.e2e_required:
                conn.e2e_required = True
                conn.settings = room_settings
            problem = conn._e2e_problem()
            if problem:
                conn.fail(problem)
            conn.owner_token = conn.owner_token or owner_token
        if conn is None:
            conn = RoomConnection(
                room,
                name,
                agent=agent,
                server=relay,
                batch_seconds=self._timing["batch_seconds"],
                edit_check_timeout=self._timing["edit_check_timeout"],
                settings=room_settings,
                rename_hint=(
                    "Pick another name (`name` in ~/.hivelane/config.toml or "
                    "HIVELANE_NAME) and reconnect the repo."
                ),
                e2e_key=e2e_key,
                owner_token=owner_token,
            )
            conn.repo_id = repo_id
            conn.repo_root = key
            conn.key_fingerprint = key_fingerprint
            if self._running:
                self._start_connection(conn)
        binding = RepoBinding(
            root=key,
            connection=conn,
            ignore=IgnoreRules(ignore),
            share_diffs=share_diffs,
            auto=auto,
            spec=spec,
            redactor=redactor,
        )
        self._bindings[key] = binding
        self._route_cache.clear()
        self._negative.clear()
        how = "found" if auto else "registered"
        extras = " (end-to-end encrypted)" if conn.e2e_required else ""
        print(f"hivelane: {how} {key}: room {room} as {name} via {relay}{extras}")
        return binding

    async def unregister(self, root: str | Path) -> bool:
        """Stop coordinating the repo at root; True if it was registered."""
        try:
            key = Path(root).expanduser().resolve()
        except (OSError, ValueError):
            return False
        self._disabled.add(key)
        binding = self._bindings.get(key)
        if binding is None:
            return False
        await self._unbind(binding)
        print(f"hivelane: stopped coordinating {key}")
        return True

    async def _unbind(self, binding: RepoBinding) -> None:
        if binding.root is not None and self._bindings.get(binding.root) is binding:
            del self._bindings[binding.root]
        self._route_cache.clear()
        conn = binding.connection
        if all(other.connection is not conn for other in self._all_bindings()):
            await conn.close()

    def _start_connection(self, conn: RoomConnection) -> None:
        task = conn.start()
        if self._default is None or conn is not self._default.connection:
            task.add_done_callback(lambda _t, conn=conn: self._report_fatal(conn))

    @staticmethod
    def _report_fatal(conn: RoomConnection) -> None:
        if conn.fatal:
            print(f"hivelane: room {conn.room}: {conn.fatal}", file=sys.stderr)

    # --- events ----------------------------------------------------------------

    async def handle_event(self, event: NormalizedEvent) -> EventAck:
        binding = await self.binding_for(event.cwd)
        if binding is None:
            # Nobody coordinates this repo: fail open.
            return EventAck()
        return await binding.connection.handle_event(event, binding)

    # --- lifecycle -----------------------------------------------------------

    def warn_if_unanchored(self) -> bool:
        """Warn when path keys have nothing shared to be relative to.

        Inside a git repo every agent's paths are keyed on the repo root, so
        teammates in different subdirectories still agree on what a file is
        called. Outside one, with no --root, each agent's own cwd is the
        anchor, and the same file gets a different key per directory: overlaps
        between those agents silently go undetected. Returns True if it warned.
        """
        if self.root is not None:
            return False
        cwd = Path.cwd()
        if repo_root(str(cwd)) is not None:
            return False
        print(
            f"hivelane: warning: {cwd} is not inside a git repository, so file "
            "paths fall back to each agent's working directory. Teammates whose "
            "agents run from different directories will not see each other's "
            "overlaps. Pass --root <project dir> (the same project directory "
            "for everyone) to key paths on it instead.",
            file=sys.stderr,
        )
        return True

    async def start(self) -> None:
        """Start every registered connection on the running loop. run() calls
        this; tests driving handle_event directly may call it themselves."""
        self._loop = asyncio.get_running_loop()
        self._running = True
        for conn in self._connections():
            self._start_connection(conn)

    async def aclose(self) -> None:
        """Graceful stop of the relay side: flush batches, close sockets."""
        self._running = False
        for task in list(self._discovering.values()):
            task.cancel()
        await asyncio.gather(
            *(conn.close() for conn in self._connections()), return_exceptions=True
        )

    def stop(self) -> None:
        """Ask run() to shut down gracefully. Safe from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._stopping.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._stopping.set()
        else:
            loop.call_soon_threadsafe(self._stopping.set)

    @contextlib.contextmanager
    def _capture_signals(self) -> Any:
        """While serving: the first SIGINT/SIGTERM (Ctrl+C, `systemctl stop`,
        launchd, `hivelane down`'s kill) stops gracefully, flushing batched
        edits; a second one interrupts at once."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        wanted: list[int] = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            wanted.append(signal.SIGBREAK)  # Ctrl+Break on Windows
        original: dict[int, Any] = {}
        sig: int

        def handler(signum: int, _frame: Any) -> None:
            if self._stopping.is_set():
                raise KeyboardInterrupt
            print(f"hivelane: {signal.Signals(signum).name} received; shutting down")
            self.stop()

        for sig in wanted:
            try:
                original[sig] = signal.signal(sig, handler)
            except (OSError, ValueError):
                continue
        try:
            yield
        finally:
            for sig, previous in original.items():
                try:
                    signal.signal(sig, previous)
                except (OSError, ValueError):
                    pass

    async def run(self) -> int:
        default = self._default
        if default is not None:
            self.warn_if_unanchored()
            cwd = str(Path.cwd())
            self._anchor = self.root or repo_root(cwd) or Path(cwd)
            # Resolve the daemon's own repo before joining (sent with the join)
            # and so the first hook from it finds the identity already cached.
            default.connection.repo_id = await default.connection.repo_id_for_cwd(cwd, default)

        # Before the socket opens: the first hook must find the token on disk.
        await asyncio.to_thread(self.ensure_token)
        config = uvicorn.Config(
            self.app,
            host=LOOPBACK,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        # Our own SIGINT/SIGTERM handling, not uvicorn's: its handler only
        # stops the HTTP side and re-raises the signal afterwards, which skips
        # aclose() (batched edits lost) and, for SIGTERM, the pid file cleanup.
        server.capture_signals = self._capture_signals  # type: ignore[method-assign]
        print(f"hivelane: listening on http://{LOOPBACK}:{self.port}")
        if default is not None:
            conn = default.connection
            print(f"hivelane: joining {conn.ws_url} as {conn.name} ({conn.agent})")
        else:
            print("hivelane: coordinating every repo with a .hivelane.toml (and any registered)")

        await self.start()
        http = asyncio.create_task(server.serve(), name="http")
        stopper = asyncio.create_task(self._stopping.wait(), name="stop")
        waiters: set[asyncio.Task[Any]] = {http, stopper}
        relay = default.connection._task if default is not None else None
        if relay is not None:
            # Single-room mode ends with its room (fatal rejection).
            waiters.add(relay)
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

        # Best effort: get debounced edits out and sockets closed before the
        # HTTP side goes, so nothing in flight is dropped on the floor.
        await self.aclose()
        # uvicorn unwinds its lifespan on should_exit; cancelling it instead
        # spills a CancelledError traceback over our own error message.
        server.should_exit = True
        if not http.done():
            finished, _ = await asyncio.wait({http}, timeout=SHUTDOWN_GRACE)
            if not finished:
                http.cancel()
        stopper.cancel()
        await asyncio.gather(http, stopper, return_exceptions=True)

        if default is not None and default.connection.fatal:
            print(f"hivelane: {default.connection.fatal}", file=sys.stderr)
            return 1
        if not http.cancelled():
            exc = http.exception()
            if isinstance(exc, SystemExit):
                return int(exc.code or 1)
            if exc is not None:
                raise exc
        return 0


def _room_settings(
    settings: Optional[dict[str, Any] | RoomSettings],
) -> Optional[RoomSettings]:
    if settings is None or isinstance(settings, RoomSettings):
        return settings
    return RoomSettings.model_validate(settings)


def _room_key(
    settings: Optional[RoomSettings], passphrase: Optional[str], room: str
) -> Optional[crypto.RoomKey]:
    """The derived key for an e2e room, or None (not e2e, or no passphrase:
    the connection then refuses to join rather than send cleartext)."""
    if settings is None or not settings.e2e or not passphrase:
        return None
    return crypto.derive(passphrase, room)


def _redactor(enabled: bool, patterns: Iterable[str]) -> redact.Redactor:
    try:
        return redact.Redactor(enabled, patterns)
    except re.error as exc:
        raise ValueError(f"invalid redact_patterns: {exc}") from None


def _delegate(attr: str) -> property:
    def get(self: Daemon) -> Any:
        return getattr(self.connection, attr)

    def set_(self: Daemon, value: Any) -> None:
        setattr(self.connection, attr, value)

    return property(get, set_, doc=f"Single-room mode: the connection's {attr}.")


for _attr in _DELEGATED:
    setattr(Daemon, _attr, _delegate(_attr))
del _attr
