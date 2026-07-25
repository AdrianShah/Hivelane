"""Local companion API: lets the dashboard start/stop watchers on this machine.

Bound to 127.0.0.1 only — this service spawns local processes, so it must
never listen on 0.0.0.0 or be exposed through a tunnel.

Run from the repo root:
    python -m companion.main
or:
    uvicorn companion.main:app --host 127.0.0.1 --port 4500
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from .detect import detect_tools
    from .process_manager import WatcherAlreadyRunning, WatcherManager, WatcherRecord
except ImportError:  # launched as a flat script: python companion/main.py
    from detect import detect_tools
    from process_manager import WatcherAlreadyRunning, WatcherManager, WatcherRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("companion")

HOST = "127.0.0.1"
PORT = int(os.getenv("COMPANION_PORT", "4500"))

# Any localhost port (Vite dev servers move around) plus explicit prod origins.
LOCALHOST_ORIGIN_REGEX = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"
EXTRA_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "COMPANION_CORS_ORIGINS", "https://your-app.vercel.app"
    ).split(",")
    if origin.strip()
]

manager = WatcherManager()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _install_signal_handlers()
    log.info("Companion listening on http://%s:%d", HOST, PORT)
    try:
        yield
    finally:
        log.info("Shutting down; terminating spawned watchers")
        manager.shutdown()


app = FastAPI(title="Watcher companion", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=EXTRA_ORIGINS,
    allow_origin_regex=LOCALHOST_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


def _install_signal_handlers() -> None:
    """Reap watchers on Ctrl+C / SIGTERM even if uvicorn exits abruptly."""

    def handle(signum: int, _frame: object) -> None:
        log.info("Received signal %s; terminating spawned watchers", signum)
        manager.shutdown()
        # Restore default behaviour so the second signal really exits.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError, AttributeError):
            # Not on the main thread, or unsupported on this platform.
            log.debug("Could not install handler for %s", sig)


# --- schemas -----------------------------------------------------------------

AgentId = Literal["cursor", "claude-code", "codex", "antigravity", "unknown"]


class CreateWatcherRequest(BaseModel):
    path: str = Field(min_length=1)
    room_id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=20)
    agent: AgentId = "unknown"
    server_url: str = Field(default="ws://localhost:8000/ws", min_length=1)


class CreateWatcherResponse(BaseModel):
    watcher_id: str


class WatcherInfo(BaseModel):
    watcher_id: str
    path: str
    agent: str
    room_id: str
    name: str
    server_url: str
    status: Literal["running", "stopped", "errored"]
    pid: Optional[int]
    exit_code: Optional[int]
    started_at: float
    logs: list[str]


def _to_info(record: WatcherRecord) -> WatcherInfo:
    return WatcherInfo(
        watcher_id=record.watcher_id,
        path=record.path,
        agent=record.agent,
        room_id=record.room_id,
        name=record.name,
        server_url=record.server_url,
        status=record.status,
        pid=record.process.pid,
        exit_code=record.exit_code,
        started_at=record.started_at,
        logs=record.recent_logs(),
    )


# --- endpoints ---------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/detect")
async def detect() -> dict[str, object]:
    """Installed / running state for each known AI coding tool."""
    return detect_tools()


@app.post("/watchers", response_model=CreateWatcherResponse, status_code=201)
async def create_watcher(request: CreateWatcherRequest) -> CreateWatcherResponse:
    resolved = Path(os.path.expanduser(request.path))
    if not resolved.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {request.path}")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {request.path}")

    try:
        record = manager.start(
            path=request.path,
            room_id=request.room_id,
            name=request.name,
            agent=request.agent,
            server_url=request.server_url,
        )
    except WatcherAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to spawn watcher: {exc}") from exc

    return CreateWatcherResponse(watcher_id=record.watcher_id)


@app.get("/watchers", response_model=list[WatcherInfo])
async def list_watchers() -> list[WatcherInfo]:
    return [_to_info(record) for record in manager.list()]


@app.delete("/watchers/{watcher_id}", response_model=WatcherInfo)
async def delete_watcher(watcher_id: str) -> WatcherInfo:
    record = manager.stop(watcher_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Watcher not found")
    info = _to_info(record)
    manager.remove(watcher_id)
    return info


def main() -> None:
    import uvicorn

    # host is hard-coded: exposing a process spawner on a LAN would be a
    # remote code execution hole.
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
