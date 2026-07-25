"""Local file watcher.

Watches a project directory for saves, coalesces a burst of changes into one
batch, and ships it to the relay as a single `activity` message.

    # Room-based relay (after deploy):
    python watcher/watcher.py --path ../my-project --room K7M2PQ --server ws://localhost:8000/ws

    # Legacy single-room relay (current Render deploy):
    python watcher/watcher.py --path ../my-project --name adrian --server wss://HOST/ws

--room is the workspace code shared by whoever created the room (see
POST /rooms). Omit it to talk to an older /ws-only relay.
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
PROMPT_POLL_SECONDS = 3.0
MAX_FILE_BYTES = 512_000
# Render proxies and busy event loops often miss a 20s pong window; keep
# pings frequent enough to stay alive, but tolerate delayed replies.
WS_PING_INTERVAL = 20
WS_PING_TIMEOUT = 60
READ_RETRY_SECONDS = 0.08

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
IGNORED_SUFFIXES = {".pyc", ".pyo", ".log", ".lock", ".swp", ".tmp"}

_UNSAFE_NAME = re.compile(r"[^a-z0-9_-]")

ALLOWED_AGENTS = ("cursor", "claude-code", "codex", "unknown")


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
    """Bridges watchdog's observer thread onto the asyncio loop.

    Cursor/VS Code often save via temp-file + rename, which surfaces as
    on_moved rather than on_modified — we must handle that or teammates'
    new files never get reported.
    """

    def __init__(self, root: Path, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._root = root
        self._loop = loop
        self._queue = queue

    def on_modified(self, event: FileSystemEvent) -> None:
        self._enqueue(getattr(event, "src_path", None), event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        self._enqueue(getattr(event, "src_path", None), event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Atomic saves: content lands at dest_path.
        self._enqueue(getattr(event, "dest_path", None), event.is_directory)

    def on_closed(self, event: FileSystemEvent) -> None:
        # Linux/some editors fire this after a write finishes.
        self._enqueue(getattr(event, "src_path", None), event.is_directory)

    def _enqueue(self, raw_path: object, is_directory: bool) -> None:
        if is_directory or not raw_path:
            return
        path = Path(str(raw_path)).resolve()
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
        agent: str = "unknown",
    ) -> None:
        self._root = root
        self._user_id = user_id
        self._socket = socket
        self._prompt_override = prompt_override
        self._agent = agent
        self._tracker = DiffTracker()
        self._pending: dict[str, str] = {}
        self._deadline: Optional[float] = None
        self._last_prompt: Optional[str] = None

    async def add(self, path: Path) -> None:
        content = await asyncio.to_thread(read_text, path)
        if content is None:
            # Create/rename events can race the writer; one short retry helps.
            # Use asyncio.sleep so the WS keepalive loop can still process pongs.
            await asyncio.sleep(READ_RETRY_SECONDS)
            content = await asyncio.to_thread(read_text, path)
        if content is None:
            return
        try:
            key = relative_key(path, self._root)
        except ValueError:
            return
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
        # SQLite prompt capture is sync I/O — keep it off the event loop so
        # WebSocket ping/pong frames are not starved during bursts.
        prompt = await asyncio.to_thread(self._resolve_prompt)
        self._last_prompt = prompt
        await self._socket.send(
            json.dumps(
                {
                    "type": "activity",
                    "user_id": self._user_id,
                    "timestamp": time.time(),
                    "prompt": prompt,
                    "changes": changes,
                    "agent": self._agent,
                }
            )
        )
        summary = ", ".join(c["file_path"] for c in changes)
        prompt_note = f' prompt="{prompt[:60]}…"' if prompt and len(prompt) > 60 else (
            f' prompt="{prompt}"' if prompt else " prompt=null"
        )
        print(f"-> sent {len(changes)} change(s): {summary}{prompt_note}", flush=True)

    async def flush_prompt_if_changed(self) -> None:
        """Push a prompt-only activity when Cursor has a newer user message.

        File saves are no longer required for the dashboard Active Prompt panel
        to update — we poll Cursor's local DB on a short interval.
        """
        if self._pending:
            return  # A file batch is about to flush with the prompt attached.
        prompt = await asyncio.to_thread(self._resolve_prompt)
        if not prompt or prompt == self._last_prompt:
            return
        self._last_prompt = prompt
        await self._socket.send(
            json.dumps(
                {
                    "type": "activity",
                    "user_id": self._user_id,
                    "timestamp": time.time(),
                    "prompt": prompt,
                    "changes": [],
                    "agent": self._agent,
                }
            )
        )
        note = f'"{prompt[:60]}…"' if len(prompt) > 60 else f'"{prompt}"'
        print(f"-> sent prompt-only update: {note}", flush=True)


def prime_existing_files(batcher: Batcher, root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not is_ignored(path.resolve(), root):
            batcher.prime(path.resolve())


async def join(socket, initial_name: Optional[str], agent: str) -> str:
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

        await socket.send(json.dumps({"type": "join", "name": cleaned, "agent": agent}))
        message = json.loads(await socket.recv())

        if message.get("type") == "name_rejected":
            print(f"Rejected: {message.get('reason')}", flush=True)
            candidate = None
            continue
        # Receiving any broadcast means the server accepted us into the room.
        print(f"Joined as {cleaned}", flush=True)
        return cleaned


def room_ws_url(server_url: str, room_id: str) -> str:
    """Append the room code to the relay's base WS URL, e.g.
    ws://localhost:8000/ws + K7M2PQ -> ws://localhost:8000/ws/K7M2PQ."""
    return f"{server_url.rstrip('/')}/{room_id.strip().upper()}"


def resolve_ws_url(server_url: str, room_id: Optional[str]) -> str:
    """Build the WebSocket URL.

    Prefer room-scoped URLs (`/ws/{ROOM}`) when --room is set. If omitted,
    fall back to the bare `/ws` path so this client still works against an
    older single-room relay that hasn't been redeployed yet.
    """
    base = server_url.rstrip("/")
    if room_id:
        return room_ws_url(base, room_id)
    return base


async def watch_session(
    socket,
    root: Path,
    user_id: str,
    queue: asyncio.Queue,
    prompt_override: Optional[str],
    agent: str,
    need_prime: bool,
) -> None:
    """Pump file events into the relay until the socket drops."""
    batcher = Batcher(
        root, user_id, socket, prompt_override=prompt_override, agent=agent
    )
    if need_prime:
        await asyncio.to_thread(prime_existing_files, batcher, root)

    while True:
        timeout = batcher.seconds_until_flush
        # Wake often enough to flush file batches and poll Cursor for new prompts.
        wait_timeout = (
            PROMPT_POLL_SECONDS if timeout is None else min(timeout, PROMPT_POLL_SECONDS)
        )
        try:
            path = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            if batcher.seconds_until_flush == 0.0:
                await batcher.flush()
            else:
                await batcher.flush_prompt_if_changed()
            continue
        await batcher.add(path)


async def run(
    root: Path,
    server_url: str,
    room_id: Optional[str],
    name: Optional[str],
    prompt_override: Optional[str],
    agent: str,
) -> None:
    url = resolve_ws_url(server_url, room_id)
    if not room_id:
        print(
            "No --room set; using legacy /ws. "
            "Pass --room CODE once the room-based relay is deployed.",
            flush=True,
        )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()
    observer = Observer()
    observer.schedule(SaveHandler(root, loop, queue), str(root), recursive=True)
    observer.start()

    capture_mode = (
        "cli --prompt" if prompt_override is not None else "Cursor state.vscdb (best-effort)"
    )
    print(
        f"Watching {root} (debounce {DEBOUNCE_SECONDS}s). "
        f"Prompt source: {capture_mode}. Agent: {agent}",
        flush=True,
    )
    if prompt_override is None:
        print(
            "Tip: new Cursor prompts appear on Synergo within a few seconds "
            "(--path must be the same folder Cursor has open).",
            flush=True,
        )
    print("Ctrl+C to stop.", flush=True)

    primed = False
    display_name = name
    backoff = 1.0
    try:
        while True:
            print(f"Connecting to {url}", flush=True)
            try:
                async with websockets.connect(
                    url,
                    max_size=10 * 1024 * 1024,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=5,
                    open_timeout=30,
                ) as socket:
                    user_id = await join(socket, display_name, agent)
                    display_name = user_id  # reuse accepted name on reconnect
                    backoff = 1.0
                    need_prime = not primed
                    primed = True
                    await watch_session(
                        socket,
                        root,
                        user_id,
                        queue,
                        prompt_override,
                        agent,
                        need_prime,
                    )
            except websockets.exceptions.InvalidStatus as exc:
                response = getattr(exc, "response", None)
                status = getattr(exc, "status_code", None) or getattr(
                    response, "status_code", None
                )
                if room_id and status in (403, 404):
                    sys.exit(
                        f"Server rejected {url} (HTTP {status}). "
                        "The live relay may still be the old single-room build — "
                        "omit --room, or deploy the room-based server, then retry."
                    )
                raise
            except (
                websockets.exceptions.ConnectionClosed,
                OSError,
                asyncio.TimeoutError,
            ) as exc:
                print(
                    f"Disconnected ({exc}). Reconnecting in {backoff:.0f}s…",
                    flush=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    finally:
        observer.stop()
        observer.join()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream local file changes to the relay.")
    parser.add_argument("--path", default=".", help="Project directory to watch")
    parser.add_argument(
        "--server", default="ws://localhost:8000/ws", help="Relay WebSocket base URL (without room code)"
    )
    parser.add_argument(
        "--room",
        default=None,
        help="Workspace room code (required once the room-based relay is live). "
        "Omit to use legacy single-room /ws.",
    )
    parser.add_argument("--name", default=None, help="Display name (prompted if omitted)")
    parser.add_argument(
        "--agent",
        default="unknown",
        choices=ALLOWED_AGENTS,
        help="AI coding tool this watcher is reporting for "
        f"(one of: {', '.join(ALLOWED_AGENTS)}; default: unknown)",
    )
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
        asyncio.run(
            run(root, args.server, args.room, args.name, prompt_override, args.agent)
        )
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

        