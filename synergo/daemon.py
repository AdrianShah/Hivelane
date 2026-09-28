"""The Synergo daemon: one asyncio process that is both a relay room member
and a loopback HTTP endpoint for agent hooks."""

from __future__ import annotations

import asyncio
import difflib
import json
import sys
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

import uvicorn
import websockets
from fastapi import FastAPI
from websockets.asyncio.client import connect as ws_connect

from synergo.events import EventAck, NormalizedEvent
from synergo.protocol import (
    ActivityMessage,
    AgentKind,
    EditCheckRequest,
    EditCheckResponse,
    FileChange,
    IntentMessage,
    JoinMessage,
)

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 4600
DEFAULT_SERVER = "ws://localhost:8000/ws"

BATCH_SECONDS = 1.5
EDIT_CHECK_TIMEOUT = 1.5
MAX_BACKOFF = 30.0
SHUTDOWN_GRACE = 5.0
OLD_CONTENT_CACHE_SIZE = 512
OUTBOX_LIMIT = 500


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


class Daemon:
    def __init__(
        self,
        room: str,
        name: str,
        *,
        agent: AgentKind = "claude-code",
        server: str = DEFAULT_SERVER,
        port: int = DEFAULT_PORT,
        batch_seconds: float = BATCH_SECONDS,
        edit_check_timeout: float = EDIT_CHECK_TIMEOUT,
    ) -> None:
        self.room = room
        self.name = name
        self.agent: AgentKind = agent
        self.server = server
        self.port = port
        self.batch_seconds = batch_seconds
        self.edit_check_timeout = edit_check_timeout

        self.connected = False
        self.fatal: Optional[str] = None

        self._shutdown = asyncio.Event()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[EditCheckResponse]] = {}
        self._peers: set[str] = set()
        self._old_content: OrderedDict[tuple[str, str], str] = OrderedDict()

        self._batch: dict[str, FileChange] = {}
        self._batch_deadline = 0.0
        self._batch_task: Optional[asyncio.Task[None]] = None
        self._last_prompt: Optional[str] = None

        self.app = self._build_app()

    # --- relay url -----------------------------------------------------------

    @property
    def ws_url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.room}"

    # --- local HTTP ----------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="synergo-daemon", docs_url=None, redoc_url=None)

        @app.get("/status")
        async def status() -> dict[str, Any]:
            return self.status()

        @app.post("/events")
        async def events(event: NormalizedEvent) -> EventAck:
            return await self.handle_event(event)

        return app

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "room": self.room,
            "name": self.name,
            "agent": self.agent,
            "server": self.ws_url,
            "port": self.port,
            "peers": sorted(self._peers),
            "queued_events": self._outbox.qsize() + len(self._batch),
        }

    # --- event handling ------------------------------------------------------

    async def handle_event(self, event: NormalizedEvent) -> EventAck:
        if event.kind == "intent":
            return self._handle_intent(event)
        if event.kind == "pre_edit":
            return await self._handle_pre_edit(event)
        return self._handle_post_edit(event)

    def _handle_intent(self, event: NormalizedEvent) -> EventAck:
        if not event.prompt:
            return EventAck()
        self._last_prompt = event.prompt
        self._send(IntentMessage(user_id=self.name, prompt=event.prompt))
        return EventAck()

    async def _handle_pre_edit(self, event: NormalizedEvent) -> EventAck:
        if not event.file_path:
            return EventAck()
        rel = relative_path(event.file_path, event.cwd)
        if event.old_content is not None:
            self._remember_old(event.session_id, rel, event.old_content)

        if not self.connected:
            return EventAck(file_path=rel)

        request_id = uuid.uuid4().hex
        future: asyncio.Future[EditCheckResponse] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._send(EditCheckRequest(request_id=request_id, file_path=rel))
        try:
            response = await asyncio.wait_for(future, timeout=self.edit_check_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return EventAck(file_path=rel)
        return EventAck(
            decision=response.decision,
            reason=response.reason,
            file_path=response.file_path or rel,
            others=response.others,
        )

    def _handle_post_edit(self, event: NormalizedEvent) -> EventAck:
        if not event.file_path:
            return EventAck()
        rel = relative_path(event.file_path, event.cwd)
        old = event.old_content
        if old is None:
            old = self._old_content.pop((event.session_id, rel), "")
        diff = unified_diff(rel, old, event.new_content or "")
        if diff:
            self._batch[rel] = FileChange(file_path=rel, diff=diff)
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
        if not self._batch:
            return
        changes = list(self._batch.values())
        self._batch.clear()
        self._send(
            ActivityMessage(
                user_id=self.name,
                prompt=self._last_prompt,
                changes=changes,
                agent=self.agent,
            )
        )

    # --- relay socket --------------------------------------------------------

    def _send(self, message: ActivityMessage | IntentMessage | EditCheckRequest) -> None:
        if self._outbox.qsize() >= OUTBOX_LIMIT:
            self._outbox.get_nowait()
        self._outbox.put_nowait(message.model_dump_json())

    async def _relay_loop(self) -> None:
        backoff = 1.0
        while not self._shutdown.is_set():
            try:
                async with ws_connect(self.ws_url) as socket:
                    await socket.send(
                        JoinMessage(name=self.name, agent=self.agent).model_dump_json()
                    )
                    self.connected = True
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
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                if self.connected:
                    print(f"synergo: relay connection lost ({exc})", file=sys.stderr)
            finally:
                self.connected = False

            if self.fatal or self._shutdown.is_set():
                return
            print(f"synergo: reconnecting in {backoff:.0f}s", file=sys.stderr)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _drain(self, socket: Any) -> None:
        while True:
            payload = await self._outbox.get()
            await socket.send(payload)

    def _handle_frame(self, raw: str | bytes) -> None:
        data = json.loads(raw)
        kind = data.get("type")

        if kind == "edit_check_response":
            response = EditCheckResponse.model_validate(data)
            self._peers.update(other.user_id for other in response.others)
            future = self._pending.pop(response.request_id, None)
            if future is not None and not future.done():
                future.set_result(response)
            return

        if kind == "synergo_feed":
            for event in data.get("events") or []:
                user_id = event.get("user_id")
                if user_id and user_id != self.name:
                    self._peers.add(user_id)
                for other in event.get("users") or []:
                    if other != self.name:
                        self._peers.add(other)
            return

        if kind == "name_rejected":
            self.fatal = (
                f"the relay rejected the name {self.name!r}: {data.get('reason', '')}\n"
                "Re-run with a different --name."
            )
        elif kind == "room_rejected":
            self.fatal = (
                f"the relay rejected room {self.room!r}: {data.get('reason', '')}\n"
                "Check the room code, or have a teammate create the room first."
            )

    # --- lifecycle -----------------------------------------------------------

    async def run(self) -> int:
        config = uvicorn.Config(
            self.app,
            host=LOOPBACK,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        print(f"synergo: listening on http://{LOOPBACK}:{self.port}")
        print(f"synergo: joining {self.ws_url} as {self.name} ({self.agent})")

        http = asyncio.create_task(server.serve(), name="http")
        relay = asyncio.create_task(self._relay_loop(), name="relay")
        done, pending = await asyncio.wait({http, relay}, return_when=asyncio.FIRST_COMPLETED)

        self._shutdown.set()
        if self._batch_task is not None:
            self._batch_task.cancel()

        # uvicorn unwinds its lifespan on should_exit; cancelling it instead
        # spills a CancelledError traceback over our own error message.
        server.should_exit = True
        if relay in pending:
            relay.cancel()
        if pending:
            await asyncio.wait(pending, timeout=SHUTDOWN_GRACE)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        if self.fatal:
            print(f"synergo: {self.fatal}", file=sys.stderr)
            return 1
        for task in done:
            exc = task.exception()
            if isinstance(exc, SystemExit):
                return int(exc.code or 1)
            if exc is not None:
                raise exc
        return 0
