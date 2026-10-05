"""`synergo watch`: observe-only fallback for agents with no hooks (e.g. Aider).

Polls file mtimes with the stdlib and posts a post_edit for every created,
modified or deleted text file. It cannot stop an edit, only warn after it.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from synergo.mcp_server import MAX_CONTENT, DaemonClient, DaemonDown, read_text

SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".synergo"})

Poster = Callable[[dict[str, Any]], Optional[dict[str, Any]]]


class Snapshot:
    """mtime/size + cached text of every watched file."""

    def __init__(self) -> None:
        self.stats: dict[str, tuple[float, int]] = {}
        self.texts: dict[str, Optional[str]] = {}


class Watcher:
    def __init__(
        self,
        root: Path,
        post: Poster,
        *,
        agent: str = "unknown",
        ignore: Iterable[str] = (),
        cap: int = MAX_CONTENT,
    ) -> None:
        from synergo.daemon import IgnoreRules
        from synergo.protocol import AGENT_KINDS

        self.root = Path(root).resolve()
        self.post = post
        self.agent = agent if agent in AGENT_KINDS else "unknown"
        self.tool = f"watch:{agent}"
        self.rules = IgnoreRules(ignore)
        self.cap = cap
        self.session_id = f"watch-{uuid.uuid4().hex[:12]}"
        self.snap = Snapshot()

    def _scan(self) -> dict[str, tuple[float, int]]:
        found: dict[str, tuple[float, int]] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                if self.rules and self.rules.matches(rel):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found[full] = (st.st_mtime, st.st_size)
        return found

    def prime(self) -> None:
        self.snap.stats = self._scan()
        self.snap.texts = {p: read_text(Path(p), self.cap) for p in self.snap.stats}

    def poll(self) -> list[dict[str, Any]]:
        """One scan; posts and returns the events for what changed."""
        current = self._scan()
        events: list[dict[str, Any]] = []
        for path, stat in current.items():
            if self.snap.stats.get(path) == stat:
                continue
            new = read_text(Path(path), self.cap)
            old = self.snap.texts.get(path)
            self.snap.texts[path] = new
            if new is None or new == old:
                continue  # binary, oversized, or touched without a change
            events.append(self._event(path, "" if old is None else old, new))
        for path in set(self.snap.stats) - set(current):
            old = self.snap.texts.pop(path, None)
            if old is not None:
                events.append(self._event(path, old, ""))
        self.snap.stats = current
        for event in events:
            self.post(event)
        return events

    def _event(self, path: str, old: str, new: str) -> dict[str, Any]:
        return {
            "kind": "post_edit",
            "agent": self.agent,
            "session_id": self.session_id,
            "cwd": str(self.root),
            "file_path": path,
            "old_content": old,
            "new_content": new,
            "tool": self.tool,
        }

    def run(self, interval: float = 1.0) -> None:
        self.prime()
        while True:
            time.sleep(interval)
            self.poll()


def daemon_poster(port: int, say: Callable[[str], None]) -> Poster:
    client = DaemonClient(port)
    warned = {"down": False}

    def post(event: dict[str, Any]) -> Optional[dict[str, Any]]:
        rel = event["file_path"]
        try:
            ack = client.post_event(event)
        except DaemonDown as exc:
            if not warned["down"]:
                say(f"synergo watch: {exc}; edits are not being shared")
                warned["down"] = True
            return None
        warned["down"] = False
        if ack.get("decision", "allow") != "allow" or ack.get("others"):
            who = ", ".join(str(o.get("user_id", "?")) for o in ack.get("others") or [])
            say(f"! {rel}: {ack.get('reason') or 'teammate active here'}" + (f" ({who})" if who else ""))
        else:
            say(f"  shared edit: {rel}")
        return ack

    return post
