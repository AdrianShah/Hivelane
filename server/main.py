"""Relay server: accepts watcher and spectator WebSocket connections, keeps
room state in memory, and broadcasts activity to everyone.

Run from the repo root:
    uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
Then expose it with:
    ngrok http 8000
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from .models import (
    ActivityMessage,
    ActivityUpdate,
    JoinMessage,
    NameRejected,
    UserJoined,
    UserLeft,
    placeholder_caption,
)
from .room import Room
from .sanitize import sanitize_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

app = FastAPI(title="Multiplayer AI Agent Viewer relay")

# Dev/hackathon setting: the dashboard and watchers arrive from localhost, LAN
# addresses, and rotating ngrok hostnames.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

room = Room()


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "users": room.member_ids}


async def handshake(socket: WebSocket) -> Optional[str]:
    """Run the join handshake. Returns the accepted user_id, or None for a
    spectator. Re-prompts on the same socket when a name is rejected."""
    while True:
        raw = await socket.receive_json()
        try:
            join = JoinMessage.model_validate(raw)
        except ValidationError:
            await room.send(socket, NameRejected(reason="Invalid name"))
            continue

        if join.is_spectator:
            return None

        try:
            user_id = sanitize_name(join.name or "")
        except ValueError as exc:
            await room.send(socket, NameRejected(reason=str(exc)))
            continue

        if room.is_taken(user_id):
            await room.send(socket, NameRejected(reason="Name taken"))
            continue

        return user_id


async def handle_activity(user_id: str, raw: dict) -> None:
    try:
        message = ActivityMessage.model_validate(raw)
    except ValidationError as exc:
        log.warning("Ignoring malformed activity from %s: %s", user_id, exc)
        return

    # The connection owns its identity; never trust a client-supplied user_id.
    message.user_id = user_id
    room.record_activity(message)

    # Swapped for a Gemini call later; the message shape does not change.
    caption = placeholder_caption(message)

    await room.broadcast(
        ActivityUpdate(
            user_id=message.user_id,
            timestamp=message.timestamp,
            prompt=message.prompt,
            changes=message.changes,
            caption=caption,
        )
    )


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket) -> None:
    await socket.accept()

    try:
        user_id = await handshake(socket)
    except (WebSocketDisconnect, RuntimeError):
        return

    if user_id is None:
        room.add_spectator(socket)
        log.info("Spectator connected")
        for existing in room.member_ids:
            await room.send(socket, UserJoined(user_id=existing))
        try:
            while True:
                await socket.receive_json()
        except (WebSocketDisconnect, RuntimeError):
            room.remove_spectator(socket)
            log.info("Spectator disconnected")
        return

    room.add_member(user_id, socket)
    log.info("%s joined (%d in room)", user_id, len(room.member_ids))
    await room.broadcast(UserJoined(user_id=user_id))

    try:
        while True:
            raw = await socket.receive_json()
            if raw.get("type") == "activity":
                await handle_activity(user_id, raw)
            else:
                log.debug("Unhandled message type from %s: %s", user_id, raw.get("type"))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.remove_member(user_id)
        log.info("%s left (%d in room)", user_id, len(room.member_ids))
        await room.broadcast(UserLeft(user_id=user_id))
