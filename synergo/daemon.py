"""The Synergo daemon: one asyncio process per machine that is both a relay
room member for every repo it coordinates and a loopback HTTP endpoint for the
agent hooks running in those repos.

Each repo, keyed by its root path, is bound to a RoomConnection: one WebSocket
to one relay room under one display name. Hooks only report their cwd, so the
HTTP side routes every event to the repo containing that cwd, registering a
repo on first sight when it carries a `.synergo.toml`.

`Daemon(room, name)` is the single-room form behind `synergo join`: one
connection that every event falls through to whatever its cwd, as before
there was more than one repo to serve. `Daemon()` is the per-machine form
behind `synergo serve` / the background service.
"""

from __future__ import annotations

import asyncio
import difflib
import getpass
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
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

from synergo import __version__, auth, crypto, gitinfo, redact, symbols
from synergo.depgraph import DepTracker
from synergo.config import ConfigFileError, SynergoConfig, load_config, transport_problem
from synergo.events import EventAck, NormalizedEvent
from synergo.protocol import (
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

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 4600
DEFAULT_SERVER = "ws://localhost:8000/ws"

BATCH_SECONDS = 1.5
EDIT_CHECK_TIMEOUT = 1.5
MAX_BACKOFF = 30.0
SHUTDOWN_GRACE = 5.0
# How long a stopping connection waits for its outbox to reach the relay
# before closing the socket anyway.
FLUSH_GRACE = 1.0
OLD_CONTENT_CACHE_SIZE = 512
REPO_ROOT_CACHE_SIZE = 64
ROUTE_CACHE_SIZE = 256
# Ordinary (non-priority) messages kept for the relay while it is unreachable,
# per room connection; the oldest is dropped beyond it. SYNERGO_OUTBOX_LIMIT
# overrides it, 0 meaning unlimited (a long outage then grows memory).
DEFAULT_OUTBOX_LIMIT = 500


def _outbox_limit(raw: Optional[str]) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_OUTBOX_LIMIT
    return value if value >= 0 else DEFAULT_OUTBOX_LIMIT


OUTBOX_LIMIT = _outbox_limit(os.environ.get("SYNERGO_OUTBOX_LIMIT"))
# Hex characters of repo identity sent on the wire: 64 bits is ample to keep
# the handful of repos in one room apart, and short enough to read in logs.
REPO_ID_LENGTH = 16
GIT_TIMEOUT = 3.0
# A cwd with no project config is re-checked at most this often, so repos that
# never opted in do not pay a filesystem walk (and a TOML parse) on every edit,
# while one that just ran `synergo init` is picked up within half a minute.
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
E2E_BLOB_BUDGET = E2E_MAX_CHARS - 1024
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
# One POST /wait holds at most this long; `synergo wait` polls in slices.
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
    ".synergo.toml",
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
    neither, or a directory that is not a repo at all, yields "" — which the
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


_repo_ids: dict[str, str] = {}


def cached_repo_id(root: Optional[Path]) -> Optional[str]:
    """The cached id for root, "" for no root, or None if not computed yet."""
    if root is None:
        return ""
    return _repo_ids.get(str(root))


def repo_id_for(root: Optional[Path]) -> str:
    """compute_repo_id, at most once per root for the life of the process.

    A repo's origin and root commit practically never change under a running
    daemon, and the pre_edit hook path cannot afford a git subprocess per call.
    """
    if root is None:
        return ""
    cached = _repo_ids.get(str(root))
    if cached is not None:
        return cached
    # Only a directory that is itself a repo root gets an identity. An explicit
    # --root that is not one must not inherit an enclosing repo's (a dotfiles
    # repo in $HOME, say): teammates would then get different ids for the same
    # project and stop seeing each other's overlaps.
    repo_id = compute_repo_id(root) if (root / ".git").exists() else ""
    if len(_repo_ids) > REPO_ROOT_CACHE_SIZE:
        _repo_ids.clear()
    _repo_ids[str(root)] = repo_id
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


def discover_repo(cwd: str) -> Optional[tuple[SynergoConfig, str]]:
    """The project config and display name for an unregistered cwd, or None
    when nothing there says which room to join.

    Blocking (filesystem walk, TOML parse, maybe `git config`): the daemon
    runs it in a worker thread.
    """
    try:
        config = load_config(cwd)
    except ConfigFileError as exc:
        print(f"synergo: not coordinating {cwd}: {exc}", file=sys.stderr)
        return None
    if not config.room or config.root is None:
        return None
    name = config.name or default_name(config.root)
    if not name:
        print(
            f"synergo: not coordinating {config.root}: no display name "
            "(set `name` in ~/.synergo/config.toml or SYNERGO_NAME)",
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
    # Registered on first sight from a .synergo.toml rather than explicitly.
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
        # Team conflict windows from the repo's .synergo.toml, sent on join.
        self.settings = settings
        self.rename_hint = rename_hint
        self._self_ids = {name, relay_user_id(name)}
        # End-to-end encryption: the team's .synergo.toml says e2e, and this
        # is the key derived from the room passphrase (None: not configured,
        # which is fatal rather than a fallback to cleartext).
        self.e2e_required = bool(settings is not None and settings.e2e)
        self.e2e_key = e2e_key
        # h:-token -> the name it hashes, learned from our own sends and from
        # teammates' decrypted blobs, so relay reasons read in real names.
        self._names: OrderedDict[str, str] = OrderedDict()
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
        # Identity of the repo this connection was opened for, sent on join.
        self.repo_id = ""

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
        self._peers: set[str] = set()
        self._old_content: OrderedDict[tuple[str, str], str] = OrderedDict()

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
        # Plan events waiting for the relay's plan_conflicts reply, in order.
        self._plan_waiters: list[tuple[str, asyncio.Future[PlanConflicts]]] = []
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
        # `synergo wait` callers: (repo_id, path key) -> futures resolved with
        # how the file came free ("released" or "allowed").
        self._file_waiters: dict[tuple[str, str], list[asyncio.Future[str]]] = {}

    @property
    def ws_url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.room}"

    @property
    def live(self) -> bool:
        """Joined and admitted: worth asking the relay before an edit."""
        return self.connected and self.awaiting_approval is None

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
        names: list[str] = []
        for change in envelope.get("changes") or []:
            if isinstance(change, dict):
                names.append(change.get("file_path"))
                names.extend(change.get("symbols") or [])
        names.append(envelope.get("file_path"))
        names.extend(envelope.get("files") or [])
        names.extend(envelope.get("symbols") or [])
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
            print(f"synergo: room {self.room}: cannot decrypt a teammate's message ({exc})", file=sys.stderr)
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
            return message.model_copy(update={"file_path": self._hash(rel), "e2e": blob})
        if isinstance(message, IntentMessage):
            blob = key.seal("intent", {"v": 1, "prompt": message.prompt})
            return message.model_copy(update={"prompt": "", "e2e": blob})
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
        # Redacted before it is remembered: the prompt rides on every later
        # activity message too.
        prompt = (redactor or redact.DEFAULT).text(event.prompt) or ""
        self._last_prompt = prompt
        self._send(IntentMessage(user_id=self.name, prompt=prompt, repo_id=repo_id))
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
        summary = binding.redactor.text(summary) or ""
        tracked = await asyncio.to_thread(tracked_files, root) if root is not None else []
        files = [
            f for f in resolve_plan_files(event.files or [], tracked) if not binding.ignore.matches(f)
        ]
        if not files and not event.symbols:
            return EventAck()
        head = gitinfo.head_for(root)
        future: asyncio.Future[PlanConflicts] = asyncio.get_running_loop().create_future()
        self._plan_waiters.append((event.session_id, future))
        self._send(
            PlanMessage(
                user_id=self.name,
                summary=summary[:2000],
                files=files,
                symbols=list(event.symbols or [])[:50],
                repo_id=repo_id,
                branch=(head.branch or "") if head else "",
            )
        )
        try:
            conflicts = await asyncio.wait_for(asyncio.shield(future), timeout=self.edit_check_timeout)
        except asyncio.TimeoutError:
            future.cancel()  # tells _on_plan_conflicts to keep the note for later
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
        self._send(
            EditCheckRequest(
                request_id=request_id,
                file_path=rel,
                repo_id=repo_id,
                symbols=names,
                line_ranges=ranges,
                branch=branch,
            )
        )
        try:
            response = await asyncio.wait_for(future, timeout=self.edit_check_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            self._pending_paths.pop(request_id, None)
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
            # `synergo wait` on it can stop.
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
        release answered a `synergo wait`). Used up either way; a block means
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
        return f"{rel} was released to you — it's your turn"

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
        key = (str(data.get("repo_id") or ""), rel)
        queued = self._queued.pop(key, None)
        sessions = set(queued["sessions"]) if queued else set()
        self._remember_bounded(self._released, key, {"at": time.time(), "sessions": sessions})
        self._resolve_file_waiters(key, "released")
        place = f" (you were #{queued['position']} in line)" if queued and queued.get("position") else ""
        print(f"synergo: room {self.room}: {rel} is free — your turn{place}")

    def _on_file_requested(self, data: dict[str, Any]) -> None:
        rel = self._frame_path(data)
        if not rel:
            return
        repo_id = str(data.get("repo_id") or "")
        by = str(data.get("by") or "a teammate")
        agent = str(data.get("agent") or "unknown")
        session = self._last_editor.get((repo_id, rel))
        self._add_session_note(
            session or f"*:{repo_id}",
            f"{by}'s agent is waiting for {rel} — finish up or hand it off",
        )
        print(f"synergo: room {self.room}: {by} ({agent}) is waiting for {rel}")

    async def wait_for_file(self, rel: str, repo_id: str, timeout: float) -> dict[str, Any]:
        """One slice of `synergo wait`: ask the relay for the file (it answers
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
            old = self._old_content.pop((event.session_id, rel), "")
        new = event.new_content or ""
        # Which definitions changed. Without a known old version every line
        # would count as changed, which says nothing, so nothing is sent.
        names: list[str] = []
        ranges: list[tuple[int, int]] = []
        if known_old and old != new:
            if max(len(old), len(new)) > INLINE_SYMBOLS_BYTES:
                names, ranges = await asyncio.to_thread(change_symbols, rel, old, new)
                await asyncio.to_thread(self.deps.record_local, rel, old, new, repo_id)
            else:
                names, ranges = change_symbols(rel, old, new)
                self.deps.record_local(rel, old, new, repo_id)
        if share_diffs:
            diff = unified_diff(rel, old, new)
            changed = bool(diff)
            # Secrets out, and a sensitive file's diff withheld entirely; the
            # path still goes, so overlaps on it are still detected.
            if diff:
                diff = (redactor or redact.DEFAULT).diff(rel, diff)
        else:
            # The path still goes out, so overlaps are still detected; the
            # content of the change stays on this machine.
            diff = ""
            changed = old != new
        if changed:
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
        self._old_content.pop(key, None)
        self._old_content[key] = content
        while len(self._old_content) > OLD_CONTENT_CACHE_SIZE:
            self._old_content.popitem(last=False)

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
        for repo_id, changes in by_repo.items():
            branch, commit = self._heads.get(repo_id, ("", ""))
            self._send(
                ActivityMessage(
                    user_id=self.name,
                    prompt=self._last_prompt,
                    changes=changes,
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
    ) -> None:
        sealable = (ActivityMessage, IntentMessage, EditCheckRequest, PlanMessage, FileRequest)
        if self.e2e_key is not None and isinstance(message, sealable):
            message = self._seal(message)
        elif self.e2e_required and isinstance(message, sealable):
            # An e2e room without a key: never fall back to cleartext.
            return
        payload = message.model_dump_json()
        # A plan ack, like an edit_check, has an agent hook waiting on it; an
        # owner's decision has a teammate waiting at the door; a file_request
        # has `synergo wait` (and so an agent) waiting on it.
        if isinstance(message, (EditCheckRequest, PlanMessage, FileRequest, JoinDecision, RoomPolicy)):
            self._priority_outbox.put_nowait(payload)
        else:
            if OUTBOX_LIMIT > 0 and self._outbox.qsize() >= OUTBOX_LIMIT:
                self._outbox.get_nowait()
            self._outbox.put_nowait(payload)
        self._outbox_ready.set()

    def _join_payload(self) -> str:
        payload = JoinMessage(
            name=self.name,
            agent=self.agent,
            repo_id=self.repo_id,
            settings=self.settings,
            owner_token=self.owner_token,
        ).model_dump(mode="json")
        settings = payload.get("settings")
        if isinstance(settings, dict) and not settings.get("e2e"):
            # Only ever switch e2e on. A teammate on an older .synergo.toml
            # (no e2e line) must not turn the room's encryption flag off for
            # everyone; the relay keeps the room's flag when the join omits it.
            settings.pop("e2e", None)
        if (
            isinstance(settings, dict)
            and self.settings is not None
            and "history_hours" not in self.settings.model_fields_set
        ):
            # Sent only when the .synergo.toml sets it: the relay then keeps
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
                f"room {self.room} is end-to-end encrypted (e2e = true in .synergo.toml) but no "
                "room key is configured on this machine. Get the key from a teammate and run "
                f"`synergo key set {self.room}` (or set SYNERGO_ROOM_KEY), then `synergo up` again"
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

    async def _relay_loop(self) -> None:
        problem = self._e2e_problem()
        if problem:
            self._set_fatal(problem)
            return
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with ws_connect(self.ws_url) as socket:
                    self._socket = socket
                    # A fresh join: any approval wait belonged to the old socket,
                    # and so did our places in the relay's wait queues.
                    self.awaiting_approval = None
                    self._queued.clear()
                    self._released.clear()
                    await socket.send(self._join_payload())
                    self.connected = True
                    self.connected_since = time.time()
                    self.last_relay_contact = self.connected_since
                    backoff = 1.0
                    print(f"synergo: connected to room {self.room} as {self.name}")
                    drain = asyncio.create_task(self._drain(socket))
                    try:
                        async for raw in socket:
                            self._handle_frame(raw)
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
                        f"synergo: relay connection lost for room {self.room} ({exc})",
                        file=sys.stderr,
                    )
            finally:
                self._socket = None
                self.connected = False
                self.connected_since = None

            if self.fatal or self._stop.is_set():
                return
            if self.retry_after > 0:
                backoff = max(backoff, self.retry_after)
                self.retry_after = 0.0
            print(
                f"synergo: reconnecting to room {self.room} in {backoff:.0f}s",
                file=sys.stderr,
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _next_payload(self) -> str:
        """Next payload to send: any retained in-flight one, then priority, then bulk.

        The ready event is cleared before the queues are polled so a put landing
        during the poll either shows up in the poll or leaves the event set.
        """
        while True:
            if self._inflight is not None:
                return self._inflight
            self._outbox_ready.clear()
            for queue in (self._priority_outbox, self._outbox):
                try:
                    return queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue
            await self._outbox_ready.wait()

    async def _drain(self, socket: Any) -> None:
        while True:
            payload = await self._next_payload()
            self._inflight = payload
            await socket.send(payload)
            self._inflight = None
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
        if not self.connected:
            # Connecting or backing off: there is nothing to deliver over a
            # socket that does not exist, so do not wait out a connect timeout.
            task.cancel()
        else:
            deadline = time.monotonic() + min(grace, FLUSH_GRACE)
            while self.connected and self._has_backlog() and time.monotonic() < deadline:
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
            print("synergo: ignoring unparseable relay frame", file=sys.stderr)
            return
        if not isinstance(data, dict):
            return
        try:
            self._dispatch_frame(data)
        except ValidationError as exc:
            print(f"synergo: ignoring malformed relay frame: {exc}", file=sys.stderr)

    def _on_plan_conflicts(self, conflicts: PlanConflicts) -> None:
        """Replies arrive in the order plans were sent. One whose plan ack is
        still waiting goes on that ack; otherwise it is kept as a note for the
        session's next pre_edit ack."""
        if not self._plan_waiters:
            return
        session_id, future = self._plan_waiters.pop(0)
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
        repo_id = str(event.get("repo_id") or "")
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
        if event.get("type") == "user_left":
            self._peers.discard(user_id)
            return
        if user_id and user_id not in self._self_ids:
            self._peers.add(user_id)
        for other in event.get("users") or []:
            if other not in self._self_ids:
                self._peers.add(other)

    def _on_join_request(self, data: dict[str, Any]) -> None:
        """Someone waits at the door of a room we own: keep it for `synergo
        status` / `synergo room approve`, and say so in the log (one plain
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
            f"synergo: room {self.room}: {name} ({agent}) asks to join; "
            f"approve with `synergo room approve {request_id}` "
            f"or deny with `synergo room deny {request_id}`"
        )

    def _note_room_settings(self, event: dict[str, Any]) -> None:
        """A teammate's .synergo.toml turned e2e on while ours says nothing of
        it: stop rather than keep sending them cleartext."""
        if event.get("type") != "room_settings" or not event.get("e2e"):
            return
        if self.e2e_required:
            return
        self._set_fatal(
            f"room {self.room} is end-to-end encrypted (set by {event.get('set_by') or 'a teammate'}), "
            "but this repo's .synergo.toml does not say `e2e = true`. Pull the latest "
            ".synergo.toml (and get the room key: `synergo key set "
            f"{self.room}`), then run `synergo up` again"
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
                f"synergo: room {self.room}: waiting for an owner to approve this join"
                + (f" ({data['reason']})" if data.get("reason") else "")
            )
            return
        if self.awaiting_approval is not None and kind not in ("room_rejected", "rate_limited"):
            # Anything else after join_pending is the room's normal traffic:
            # an owner let us in.
            print(f"synergo: room {self.room}: join approved")
            self.awaiting_approval = None

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
            self._on_plan_conflicts(PlanConflicts.model_validate(data))
            return

        if kind == "synergo_feed":
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
            self.retry_after = float(data.get("retry_after") or 0)
            print(
                f"synergo: relay is throttling connections ({data.get('reason', '')})",
                file=sys.stderr,
            )
            return

        if kind == "version_rejected":
            self._set_fatal(
                f"{data.get('reason', 'this synergo build is too old for the relay')}\n"
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
    return blob


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
        task = f' for "{conflict.summary[:80]}"' if conflict.summary else ""
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
    # SYNERGO_ROOM_KEY or the personal config. Loopback and token-guarded;
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
    """POST /wait body (`synergo wait`): wait up to timeout seconds for
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
    2. Every route except `GET /health` needs X-Synergo-Token equal to the
       install's token (constant-time compare; 401 otherwise). On a mismatch
       the token file is re-read once, so a token regenerated on disk while the
       daemon runs (a deleted SYNERGO_HOME, say) heals instead of locking
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

    Daemon(room, name, ...) is single-room mode (`synergo join`): one
    connection that every event falls through to whatever its cwd, with the
    old exit-on-fatal behaviour. Daemon() is multi-repo mode (`synergo serve`):
    repos are registered over POST /repos or on first sight of a .synergo.toml,
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
        # The secret every HTTP caller presents (synergo.auth). None until the
        # daemon serves: run() loads or creates SYNERGO_HOME/token then, so
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
        # the per-machine daemon adopts repos from their .synergo.toml.
        self.auto_register = (self._default is None) if auto_register is None else auto_register

        self._bindings: dict[Path, RepoBinding] = {}
        self._route_cache: dict[str, Any] = {}
        self._negative: dict[str, float] = {}
        # Roots explicitly disconnected: never re-adopted from their config
        # until registered again, or `synergo disconnect` would not stick.
        self._disabled: set[Path] = set()
        self._discovering: dict[str, asyncio.Task[Optional[RepoBinding]]] = {}
        self._closing: set[asyncio.Task[None]] = set()
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
            # if SYNERGO_HOME changes under it later.
            self._token_path = auth.token_path()
            self.token = auth.ensure_token(self._token_path)
        return self.token

    def token_matches(self, presented: bytes) -> bool:
        """Constant-time check of a request's X-Synergo-Token."""
        expected = self.ensure_token().encode("ascii")
        if presented and hmac.compare_digest(presented, expected):
            return True
        if not presented or self._token_pinned:
            return False
        fresh = auth.read_token(self._token_path)
        if fresh is None or fresh == self.token:
            return False
        if hmac.compare_digest(presented, fresh.encode("ascii")):
            print("synergo: the token file changed on disk; using the new token")
            self.token = fresh
            return True
        return False

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="synergo-daemon", docs_url=None, redoc_url=None)
        app.add_middleware(LocalGuard, daemon=self)

        @app.get("/health")
        async def health() -> dict[str, Any]:
            # Unauthenticated, so it says nothing beyond "a synergo daemon is
            # here": enough for `is_running` and for a client to tell a token
            # problem from a dead port.
            return {"ok": True, "service": "synergo-daemon"}

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
                "(see `synergo status`; requests expire)",
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

    # --- synergo wait -------------------------------------------------------------

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
        .synergo.toml if need be; None means nobody coordinates it."""
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
            print(f"synergo: ignoring settings in {config.root}: {exc}", file=sys.stderr)
            settings = None
        agent = config.agent if config.agent in AGENT_KINDS else "unknown"
        if transport_problem(config.relay, config.insecure):
            print(
                f"synergo: not coordinating {config.root}: {transport_problem(config.relay, config.insecure)}",
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
                room_key=config.room_key(),
                owner_token=config.owner_token(),
                auto=True,
            )
        except ValueError as exc:
            print(f"synergo: not coordinating {config.root}: {exc}", file=sys.stderr)
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
                raise ValueError("no display name: set `name` in ~/.synergo/config.toml")
        ignore = tuple(ignore)
        redact_patterns = tuple(redact_patterns)
        redactor = _redactor(redact, redact_patterns)
        e2e_key = _room_key(room_settings, room_key, room)
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
            # Re-running `synergo connect` must not bounce a healthy connection.
            existing.auto = existing.auto and auto
            return existing
        if existing is not None:
            await self._unbind(existing)
        self._disabled.discard(key)

        conn = self._shared_connection(relay, room, name, agent)
        if conn is not None:
            # One socket per name per room: a repo joining it brings what it
            # knows (a key for the same room, an owner token) to the shared link.
            conn.e2e_key = conn.e2e_key or e2e_key
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
                    "Pick another name (`name` in ~/.synergo/config.toml or "
                    "SYNERGO_NAME) and reconnect the repo."
                ),
                e2e_key=e2e_key,
                owner_token=owner_token,
            )
            conn.repo_id = repo_id
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
        print(f"synergo: {how} {key}: room {room} as {name} via {relay}{extras}")
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
        print(f"synergo: stopped coordinating {key}")
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
            print(f"synergo: room {conn.room}: {conn.fatal}", file=sys.stderr)

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
            f"synergo: warning: {cwd} is not inside a git repository, so file "
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
        print(f"synergo: listening on http://{LOOPBACK}:{self.port}")
        if default is not None:
            conn = default.connection
            print(f"synergo: joining {conn.ws_url} as {conn.name} ({conn.agent})")
        else:
            print("synergo: coordinating every repo with a .synergo.toml (and any registered)")

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
            print(f"synergo: {default.connection.fatal}", file=sys.stderr)
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
