"""Relay server: accepts WebSocket connections from hivelane daemons (named room
members, one per agent) and dashboards (spectators), keeps per-room state in
memory, answers pre-edit checks point-to-point, and broadcasts activity to the
room as a debounced feed.

Run from the repo root:
    uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
To share a local relay with teammates, expose it with e.g. `ngrok http 8000`;
deployed relays run from the Dockerfile image.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError, model_validator

try:
    from . import config, limits, logging_config, metrics
    from . import dashboard, directory
    from . import history, webhooks
    from .gemini import classify_conflict, generate_caption, heuristic_conflict_alert
    from .headers import SecurityHeadersMiddleware
    from .limits import check_room_creation_async, check_ws_attempt_async, client_ip
    from .models import (
        MIN_SUPPORTED_PROTOCOL,
        PROTOCOL_VERSION,
        ActivityMessage,
        ActivityUpdate,
        AgentKind,
        Decision,
        DecisionFeedback,
        DecisionScope,
        EditCheckRequest,
        EditCheckResponse,
        EditDecision,
        FileRequest,
        IntentMessage,
        JoinDecision,
        JoinMessage,
        JoinPending,
        JoinResolved,
        NameRejected,
        PeerTouch,
        PlanConflict,
        PlanConflicts,
        PlanMessage,
        RateLimited,
        RoomPolicy,
        RoomRejected,
        RoomSettings,
        UserJoined,
        UserLeft,
        VersionRejected,
        placeholder_caption,
    )
    from .room import (
        DEFAULT_SETTINGS,
        FileOverlap,
        FileTouch,
        Room,
        add_event_sink,
        fit_e2e,
        hash_owner_token,
        new_owner_token,
        settings_problem,
    )
    from .room_manager import RoomManager, room_is_full
    from .store import StoreReadError, store_kind
    from .sanitize import (
        MAX_FRAME_BYTES,
        cap_activity,
        cap_edit_check,
        cap_intent,
        cap_plan,
        sanitize_name,
    )
except ImportError:  # Render runs with rootDir=server as a flat module tree
    # Imported as server.main (installed, or from the repo root), an
    # ImportError is a genuinely missing dependency. Falling through would
    # import whatever top-level `config` module happens to be on sys.path.
    if __package__:
        raise
    import config  # type: ignore[no-redef]
    import dashboard  # type: ignore[no-redef]
    import directory  # type: ignore[no-redef]
    import history  # type: ignore[no-redef]
    import limits  # type: ignore[no-redef]
    import logging_config  # type: ignore[no-redef]
    import metrics  # type: ignore[no-redef]
    import webhooks  # type: ignore[no-redef]
    from gemini import classify_conflict, generate_caption, heuristic_conflict_alert
    from headers import SecurityHeadersMiddleware  # type: ignore[no-redef]
    from limits import check_room_creation_async, check_ws_attempt_async, client_ip
    from models import (
        MIN_SUPPORTED_PROTOCOL,
        PROTOCOL_VERSION,
        ActivityMessage,
        ActivityUpdate,
        AgentKind,
        Decision,
        DecisionFeedback,
        DecisionScope,
        EditCheckRequest,
        EditCheckResponse,
        EditDecision,
        FileRequest,
        IntentMessage,
        JoinDecision,
        JoinMessage,
        JoinPending,
        JoinResolved,
        NameRejected,
        PeerTouch,
        PlanConflict,
        PlanConflicts,
        PlanMessage,
        RateLimited,
        RoomPolicy,
        RoomRejected,
        RoomSettings,
        UserJoined,
        UserLeft,
        VersionRejected,
        placeholder_caption,
    )
    from room import (  # type: ignore[no-redef]
        DEFAULT_SETTINGS,
        FileOverlap,
        FileTouch,
        Room,
        add_event_sink,
        fit_e2e,
        hash_owner_token,
        new_owner_token,
        settings_problem,
    )
    from room_manager import RoomManager, room_is_full  # type: ignore[no-redef]
    from store import StoreReadError, store_kind  # type: ignore[no-redef]
    from sanitize import (  # type: ignore[no-redef]
        MAX_FRAME_BYTES,
        cap_activity,
        cap_edit_check,
        cap_intent,
        cap_plan,
        sanitize_name,
    )

logging_config.configure()
log = logging.getLogger("relay")


def _relay_version() -> str:
    """The hivelane release this relay runs, for /readyz. The flat Render tree
    (rootDir=server) has no hivelane package, so fall back to the installed
    distribution's metadata, then to ""."""
    try:
        from hivelane import __version__

        return str(__version__)
    except ImportError:
        pass
    try:
        from importlib.metadata import version

        return version("hivelane")
    except Exception:
        return ""


RELAY_VERSION = _relay_version()
# The commit this build was made from, when the deploy says (HIVELANE_COMMIT,
# or Render's RENDER_GIT_COMMIT).
RELAY_COMMIT = (
    os.environ.get("HIVELANE_COMMIT") or os.environ.get("RENDER_GIT_COMMIT") or ""
).strip()


def redact_code(code: str) -> str:
    """A room code is the join credential: logs carry only a short prefix."""
    return f"{code[:3]}…" if code else "?"


app = FastAPI(
    title="Hivelane relay",
    # The interactive API docs serve nothing a client needs, and the
    # dashboard CSP breaks them anyway: off in production.
    docs_url=None if config.IS_PRODUCTION else "/docs",
    redoc_url=None if config.IS_PRODUCTION else "/redoc",
    openapi_url=None if config.IS_PRODUCTION else "/openapi.json",
    # With several relay instances, keep this one's room leases alive while it
    # runs and hand them back at shutdown (see directory.py). A single instance
    # holds no leases, and this does nothing.
    lifespan=lambda _app: directory.maintain_leases(lambda: room_manager),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.CORS_ORIGINS),
    allow_origin_regex=config.CORS_ORIGIN_REGEX,
    allow_credentials=False,
    # DELETE and the owner-token header: the dashboard's webhook panel.
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", webhooks.OWNER_TOKEN_HEADER],
)
# Added after CORS so it wraps it: preflights and error responses get the
# headers too. See headers.py for what is sent and why.
app.add_middleware(SecurityHeadersMiddleware, hsts=config.HSTS_ENABLED)
# Outermost: an upgrade for a room another instance owns is answered with
# fly-replay (on Fly.io) before any route accepts it. Looked up per call so
# tests can swap room_manager.
app.add_middleware(directory.RoomAffinityMiddleware, router=lambda: room_manager)
# The bundled dashboard (Docker image, `hivelane relay`), when
# HIVELANE_STATIC_DIR points at a build. Only answers what no route matches.
dashboard.install(app)

log.info(
    "Hivelane relay configured: env=%s cors_origins=%s cors_origin_regex=%s "
    "trust_proxy_header=%s client_ip_from=%s room_message_limit=%s/min hsts=%s "
    "max_rooms=%s max_members=%s",
    config.ENV,
    ", ".join(config.CORS_ORIGINS),
    config.CORS_ORIGIN_REGEX or "-",
    config.TRUST_PROXY_HEADER,
    limits.client_ip_source(),
    config.ROOM_MESSAGE_LIMIT or "unlimited",
    config.HSTS_ENABLED,
    config.MAX_ROOMS or "unlimited",
    config.MAX_MEMBERS or "unlimited",
)

room_manager = RoomManager()

# --- owner webhooks (server/webhooks.py) ----------------------------------------
# Looked up through room_manager on every call, so tests can swap it.
webhook_service = webhooks.WebhookService(
    load=lambda code: room_manager.load_webhooks(code),
    save=lambda code, record: room_manager.save_webhooks(code, record),
    owner_check=lambda code, token: room_manager.verify_owner_token(code, token),
)
webhooks.install(app, lambda: webhook_service)
add_event_sink(lambda room, payload: webhook_service.observe(room, payload))
# An evicted room's cached webhooks and notice budget go with it.
room_manager.evict_listeners.append(lambda code: webhook_service.forget(code))

metrics.register_gauge(
    "hivelane_rooms_live",
    "Rooms with in-memory state in this process.",
    lambda: room_manager.room_count,
)
metrics.register_gauge(
    "hivelane_rooms_durable",
    "Valid room codes in the room store, live or not.",
    lambda: room_manager.durable_room_count,
    # A store round trip (Redis/SQLite) per scrape: cached.
    cache_seconds=15,
)
metrics.register_gauge(
    "hivelane_members",
    "Named agents connected across all rooms.",
    lambda: room_manager.member_count,
)
metrics.register_gauge(
    "hivelane_spectators",
    "Dashboards connected across all rooms.",
    lambda: room_manager.spectator_count,
)
metrics.register_gauge(
    "hivelane_room_leases",
    "Rooms this instance owns in the shared room directory (0 without Redis).",
    lambda: room_manager.leases_held,
)

# Drop activity spam: same user must wait this long between accepted updates.
ACTIVITY_MIN_INTERVAL_SECONDS = 0.5

# Keep empty rooms around so a shared Vercel link / daemon reconnect still
# works after a brief disconnect (Render free-tier blips, laptop sleep, etc.).
EMPTY_ROOM_GRACE_SECONDS = 30 * 60

# How long a connection may take to send a valid join (not counting an
# approval wait, which has its own timeout), and how many invalid joins it
# may send, before it is closed. Pre-join sockets count against no member
# limit, so without these they could be held open indefinitely.
HANDSHAKE_TIMEOUT_SECONDS = 15.0
MAX_HANDSHAKE_ATTEMPTS = 5
# Private-use close code: no valid join in time (or too many invalid ones).
WS_CLOSE_HANDSHAKE_TIMEOUT = 4408

# Inbound frames per minute from one dashboard socket. Spectators have their
# own budget instead of drawing on the room's, so no dashboard can starve
# the members' traffic.
SPECTATOR_MESSAGE_LIMIT = 120

# Gemini calls (captions and conflict classification) running at once,
# process-wide. Past it, the deterministic placeholder is used instead of
# queueing: a slow Gemini must not pile up tasks holding 512 KB messages.
MAX_GEMINI_IN_FLIGHT = 16
_gemini_in_flight = 0
# The same, per room, so one busy room cannot take every slot; and how many
# overlapping files of one activity message are classified by Gemini (the
# rest get the deterministic alert).
MAX_GEMINI_IN_FLIGHT_PER_ROOM = 4
MAX_CLASSIFIED_OVERLAPS_PER_MESSAGE = 5

# Private-use close code: room code unknown / expired.
WS_CLOSE_ROOM_NOT_FOUND = 4404
# Private-use close code: client protocol too old to coordinate safely.
WS_CLOSE_VERSION_TOO_OLD = 4426
# Private-use close code: too many connection attempts from this IP.
WS_CLOSE_RATE_LIMITED = 4429
# Private-use close code: an approval-required room did not let this joiner in
# (an owner declined, nobody answered in time, or too many were waiting).
WS_CLOSE_JOIN_REFUSED = 4403
# Standard close code (RFC 6455 "message too big"): a frame over MAX_FRAME_BYTES.
WS_CLOSE_FRAME_TOO_LARGE = 1009

# How long a joiner waits for an owner's answer before being turned away.
JOIN_APPROVAL_TIMEOUT_SECONDS = 5 * 60.0
# What owners see for a dashboard waiting to watch an approval-required room.
SPECTATOR_DISPLAY_NAME = "dashboard viewer"

# room_rejected reasons for an approval-required room. Short and stable: a
# client may show them verbatim.
REASON_APPROVAL_DECLINED = "an owner declined your request"
REASON_APPROVAL_TIMED_OUT = "approval timed out"
REASON_APPROVAL_QUEUE_FULL = (
    "too many join requests are waiting for approval; try again later"
)


class VersionTooOld(Exception):
    """Raised by the handshake so the caller closes with the right code."""


class JoinRefused(Exception):
    """Raised while waiting for approval: the joiner may not come in.

    `reason` goes to the client as room_rejected; `metric` is the
    hivelane_ws_rejections_total label."""

    def __init__(self, reason: str, metric: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.metric = metric


class FrameTooLarge(Exception):
    """A frame over MAX_FRAME_BYTES arrived; the socket is closed with 1009."""


@dataclass
class Admission:
    """Who a socket turned out to be once the handshake (and any approval)
    finished. user_id is None for a spectator."""

    user_id: Optional[str]
    agent: AgentKind
    settings: Optional[RoomSettings]
    is_owner: bool = False

    @property
    def is_spectator(self) -> bool:
        return self.user_id is None


async def receive_frame(socket: WebSocket) -> object:
    """The next frame, decoded as JSON. WebSocket.receive_json, plus a size cap.

    Raises FrameTooLarge for a frame over MAX_FRAME_BYTES, and
    WebSocketDisconnect when the peer leaves. A frame that is not JSON comes
    back as None instead of raising, so one bad frame cannot end the session.

    uvicorn has already read the whole frame by now (its own cap is
    --ws-max-size, 16 MiB by default), so this bounds what the relay stores and
    fans out, not what it receives; set --ws-max-size too to bound that.
    """
    message = await socket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
    text = message.get("text")
    if not MAX_FRAME_BYTES:  # HIVELANE_MAX_FRAME_BYTES=0: uvicorn's cap only
        raw = text if text is not None else message.get("bytes") or b""
        try:
            return json.loads(raw)
        except ValueError:
            return None
    if text is None:
        data = message.get("bytes") or b""
        if len(data) > MAX_FRAME_BYTES:
            raise FrameTooLarge
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    # A character is 1-4 bytes in UTF-8, so only a long frame needs encoding.
    elif len(text) > MAX_FRAME_BYTES or (
        len(text) * 4 > MAX_FRAME_BYTES and len(text.encode("utf-8")) > MAX_FRAME_BYTES
    ):
        raise FrameTooLarge
    try:
        return json.loads(text)
    except ValueError:
        return None


async def _close_quietly(socket: WebSocket, code: int, reason: str) -> None:
    """Close, ignoring a peer that is already gone."""
    with contextlib.suppress(Exception):
        await socket.close(code=code, reason=reason)


async def _close_frame_too_large(socket: WebSocket) -> None:
    await _close_quietly(
        socket,
        WS_CLOSE_FRAME_TOO_LARGE,
        f"Frame larger than {MAX_FRAME_BYTES // 1024} KB",
    )


def _validation_summary(exc: ValidationError) -> str:
    """Which fields failed and why, without the submitted values: str(exc)
    echoes input, and input here is prompts and diffs."""
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or '<message>'}: {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False)[:5]
    ]
    more = exc.error_count() - len(problems)
    return "; ".join(problems) + (f" (+{more} more)" if more > 0 else "")


def _drop_invalid(kind: str, sender: str, exc: ValidationError) -> None:
    metrics.messages_dropped_total.inc("invalid")
    log.warning("Ignoring malformed %s from %s: %s", kind, sender, _validation_summary(exc))


def _schedule_empty_room_cleanup(room_id: str) -> None:
    """(Re)arm the room's single cleanup timer: the grace is measured from the
    latest departure, and a room that is in use when it fires is kept."""
    room_manager.schedule_cleanup(room_id, EMPTY_ROOM_GRACE_SECONDS)


def _take_gemini_slot(room: Optional[Room] = None) -> bool:
    global _gemini_in_flight
    if _gemini_in_flight >= MAX_GEMINI_IN_FLIGHT:
        return False
    if room is not None:
        if room.gemini_in_flight >= MAX_GEMINI_IN_FLIGHT_PER_ROOM:
            return False
        room.gemini_in_flight += 1
    _gemini_in_flight += 1
    return True


def _release_gemini_slot(room: Optional[Room] = None) -> None:
    global _gemini_in_flight
    _gemini_in_flight = max(0, _gemini_in_flight - 1)
    if room is not None:
        room.gemini_in_flight = max(0, room.gemini_in_flight - 1)


def _captions_enabled() -> bool:
    """True when captions come from Gemini (a network call): they are then
    generated off the member's receive loop."""
    return bool(os.environ.get("GEMINI_API_KEY"))


def _log_conflict_task_error(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        log.warning("Conflict broadcast phase failed: %s", exc)


class CreateRoomRequest(BaseModel):
    """Optional POST /rooms body: start the room with these conflict windows.

    HTTP-only, so it lives here rather than in models.py, whose models are the
    WebSocket frames mirrored in hivelane/protocol.py. Unlike a join, a bad
    request here is refused (422): the caller is waiting on the response and can
    fix it, whereas a join's settings come from a committed file.
    """

    settings: Optional[RoomSettings] = None

    @model_validator(mode="after")
    def _settings_are_enforceable(self) -> CreateRoomRequest:
        if self.settings is not None:
            problem = settings_problem(self.settings)
            if problem is not None:
                raise ValueError(problem)
        return self


@app.post("/rooms")
async def create_room(
    request: Request, body: Optional[CreateRoomRequest] = None
) -> dict[str, object]:
    """Generate a fresh room code. The room code is the shared secret:
    whoever has it can join, same trust model as a Google Docs share link,
    unless an owner turns on join approval.

    The body is optional: no body creates a room on the default windows,
    exactly as before settings existed. The response echoes the windows in
    force so a CLI can show them without joining first.

    The response also carries owner_token, returned this once and never
    stored (the relay keeps its SHA-256). A join presenting it is an owner:
    it may approve joiners and set the room's join policy.

    Unauthenticated, so it is rate-limited per IP, and a room nobody joins
    is evicted (code and all) after HIVELANE_UNCLAIMED_ROOM_TTL_SECONDS
    (room_manager.UNCLAIMED_ROOM_TTL_SECONDS). Together they bound how long
    a script can hold HIVELANE_MAX_ROOMS (when one is set) or grow an
    unlimited relay's memory.
    """
    # Off the event loop when the limiter is Redis-backed.
    allowance = await check_room_creation_async(request)
    if not allowance.allowed:
        log.warning("Room creation rate-limited for %s", client_ip(request))
        raise HTTPException(
            status_code=429,
            detail="Too many rooms created from this address. Try again later.",
            headers={"Retry-After": str(allowance.retry_after_seconds)},
        )

    settings = body.settings if body is not None else None
    try:
        room_id = room_manager.create_room(settings)
    except RuntimeError as exc:
        # 503 means the relay itself is full, not that this caller is throttled;
        # the dashboard shows different copy for each.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    owner_token = new_owner_token()
    room = room_manager.get(room_id)
    if room is not None:
        room.set_owner_hash(hash_owner_token(owner_token))
        room_manager.save_access(room_id, room)
    effective = settings if settings is not None else DEFAULT_SETTINGS
    return {
        "room_id": room_id,
        "settings": effective.model_dump(),
        "owner_token": owner_token,
    }


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "rooms": room_manager.room_count}


@app.get("/metrics")
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus text exposition of this process's counters and gauges.

    Unauthenticated like /health: it reports only aggregate counts, never room
    codes or names, so exposing it leaks nothing a room code would protect.
    """
    return PlainTextResponse(metrics.render(), media_type=metrics.CONTENT_TYPE)


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    """Readiness: configuration this deploy needs before it can serve a
    dashboard. /health stays a cheap liveness ping."""
    return {
        "status": "ready",
        # Which build is serving: the hivelane version and, when the deploy
        # sets HIVELANE_COMMIT, the commit ("" otherwise).
        "version": RELAY_VERSION,
        "commit": RELAY_COMMIT,
        "env": config.ENV,
        "cors_origins_configured": len(config.CORS_ORIGINS),
        "cors_origin_regex_configured": config.CORS_ORIGIN_REGEX is not None,
        "trust_proxy_header": config.TRUST_PROXY_HEADER,
        # Which header client_ip reads first; "socket" alone means none.
        "client_ip_from": limits.client_ip_source(),
        "room_message_limit_per_minute": config.ROOM_MESSAGE_LIMIT,
        "protocol_version": PROTOCOL_VERSION,
        "min_supported_protocol": MIN_SUPPORTED_PROTOCOL,
        "rooms": room_manager.room_count,
        # "memory" means per-instance limits: fine for one relay, a speed bump
        # only when several run behind a load balancer (see limits.py).
        "rate_limit_backend": limits.BACKEND,
        # Where room codes live: "sqlite", "memory" (lost on restart) or "redis".
        "room_store": store_kind(getattr(room_manager, "_store", None)),
        # Capacity and rate limits in force, 0 meaning unlimited.
        "limits": config.limits_summary(),
    }


def _parse_join(raw: object) -> Optional[JoinMessage]:
    """Validate a join frame, dropping settings that do not validate.

    Settings come from a teammate's committed .hivelane.toml. An out-of-range
    value there must not surface as "Invalid name", the daemon treats a name
    rejection as fatal, so the join is retried without them and the room keeps
    the windows it has. Returns None when the join is invalid regardless.
    """
    try:
        return JoinMessage.model_validate(raw)
    except ValidationError as exc:
        if not isinstance(raw, dict) or raw.get("settings") is None:
            return None
        try:
            join = JoinMessage.model_validate(
                {key: value for key, value in raw.items() if key != "settings"}
            )
        except ValidationError:
            return None
        # Never the raw name or the validation message (which echoes input):
        # client input can be 512 KB, and this runs on every retry.
        log.warning(
            "Ignoring invalid room settings in a join: %s", _validation_summary(exc)
        )
        return join


async def _until_disconnect(socket: WebSocket) -> None:
    """Read and discard frames from a joiner still waiting for approval, and
    return once it disconnects. Nothing it sends counts until it is admitted."""
    while True:
        message = await socket.receive()
        if message["type"] == "websocket.disconnect":
            return


def _pending_reason(room: Room) -> str:
    minutes = int(JOIN_APPROVAL_TIMEOUT_SECONDS // 60)
    if room.owner_count == 0:
        return (
            "This room requires an owner's approval to join. No owner is "
            f"connected right now; you will be let in if one approves within "
            f"{minutes} minutes."
        )
    return (
        "This room requires an owner's approval to join. Waiting up to "
        f"{minutes} minutes for an owner to let you in."
    )


async def await_approval(
    socket: WebSocket, room: Room, name: str, agent: AgentKind, *, ip: str = ""
) -> str:
    """Hold a joiner until an owner of an approval-required room answers.

    Returns the request id once approved; the caller confirms admission to
    the joiner with admit_joiner once nothing else stands in the way. Raises
    JoinRefused (declined, timed out, queue full) or WebSocketDisconnect (the
    joiner left). Either way the request leaves the queue and every owner is
    told how it ended.

    Frames the joiner sends while waiting are read and discarded (that is how
    a disconnect is noticed), which is why the joiner must wait for its own
    join_resolved before sending anything.
    """
    pending = room.open_pending(name, agent, ip=ip)
    if pending is None:
        raise JoinRefused(REASON_APPROVAL_QUEUE_FULL, "approval_queue_full")
    outcome = "withdrawn"
    try:
        await room.send(
            socket,
            JoinPending(request_id=pending.request_id, reason=_pending_reason(room)),
        )
        await room.send_to_owners(pending.request())
        log.info(
            "Join request %s from %s waiting for approval (%d pending, %d owner(s) online)",
            pending.request_id[:8],
            name,
            room.pending_count,
            room.owner_count,
        )
        watcher = asyncio.create_task(_until_disconnect(socket))
        try:
            done, _ = await asyncio.wait(
                {pending.decision, watcher},
                timeout=JOIN_APPROVAL_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            watcher.cancel()
            with contextlib.suppress(BaseException):
                await watcher
        if watcher in done:
            raise WebSocketDisconnect(1001)
        if pending.decision not in done:
            outcome = "timed_out"
            raise JoinRefused(REASON_APPROVAL_TIMED_OUT, "approval_timeout")
        if not pending.decision.result():
            outcome = "declined"
            raise JoinRefused(REASON_APPROVAL_DECLINED, "approval_declined")
        outcome = "approved"
        return pending.request_id
    finally:
        room.close_pending(pending.request_id)
        log.info("Join request %s %s", pending.request_id[:8], outcome)
        # Its own task, shielded: when the joiner's handler is being cancelled
        # (it hung up), the owners must still hear how the request ended.
        announce = asyncio.ensure_future(room.announce_resolved(pending.request_id, outcome))
        with contextlib.suppress(BaseException):
            await asyncio.shield(announce)


async def admit_joiner(socket: WebSocket, room: Room, request_id: str) -> None:
    """Tell an approved joiner it is in. Until this frame arrives it cannot
    know: a member's normal join has no acknowledgement of its own."""
    await room.send(socket, JoinResolved(request_id=request_id, outcome="approved"))


class HandshakeTimeout(Exception):
    """No valid join in HANDSHAKE_TIMEOUT_SECONDS, or too many invalid ones."""


async def handshake(socket: WebSocket, room: Room) -> Admission:
    """Run the join handshake: who is this socket, and may it come in?

    Re-prompts on the same socket when a name is rejected, up to
    MAX_HANDSHAKE_ATTEMPTS joins within HANDSHAKE_TIMEOUT_SECONDS (the clock
    restarts after an approval wait, which has its own timeout). A join
    carrying the room's owner token is an owner. In an approval-required room
    every other joiner (spectators too) waits in await_approval first; an
    approval survives a later "Name taken" or "Room full", so retrying does
    not queue the joiner again.

    A member is registered (room.add_member) here, synchronously after the
    last check passes, so two concurrent joins under one name cannot both get
    in. The caller owns removing it again. Settings are returned rather than
    applied: only a member who is actually admitted gets to change the room's
    windows."""
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
    attempts = 0
    approved: Optional[str] = None
    ip = client_ip(socket)

    async def reject(reason: str) -> None:
        metrics.ws_rejections_total.inc("name")
        await room.send(socket, NameRejected(reason=reason))

    while True:
        attempts += 1
        if attempts > MAX_HANDSHAKE_ATTEMPTS:
            raise HandshakeTimeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HandshakeTimeout
        try:
            raw = await asyncio.wait_for(receive_frame(socket), remaining)
        except asyncio.TimeoutError:
            raise HandshakeTimeout from None
        join = _parse_join(raw)
        if join is None:
            await reject("Invalid name")
            continue

        is_owner = room.verify_owner_token(join.owner_token)

        if join.is_spectator:
            if room.require_approval and not is_owner and approved is None:
                approved = await await_approval(
                    socket, room, SPECTATOR_DISPLAY_NAME, join.agent, ip=ip
                )
                await admit_joiner(socket, room, approved)
            return Admission(None, join.agent, None, is_owner)

        if join.protocol_version < MIN_SUPPORTED_PROTOCOL:
            log.info(
                "Rejecting protocol %s (need >= %s)",
                join.protocol_version,
                MIN_SUPPORTED_PROTOCOL,
            )
            metrics.ws_rejections_total.inc("version")
            await room.send(
                socket,
                VersionRejected(
                    reason=(
                        "This hivelane build is too old for the relay and would "
                        "silently skip conflict checks. Upgrade with "
                        "`uv tool install --force hivelane` (or `pip install -U hivelane`)."
                    )
                ),
            )
            raise VersionTooOld

        try:
            user_id = sanitize_name(join.name or "")
        except ValueError as exc:
            await reject(str(exc))
            continue

        if room.is_taken(user_id):
            await reject("Name taken")
            continue

        if room_is_full(room):
            await reject("Room full")
            continue

        if room.require_approval and not is_owner and approved is None:
            approved = await await_approval(socket, room, user_id, join.agent, ip=ip)
            deadline = time.monotonic() + HANDSHAKE_TIMEOUT_SECONDS
            # Waiting took time: the name or the last seat may be gone since.
            if room.is_taken(user_id):
                await reject("Name taken")
                continue
            if room_is_full(room):
                await reject("Room full")
                continue

        # Every check passed: take the name now, with no await since the
        # checks, so a concurrent join under the same name sees it taken.
        room.add_member(user_id, socket, join.agent)
        room.begin_catch_up(socket)
        if approved is not None:
            try:
                await admit_joiner(socket, room, approved)
            except BaseException:
                room.remove_member(user_id, socket)
                raise
        return Admission(user_id, join.agent, join.settings, is_owner)


async def apply_member_settings(
    room: Room, room_id: str, user_id: str, settings: RoomSettings
) -> None:
    """Adopt a joining member's committed windows room-wide and announce them.

    Last join wins: a team shares one .hivelane.toml, so differing settings mean
    someone is on an older or newer commit, and the member joining now is the
    best evidence of the current one. Persisted so a revived room keeps them.

    e2e is the exception to "last join wins": a join can switch it on but
    never off. A daemon that predates the flag sends only the windows, and a
    teammate on a stale .hivelane.toml (or a hostile client) must not be able
    to tell everyone else the room is cleartext again. Turning e2e off takes a
    new room.

    history_hours follows "last join wins" only when the join states it: a
    daemon that predates it, or a .hivelane.toml without the key, keeps the
    room's (server/history.py), so it cannot switch replay off by joining.
    """
    if room.settings.e2e and not settings.e2e:
        if "e2e" in settings.model_fields_set:
            log.info("Ignoring e2e=false from %s: a join cannot clear e2e", user_id)
        settings = settings.model_copy(update={"e2e": True})
    if "history_hours" not in settings.model_fields_set:
        settings = settings.model_copy(update={"history_hours": room.settings.history_hours})
    update = room.apply_settings(settings, set_by=user_id)
    if update is None:
        return
    room_manager.save_settings(room_id, room.settings)
    await room.broadcast(update)


async def evaluate_and_broadcast_conflicts(
    room: Room,
    user_id: str,
    message: ActivityMessage,
    overlaps: list[FileOverlap],
) -> None:
    """Classify each overlapping file and broadcast conflict_alert when warranted.

    Fully isolated from the activity_update path, any failure here is logged
    and swallowed so the main broadcast loop keeps working. In an e2e room
    nothing is sent to Gemini: the deterministic same-file alert is used, as
    it is whenever MAX_GEMINI_IN_FLIGHT calls are already running.
    """
    if not overlaps:
        return

    private = room.settings.e2e
    for index, overlap in enumerate(overlaps):
        try:
            if (
                private
                or index >= MAX_CLASSIFIED_OVERLAPS_PER_MESSAGE
                or not _take_gemini_slot(room)
            ):
                alert = heuristic_conflict_alert(overlap.file_path, user_id, overlap.other)
            else:
                try:
                    alert = await classify_conflict(
                        file_path=overlap.file_path,
                        current_user=user_id,
                        current_diff=overlap.current_diff,
                        current_prompt=message.prompt,
                        other=overlap.other,
                    )
                finally:
                    _release_gemini_slot(room)
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


def _activity_update(message: ActivityMessage, caption: Optional[str]) -> ActivityUpdate:
    return ActivityUpdate(
        user_id=message.user_id,
        timestamp=message.timestamp,
        prompt=message.prompt,
        changes=message.changes,
        caption=caption,
        agent=message.agent,
        repo_id=message.repo_id,
        branch=message.branch,
        e2e=message.e2e,
    )


async def _captioned_publish(
    room: Room, message: ActivityMessage, previous: Optional[asyncio.Task]
) -> None:
    """Background half of _publish_activity when captions come from Gemini:
    caption, then broadcast, after the same sender's earlier update."""
    caption: Optional[str] = None
    if _take_gemini_slot(room):
        try:
            caption = await generate_caption(message, room.code)
        except Exception as exc:
            log.warning("Caption path crashed; using placeholder: %s", exc)
        finally:
            _release_gemini_slot(room)
    if caption is None:
        caption = placeholder_caption(message)
    if previous is not None and not previous.done():
        with contextlib.suppress(BaseException):
            await previous
    update = _activity_update(message, caption)
    room.remember_update(update)
    await room.broadcast(update)


async def _publish_activity(room: Room, message: ActivityMessage) -> None:
    """Caption an accepted activity and broadcast it as activity_update.

    With Gemini configured the caption is a network call of up to seconds,
    so it runs in a background task: the sender's receive loop (and the
    edit_check queued behind this frame) never waits for it."""
    if room.settings.e2e:
        # The readable content is in the e2e blob, and nothing from an e2e
        # room goes to a third party: the no-network placeholder caption.
        caption: Optional[str] = placeholder_caption(message)
    elif _captions_enabled():
        key = (message.user_id, message.repo_id)
        previous = room.caption_tail.get(key)
        task = asyncio.create_task(_captioned_publish(room, message, previous))
        room.caption_tail[key] = task

        def _done(finished: asyncio.Task, key: tuple[str, str] = key) -> None:
            if room.caption_tail.get(key) is finished:
                room.caption_tail.pop(key, None)
            _log_conflict_task_error(finished)

        task.add_done_callback(_done)
        return
    else:
        try:
            caption = await generate_caption(message, room.code)
        except Exception as exc:
            log.warning("Caption path crashed; using empty caption: %s", exc)
            caption = None
    update = _activity_update(message, caption)
    room.remember_update(update)
    await room.broadcast(update)


def _merge_deferred(pending: ActivityMessage, message: ActivityMessage) -> ActivityMessage:
    """Fold a rate-limited activity into the one already waiting.

    In an e2e room the blob describes exactly one message, so the newer one
    replaces the older; otherwise their changes merge (newest per file)."""
    if message.e2e or pending.e2e:
        return message
    by_path = {change.file_path: change for change in pending.changes}
    for change in message.changes:
        by_path.pop(change.file_path, None)
        by_path[change.file_path] = change
    merged = message.model_copy()
    merged.changes = list(by_path.values())
    merged.prompt = message.prompt or pending.prompt
    return merged


async def _publish_deferred(room: Room, key: tuple[str, str], delay: float) -> None:
    await asyncio.sleep(delay)
    message = room.deferred_activity.pop(key, None)
    if message is None or not room.is_active(key[0]):
        return
    # Counts as this sender's accepted broadcast for the next interval.
    room.is_activity_rate_limited(
        key[0], ACTIVITY_MIN_INTERVAL_SECONDS, channel="activity", scope=key[1]
    )
    await _publish_activity(room, message)


async def handle_activity(room: Room, user_id: str, raw: dict) -> None:
    try:
        message = ActivityMessage.model_validate(raw)
    except ValidationError as exc:
        _drop_invalid("activity", user_id, exc)
        return
    cap_activity(message)

    # The connection owns its identity; never trust a client-supplied user_id.
    # agent is labeling-only and is taken from the client (validated by Pydantic).
    message.user_id = user_id
    # Server time is authoritative, ignore client-supplied timestamps.
    message.timestamp = time.time()
    metrics.activity_messages_total.inc()

    # Activity is state, not chatter: it is always recorded, or peers would
    # get "allow" on a file being edited. Detect same-file overlaps against
    # active peers BEFORE recording this batch.
    overlaps = room.find_overlaps(message)
    room.record_activity(message)

    # Conflict Gemini calls can take several seconds each, run them in the
    # background so this socket's receive loop stays free.
    if overlaps:
        task = asyncio.create_task(
            evaluate_and_broadcast_conflicts(room, user_id, message, overlaps)
        )
        task.add_done_callback(_log_conflict_task_error)

    # Only the broadcast is rate-limited, per (member, repo): a daemon sends
    # one activity per repo back to back, and an outbox drain sends many.
    # Throttled updates are coalesced and published once the interval passes.
    key = (user_id, message.repo_id)
    if room.is_activity_rate_limited(
        user_id, ACTIVITY_MIN_INTERVAL_SECONDS, channel="activity", scope=message.repo_id
    ):
        pending = room.deferred_activity.get(key)
        if pending is None:
            room.deferred_activity[key] = message
            task = asyncio.create_task(
                _publish_deferred(room, key, ACTIVITY_MIN_INTERVAL_SECONDS)
            )
            task.add_done_callback(_log_conflict_task_error)
        else:
            room.deferred_activity[key] = _merge_deferred(pending, message)
        log.debug("Coalescing activity from %s", user_id)
        return

    log.debug(
        "activity from %s (%d file(s), prompt=%s)",
        user_id,
        len(message.changes),
        "yes" if message.prompt else "no",
    )
    await _publish_activity(room, message)


async def handle_intent(room: Room, user_id: str, raw: dict) -> None:
    try:
        message = IntentMessage.model_validate(raw)
    except ValidationError as exc:
        _drop_invalid("intent", user_id, exc)
        return
    cap_intent(message)

    message.user_id = user_id
    message.timestamp = time.time()

    if room.is_activity_rate_limited(
        user_id, ACTIVITY_MIN_INTERVAL_SECONDS, channel="intent", scope=message.repo_id
    ):
        log.debug("Rate-limiting intent from %s", user_id)
        return
    metrics.intents_total.inc()

    update = room.record_intent(
        user_id,
        message.prompt,
        message.timestamp,
        room.member_agent(user_id),
        repo_id=message.repo_id,
        e2e=message.e2e,
    )
    # Never the prompt itself: it is the user's words, and logs outlive rooms.
    log.debug("intent from %s (%d chars)", user_id, len(message.prompt))
    await room.broadcast(update)


def _raw_request_id(raw: object) -> str:
    """A plan's request_id even when the plan itself does not validate."""
    value = raw.get("request_id") if isinstance(raw, dict) else None
    return value[:128] if isinstance(value, str) else ""


async def _answer_plan(room: Room, socket: WebSocket, raw: object, *, reason: str) -> None:
    """Every plan gets exactly one plan_conflicts: an unrecorded one says why."""
    with contextlib.suppress(Exception):
        await room.send(
            socket, PlanConflicts(conflicts=[], request_id=_raw_request_id(raw), reason=reason)
        )


def _fit_plan_conflicts(conflicts: list[PlanConflict], request_id: str) -> None:
    """Keep the reply under the outbound frame budget (see room.fit_e2e)."""
    base = len(PlanConflicts(conflicts=conflicts, request_id=request_id).model_dump_json())
    base -= sum(len(conflict.e2e) for conflict in conflicts)
    fit_e2e(conflicts, base)


async def handle_plan(room: Room, socket: WebSocket, user_id: str, raw: dict) -> None:
    """Store the sender's plan, reply with what it collides with, announce it."""
    try:
        message = PlanMessage.model_validate(raw)
    except ValidationError as exc:
        _drop_invalid("plan", user_id, exc)
        await _answer_plan(room, socket, raw, reason="invalid")
        return
    cap_plan(message)

    message.user_id = user_id
    if room.is_activity_rate_limited(
        user_id, ACTIVITY_MIN_INTERVAL_SECONDS, channel="plan", scope=message.repo_id
    ):
        log.debug("Rate-limiting plan from %s", user_id)
        await _answer_plan(room, socket, raw, reason="rate_limited")
        return

    now = time.time()
    update = room.record_plan(message, now, room.member_agent(user_id))
    conflicts = room.plan_conflicts(update, now)
    _fit_plan_conflicts(conflicts, message.request_id)
    await room.send(socket, PlanConflicts(conflicts=conflicts, request_id=message.request_id))
    log.debug(
        "plan from %s (%d file(s), %d conflict(s))",
        user_id,
        len(message.files),
        len(conflicts),
    )
    await room.broadcast(update)


async def handle_decision_feedback(room: Room, voter: str, raw: dict) -> None:
    """Count a dashboard's useful/noise vote on a warn or block decision."""
    try:
        message = DecisionFeedback.model_validate(raw)
    except ValidationError as exc:
        metrics.messages_dropped_total.inc("invalid")
        log.debug("Ignoring malformed decision_feedback: %s", _validation_summary(exc))
        return
    scope = room.decision_scope(message.decision_id)
    update = room.record_feedback(message.decision_id, message.useful, voter)
    if update is None or scope is None:
        return
    metrics.decision_feedback_total.inc(
        ("true" if message.useful else "false", scope)
    )
    await room.broadcast(update)


def _edit_check_reason(
    decision: Decision,
    file_path: str,
    peers: list[FileTouch],
    now: float,
    scope: DecisionScope = "file",
) -> str:
    """One human-readable sentence about the peer that drove the decision."""
    if decision == "allow" or not peers:
        return ""
    peer = peers[0]
    seconds = int(max(0.0, now - peer.timestamp))
    who = f"{peer.user_id}'s {peer.agent}"
    if scope == "plan" or peer.kind == "planned":
        summary = f": {peer.prompt}" if peer.prompt else ""
        return f"{peer.user_id} plans to change {file_path}{summary}"
    # Peers matched only by their declared intent have not edited the file yet.
    # (Judged by kind alone: a real edit's diff is empty in every e2e room and
    # with share_diffs off, and must still be described as an edit.)
    if scope == "intent" or peer.kind == "intent":
        return f"{who} said they are working on {file_path}"
    if scope == "branch":
        return (
            f"{peer.user_id} edited {file_path} on branch {peer.branch} "
            f"{seconds}s ago, this will conflict at merge"
        )
    if scope == "symbol":
        names = ", ".join(peer.symbols[:3])
        if decision == "block":
            return f"{who} is editing {names} in {file_path} ({seconds}s ago)"
        return (
            f"{who} is editing {names} in {file_path} ({seconds}s ago), "
            "different function, safe to proceed"
        )
    if scope == "lines":
        if decision == "block":
            return f"{who} is editing the same lines of {file_path} ({seconds}s ago)"
        return (
            f"{who} edited a different part of {file_path} {seconds}s ago, "
            "safe to proceed"
        )
    if decision == "block":
        return f"{who} edited {file_path} {seconds}s ago"
    return f"{who} touched this file {seconds}s ago"


async def handle_edit_check(
    room: Room, socket: WebSocket, user_id: str, raw: dict
) -> None:
    """Answer "may I edit this file?" on the agent's blocking hook.

    Deliberately not rate-limited (an agent may probe many files at once) and
    deliberately free of Gemini calls, the reply must go out immediately.
    """
    started = time.perf_counter()
    try:
        request = EditCheckRequest.model_validate(raw)
    except ValidationError as exc:
        _drop_invalid("edit_check", user_id, exc)
        return
    now = time.time()
    if request.symbol and request.symbol not in request.symbols:
        request.symbols.append(request.symbol)
    # Capped after the merge, so the legacy `symbol` cannot add one more.
    cap_edit_check(request)
    symbols = list(request.symbols)
    verdict = room.evaluate_edit(
        user_id,
        request.file_path,
        now,
        repo_id=request.repo_id,
        symbols=symbols,
        line_ranges=list(request.line_ranges),
        branch=request.branch,
    )
    # The file's wait queue (server/file_queue.py) has the last word: it can
    # hold back an edit no touch blocks (the file is promised to whoever is
    # next in line), and it remembers who a block left waiting.
    outcome = room.queue_edit_check(
        user_id,
        room.member_agent(user_id),
        request.repo_id,
        request.file_path,
        verdict,
        now,
        e2e=request.e2e,
        symbols=symbols,
        line_ranges=list(request.line_ranges),
        branch=request.branch,
    )
    decision, peers, scope = outcome.decision, outcome.peers, outcome.scope
    reason = outcome.reason or _edit_check_reason(
        decision, request.file_path, peers, now, scope
    )
    suggestion = room.edit_suggestion(
        decision, user_id, request.repo_id, request.file_path, now, outcome.wait
    )
    decision_id = "" if decision == "allow" else uuid.uuid4().hex
    others = [
        PeerTouch(
            user_id=peer.user_id,
            agent=peer.agent,
            seconds_ago=max(0.0, now - peer.timestamp),
            prompt=peer.prompt,
            symbols=list(peer.symbols),
            branch=peer.branch,
            kind=peer.kind,
            e2e=peer.e2e,
        )
        for peer in peers
    ]
    # Each peer carries its own e2e blob: keep the reply under the frame
    # budget a daemon accepts, dropping the copies of the least relevant.
    fit_e2e(others, len(reason) + len(suggestion) + 1024 + 512 * len(others))
    response = EditCheckResponse(
        request_id=request.request_id,
        decision=decision,
        file_path=request.file_path,
        others=others,
        reason=reason,
        scope=scope,
        decision_id=decision_id,
        queue_position=outcome.queue_position,
        suggestion=suggestion,
    )
    # Recorded once the reply is ready and before it is sent, so anyone who
    # has seen the reply also sees it counted. The feed broadcast below is
    # excluded: the agent's hook is unblocked by the reply alone.
    metrics.edit_check_seconds.observe(time.perf_counter() - started)
    metrics.edit_checks_total.inc((decision, scope))
    await room.send(socket, response)
    # After the reply: the requester's hook is waiting on that, the holders
    # being told someone waits is not urgent.
    await room.deliver_queue_notices(outcome.notices)
    room.ensure_queue_sweeper()

    if decision == "allow":
        return

    room.register_decision(decision_id, scope)
    log.debug(
        "edit_check %s (%s) for %s on %s", decision, scope, user_id, request.file_path[:200]
    )
    await room.broadcast(
        EditDecision(
            user_id=user_id,
            agent=room.member_agent(user_id),
            file_path=request.file_path,
            decision=decision,
            others=[peer.user_id for peer in peers],
            reason=reason,
            timestamp=now,
            repo_id=request.repo_id,
            scope=scope,
            decision_id=decision_id,
            queue_position=outcome.queue_position,
            suggestion=suggestion,
            e2e=request.e2e,
        )
    )


async def handle_file_request(room: Room, user_id: str, raw: dict) -> None:
    """A member asks for a file someone else holds (`hivelane wait`): it joins
    the file's line and the holders hear about it, or, when the file is
    already free and nobody is ahead, it is sent file_released at once."""
    try:
        request = FileRequest.model_validate(raw)
    except ValidationError as exc:
        _drop_invalid("file_request", user_id, exc)
        return
    now = time.time()
    notices = room.queue_file_request(
        user_id,
        room.member_agent(user_id),
        request.repo_id,
        request.file_path,
        now,
        e2e=request.e2e,
        branch=request.branch,
        symbols=list(request.symbols),
    )
    await room.deliver_queue_notices(notices)
    room.ensure_queue_sweeper()


async def handle_join_decision(room: Room, socket: WebSocket, raw: dict) -> None:
    """An owner admits or declines someone waiting. Ignored from non-owners:
    without the owner token, a join_decision is just a frame."""
    if not room.is_owner(socket):
        log.debug("Ignoring join_decision from a socket without the owner token")
        return
    try:
        decision = JoinDecision.model_validate(raw)
    except ValidationError as exc:
        metrics.messages_dropped_total.inc("invalid")
        log.debug("Ignoring malformed join_decision: %s", _validation_summary(exc))
        return
    if not room.resolve_pending(decision.request_id, decision.approve):
        log.debug("join_decision for unknown or settled request %s", decision.request_id[:8])


async def handle_room_policy(
    room: Room, room_id: str, socket: WebSocket, raw: dict
) -> None:
    """An owner turns join approval on or off: persisted, then broadcast."""
    if not room.is_owner(socket):
        log.debug("Ignoring room_policy from a socket without the owner token")
        return
    try:
        policy = RoomPolicy.model_validate(raw)
    except ValidationError as exc:
        metrics.messages_dropped_total.inc("invalid")
        log.debug("Ignoring malformed room_policy: %s", _validation_summary(exc))
        return
    update = room.set_require_approval(policy.require_approval)
    if update is None:
        return
    room_manager.save_access(room_id, room)
    log.info(
        "Room %s now %s approval to join",
        redact_code(room_id),
        "requires" if update.require_approval else "does not require",
    )
    await room.broadcast(update)


async def _answer_throttled_edit_check(room: Room, socket: WebSocket, raw: dict) -> None:
    """Over its edit-check budget, a member still gets an answer: warn, so
    the agent proceeds knowingly instead of timing out (which fails open
    silently)."""
    request_id = raw.get("request_id")
    file_path = raw.get("file_path")
    if not isinstance(request_id, str) or not isinstance(file_path, str):
        return
    with contextlib.suppress(Exception):
        await room.send(
            socket,
            EditCheckResponse(
                request_id=request_id,
                decision="warn",
                file_path=file_path,
                reason="Too many edit checks from this agent; not checked for conflicts",
            ),
        )


def _over_quota(room: Room) -> bool:
    """Spend one unit of the room's message quota; True means drop the frame."""
    if room.allow_message():
        return False
    metrics.messages_dropped_total.inc("room_quota")
    return True


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(socket: WebSocket, room_id: str) -> None:
    # Checked before any room lookup: even an 8-character code is ~40 bits
    # (legacy 6-character ones ~30), and it stays valid for a week, so
    # unlimited connection attempts would make codes brute-forceable.
    allowance = await check_ws_attempt_async(socket)
    if not allowance.allowed:
        log.warning("WebSocket attempt rate-limited for %s", client_ip(socket))
        metrics.ws_rejections_total.inc("rate_limited")
        with contextlib.suppress(Exception):
            await socket.accept()
            await socket.send_json(
                RateLimited(
                    reason=(
                        "Too many connection attempts from this address. "
                        "Wait a moment and reconnect."
                    ),
                    retry_after=allowance.retry_after_seconds,
                ).model_dump()
            )
        await _close_quietly(socket, WS_CLOSE_RATE_LIMITED, "Rate limited")
        return

    try:
        room = room_manager.get(room_id)
    except StoreReadError:
        # The registry cannot answer: refuse (fail closed) rather than revive
        # the room without its owner and approval policy. 1013 tells clients
        # to retry, unlike 4404.
        log.warning("Room registry unreadable; refusing %s for now", redact_code(room_id))
        metrics.ws_rejections_total.inc("store_unavailable")
        with contextlib.suppress(Exception):
            await socket.accept()
        await _close_quietly(socket, 1013, "Room registry unavailable, retry")
        return
    if room is None:
        # Accept + JSON reject (then close) so browsers and the daemon get a
        # clear signal. Closing before accept becomes an opaque 1006 in Chrome;
        # closing after accept without a body often arrives as "no close frame"
        # through Render's proxy.
        log.info("Rejecting unknown room %s", redact_code(room_id))
        metrics.ws_rejections_total.inc("room_not_found")
        await _reject_unknown_room(socket)
        return

    # From here until it is a member or spectator (or gone), this connection
    # keeps the room from being evicted under it.
    room.connecting += 1
    try:
        await socket.accept()
        try:
            admission = await handshake(socket, room)
        except VersionTooOld:
            await _close_quietly(socket, WS_CLOSE_VERSION_TOO_OLD, "Client too old")
            return
        except JoinRefused as refusal:
            metrics.ws_rejections_total.inc(refusal.metric)
            with contextlib.suppress(Exception):
                await room.send(socket, RoomRejected(reason=refusal.reason))
            await _close_quietly(socket, WS_CLOSE_JOIN_REFUSED, "Join refused")
            return
        except HandshakeTimeout:
            metrics.ws_rejections_total.inc("handshake_timeout")
            await _close_quietly(socket, WS_CLOSE_HANDSHAKE_TIMEOUT, "No valid join in time")
            return
        except FrameTooLarge:
            metrics.ws_rejections_total.inc("frame_too_large")
            await _close_frame_too_large(socket)
            return
        except (WebSocketDisconnect, RuntimeError):
            return

        if room_manager.live(room_id) is not room:
            # The room was evicted (and maybe revived as a new Room) while this
            # joiner waited: joining the old object would orphan it.
            if admission.user_id is not None:
                room.remove_member(admission.user_id, socket)
            log.info("Room %s went away during a join", redact_code(room_id))
            await _reject_unknown_room(socket)
            return

        if admission.is_spectator:
            await serve_spectator(socket, room, room_id, admission)
        else:
            await serve_member(socket, room, room_id, admission)
    finally:
        room.connecting -= 1
        _schedule_empty_room_cleanup(room_id)


async def _reject_unknown_room(socket: WebSocket) -> None:
    with contextlib.suppress(Exception):
        if socket.client_state.name == "CONNECTING":
            await socket.accept()
        await socket.send_json(
            RoomRejected(
                reason=(
                    "Room not found. It may have expired after the relay "
                    "restarted, create a new workspace on the dashboard."
                )
            ).model_dump()
        )
    await _close_quietly(socket, WS_CLOSE_ROOM_NOT_FOUND, "Room not found")


async def serve_spectator(
    socket: WebSocket, room: Room, room_id: str, admission: Admission
) -> None:
    """A dashboard: catch-up, then votes and (for owners) access decisions."""
    # Added and snapshotted with no await in between; feeds that arrive while
    # the catch-up is being sent are held and sent after it (finish_catch_up).
    room.add_spectator(socket)
    room.begin_catch_up(socket)
    if admission.is_owner:
        room.add_owner(socket)
    log.info(
        "Spectator connected to room %s%s",
        redact_code(room_id),
        " (owner)" if admission.is_owner else "",
    )
    voter = f"socket:{id(socket)}"
    quota = limits.TokenBucket(SPECTATOR_MESSAGE_LIMIT) if SPECTATOR_MESSAGE_LIMIT > 0 else None
    catch_up: list[BaseModel] = [room.settings_update(), room.policy_update()]
    catch_up.extend(
        UserJoined(user_id=existing, agent=room.member_agent(existing))
        for existing in room.member_ids
    )
    catch_up.extend(room.latest_intents())
    catch_up.extend(room.active_plans())
    catch_up.extend(room.latest_updates())
    if admission.is_owner:
        catch_up.extend(room.pending_requests())
    try:
        # Catch up: the windows in force, the join policy, current lanes, and
        # each user's most recent activity snapshot. Settings go first: they
        # frame everything the dashboard renders after them.
        for frame in catch_up:
            await room.send(socket, frame)
        await room.finish_catch_up(socket)
        while True:
            raw = await receive_frame(socket)
            if quota is not None and not quota.take():
                metrics.messages_dropped_total.inc("spectator_quota")
                continue
            if not isinstance(raw, dict):
                metrics.messages_dropped_total.inc("invalid")
                continue
            kind = raw.get("type")
            if kind == "decision_feedback":
                await handle_decision_feedback(room, voter, raw)
            elif kind == "join_decision":
                await handle_join_decision(room, socket, raw)
            elif kind == "room_policy":
                await handle_room_policy(room, room_id, socket, raw)
            elif kind == "history_request":  # server/history.py
                await history.handle_request(room, socket, raw)
    except FrameTooLarge:
        metrics.messages_dropped_total.inc("frame_too_large")
        await _close_frame_too_large(socket)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.remove_spectator(socket)
        log.info("Spectator disconnected from room %s", redact_code(room_id))


# Frames that judge against the wait queues: lapsed holders are released
# (the lazy half of the sweep) before these, and only these.
_QUEUE_JUDGED = frozenset({"edit_check", "file_request"})


async def serve_member(
    socket: WebSocket, room: Room, room_id: str, admission: Admission
) -> None:
    """A named agent (already registered by the handshake): catch it up,
    announce it, then dispatch its messages until it leaves."""
    assert admission.user_id is not None
    user_id, agent, settings = admission.user_id, admission.agent, admission.settings
    try:
        # Tell the joiner who is already here before announcing them, so a
        # daemon that connects second still knows its peers. Feeds meanwhile
        # are held and follow the catch-up.
        for existing in [m for m in room.member_ids if m != user_id]:
            await room.send(
                socket, UserJoined(user_id=existing, agent=room.member_agent(existing))
            )
        await room.finish_catch_up(socket)
        log.info(
            "%s joined room %s as %s (%d in room%s)",
            user_id,
            redact_code(room_id),
            agent,
            room.member_count,
            ", owner" if admission.is_owner else "",
        )
        await room.broadcast(UserJoined(user_id=user_id, agent=agent))
        if settings is not None:
            await apply_member_settings(room, room_id, user_id, settings)
        if admission.is_owner:
            room.add_owner(socket)
            await room.send(socket, room.policy_update())
            for request in room.pending_requests():
                await room.send(socket, request)
        while True:
            raw = await receive_frame(socket)
            if not isinstance(raw, dict):
                if _over_quota(room):
                    continue
                metrics.messages_dropped_total.inc("invalid")
                continue
            kind = raw.get("type")
            if kind == "edit_check":
                # Its own per-member budget, never the room's: a throttled
                # edit check is answered (warn), never silently dropped.
                await room.tick_file_queues()
                if not room.allow_edit_check(user_id):
                    metrics.messages_dropped_total.inc("edit_check_quota")
                    await _answer_throttled_edit_check(room, socket, raw)
                    continue
                await handle_edit_check(room, socket, user_id, raw)
                continue
            if _over_quota(room):
                if kind == "plan":
                    await _answer_plan(room, socket, raw, reason="rate_limited")
                continue
            if kind in _QUEUE_JUDGED:
                await room.tick_file_queues()
            if kind == "activity":
                await handle_activity(room, user_id, raw)
            elif kind == "intent":
                await handle_intent(room, user_id, raw)
            elif kind == "file_request":
                await handle_file_request(room, user_id, raw)
            elif kind == "plan":
                await handle_plan(room, socket, user_id, raw)
            elif kind == "decision_feedback":
                await handle_decision_feedback(room, f"socket:{id(socket)}", raw)
            elif kind == "join_decision":
                await handle_join_decision(room, socket, raw)
            elif kind == "room_policy":
                await handle_room_policy(room, room_id, socket, raw)
            elif kind == "history_request":  # server/history.py
                await history.handle_request(room, socket, raw)
            else:
                log.debug("Unhandled message type from %s: %.40r", user_id, kind)
    except FrameTooLarge:
        metrics.messages_dropped_total.inc("frame_too_large")
        await _close_frame_too_large(socket)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # Only if this socket still holds the name: a replaced connection's
        # handler must not evict (or announce the departure of) its successor.
        removed = room.remove_member(user_id, socket)
        if removed:
            # Files it held go to whoever waits for them; its places in line go.
            with contextlib.suppress(Exception):
                await room.tick_file_queues()
            log.info(
                "%s left room %s (%d in room)", user_id, redact_code(room_id), room.member_count
            )
            with contextlib.suppress(Exception):
                await room.broadcast(UserLeft(user_id=user_id))
