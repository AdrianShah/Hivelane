"""Relay server: accepts watcher and spectator WebSocket connections, keeps
room state in memory, and broadcasts activity to everyone.

Run from the repo root:
    uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
Then expose it with:
    ngrok http 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

try:
    from .gemini import classify_conflict, generate_caption
    from .models import (
        ActivityMessage,
        ActivityUpdate,
        Decision,
        EditCheckRequest,
        EditCheckResponse,
        EditDecision,
        IntentMessage,
        IntentUpdate,
        JoinMessage,
        NameRejected,
        PeerTouch,
        RoomRejected,
        UserJoined,
        UserLeft,
    )
    from .room import FileOverlap, FileTouch, Room
    from .room_manager import MAX_MEMBERS, RoomManager
    from .sanitize import sanitize_name
except ImportError:  # Render runs with rootDir=server as a flat module tree
    from gemini import classify_conflict, generate_caption
    from models import (
        ActivityMessage,
        ActivityUpdate,
        Decision,
        EditCheckRequest,
        EditCheckResponse,
        EditDecision,
        IntentMessage,
        IntentUpdate,
        JoinMessage,
        NameRejected,
        PeerTouch,
        RoomRejected,
        UserJoined,
        UserLeft,
    )
    from room import FileOverlap, FileTouch, Room
    from room_manager import MAX_MEMBERS, RoomManager
    from sanitize import sanitize_name

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

app = FastAPI(title="Synergo relay")

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "https://hackday-agent-viewer.vercel.app,http://localhost:5173",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # Preview deploys use https://hackday-agent-viewer-*.vercel.app
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

room_manager = RoomManager()

# Drop activity spam: same user must wait this long between accepted updates.
ACTIVITY_MIN_INTERVAL_SECONDS = 0.5

# Keep empty rooms around so a shared Vercel link / watcher reconnect still
# works after a brief disconnect (Render free-tier blips, laptop sleep, etc.).
EMPTY_ROOM_GRACE_SECONDS = 30 * 60

# Private-use close code: room code unknown / expired.
WS_CLOSE_ROOM_NOT_FOUND = 4404


def _schedule_empty_room_cleanup(room_id: str) -> None:
    async def _cleanup_later() -> None:
        await asyncio.sleep(EMPTY_ROOM_GRACE_SECONDS)
        room_manager.cleanup_if_empty(room_id)

    asyncio.create_task(_cleanup_later())


def _log_conflict_task_error(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.warning("Conflict broadcast phase failed: %s", exc)


@app.post("/rooms")
async def create_room() -> dict[str, str]:
    """Generate a fresh room code. The room code is the shared secret —
    whoever has it can join, same trust model as a Google Docs share link."""
    try:
        room_id = room_manager.create_room()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"room_id": room_id}


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "rooms": room_manager.room_count}


async def handshake(socket: WebSocket, room: Room) -> Optional[tuple[str, str]]:
    """Run the join handshake. Returns (user_id, agent) for a named member, or
    None for a spectator. Re-prompts on the same socket when a name is rejected."""
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

        if len(room.member_ids) >= MAX_MEMBERS:
            await room.send(socket, NameRejected(reason="Room full"))
            continue

        return user_id, join.agent


async def evaluate_and_broadcast_conflicts(
    room: Room,
    user_id: str,
    message: ActivityMessage,
    overlaps: list[FileOverlap],
) -> None:
    """Classify each overlapping file and broadcast conflict_alert when warranted.

    Fully isolated from the activity_update path — any failure here is logged
    and swallowed so the main broadcast loop keeps working.
    """
    if not overlaps:
        return

    for overlap in overlaps:
        try:
            alert = await classify_conflict(
                file_path=overlap.file_path,
                current_user=user_id,
                current_diff=overlap.current_diff,
                current_prompt=message.prompt,
                other=overlap.other,
            )
            if alert is None:
                continue
            log.info(
                "Conflict %s on %s between %s",
                alert.verdict,
                alert.file_path,
                alert.users,
            )
            await room.broadcast(alert)
        except Exception as exc:
            log.warning(
                "Conflict evaluation failed for %s on %s: %s",
                user_id,
                overlap.file_path,
                exc,
            )


async def handle_activity(room: Room, user_id: str, raw: dict) -> None:
    try:
        message = ActivityMessage.model_validate(raw)
    except ValidationError as exc:
        log.warning("Ignoring malformed activity from %s: %s", user_id, exc)
        return

    # The connection owns its identity; never trust a client-supplied user_id.
    # agent is labeling-only and is taken from the client (validated by Pydantic).
    message.user_id = user_id
    # Server time is authoritative — ignore client-supplied timestamps.
    message.timestamp = time.time()

    if room.is_activity_rate_limited(user_id, ACTIVITY_MIN_INTERVAL_SECONDS):
        log.debug("Rate-limiting activity from %s", user_id)
        return

    # Detect same-file overlaps against active peers BEFORE recording this batch.
    overlaps = room.find_overlaps(message)
    room.record_activity(message)

    # Always broadcast the activity update first — conflict work is best-effort.
    try:
        caption = await generate_caption(message)
    except Exception as exc:
        log.warning("Caption path crashed; using empty caption: %s", exc)
        caption = None

    update = ActivityUpdate(
        user_id=message.user_id,
        timestamp=message.timestamp,
        prompt=message.prompt,
        changes=message.changes,
        caption=caption,
        agent=message.agent,
    )
    room.remember_update(update)
    log.info(
        "activity from %s (%d file(s), prompt=%s)",
        user_id,
        len(message.changes),
        "yes" if message.prompt else "no",
    )
    await room.broadcast(update)

    # Conflict Gemini calls can take several seconds each — run them in the
    # background so this socket's receive loop stays free for keepalive frames.
    if overlaps:
        task = asyncio.create_task(
            evaluate_and_broadcast_conflicts(room, user_id, message, overlaps)
        )
        task.add_done_callback(_log_conflict_task_error)


async def handle_intent(room: Room, user_id: str, raw: dict) -> None:
    try:
        message = IntentMessage.model_validate(raw)
    except ValidationError as exc:
        log.warning("Ignoring malformed intent from %s: %s", user_id, exc)
        return

    message.user_id = user_id
    message.timestamp = time.time()

    if room.is_activity_rate_limited(
        user_id, ACTIVITY_MIN_INTERVAL_SECONDS, channel="intent"
    ):
        log.debug("Rate-limiting intent from %s", user_id)
        return

    agent = room.member_agent(user_id)
    room.record_intent(user_id, message.prompt, message.timestamp, agent)
    log.info("intent from %s: %s", user_id, message.prompt)
    await room.broadcast(
        IntentUpdate(
            user_id=user_id,
            agent=agent,
            prompt=message.prompt,
            timestamp=message.timestamp,
        )
    )


def _edit_check_reason(
    decision: Decision, file_path: str, peers: list[FileTouch], now: float
) -> str:
    """One human-readable sentence about the peer that drove the decision."""
    if decision == "allow" or not peers:
        return ""
    peer = peers[0]
    seconds = int(max(0.0, now - peer.timestamp))
    # Room.check_edit synthesizes a diff-less touch for peers matched only by
    # their declared intent — they have not edited the file yet.
    if not peer.diff:
        return f"{peer.user_id}'s {peer.agent} said they are working on {file_path}"
    if decision == "block":
        return f"{peer.user_id}'s {peer.agent} edited {file_path} {seconds}s ago"
    return f"{peer.user_id}'s {peer.agent} touched this file {seconds}s ago"


async def handle_edit_check(
    room: Room, socket: WebSocket, user_id: str, raw: dict
) -> None:
    """Answer "may I edit this file?" on the agent's blocking hook.

    Deliberately not rate-limited (an agent may probe many files at once) and
    deliberately free of Gemini calls — the reply must go out immediately.
    """
    try:
        request = EditCheckRequest.model_validate(raw)
    except ValidationError as exc:
        log.warning("Ignoring malformed edit_check from %s: %s", user_id, exc)
        return

    now = time.time()
    decision, peers = room.check_edit(user_id, request.file_path, now)
    reason = _edit_check_reason(decision, request.file_path, peers, now)

    await room.send(
        socket,
        EditCheckResponse(
            request_id=request.request_id,
            decision=decision,
            file_path=request.file_path,
            others=[
                PeerTouch(
                    user_id=peer.user_id,
                    agent=peer.agent,
                    seconds_ago=max(0.0, now - peer.timestamp),
                    prompt=peer.prompt,
                )
                for peer in peers
            ],
            reason=reason,
        ),
    )

    if decision == "allow":
        return

    log.info("edit_check %s for %s on %s", decision, user_id, request.file_path)
    await room.broadcast(
        EditDecision(
            user_id=user_id,
            agent=room.member_agent(user_id),
            file_path=request.file_path,
            decision=decision,
            others=[peer.user_id for peer in peers],
            reason=reason,
            timestamp=now,
        )
    )


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(socket: WebSocket, room_id: str) -> None:
    room = room_manager.get(room_id)
    await socket.accept()
    if room is None:
        # Accept + JSON reject (then close) so browsers and the watcher get a
        # clear signal. Closing before accept becomes an opaque 1006 in Chrome;
        # closing after accept without a body often arrives as "no close frame"
        # through Render's proxy.
        log.info("Rejecting unknown room %s", room_id)
        await socket.send_json(
            RoomRejected(
                reason=(
                    "Room not found. It may have expired after the relay "
                    "restarted — create a new workspace on the dashboard."
                )
            ).model_dump()
        )
        await socket.close(code=WS_CLOSE_ROOM_NOT_FOUND, reason="Room not found")
        return

    try:
        joined = await handshake(socket, room)
    except (WebSocketDisconnect, RuntimeError):
        _schedule_empty_room_cleanup(room_id)
        return

    if joined is None:
        room.add_spectator(socket)
        log.info("Spectator connected to room %s", room_id)
        # Catch up: current lanes + each user's most recent activity snapshot.
        for existing in room.member_ids:
            await room.send(
                socket,
                UserJoined(user_id=existing, agent=room.member_agent(existing)),
            )
        for intent in room.latest_intents():
            await room.send(socket, intent)
        for update in room.latest_updates():
            await room.send(socket, update)
        try:
            while True:
                await socket.receive_json()
        except (WebSocketDisconnect, RuntimeError):
            room.remove_spectator(socket)
            log.info("Spectator disconnected from room %s", room_id)
            _schedule_empty_room_cleanup(room_id)
        return

    user_id, agent = joined
    # Tell the joiner who is already here before announcing them, so a daemon
    # that connects second still knows its peers.
    for existing in room.member_ids:
        await room.send(
            socket, UserJoined(user_id=existing, agent=room.member_agent(existing))
        )
    room.add_member(user_id, socket, agent)
    log.info(
        "%s joined room %s as %s (%d in room)",
        user_id,
        room_id,
        agent,
        len(room.member_ids),
    )
    await room.broadcast(UserJoined(user_id=user_id, agent=agent))

    try:
        while True:
            raw = await socket.receive_json()
            kind = raw.get("type")
            if kind == "activity":
                await handle_activity(room, user_id, raw)
            elif kind == "intent":
                await handle_intent(room, user_id, raw)
            elif kind == "edit_check":
                await handle_edit_check(room, socket, user_id, raw)
            else:
                log.debug("Unhandled message type from %s: %s", user_id, kind)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.remove_member(user_id)
        log.info("%s left room %s (%d in room)", user_id, room_id, len(room.member_ids))
        await room.broadcast(UserLeft(user_id=user_id))
        _schedule_empty_room_cleanup(room_id)
