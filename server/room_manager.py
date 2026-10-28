"""Registry of live Room instances, keyed by short shareable room codes.

The room code is the only access control: whoever has the code can join.
Same trust model as a Google Docs share link, a Kahoot PIN, or a Figma
multiplayer link, no accounts, no passwords, no sessions."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import time
from typing import Any, Callable, Optional

from pydantic import ValidationError

try:
    from . import config
    from .directory import RoomDirectory, RoomRoute, build_room_directory
    from .models import RoomSettings
    from .room import Room, hash_owner_token, settings_problem
    from .store import RoomStore, build_room_store
except ImportError:  # Render rootDir=server
    if __package__:  # imported as server.*: a real missing dependency, not the flat tree
        raise
    import config  # type: ignore[no-redef]
    from directory import RoomDirectory, RoomRoute, build_room_directory
    from models import RoomSettings
    from room import Room, hash_owner_token, settings_problem
    from store import RoomStore, build_room_store

log = logging.getLogger(__name__)

# Base32-ish alphabet with ambiguous characters removed (0/O, 1/I) so codes
# are easy to read aloud and type on a whiteboard.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# 8 characters of a 32-letter alphabet is 40 bits. Codes now stay valid for a
# week (store.ROOM_TTL_SECONDS), so they have to survive far more guessing
# than the old 6-character (30-bit) codes did over a single session.
CODE_LENGTH = 8
# Codes issued before the switch are 6 characters and live in the store for up
# to a week; lookups accept anything in between so they keep resolving.
MIN_CODE_LENGTH = 6
MAX_CODE_LENGTH = CODE_LENGTH

# What people paste: "abcd-efgh", "ABCD EFGH". Separators are cosmetic.
_CODE_SEPARATORS = re.compile(r"[\s\-]+")
_PLAUSIBLE_CODE = re.compile(rf"[A-Z0-9]{{{MIN_CODE_LENGTH},{MAX_CODE_LENGTH}}}")

# Named members per room (HIVELANE_MAX_MEMBERS) and valid room codes on this
# relay (HIVELANE_MAX_ROOMS). 0, the default, is unlimited; a public relay sets
# both to bound memory and ghost-room flooding (docs/self-hosting.md).
MAX_MEMBERS = config.MAX_MEMBERS
MAX_SPECTATORS = config.MAX_SPECTATORS
MAX_ROOMS = config.MAX_ROOMS

# A room created by POST /rooms that nobody joins within this long is evicted,
# code and all: otherwise a script could hold HIVELANE_MAX_ROOMS (or grow an
# unlimited relay) for the store's week-long TTL. Once anyone has joined, the
# room follows the normal empty-room grace instead.
# HIVELANE_UNCLAIMED_ROOM_TTL_SECONDS; 0 disables the eviction.
def _ttl_from_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value >= 0 else default


UNCLAIMED_ROOM_TTL_SECONDS = _ttl_from_env("HIVELANE_UNCLAIMED_ROOM_TTL_SECONDS", 10 * 60)
# How long an empty room that was in use is kept (main.EMPTY_ROOM_GRACE_SECONDS
# passes its own value; this is the default for revived rooms).
DEFAULT_EMPTY_ROOM_GRACE_SECONDS = 30 * 60


def redact_code(code: str) -> str:
    """Room codes are the join credential: logs carry only a short prefix."""
    return f"{code[:3]}…" if code else "?"


# How long a room lease outlives the last socket in the room on this instance
# (and covers a connection that has claimed it but not joined yet).
RENEW_GRACE_SECONDS = 10.0


def room_is_full(room: Room) -> bool:
    """True when MAX_MEMBERS is set and the room already has that many."""
    return MAX_MEMBERS > 0 and room.member_count >= MAX_MEMBERS


def spectators_full(room: Room) -> bool:
    """True when MAX_SPECTATORS is set and the room already has that many dashboards."""
    return MAX_SPECTATORS > 0 and room.spectator_count >= MAX_SPECTATORS


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_room_code(raw: str) -> str:
    """Canonical form of a typed or pasted code: upper case, no separators."""
    return _CODE_SEPARATORS.sub("", raw).upper()


def is_plausible_code(code: str) -> bool:
    """Cheap shape check before touching the store.

    Deliberately not restricted to CODE_ALPHABET: what matters is that a code
    the store knows keeps resolving, and the shape check exists only to turn
    away garbage (a pasted URL, a 200-character path) without a lookup.
    """
    return _PLAUSIBLE_CODE.fullmatch(code) is not None


def _settings_from_store(room_id: str, stored: Optional[dict]) -> Optional[RoomSettings]:
    """Stored settings back as a model, or None to use the defaults.

    A row written by another build (or edited by hand) may no longer validate;
    the room still revives, on the defaults, rather than staying unknown."""
    if stored is None:
        return None
    try:
        settings = RoomSettings.model_validate(stored)
    except ValidationError as exc:
        log.warning("Ignoring invalid stored settings for room %s: %s", room_id, exc)
        return None
    problem = settings_problem(settings)
    if problem is not None:
        log.warning("Ignoring invalid stored settings for room %s: %s", room_id, problem)
        return None
    return settings


class RoomManager:
    """Live Room instances plus the durable set of valid room codes.

    Room contents stay in memory, but the codes (and each room's conflict
    windows) are recorded in a RoomStore so a restart does not invalidate every
    workspace that is currently shared, nor reset its windows to the defaults."""

    def __init__(
        self,
        store: Optional[RoomStore] = None,
        directory: Optional[RoomDirectory] = None,
    ) -> None:
        self._rooms: dict[str, Room] = {}
        self._store = build_room_store() if store is None else store
        # Which instance serves each room when several share one store (see
        # directory.py). The default, with no Redis, is this instance for all.
        self._directory = build_room_directory() if directory is None else directory
        # Rooms whose lease this instance holds, with when it last had a
        # socket in them (or claimed them, for a connection still joining).
        self._claimed: dict[str, float] = {}
        # Live rooms here that another instance owns, already warned about.
        self._split: set[str] = set()
        # One cleanup timer per room code, re-armed on every departure, so
        # the grace runs from the latest one (and no task piles up per socket).
        self._cleanup_timers: dict[str, asyncio.TimerHandle] = {}
        # Called with a room code when its live state is evicted (main.py
        # registers the webhook cache's forget()). One that raises is logged.
        self.evict_listeners: list[Callable[[str], None]] = []

    def create_room(self, settings: Optional[RoomSettings] = None) -> str:
        """Generate a fresh room code and register an empty Room under it.

        settings, when given, must already be valid (see settings_problem);
        POST /rooms checks them so a bad request is a 422, not a silent default.
        """
        problem = None if settings is None else settings_problem(settings)
        if problem is not None:
            raise ValueError(problem)
        self._store.purge_expired(time.time())
        # The durable count is the one that matters: after a restart, live rooms
        # start at zero and would otherwise let the registry grow past the cap.
        # The live count still bounds memory if the store is degraded. With no
        # cap (the default) the store is not counted at all.
        if MAX_ROOMS > 0 and max(len(self._rooms), self._store.count()) >= MAX_ROOMS:
            raise RuntimeError(f"Maximum number of rooms reached ({MAX_ROOMS})")
        for _ in range(10):  # collisions are astronomically unlikely; bound retries anyway
            room_id = _generate_code()
            if room_id not in self._rooms and not self._store.exists(room_id):
                self._rooms[room_id] = Room(settings=settings)
                self._rooms[room_id].code = room_id
                self._store.add(
                    room_id,
                    time.time(),
                    None if settings is None else settings.model_dump(),
                )
                log.info(
                    "Room %s created (%d room(s) live)", redact_code(room_id), len(self._rooms)
                )
                if UNCLAIMED_ROOM_TTL_SECONDS > 0:
                    self.schedule_cleanup(room_id, UNCLAIMED_ROOM_TTL_SECONDS)
                return room_id
        raise RuntimeError("Failed to generate a unique room code")

    def get(self, room_id: str) -> Room | None:
        """Look up a room by code, reviving a known code whose Room is gone.

        Still strict about unknown codes: a typo or an expired code returns None
        and the caller rejects the connection. Accepts both current 8-character
        codes and legacy 6-character ones, with or without separators.
        Raises store.StoreReadError when the store cannot say whether the code
        exists or what its access record is."""
        normalized = normalize_room_code(room_id)
        room = self._rooms.get(normalized)
        if room is not None:
            return room
        if not is_plausible_code(normalized):
            return None
        # One read for the code, its settings and its access record. A store
        # that cannot answer raises StoreReadError, which propagates: the
        # caller must refuse the connection (fail closed) rather than revive
        # the room without its owner and approval policy.
        record = self._store.get_record(normalized)
        if record is None:
            return None
        # Known code with no live Room: the relay restarted. An empty Room is
        # the right starting point, every touch older than the conflict window
        # is ignored anyway, so the room re-forms from live activity. Its
        # windows are not live activity, though, so they come back from the store.
        room = Room(settings=_settings_from_store(normalized, record.get("settings")))
        # Who owns the room and whether joins need approval are not live
        # activity either.
        room.load_access(record.get("access"))
        room.code = normalized
        self._rooms[normalized] = room
        log.info(
            "Room %s revived after restart (%d room(s) live)",
            redact_code(normalized),
            len(self._rooms),
        )
        # A revived room whose connection never completes is evicted again
        # (from memory only, see cleanup_if_empty) after the usual grace.
        self.schedule_cleanup(normalized, DEFAULT_EMPTY_ROOM_GRACE_SECONDS)
        return room

    def live(self, room_id: str) -> Room | None:
        """The live Room for a code, without reviving one."""
        return self._rooms.get(normalize_room_code(room_id))

    def schedule_cleanup(self, room_id: str, delay: float) -> None:
        """(Re)arm the room's single cleanup timer to fire `delay` from now.

        When it fires, cleanup_if_empty decides: a room in use then is kept,
        and its next departure arms the timer again. Without a running loop
        (synchronous tests) nothing is scheduled."""
        normalized = normalize_room_code(room_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        previous = self._cleanup_timers.pop(normalized, None)
        if previous is not None:
            previous.cancel()
        self._cleanup_timers[normalized] = loop.call_later(
            max(0.0, delay), self._cleanup_due, normalized
        )

    def _cleanup_due(self, code: str) -> None:
        self._cleanup_timers.pop(code, None)
        try:
            self.cleanup_if_empty(code)
        except Exception:
            log.exception("Cleanup of room %s failed", redact_code(code))

    @property
    def cleanup_timer_count(self) -> int:
        return len(self._cleanup_timers)

    def save_settings(self, room_id: str, settings: RoomSettings) -> None:
        """Persist a room's current windows so a revived room keeps them."""
        self._store.set_settings(normalize_room_code(room_id), settings.model_dump())

    def save_access(self, room_id: str, room: Room) -> None:
        """Persist a room's owner-token hash and join policy (never the token)."""
        self._store.set_access(normalize_room_code(room_id), room.access_record())

    # --- webhooks (server/webhooks.py) ----------------------------------------

    def verify_owner_token(self, room_id: str, token: Optional[str]) -> bool:
        """True if token is the owner token of a valid room code, live or not.

        Read from the store for a room with no live state, so an owner can
        manage webhooks without reviving (and so keeping alive) the room."""
        if not token:
            return False
        normalized = normalize_room_code(room_id)
        room = self._rooms.get(normalized)
        if room is not None:
            return room.verify_owner_token(token)
        if not is_plausible_code(normalized) or not self._store.exists(normalized):
            return False
        access = self._store.get_access(normalized) or {}
        owner_hash = access.get("owner_hash")
        if not isinstance(owner_hash, str) or not owner_hash:
            return False
        return hmac.compare_digest(hash_owner_token(token), owner_hash)

    def load_webhooks(self, room_id: str) -> Optional[dict[str, Any]]:
        return self._store.get_webhooks(normalize_room_code(room_id))

    def save_webhooks(self, room_id: str, record: dict[str, Any]) -> None:
        self._store.set_webhooks(normalize_room_code(room_id), record)

    def cleanup_if_empty(self, room_id: str) -> None:
        normalized = normalize_room_code(room_id)
        room = self._rooms.get(normalized)
        if room is not None and room.is_empty:
            del self._rooms[normalized]
            timer = self._cleanup_timers.pop(normalized, None)
            if timer is not None:
                timer.cancel()
            for listener in list(self.evict_listeners):
                try:
                    listener(normalized)
                except Exception:
                    log.exception("Room eviction listener failed")
            # Opt-in history (server/history.py) goes with the room's state.
            room.history.clear()
            if self._directory.shared:
                if self._claimed.pop(normalized, None) is not None:
                    self._directory.release(normalized)
                owner = self._directory.owner(normalized)
                if owner is not None and owner != self._directory.instance_id:
                    # The room's members are on another instance now (or this
                    # one only served a stray connection): the code stays valid.
                    log.info(
                        "Room %s: dropped this instance's empty copy", redact_code(normalized)
                    )
                    return
            # A finished room must stop being a valid code, or it would be
            # revived by the next connection that guesses or replays it.
            self._store.remove(normalized)
            log.info(
                "Room %s removed (empty, %d room(s) left)",
                redact_code(normalized),
                len(self._rooms),
            )

    # --- which instance serves a room (see directory.py) ---------------------

    @property
    def instance_id(self) -> str:
        return self._directory.instance_id

    @property
    def shares_rooms(self) -> bool:
        """True when other relay instances may own some of the rooms."""
        return self._directory.shared

    def route(self, room_id: str) -> RoomRoute:
        """Which instance should serve a connection for `room_id`, claiming the
        room for this one if nobody holds it.

        Codes that will be refused anyway (implausible, unknown, expired) are
        "local": the WebSocket route rejects them, and nothing is claimed.
        """
        normalized = normalize_room_code(room_id)
        me = self._directory.instance_id
        if not self._directory.shared or normalized in self._claimed:
            return RoomRoute(normalized, me, True)
        if not is_plausible_code(normalized) or not self._store.exists(normalized):
            return RoomRoute(normalized, me, True)
        owner = self._directory.claim(normalized)
        if owner == me:
            self._claimed[normalized] = time.monotonic()
            return RoomRoute(normalized, me, True)
        return RoomRoute(normalized, owner, False)

    def take_over(self, room_id: str, stale_owner: str) -> bool:
        """Claim a room whose owner is known to be unreachable."""
        normalized = normalize_room_code(room_id)
        if not self._directory.take_over(normalized, stale_owner):
            return False
        self._claimed[normalized] = time.monotonic()
        return True

    def renew_leases(self, now: Optional[float] = None) -> None:
        """Hold a lease exactly while this instance has sockets in the room.
        Run every directory.RENEW_INTERVAL_SECONDS.

        - A room with members or spectators here keeps its lease, or takes it
          if it lapsed (say this instance served the room while another held
          it, and that one's copy emptied).
        - A lease whose room has had nobody here for RENEW_GRACE_SECONDS is
          released, even though the empty Room lingers for its grace period:
          the next teammate to connect may then claim it wherever they land.
        """
        moment = time.monotonic() if now is None else now
        for code, room in list(self._rooms.items()):
            if room.is_empty:
                continue
            held = code in self._claimed
            if self._directory.renew(code):
                if not held:
                    log.info("Room %s: this instance owns it now", code)
                self._claimed[code] = moment
                self._split.discard(code)
            elif held:
                del self._claimed[code]
                log.warning(
                    "Room %s is now owned by another relay instance; members "
                    "still connected here are split from it until they reconnect",
                    code,
                )
            elif code not in self._split:
                self._split.add(code)
                log.warning(
                    "Room %s has members on this instance and on its owner %s: "
                    "they cannot see each other's edits. Route /ws/<code> by room "
                    "code at the load balancer (docs/self-hosting.md#scaling-out)",
                    code,
                    self._directory.owner(code) or "?",
                )
        for code, seen in list(self._claimed.items()):
            room = self._rooms.get(code)
            if (room is None or room.is_empty) and moment - seen >= RENEW_GRACE_SECONDS:
                # Claimed for a connection that never revived the room (it was
                # rate-limited, say), or a room everyone here has left.
                del self._claimed[code]
                self._directory.release(code)
        self._split.intersection_update(self._rooms)

    def release_leases(self) -> None:
        """Give up every lease, so the rooms move as soon as members reconnect
        (at shutdown) instead of after the lease runs out."""
        for code in list(self._claimed):
            self._directory.release(code)
        self._claimed.clear()

    @property
    def leases_held(self) -> int:
        return len(self._claimed)

    @property
    def room_count(self) -> int:
        """Rooms live in this process (what /health reports)."""
        return len(self._rooms)

    @property
    def durable_room_count(self) -> int:
        """Valid room codes, including those with no live Room yet."""
        return self._store.count()

    @property
    def member_count(self) -> int:
        """Named agents connected across every live room (for /metrics)."""
        return sum(room.member_count for room in self._rooms.values())

    @property
    def spectator_count(self) -> int:
        """Dashboards connected across every live room (for /metrics)."""
        return sum(room.spectator_count for room in self._rooms.values())
