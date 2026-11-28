"""Prompt blame: which agent, prompt and task wrote a line of code, and why.

Three parts, all on this machine; nothing here ever talks to the relay:

1. **Record** (daemon). `Recorder` keeps each agent session's latest prompt,
   plan and active plan step in memory, and on every finished edit appends one
   record to a journal inside the repo's git directory
   (`<git-dir>/hivelane/provenance.jsonl`, so it is never committed, and per
   worktree). A record names the file, the agent, the person, the session, the
   prompt excerpt (redacted, capped), the task, the symbols and line ranges,
   and short hashes of the lines the edit wrote. The hashes are what later tie
   a committed line back to the edit that wrote it, wherever it has moved.

2. **Attach** (post-commit hook). `attach()` reads the commit's added lines,
   picks the journal records whose lines made it into the commit, and writes
   them as a git note on the commit under `refs/notes/hivelane`. A note is JSON
   Lines, one entry per edit, each with a unique `id`: so git's own
   concatenating and `cat_sort_uniq` note merges keep it valid. Records are
   marked attached (`attached.json`, written only by this side; the daemon is
   the journal's only writer) so the next commit does not claim them again.
   `rewrite()` (post-rewrite hook) carries notes across `commit --amend` and
   `rebase`.

3. **Blame** (CLI and MCP). `blame()` runs `git blame`, reads the notes of
   the commits it names, and picks for every line the entry whose line hashes
   (then line ranges) match it. Uncommitted lines are matched against the
   journal.

Everything fails open. The hooks never fail or noticeably slow a commit: every
git call has a timeout, and any error means nothing is attached. Notes leave
the machine only through `hivelane notes push`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from hivelane.events import NormalizedEvent
    from hivelane.redact import Redactor

log = logging.getLogger("hivelane.provenance")

NOTES_REF = "refs/notes/hivelane"
# Where `hivelane notes fetch` puts a remote's notes before merging them.
REMOTE_NOTES_PREFIX = "refs/notes/hivelane-remote/"
JOURNAL_DIR = "hivelane"
JOURNAL_NAME = "provenance.jsonl"
ATTACHED_NAME = "attached.json"
NOTE_VERSION = 1

# The journal is compacted (attached and expired records dropped, then the
# oldest) once it grows past this; records older than MAX_AGE never attach.
JOURNAL_MAX_BYTES = 4 * 1024 * 1024
MAX_AGE_SECONDS = 30 * 86400.0
COMPACT_EVERY = 200
# A commit's timestamp has one-second resolution and clocks drift a little.
CLOCK_SKEW_SECONDS = 120.0

# Per edit: hashed lines, line ranges, symbols. Per note: entries.
MAX_HASHES = 2000
MAX_RANGES = 50
MAX_SYMBOLS = 20
MAX_NOTE_ENTRIES = 300
# Raw prompt and plan text kept in memory per session, before redaction and
# the excerpt cut (blame_excerpt_chars, at most config.MAX_BLAME_EXCERPT_CHARS).
MAX_CONTEXT_CHARS = 4000
MAX_CONTEXTS = 256
MAX_DELEGATIONS = 8
DELEGATION_TTL = 600.0
# Journal files whose commit diff is read in one go (past this, all of it).
MAX_PATHSPEC = 200

# git calls from the commit hooks are bounded so a wedged git (a lock, a slow
# disk) cannot hold the commit; blame and notes push/fetch may take longer.
HOOK_GIT_TIMEOUT = 3.0
GIT_TIMEOUT = 30.0
NETWORK_TIMEOUT = 120.0
SETTINGS_TTL = 15.0

ZERO_SHA = "0" * 40
ELLIPSIS = "…"


# --- small helpers ---------------------------------------------------------------


def line_key(line: str) -> Optional[str]:
    """A short hash of one line of code, or None for a line too generic to
    say anything (blank, or only brackets and punctuation): `}` is in every
    edit, so matching on it would attribute any line to any edit.
    Whitespace at either end is ignored, so a re-indent keeps its key."""
    text = line.strip()
    if len(text) < 3 or not any(c.isalnum() for c in text):
        return None
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=5).hexdigest()


def excerpt(text: Optional[str], limit: int) -> Optional[str]:
    """text with its whitespace collapsed, cut to `limit` characters."""
    if not text or limit <= 0:
        return None
    flat = " ".join(text.split())
    if not flat:
        return None
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)].rstrip() + ELLIPSIS


def find_git_dirs(start: str) -> Optional[tuple[Path, Path]]:
    """(work tree top, git dir) of the repo around `start`, found on the
    filesystem without running git: this runs in the daemon for every edit.
    In a linked worktree or a submodule `.git` is a file naming the git dir."""
    try:
        current = Path(start).resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        dot = candidate / ".git"
        try:
            if dot.is_dir():
                return candidate, dot.resolve()
            if dot.is_file():
                first = dot.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
                if first and first[0].startswith("gitdir:"):
                    target = Path(first[0][len("gitdir:"):].strip())
                    if not target.is_absolute():
                        target = candidate / target
                    return candidate, target.resolve()
                return None
        except OSError:
            return None
    return None


@dataclass(frozen=True)
class BlameSettings:
    enabled: bool = True
    prompts: bool = True
    excerpt_chars: int = 280


# A config file that cannot be read keeps recording, but no prompt text:
# the problem is reported elsewhere, and guessing towards privacy is safer.
UNREADABLE_SETTINGS = BlameSettings(enabled=True, prompts=False, excerpt_chars=0)


def load_settings(top: Path) -> BlameSettings:
    from hivelane.config import ConfigFileError, load_config

    try:
        config = load_config(top)
    except (ConfigFileError, OSError, ValueError):
        return UNREADABLE_SETTINGS
    return BlameSettings(
        enabled=bool(config.blame),
        prompts=bool(config.blame_prompts) and config.blame_excerpt_chars > 0,
        excerpt_chars=int(config.blame_excerpt_chars),
    )


# --- the journal -----------------------------------------------------------------


def journal_path(git_dir: Path) -> Path:
    return git_dir / JOURNAL_DIR / JOURNAL_NAME


def attached_path(git_dir: Path) -> Path:
    return git_dir / JOURNAL_DIR / ATTACHED_NAME


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def append_record(git_dir: Path, record: dict[str, Any]) -> None:
    """Append one record. Owner-only (0600): it holds prompt excerpts."""
    path = journal_path(git_dir)
    _private_dir(path.parent)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line)


def read_journal(git_dir: Path) -> list[dict[str, Any]]:
    """Every readable record, oldest first; a torn or foreign line is skipped."""
    try:
        with open(journal_path(git_dir), encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str) and isinstance(record.get("file"), str):
            records.append(record)
    return records


def read_attached(git_dir: Path) -> set[str]:
    try:
        data = json.loads(attached_path(git_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ids = data.get("ids") if isinstance(data, dict) else None
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def _write_private(path: Path, text: str) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def mark_attached(git_dir: Path, ids: Iterable[str], journal_ids: set[str]) -> None:
    """Remember ids as attached. Ids no longer in the journal are dropped:
    the daemon compacted those records away, so nobody can claim them again."""
    keep = (read_attached(git_dir) | set(ids)) & journal_ids
    _write_private(attached_path(git_dir), json.dumps({"ids": sorted(keep)}))


def compact_journal(git_dir: Path, now: Optional[float] = None) -> None:
    """Drop attached and expired records, then the oldest until the journal
    is at most half its cap. Only the daemon calls this: it is the journal's
    only writer, so no append can be lost between the read and the replace."""
    now = time.time() if now is None else now
    path = journal_path(git_dir)
    attached = read_attached(git_dir)
    kept: list[str] = []
    for record in read_journal(git_dir):
        if record["id"] in attached or now - float(record.get("at") or 0) > MAX_AGE_SECONDS:
            continue
        kept.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    budget = JOURNAL_MAX_BYTES // 2
    size = 0
    tail: list[str] = []
    for line in reversed(kept):
        size += len(line.encode("utf-8"))
        if size > budget:
            break
        tail.append(line)
    _write_private(path, "".join(reversed(tail)))


# --- recording (daemon side) -----------------------------------------------------

ContextKey = tuple[str, str, str]


@dataclass
class _Context:
    prompt: str = ""
    plan: str = ""
    task: str = ""
    task_step: str = ""
    # Partial plans (TaskCreate / TaskUpdate) name a step by id after the
    # first time: id -> its text.
    steps: dict[str, str] = field(default_factory=dict)
    # (id(redactor), raw prompt, raw plan) -> their redacted text, so a burst
    # of edits under one prompt redacts it once.
    redacted: Optional[tuple[int, str, str, str, str]] = None


class Recorder:
    """The daemon's provenance journal writer. Every method is cheap and
    never raises: recording is a side effect that must not cost an edit."""

    def __init__(self, *, clock: Any = time.time) -> None:
        self._clock = clock
        self._contexts: OrderedDict[ContextKey, _Context] = OrderedDict()
        self._delegations: OrderedDict[str, list[tuple[str, str, float]]] = OrderedDict()
        self._repos: OrderedDict[str, Optional[tuple[Path, Path]]] = OrderedDict()
        self._settings: dict[Path, tuple[float, BlameSettings]] = {}
        self._appends = 0

    # --- session context ---------------------------------------------------------

    @staticmethod
    def _key(event: "NormalizedEvent", subagent: bool = True) -> ContextKey:
        return (event.agent, event.session_id, (event.subagent_id or "") if subagent else "")

    def _context(self, key: ContextKey) -> _Context:
        ctx = self._contexts.pop(key, None) or _Context()
        self._contexts[key] = ctx
        while len(self._contexts) > MAX_CONTEXTS:
            self._contexts.popitem(last=False)
        return ctx

    def observe(self, event: "NormalizedEvent") -> None:
        """Note what an event says about its session's work: a prompt, a
        plan, a step started or finished, a task handed to a subagent."""
        try:
            self._observe(event)
        except Exception:  # never let bookkeeping cost an event
            log.debug("provenance: could not observe a %s event", event.kind, exc_info=True)

    def _observe(self, event: "NormalizedEvent") -> None:
        if not event.session_id:
            return
        kind = event.kind
        if kind == "intent" and event.prompt:
            ctx = self._context(self._key(event))
            ctx.prompt = event.prompt[:MAX_CONTEXT_CHARS]
            # A new prompt is new work: the old plan text no longer explains it
            # (the active step stays until the agent's plan says otherwise).
            ctx.plan = ""
        elif kind == "plan":
            ctx = self._context(self._key(event))
            if event.summary:
                ctx.plan = event.summary[:MAX_CONTEXT_CHARS]
            if event.steps:
                self._apply_steps(ctx, event.steps, bool(event.plan_partial))
        elif kind == "delegate" and event.prompt:
            text = f"{event.summary}: {event.prompt}" if event.summary else event.prompt
            now = self._clock()
            queue = [d for d in self._delegations.pop(event.session_id, []) if now - d[2] < DELEGATION_TTL]
            queue.append(((event.subagent_type or "").lower(), text[:MAX_CONTEXT_CHARS], now))
            self._delegations[event.session_id] = queue[-MAX_DELEGATIONS:]
            while len(self._delegations) > MAX_CONTEXTS:
                self._delegations.popitem(last=False)
        elif kind == "subagent_start" and event.subagent_id:
            self._adopt_delegation(event)

    def _adopt_delegation(self, event: "NormalizedEvent") -> None:
        """A subagent's prompt is the task its parent handed it."""
        key = self._key(event)
        if key in self._contexts and self._contexts[key].prompt:
            return
        queue = self._delegations.get(event.session_id)
        if not queue:
            return
        now = self._clock()
        queue[:] = [d for d in queue if now - d[2] < DELEGATION_TTL]
        wanted = (event.subagent_type or "").lower()
        index = next((i for i, d in enumerate(queue) if d[0] == wanted), 0 if queue else None)
        if index is None:
            return
        self._context(key).prompt = queue.pop(index)[1]

    @staticmethod
    def _apply_steps(ctx: _Context, steps: list[dict[str, str]], partial: bool) -> None:
        if not partial:
            ctx.steps = {s["id"]: s.get("content", "") for s in steps if s.get("id") and s.get("content")}
            active = next((s for s in steps if s.get("status") == "in_progress"), None)
            ctx.task = (active.get("content") or "") if active else ""
            ctx.task_step = (active.get("id") or "") if active else ""
            return
        for step in steps:
            step_id = step.get("id") or ""
            content = step.get("content") or ctx.steps.get(step_id, "")
            if step_id and content:
                ctx.steps[step_id] = content
            status = step.get("status") or ""
            if status == "in_progress" and content:
                ctx.task, ctx.task_step = content, step_id
            elif status in ("completed", "removed") and (
                (step_id and step_id == ctx.task_step) or (content and content == ctx.task)
            ):
                ctx.task, ctx.task_step = "", ""
        while len(ctx.steps) > 64:
            ctx.steps.pop(next(iter(ctx.steps)))

    def _merged(self, event: "NormalizedEvent") -> _Context:
        """This lane's context, falling back to its parent chat's for what a
        subagent has not said itself."""
        own = self._contexts.get(self._key(event))
        if not event.subagent_id:
            return own or _Context()
        parent = self._contexts.get(self._key(event, subagent=False)) or _Context()
        if own is None:
            return parent
        return _Context(
            prompt=own.prompt or parent.prompt,
            plan=own.plan or parent.plan,
            task=own.task or parent.task,
        )

    # --- repos and settings ----------------------------------------------------------

    def _repo(self, directory: str) -> Optional[tuple[Path, Path]]:
        if directory in self._repos:
            self._repos.move_to_end(directory)
            return self._repos[directory]
        found = find_git_dirs(directory)
        self._repos[directory] = found
        while len(self._repos) > 512:
            self._repos.popitem(last=False)
        return found

    def settings(self, top: Path) -> BlameSettings:
        now = time.monotonic()
        cached = self._settings.get(top)
        if cached is not None and cached[0] > now:
            return cached[1]
        settings = load_settings(top)
        if len(self._settings) > 256:
            self._settings.clear()
        self._settings[top] = (now + SETTINGS_TTL, settings)
        return settings

    # --- edits -------------------------------------------------------------------------

    def note_change(
        self,
        event: "NormalizedEvent",
        *,
        user: str,
        new: str,
        ranges: list[tuple[int, int]],
        symbols: list[str],
        known_old: bool,
        redactor: Optional["Redactor"] = None,
    ) -> None:
        """Journal one finished edit (already past the repo's ignore rules)."""
        try:
            self._note_change(event, user, new, ranges, symbols, known_old, redactor)
        except Exception:
            log.debug("provenance: could not record an edit", exc_info=True)

    def _note_change(
        self,
        event: "NormalizedEvent",
        user: str,
        new: str,
        ranges: list[tuple[int, int]],
        symbols: list[str],
        known_old: bool,
        redactor: Optional["Redactor"],
    ) -> None:
        if not event.file_path:
            return
        path = os.path.normpath(os.path.join(event.cwd or "", event.file_path))
        found = self._repo(os.path.dirname(path))
        if found is None:
            return
        top, git_dir = found
        settings = self.settings(top)
        if not settings.enabled:
            return
        try:
            rel = Path(path).resolve().relative_to(top).as_posix()
        except (OSError, ValueError):
            return
        lines = new.split("\n")
        unhashed = False
        if known_old and ranges:
            chosen = [line for a, b in ranges for line in lines[max(0, a - 1) : b]]
        elif known_old:
            chosen = []  # past the symbol size cap: file-level only
            unhashed = True
        else:
            # No "before" known (a new file, or an agent that sends none): the
            # whole file is this edit's as far as anyone can tell; the commit's
            # own diff narrows it down when the note is attached.
            chosen = lines
            ranges = [(1, len(lines))] if lines else []
        # Bounded work on the event loop: a huge new file hashes its head only.
        hashes = list(dict.fromkeys(k for k in map(line_key, chosen[: MAX_HASHES * 4]) if k))[:MAX_HASHES]
        ctx = self._merged(event)
        prompt = plan = None
        if settings.prompts:
            prompt, plan = self._redacted(ctx, redactor, settings.excerpt_chars)
        record: dict[str, Any] = {
            "v": NOTE_VERSION,
            "id": uuid.uuid4().hex[:16],
            "at": round(self._clock(), 3),
            "file": rel,
            "agent": event.agent,
            "session": (event.session_id or "")[:64],
            "subagent": (event.subagent_type or "")[:64] or None,
            "user": user,
            "tool": (event.tool or "")[:64] or None,
            "prompt": prompt,
            "plan": plan,
            "task": excerpt(ctx.task, 200),
            "symbols": list(symbols)[:MAX_SYMBOLS],
            "lines": [list(r) for r in ranges[:MAX_RANGES]],
            "h": hashes,
        }
        if unhashed:
            record["unhashed"] = True
        append_record(git_dir, record)
        self._appends += 1
        if self._appends % COMPACT_EVERY == 0:
            try:
                if journal_path(git_dir).stat().st_size > JOURNAL_MAX_BYTES:
                    compact_journal(git_dir)
            except OSError:
                # Compaction is housekeeping: the record above is already
                # written, and the next COMPACT_EVERY append tries again.
                pass

    @staticmethod
    def _redacted(ctx: _Context, redactor: Optional["Redactor"], limit: int) -> tuple[Optional[str], Optional[str]]:
        from hivelane import redact

        # Always redacted, even where `redact = false` keeps the relay copy
        # raw: that switch is for false positives in shared diffs, and a
        # secret pasted into a prompt has no business in a git note.
        active = redactor if redactor is not None and redactor.enabled else redact.DEFAULT
        cached = ctx.redacted
        if cached is not None and cached[:3] == (id(active), ctx.prompt, ctx.plan):
            prompt, plan = cached[3], cached[4]
        else:
            prompt = active.text(ctx.prompt) or ""
            plan = active.text(ctx.plan) or ""
            ctx.redacted = (id(active), ctx.prompt, ctx.plan, prompt, plan)
        return excerpt(prompt, limit), excerpt(plan, limit)


# --- git ---------------------------------------------------------------------------


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    top: Path
    git_dir: Path
    timeout: float = GIT_TIMEOUT

    @classmethod
    def open(cls, start: Path | str, timeout: float = GIT_TIMEOUT) -> "Repo":
        out = run_git(Path(start), "rev-parse", "--show-toplevel", "--absolute-git-dir", timeout=timeout)
        lines = out.splitlines()
        if len(lines) < 2:
            raise GitError(f"{start} is not inside a git work tree")
        return cls(Path(lines[0]).resolve(), Path(lines[1]).resolve(), timeout)

    def git(self, *args: str, stdin: Optional[str] = None, timeout: Optional[float] = None,
            env: Optional[dict[str, str]] = None) -> str:
        return run_git(self.top, *args, stdin=stdin, timeout=timeout or self.timeout, env=env)

    def busy_rewriting(self) -> bool:
        """A rebase is replaying commits: their notes come over through
        post-rewrite, and the journal has nothing to add to them."""
        return any((self.git_dir / name).exists() for name in ("rebase-merge", "rebase-apply"))


def run_git(cwd: Path, *args: str, stdin: Optional[str] = None, timeout: float = GIT_TIMEOUT,
            env: Optional[dict[str, str]] = None) -> str:
    try:
        done = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, **env} if env else None,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} did not finish within {timeout:g}s") from None
    except OSError as exc:
        raise GitError(f"could not run git ({exc})") from None
    if done.returncode != 0:
        raise GitError((done.stderr or done.stdout or f"git {args[0]} failed").strip())
    return done.stdout


def _unquote(path: str) -> str:
    """A path as git prints it in a diff header (C-quoted when it holds
    special characters, even with core.quotepath off)."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        try:
            return path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode(
                "latin-1", "replace"
            ).decode("utf-8", "replace")
        except (UnicodeError, ValueError):
            return path[1:-1]
    return path


def parse_added_lines(diff: str) -> dict[str, set[str]]:
    """{path: keys of the lines the diff adds} from `git diff -U0` output.
    Every file in the diff is listed, even one that adds no line."""
    added: dict[str, set[str]] = {}
    current: Optional[str] = None
    in_hunk = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
            tail = line[len("diff --git "):]
            # "a/<path> b/<path>": the new side, a fallback for a file with no
            # "+++" line (binary, mode change).
            cut = tail.rfind(" b/") if not tail.endswith('"') else tail.rfind(' "b/')
            current = _unquote(tail[cut + 1:]) if cut >= 0 else None
            if current is not None:
                current = current[2:] if current.startswith("b/") else current
                added.setdefault(current, set())
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                target = _unquote(line[4:].strip())
                current = target[2:] if target.startswith("b/") else None
                if current is not None:
                    added.setdefault(current, set())
            elif line.startswith("@@"):
                in_hunk = True
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+") and current is not None:
            key = line_key(line[1:])
            if key:
                added[current].add(key)
    return added


def commit_added_lines(repo: Repo, sha: str, parent: Optional[str], paths: list[str]) -> dict[str, set[str]]:
    """{path: keys of the lines commit `sha` adds} for `paths` (first parent
    only, so a merge is judged against the branch it landed on)."""
    args = ["diff-tree", "-r", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]
    args += [parent, sha] if parent else ["--root", sha]
    env = None
    if len(paths) <= MAX_PATHSPEC:
        args += ["--", *paths]
        env = {"GIT_LITERAL_PATHSPECS": "1"}
    return parse_added_lines(repo.git(*args, env=env))


# --- notes -------------------------------------------------------------------------


def parse_note(text: str) -> list[dict[str, Any]]:
    """The entries of a note (JSON Lines; blank or foreign lines skipped)."""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            entries.append(entry)
    return entries


def render_note(entries: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n" for e in entries)


def merge_entries(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union by id, oldest first; a later copy of an id replaces the earlier."""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for entry in group:
            merged.pop(entry["id"], None)
            merged[entry["id"]] = entry
    out = sorted(merged.values(), key=lambda e: float(e.get("at") or 0))
    return out[-MAX_NOTE_ENTRIES:]


def read_note(repo: Repo, sha: str) -> list[dict[str, Any]]:
    try:
        return parse_note(repo.git("notes", f"--ref={NOTES_REF}", "show", sha))
    except GitError:
        return []


def write_note(repo: Repo, sha: str, entries: list[dict[str, Any]]) -> None:
    repo.git("notes", f"--ref={NOTES_REF}", "add", "-f", "-F", "-", sha, stdin=render_note(entries))


def note_entry(record: dict[str, Any], hashes: list[str], settings: BlameSettings) -> dict[str, Any]:
    """What of a journal record goes into a commit's note: the settings in
    force now apply (prompts turned off since the edit are dropped now), and
    only the line hashes the commit actually contains."""
    entry = {k: record.get(k) for k in (
        "v", "id", "at", "file", "agent", "session", "subagent", "user", "tool", "task", "symbols", "lines",
        "unhashed",
    )}
    if settings.prompts:
        entry["prompt"] = excerpt(record.get("prompt"), settings.excerpt_chars)
        entry["plan"] = excerpt(record.get("plan"), settings.excerpt_chars)
    else:
        entry["prompt"] = entry["plan"] = None
    entry["h"] = hashes[:MAX_HASHES]
    return {k: v for k, v in entry.items() if v not in (None, [], "")}


def attach(start: Path | str, rev: str = "HEAD", *, timeout: float = HOOK_GIT_TIMEOUT,
           now: Optional[float] = None) -> int:
    """Attach the journal's matching records to commit `rev` as its note.
    Returns how many entries were attached. Raises GitError; the hook
    wrapper (`post_commit`) turns every failure into "nothing attached"."""
    repo = Repo.open(start, timeout=timeout)
    settings = load_settings(repo.top)
    if not settings.enabled or repo.busy_rewriting():
        return 0
    records = read_journal(repo.git_dir)
    if not records:
        return 0
    attached = read_attached(repo.git_dir)
    now = time.time() if now is None else now
    pending = [
        r for r in records
        if r["id"] not in attached and now - float(r.get("at") or 0) <= MAX_AGE_SECONDS
    ]
    if not pending:
        return 0
    head = repo.git("rev-list", "--parents", "-n", "1", rev).split()
    if not head:
        return 0
    sha, parent = head[0], (head[1] if len(head) > 1 else None)
    when = float(repo.git("show", "-s", "--format=%ct", sha).strip() or 0)
    paths = sorted({r["file"] for r in pending})
    added = commit_added_lines(repo, sha, parent, paths)
    entries: list[dict[str, Any]] = []
    for record in pending:
        lines = added.get(record["file"])
        if lines is None or float(record.get("at") or 0) > when + CLOCK_SKEW_SECONDS:
            continue
        hashes = [h for h in (record.get("h") or []) if isinstance(h, str)]
        kept = [h for h in hashes if h in lines]
        if hashes and not kept:
            # None of its lines are in this commit (left unstaged, or since
            # rewritten): it may still belong to a later commit.
            continue
        entries.append(note_entry(record, kept, settings))
    if not entries:
        return 0
    write_note(repo, sha, merge_entries(read_note(repo, sha), entries))
    mark_attached(repo.git_dir, (e["id"] for e in entries), {r["id"] for r in records})
    return len(entries)


def rewrite(start: Path | str, pairs: Iterable[tuple[str, str]], *, timeout: float = HOOK_GIT_TIMEOUT) -> int:
    """Carry notes from rewritten commits to their rewrites (post-rewrite
    after `commit --amend` or `rebase`; a squash merges several into one)."""
    repo = Repo.open(start, timeout=timeout)
    moved: dict[str, list[dict[str, Any]]] = {}
    for old, new in pairs:
        entries = read_note(repo, old)
        if entries:
            moved.setdefault(new, []).extend(entries)
    for new, entries in moved.items():
        write_note(repo, new, merge_entries(read_note(repo, new), entries))
    return len(moved)


def parse_rewrite_pairs(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def post_commit(start: Path | str) -> int:
    """The post-commit hook: attach, and never fail the commit."""
    try:
        attach(start)
    except Exception:
        log.debug("provenance: post-commit attach failed", exc_info=True)
    return 0


def post_rewrite(start: Path | str, stdin_text: str) -> int:
    try:
        rewrite(start, parse_rewrite_pairs(stdin_text))
    except Exception:
        log.debug("provenance: post-rewrite failed", exc_info=True)
    return 0


# --- blame -------------------------------------------------------------------------


@dataclass
class BlameLine:
    final: int
    orig: int
    sha: str
    filename: str
    text: str
    author: str = ""
    author_mail: str = ""
    author_time: int = 0
    summary: str = ""


def parse_porcelain(out: str) -> list[BlameLine]:
    """`git blame --line-porcelain` output, one BlameLine per line."""
    lines: list[BlameLine] = []
    current: Optional[BlameLine] = None
    for raw in out.split("\n"):
        if current is None:
            parts = raw.split(" ")
            if len(parts) >= 3 and len(parts[0]) >= 40:
                try:
                    current = BlameLine(final=int(parts[2]), orig=int(parts[1]), sha=parts[0], filename="", text="")
                except ValueError:
                    current = None
            continue
        if raw.startswith("\t"):
            current.text = raw[1:]
            lines.append(current)
            current = None
            continue
        key, _, value = raw.partition(" ")
        if key == "author":
            current.author = value
        elif key == "author-mail":
            current.author_mail = value.strip("<>")
        elif key == "author-time":
            try:
                current.author_time = int(value)
            except ValueError:
                # A malformed timestamp leaves the line's time unknown (0);
                # the rest of the porcelain entry is still usable.
                pass
        elif key == "summary":
            current.summary = value
        elif key == "filename":
            current.filename = _unquote(value)
    return lines


def notes_index(repo: Repo) -> dict[str, str]:
    """{commit: note blob} for every note under NOTES_REF."""
    try:
        out = repo.git("notes", f"--ref={NOTES_REF}", "list")
    except GitError:
        return {}
    index = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            index[parts[1]] = parts[0]
    return index


def read_blobs(repo: Repo, blobs: list[str]) -> dict[str, str]:
    """Contents of several blobs in one `git cat-file --batch`."""
    if not blobs:
        return {}
    done = subprocess.run(
        ["git", "cat-file", "--batch"], cwd=repo.top, input=("\n".join(blobs) + "\n").encode(),
        capture_output=True, timeout=repo.timeout, check=False,
    )
    data, out, pos = done.stdout, {}, 0
    for blob in blobs:
        end = data.find(b"\n", pos)
        if end < 0:
            break
        header = data[pos:end].split()
        pos = end + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        out[blob] = data[pos : pos + size].decode("utf-8", "replace")
        pos += size + 1
    return out


def score(entry: dict[str, Any], key: Optional[str], line_no: int) -> int:
    """How well an entry explains a line, or -1 when it does not:
    3/2  the line is one the edit wrote (its hash), inside / outside the
         edit's recorded ranges (which drift as later edits move lines);
    1    a generic line (`}`, blank) inside the edit's ranges;
    0    the edit's file was too big to hash: it can only claim it by file.
    A hashed line the edit did not write is not the edit's, even inside its
    ranges: that is someone else's touch-up in the agent's region."""
    hashes = entry.get("h") or []
    in_range = any(
        isinstance(pair, list) and len(pair) == 2 and pair[0] <= line_no <= pair[1]
        for pair in entry.get("lines") or []
    )
    if key and key in hashes:
        return 3 if in_range else 2
    if entry.get("unhashed"):
        return 0
    if key is None and in_range:
        return 1
    return -1


MATCH = {3: "line", 2: "line", 1: "range", 0: "file"}


def best_entry(entries: list[dict[str, Any]], filename: str, text: str, line_no: int) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    key = line_key(text)
    best: Optional[dict[str, Any]] = None
    best_score = -1
    for entry in entries:
        if entry.get("file") != filename:
            continue
        s = score(entry, key, line_no)
        # Later entries win ties: the last edit to write a line wrote it.
        if s >= 0 and (s > best_score or (s == best_score and float(entry.get("at") or 0) >= float((best or {}).get("at") or 0))):
            best, best_score = entry, s
    return best, (MATCH.get(best_score) if best is not None else None)


PROVENANCE_FIELDS = ("agent", "subagent", "user", "session", "tool", "prompt", "plan", "task", "symbols", "at")


def blame(start: Path | str, path: str, first: Optional[int] = None, last: Optional[int] = None,
          *, cwd: Optional[Path] = None) -> list[dict[str, Any]]:
    """Provenance of `path`'s lines first..last (all when None), as groups
    of consecutive lines sharing a commit and an entry."""
    repo = Repo.open(start)
    base = Path(cwd) if cwd is not None else Path(start)
    full = Path(path) if Path(path).is_absolute() else base / path
    try:
        rel = full.resolve().relative_to(repo.top).as_posix()
    except ValueError:
        raise GitError(f"{path} is outside the repository at {repo.top}") from None
    args = ["blame", "--line-porcelain"]
    if first is not None:
        args += ["-L", f"{first},{last if last is not None else first}"]
    try:
        lines = parse_porcelain(repo.git(*args, "--", rel))
    except GitError as exc:
        if "no such path" not in str(exc) and "no such ref" not in str(exc) and "bad revision" not in str(exc):
            raise
        lines = _untracked_lines(full, rel, first, last)
    index = notes_index(repo)
    shas = sorted({ln.sha for ln in lines if ln.sha in index})
    blobs = read_blobs(repo, [index[s] for s in shas])
    notes = {s: parse_note(blobs.get(index[s], "")) for s in shas}
    journal: Optional[list[dict[str, Any]]] = None
    groups: list[dict[str, Any]] = []
    for ln in lines:
        uncommitted = ln.sha == ZERO_SHA
        if uncommitted:
            if journal is None:
                attached = read_attached(repo.git_dir)
                journal = [r for r in read_journal(repo.git_dir) if r["id"] not in attached]
            entries = journal
            filename = rel
        else:
            entries = notes.get(ln.sha, [])
            filename = ln.filename or rel
        entry, match = best_entry(entries, filename, ln.text, ln.orig if not uncommitted else ln.final)
        entry_id = entry.get("id") if entry else None
        last_group = groups[-1] if groups else None
        if (
            last_group is not None
            and last_group["commit"] == (None if uncommitted else ln.sha)
            and last_group["_entry"] == entry_id
            and last_group["end"] == ln.final - 1
        ):
            last_group["end"] = ln.final
            continue
        groups.append({
            "file": rel,
            "start": ln.final,
            "end": ln.final,
            "commit": None if uncommitted else ln.sha,
            "author": None if uncommitted else ln.author,
            "author_mail": None if uncommitted else ln.author_mail,
            "author_time": None if uncommitted else ln.author_time,
            "summary": None if uncommitted else ln.summary,
            "uncommitted": uncommitted,
            "match": match,
            "provenance": {k: entry.get(k) for k in PROVENANCE_FIELDS if entry.get(k) not in (None, "", [])} if entry else None,
            "_entry": entry_id,
        })
    for group in groups:
        group.pop("_entry", None)
    return groups


def _untracked_lines(full: Path, rel: str, first: Optional[int], last: Optional[int]) -> list[BlameLine]:
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise GitError(f"{rel}: no such file") from None
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    lo = first or 1
    hi = min(len(rows), last if last is not None else (first if first is not None else len(rows)))
    return [BlameLine(final=i, orig=i, sha=ZERO_SHA, filename=rel, text=rows[i - 1]) for i in range(lo, hi + 1)]


def parse_target(target: str) -> tuple[str, Optional[int], Optional[int]]:
    """`file`, `file:12` or `file:12-30` (a Windows drive letter is not a line)."""
    head, sep, tail = target.rpartition(":")
    if not sep or not head or not tail:
        return target, None, None
    lo, dash, hi = tail.partition("-")
    if not lo.isdigit() or (dash and not hi.isdigit()):
        return target, None, None
    first = int(lo)
    last = int(hi) if dash else first
    if first < 1 or last < first:
        raise ValueError(f"bad line range {tail!r}: want N or N-M with 1 <= N <= M")
    return head, first, last


def _when(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def format_blame(groups: list[dict[str, Any]], *, limit: Optional[int] = None) -> str:
    """Human-readable blame: one header per group of lines, then who and why."""
    out: list[str] = []
    shown = groups if limit is None else groups[:limit]
    for g in shown:
        span = f"{g['start']}" if g["start"] == g["end"] else f"{g['start']}-{g['end']}"
        if g["uncommitted"]:
            head = f"{g['file']}:{span}  (not committed yet)"
        else:
            head = f"{g['file']}:{span}  {str(g['commit'])[:8]}  {_when(g['author_time'])}  {g['author']}"
        p = g.get("provenance")
        if not p:
            out.append(f"{head}  - no agent provenance recorded")
            continue
        out.append(head)
        agent = str(p.get("agent", "?"))
        if p.get("subagent"):
            agent += f" (subagent {p['subagent']})"
        who = f"  agent:  {agent}"
        if p.get("user"):
            who += f" for {p['user']}"
        if p.get("at"):
            who += f", edited {_when(p['at'])}"
        if g.get("match") == "file":
            who += " (matched by file, not by line)"
        out.append(who)
        if p.get("task"):
            out.append(f"  task:   {p['task']}")
        if p.get("prompt"):
            out.append(f"  prompt: \"{p['prompt']}\"")
        if p.get("plan"):
            out.append(f"  plan:   {p['plan']}")
        if p.get("symbols"):
            out.append(f"  in:     {', '.join(p['symbols'][:6])}")
    if limit is not None and len(groups) > limit:
        out.append(f"... {len(groups) - limit} more group(s); narrow the line range to see them")
    return "\n".join(out)


def status(start: Path | str) -> dict[str, Any]:
    """What `hivelane notes status` reports."""
    repo = Repo.open(start)
    records = read_journal(repo.git_dir)
    attached = read_attached(repo.git_dir)
    settings = load_settings(repo.top)
    return {
        "enabled": settings.enabled,
        "prompts": settings.prompts,
        "excerpt_chars": settings.excerpt_chars,
        "journal": str(journal_path(repo.git_dir)),
        "pending": sum(1 for r in records if r["id"] not in attached),
        "notes": len(notes_index(repo)),
        "ref": NOTES_REF,
    }


# --- sharing (explicit only) ----------------------------------------------------------


def push(start: Path | str, remote: str = "origin") -> str:
    repo = Repo.open(start)
    if not notes_index(repo):
        return "no prompt-blame notes to push yet"
    repo.git("push", remote, f"{NOTES_REF}:{NOTES_REF}", timeout=NETWORK_TIMEOUT)
    return f"pushed {NOTES_REF} to {remote}"


def fetch(start: Path | str, remote: str = "origin") -> str:
    """Fetch a remote's notes into a side ref, then merge them into ours.
    cat_sort_uniq keeps every entry line once: notes are JSON Lines, so the
    union of two notes on one commit is still a valid note."""
    repo = Repo.open(start)
    side = f"{REMOTE_NOTES_PREFIX}{remote}"
    try:
        repo.git("fetch", remote, f"+{NOTES_REF}:{side}", timeout=NETWORK_TIMEOUT)
    except GitError as exc:
        if "couldn't find remote ref" in str(exc).lower():
            return f"{remote} has no prompt-blame notes"
        raise
    try:
        repo.git("rev-parse", "--verify", "-q", NOTES_REF)
        has_local = True
    except GitError:
        has_local = False
    if has_local:
        repo.git("notes", f"--ref={NOTES_REF}", "merge", "-q", "-s", "cat_sort_uniq", side)
    else:
        repo.git("update-ref", NOTES_REF, side)
    return f"merged {remote}'s prompt-blame notes into {NOTES_REF}"
