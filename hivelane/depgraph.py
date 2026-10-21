"""Best-effort dependency-aware warnings: "you're editing a caller of `foo`,
whose signature changed N min ago".

Deliberately simple and local (no relay change):

* A signature change is recorded when a post_edit changes the parameter list
  of a top-level function or a method (hivelane.symbols.signatures, Python via
  `ast`, JS/TS via a declaration regex), or when a peer's activity diff
  removes `def foo(...)` / `function foo(...)` and adds it back with other
  parameters.
* On pre_edit, the target file's current content is checked for each recent
  change: it must import the changed file's module (an import line naming its
  stem) — or be the changed module's sibling importing the name — and call
  `foo(`. Matches become a warn-level note on the ack.

No whole-repo graph is built: the importer test runs on the one file being
edited, which is all a pre_edit needs, and costs a couple of regex scans.
False positives (a same-named function elsewhere) are possible; it only ever
warns.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from hivelane import symbols

WINDOW_SECONDS = 3600.0
MAX_CHANGES = 200

_DIFF_DEF = re.compile(
    r"^(?P<sign>[+-])\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|function)\s+(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)?"
)


@dataclass(frozen=True)
class SignatureChange:
    name: str  # bare function/method name
    file_path: str  # repo-relative path of the defining file
    who: str  # "you" or a peer's user id
    at: float  # unix seconds
    repo_id: str = ""


def _bare(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.split(".", 1)[0]
    if stem in ("__init__", "index"):
        parts = path.replace("\\", "/").split("/")
        stem = parts[-2] if len(parts) > 1 else stem
    return stem


def changed_signatures(path: str, old: str, new: str) -> list[str]:
    """Names whose parameter text differs between old and new (both defined)."""
    if not old or not new:
        return []
    before = symbols.signatures(path, old)
    after = symbols.signatures(path, new)
    return [name for name, params in after.items() if name in before and before[name] != params]


def diff_signature_changes(diff: str) -> list[str]:
    """Function names a unified diff removes and re-adds with other params."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        match = _DIFF_DEF.match(line)
        if not match:
            continue
        params = " ".join(match.group("params").split())
        (removed if match.group("sign") == "-" else added)[match.group("name")] = params
    return [name for name, params in added.items() if name in removed and removed[name] != params]


class DepTracker:
    def __init__(self) -> None:
        self._changes: list[SignatureChange] = []
        self._lock = threading.Lock()

    def record(self, change: SignatureChange) -> None:
        with self._lock:
            self._changes = [
                c for c in self._changes
                if not (c.name == change.name and c.file_path == change.file_path and c.who == change.who)
            ]
            self._changes.append(change)
            if len(self._changes) > MAX_CHANGES:
                del self._changes[: len(self._changes) - MAX_CHANGES]

    def record_local(self, rel: str, old: str, new: str, repo_id: str = "") -> list[str]:
        names = changed_signatures(rel, old, new)
        now = time.time()
        for name in names:
            self.record(SignatureChange(_bare(name), rel, "you", now, repo_id))
        return names

    def record_peer_diff(self, user_id: str, rel: str, diff: str, at: Optional[float] = None, repo_id: str = "") -> list[str]:
        names = diff_signature_changes(diff)
        for name in names:
            self.record(SignatureChange(name, rel, user_id, at or time.time(), repo_id))
        return names

    def note_for(self, rel: str, content: Optional[str], repo_id: str = "", now: Optional[float] = None) -> Optional[str]:
        """Warn text if `content` (the file at rel) calls a recently changed
        function from a module it imports; None otherwise."""
        if not content or len(content) > symbols.MAX_SOURCE_BYTES:
            return None
        now = time.time() if now is None else now
        with self._lock:
            recent = [c for c in self._changes if now - c.at <= WINDOW_SECONDS]
            self._changes = recent
        notes: list[str] = []
        for change in reversed(recent):
            if change.file_path == rel or (repo_id and change.repo_id and change.repo_id != repo_id):
                continue
            if not _imports_module(content, change.file_path, change.name):
                continue
            if not re.search(rf"(?<![\w$]){re.escape(change.name)}\s*\(", content):
                continue
            minutes = max(0, int((now - change.at) // 60))
            ago = "just now" if minutes == 0 else f"{minutes} min ago"
            notes.append(
                f"you're editing a caller of `{change.name}` ({change.file_path}), "
                f"whose signature {change.who} changed {ago}"
            )
            if len(notes) >= 3:
                break
        return "; ".join(notes) if notes else None


def _imports_module(content: str, defining_path: str, name: str) -> bool:
    stem = re.escape(_stem(defining_path))
    # `from pkg import utils` imports pkg/utils.py only when pkg is the
    # defining file's own package (or the import is relative); a bare-word
    # match on any `from x import ...` line flagged unrelated `utils`.
    parts = defining_path.replace("\\", "/").split("/")
    if parts[-1].split(".", 1)[0] in ("__init__", "index"):
        parts = parts[:-1]
    parent = re.escape(parts[-2]) if len(parts) > 1 else None
    from_package = rf"(?:\.+|[\w.]*\b{parent})" if parent else r"\.+"
    pattern = re.compile(
        rf"^\s*(?:from\s+[\w.]*\b{stem}\b[\w.]*\s+import\b|import\s+[\w.]*\b{stem}\b"
        rf"|from\s+{from_package}\s+import\s+.*\b{stem}\b"
        rf"|import\b.*from\s+['\"][^'\"]*\b{stem}(?:\.\w+)?['\"]"
        rf"|.*require\(\s*['\"][^'\"]*\b{stem}(?:\.\w+)?['\"]\s*\))",
        re.M,
    )
    return bool(pattern.search(content))
