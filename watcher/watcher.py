"""Local file watcher.

Watches a project directory for saves, coalesces a burst of changes into one
batch, and ships it to the relay as a single `activity` message.

    python watcher/watcher.py --path ../my-project --server ws://localhost:8000/ws
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import websockets
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from prompt_capture import capture_latest_prompt

DEBOUNCE_SECONDS = 1.5
MAX_FILE_BYTES = 512_000

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".swp", ".tmp"}

_UNSAFE_NAME = re.compile(r"[^a-z0-9_-]")


def prevalidate_name(raw: str) -> Optional[str]:
    """Mirror of the server's rules, for immediate CLI feedback only."""
    name = _UNSAFE_NAME.sub("", raw.strip().lower())
    if not name or len(name) > 20:
        return None
    return name


def is_ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    if any(part in IGNORED_DIRS or part.startswith(".") for part in relative.parts[:-1]):
        return True
    return path.suffix in IGNORED_SUFFIXES


def relative_key(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def read_text(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


class DiffTracker:
    """Keeps a content snapshot per file so each save yields a unified diff
    without depending on git."""

    def __init__(self) -> None:
        self._snapshots: dict[str, str] = {}

    def diff_for(self, key: str, new_content: str) -> Optional[str]:
        previous = self._snapshots.get(key, "")
        if previous == new_content:
            return None
        self._snapshots[key] = new_content
        lines = difflib.unified_diff(
            previous.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{key}",
            tofile=f"b/{key}",
            n=2,
        )
        return "".join(lines).strip() or None


class SaveHandler(FileSystemEventHandler):
    """Bridges watchdog's observer thread onto the asyncio loop."""

    def __init__(self, root: Path, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._root = root
        self._loop = loop
        self._queue = queue

    def on_modified(self, event: FileSystemEvent) -> None:
        self._emit(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._emit(event)

    def _emit(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path)).resolve()
        if is_ignored(path, self._root):
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, path)


class Batcher:
    """Rolling debounce: flushes once no change has arrived for DEBOUNCE_SECONDS."""

    def __init__(
        self,
        root: Path,
        user_id: str,
        socket,
        prompt_override: Optional[str] = None,
    ) -> None:
        self._root = root
        self._user_id = user_id
        self._socket = socket
        self._prompt_override = prompt_override
        self._tracker = DiffTracker()
        self._pending: dict[str, str] = {}
        self._deadline: Optional[float] = None

    def add(self, path: Path) -> None:
        content = read_text(path)
        if content is None:
            return
        key = relative_key(path, self._root)
        diff = self._tracker.diff_for(key, content)
        if diff is None:
            return
        self._pending[key] = diff
        self._deadline = time.monotonic() + DEBOUNCE_SECONDS

    def prime(self, path: Path) -> None:
        """Record a file's current content without queuing it as a change, so
        the first real save diffs against the pre-existing state."""
        content = read_text(path)
        if content is not None:
            self._tracker.diff_for(relative_key(path, self._root), content)

    @property
    def seconds_until_flush(self) -> Optional[float]:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def _resolve_prompt(self) -> Optional[str]:
        if self._prompt_override is not None:
            return self._prompt_override or None
        # Best-effort Cursor capture; None is a valid, supported outcome.
        return capture_latest_prompt(self._root)

    async def flush(self) -> None:
        if not self._pending:
            self._deadline = None
            return
        changes = [
            {"file_path": key, "diff": diff} for key, diff in sorted(self._pending.items())
        ]
        self._pending.clear()
        self._deadline = None
        prompt = self._resolve_prompt()
        await self._socket.send(
            json.dumps(
                {
                    "type": "activity",
                    "user_id": self._user_id,
                    "timestamp": time.time(),
                    "prompt": prompt,
                    "changes": changes,
                }
            )
        )
        summary = ", ".join(c["file_path"] for c in changes)
        prompt_note = f' prompt="{prompt[:60]}…"' if prompt and len(prompt) > 60 else (
            f' prompt="{prompt}"' if prompt else " prompt=null"
        )
        print(f"-> sent {len(changes)} change(s): {summary}{prompt_note}", flush=True)


def prime_existing_files(batcher: Batcher, root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not is_ignored(path.resolve(), root):
            batcher.prime(path.resolve())


async def join(socket, initial_name: Optional[str]) -> str:
    """Negotiate a display name, re-prompting locally on rejection."""
    candidate = initial_name
    while True:
        if candidate is None:
            candidate = input("Display name: ")
        cleaned = prevalidate_name(candidate)
        if cleaned is None:
            print("Names must be 1-20 chars of letters, numbers, - or _.")
            candidate = None
            continue

        await socket.send(json.dumps({"type": "join", "name": cleaned}))
        message = json.loads(await socket.recv())

        if message.get("type") == "name_rejected":
            print(f"Rejected: {message.get('reason')}", flush=True)
            candidate = None
            continue
        # Receiving any broadcast means the server accepted us into the room.
        print(f"Joined as {cleaned}", flush=True)
        return cleaned


async def run(
    root: Path,
    server_url: str,
    name: Optional[str],
    prompt_override: Optional[str],
) -> None:
    async with websockets.connect(server_url, max_size=None) as socket:
        user_id = await join(socket, name)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Path] = asyncio.Queue()
        batcher = Batcher(root, user_id, socket, prompt_override=prompt_override)
        prime_existing_files(batcher, root)

        observer = Observer()
        observer.schedule(SaveHandler(root, loop, queue), str(root), recursive=True)
        observer.start()
        capture_mode = "cli --prompt" if prompt_override is not None else "Cursor state.vscdb (best-effort)"
        print(f"Watching {root} (debounce {DEBOUNCE_SECONDS}s). Prompt source: {capture_mode}", flush=True)
        print("Ctrl+C to stop.", flush=True)

        try:
            while True:
                timeout = batcher.seconds_until_flush
                try:
                    path = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    await batcher.flush()
                    continue
                batcher.add(path)
        finally:
            observer.stop()
            observer.join()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream local file changes to the relay.")
    parser.add_argument("--path", default=".", help="Project directory to watch")
    parser.add_argument(
        "--server", default="ws://localhost:8000/ws", help="Relay WebSocket URL"
    )
    parser.add_argument("--name", default=None, help="Display name (prompted if omitted)")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Override prompt text for every batch (skips Cursor capture). "
        "Pass an empty string to force prompt:null.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(os.path.expanduser(args.path)).resolve()
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")
    # Distinguish "flag omitted" from "flag passed as empty" via argparse default None.
    prompt_override = args.prompt  # may be None (capture) or str (including "")
    try:
        asyncio.run(run(root, args.server, args.name, prompt_override))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
