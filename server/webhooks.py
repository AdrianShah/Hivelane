"""Owner-configured webhooks: a room's blocks, warnings, conflict alerts and
join requests posted to Slack, Discord or any HTTPS endpoint.

HTTP API (owner only: the X-Synergo-Owner-Token header must be the token
POST /rooms returned, checked against the stored SHA-256 in constant time):

    POST   /rooms/{code}/webhooks        {url, kind, events}  -> 201 {webhook}
    GET    /rooms/{code}/webhooks                              -> {webhooks: [...]}
    DELETE /rooms/{code}/webhooks/{id}                         -> 204

- At most MAX_WEBHOOKS_PER_ROOM per room. `kind` is "slack", "discord" or
  "generic" (the payload shape); `events` picks from "block", "warn",
  "conflict_alert", "join_request".
- https:// only. A development relay (SYNERGO_ENV=development) also accepts
  http:// to localhost, so a local receiver can be tested.
- SSRF: outside development the host is resolved and every address it
  resolves to must be public (no private, loopback, link-local, CGNAT,
  multicast or reserved ranges, IPv4-mapped/6to4/NAT64 forms included). This
  is checked when the webhook is added and again before every delivery, and
  the delivery connects to the address that was checked (TLS still verifies
  the original host name), so DNS rebinding cannot swap in an internal one.
  Redirects are never followed and proxy environment variables are ignored.
- Webhooks are persisted with the room in the room store (the SQLite
  `webhooks` column or the Redis `webhooks` hash) and go when the room code
  does. URLs are secrets (a Slack or Discord URL is the credential): they are
  never logged, and GET returns them masked.

Delivery is off the hot path: Room.broadcast hands each event to
WebhookService.observe (a cheap synchronous filter), and matching events are
posted from background tasks with httpx: DELIVERY_TIMEOUT_SECONDS per
attempt, DELIVERY_RETRIES retries with backoff (on network errors, 429 and
5xx), and a per-webhook circuit breaker that pauses a webhook for
BREAKER_COOLDOWN_SECONDS after BREAKER_THRESHOLD consecutive failed
deliveries. At most ROOM_NOTICES_PER_MINUTE notifications per room per
minute; the rest are dropped and counted.

In an end-to-end encrypted room the relay holds only hashes, and nothing
readable goes to a third party: the payload says "a blocked edit in room
CODE" (and so on), with no paths, names, reasons or prompts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import socket
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

try:
    import httpx
except ImportError:  # pragma: no cover - httpx ships with the relay's dependencies
    httpx = None  # type: ignore[assignment]

try:
    from . import config, limits, metrics
    from .limits import TokenBucket, client_ip
    from .room_manager import normalize_room_code
except ImportError:  # Render rootDir=server
    if __package__:  # imported as server.*: a real missing dependency, not the flat tree
        raise
    import config  # type: ignore[no-redef]
    import limits  # type: ignore[no-redef]
    import metrics  # type: ignore[no-redef]
    from limits import TokenBucket, client_ip  # type: ignore[no-redef]
    from room_manager import normalize_room_code  # type: ignore[no-redef]

log = logging.getLogger(__name__)
# httpx logs every request's full URL at INFO ("HTTP Request: POST
# https://hooks.slack.com/services/..."), and a webhook URL is a credential.
# Keep httpx's own logging to warnings and up (it is process-wide, so the
# Gemini client's request lines go quiet too).
logging.getLogger("httpx").setLevel(logging.WARNING)

WebhookKind = Literal["slack", "discord", "generic"]
WebhookEvent = Literal["block", "warn", "conflict_alert", "join_request"]
WEBHOOK_KINDS: tuple[str, ...] = ("slack", "discord", "generic")
WEBHOOK_EVENTS: tuple[str, ...] = ("block", "warn", "conflict_alert", "join_request")

# SYNERGO_MAX_WEBHOOKS_PER_ROOM (at least 1).
MAX_WEBHOOKS_PER_ROOM = config.MAX_WEBHOOKS_PER_ROOM
MAX_URL_LENGTH = 2048
OWNER_TOKEN_HEADER = "X-Synergo-Owner-Token"

DELIVERY_TIMEOUT_SECONDS = 5.0
DELIVERY_RETRIES = 2
# Wait before retry 1, retry 2.
RETRY_BACKOFF_SECONDS = (1.0, 4.0)
BREAKER_THRESHOLD = 10
BREAKER_COOLDOWN_SECONDS = 15 * 60.0
ROOM_NOTICES_PER_MINUTE = 30
# How long a room's webhook list is trusted before it is read from the store
# again. With several relay instances, a change made through another
# instance reaches this one within this long.
CACHE_SECONDS = 30.0
# Background deliveries in flight across the process; beyond it, dropped.
# SYNERGO_WEBHOOK_MAX_IN_FLIGHT; 0 = unlimited.
MAX_IN_FLIGHT = config.WEBHOOK_MAX_IN_FLIGHT
# Owner API calls per client IP per minute (it is a room-code oracle otherwise).
API_REQUESTS_PER_MINUTE = 30
# Rooms whose notice budget is tracked at once (least recently used dropped).
# SYNERGO_WEBHOOK_MAX_TRACKED_ROOMS; 0 = unlimited.
MAX_TRACKED_ROOMS = config.WEBHOOK_MAX_TRACKED_ROOMS

# Text caps inside payloads (Slack and Discord reject oversized fields).
MAX_TEXT = 500
MAX_PATH = 300

USER_AGENT = "Synergo-Relay-Webhooks/1"

DELIVERY_OUTCOMES = ("delivered", "failed", "circuit_open", "rate_limited", "dropped")

deliveries_total = metrics.Counter(
    "synergo_webhook_deliveries_total",
    "Webhook notifications by outcome: delivered, failed (after retries), skipped "
    "while the webhook's circuit is open, over the room's per-minute budget, or "
    "dropped with too many deliveries in flight.",
    label="outcome",
    values=DELIVERY_OUTCOMES,
)
metrics.registry.unregister(deliveries_total.name)
metrics.registry.register(deliveries_total)

api_limiter = limits.build_limiter(
    "webhook_api",
    limit=API_REQUESTS_PER_MINUTE,
    window_seconds=60,
    max_keys=config.RATE_LIMIT_MAX_TRACKED_IPS,
    store=limits.redis_store,
)


class WebhookUrlError(ValueError):
    """A webhook URL the relay will not call. The message is safe to show
    (it never contains the URL)."""


# --- URL checks and SSRF protection --------------------------------------------


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str  # as written (lower case), without brackets
    port: int
    path: str  # path + query, at least "/"

    @property
    def default_port(self) -> bool:
        return self.port == (443 if self.scheme == "https" else 80)

    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.default_port else f"{host}:{self.port}"

    def pinned_url(self, address: str) -> str:
        """The URL with the host replaced by the checked address."""
        host = f"[{address}]" if ":" in address else address
        return f"{self.scheme}://{host}:{self.port}{self.path}"


def _is_loopback_name(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_url(url: str, *, development: bool) -> Target:
    """Validate a webhook URL's shape. Raises WebhookUrlError."""
    text = (url or "").strip()
    if not text or len(text) > MAX_URL_LENGTH:
        raise WebhookUrlError(f"url must be 1-{MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise WebhookUrlError("url is not a valid URL") from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise WebhookUrlError("url needs a host")
    if parts.username is not None or parts.password is not None:
        raise WebhookUrlError("url must not carry a user name or password")
    if parts.fragment:
        raise WebhookUrlError("url must not have a #fragment")
    if scheme == "http":
        if not (development and _is_loopback_name(host)):
            raise WebhookUrlError(
                "url must be https:// (http:// is accepted only for localhost on a "
                "development relay)"
            )
    elif scheme != "https":
        raise WebhookUrlError("url must be https://")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return Target(scheme, host, port or (443 if scheme == "https" else 80), path)


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable unicast addresses. IPv6 forms that
    embed an IPv4 address (mapped, 6to4, Teredo, NAT64) are judged by it."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is None and address.teredo is not None:
            embedded = address.teredo[1]
        if embedded is None and any(address in net for net in _NAT64):
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_address(embedded)
    return address.is_global and not address.is_multicast


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, in resolver order."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def checked_addresses(
    target: Target, *, resolver: Resolver, development: bool
) -> list[str]:
    """Resolve the target and return the addresses to connect to, in order.

    Outside development every address must be public: one private answer
    among public ones is refused too, since which one a client picks is not
    up to the relay. Raises WebhookUrlError."""
    try:
        literal: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address] = (
            ipaddress.ip_address(target.host)
        )
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = await asyncio.wait_for(
                resolver(target.host, target.port), DELIVERY_TIMEOUT_SECONDS
            )
        except (OSError, asyncio.TimeoutError, UnicodeError):
            raise WebhookUrlError("url's host does not resolve") from None
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in addresses:
        try:
            parsed.append(ipaddress.ip_address(str(raw).split("%", 1)[0]))
        except ValueError:
            raise WebhookUrlError("url's host resolves to something that is not an IP") from None
    if not parsed:
        raise WebhookUrlError("url's host does not resolve")
    if not development and not all(is_public_address(address) for address in parsed):
        raise WebhookUrlError(
            "url resolves to a private, loopback, link-local or otherwise "
            "non-public address"
        )
    return list(dict.fromkeys(str(address) for address in parsed))


async def checked_address(target: Target, *, resolver: Resolver, development: bool) -> str:
    """The first address checked_addresses returns."""
    addresses = await checked_addresses(target, resolver=resolver, development=development)
    return addresses[0]


def mask_url(url: str) -> str:
    """What GET shows: scheme and host, and the last four characters."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        scheme = parts.scheme or "https"
    except ValueError:
        return "…"
    tail = url[-4:] if len(url) >= 16 else ""
    return f"{scheme}://{host}/…{tail}"


# --- stored webhooks -------------------------------------------------------------


@dataclass(frozen=True)
class Webhook:
    id: str
    url: str
    kind: str
    events: tuple[str, ...]
    created_at: float

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "kind": self.kind,
            "events": list(self.events),
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, raw: object) -> Optional["Webhook"]:
        """A stored entry back, or None for one another build wrote."""
        if not isinstance(raw, dict):
            return None
        hook_id, url, kind = raw.get("id"), raw.get("url"), raw.get("kind")
        events = raw.get("events")
        if not (isinstance(hook_id, str) and hook_id and isinstance(url, str) and url):
            return None
        if kind not in WEBHOOK_KINDS or not isinstance(events, list):
            return None
        kept = tuple(e for e in WEBHOOK_EVENTS if e in events)
        if not kept:
            return None
        created = raw.get("created_at")
        return cls(
            hook_id,
            url,
            str(kind),
            kept,
            float(created) if isinstance(created, (int, float)) else 0.0,
        )


def hooks_from_record(record: Optional[dict]) -> list[Webhook]:
    if not isinstance(record, dict) or not isinstance(record.get("hooks"), list):
        return []
    hooks = [Webhook.from_record(raw) for raw in record["hooks"]]
    return [hook for hook in hooks if hook is not None][:MAX_WEBHOOKS_PER_ROOM]


def hooks_record(hooks: list[Webhook]) -> dict[str, Any]:
    return {"hooks": [hook.record() for hook in hooks]}


class WebhookCreate(BaseModel):
    """POST /rooms/{code}/webhooks body. HTTP-only, so not in models.py."""

    url: str = Field(max_length=MAX_URL_LENGTH)
    kind: WebhookKind
    events: list[WebhookEvent] = Field(min_length=1, max_length=len(WEBHOOK_EVENTS))

    @field_validator("events")
    @classmethod
    def _dedupe(cls, events: list[str]) -> list[str]:
        return [event for event in WEBHOOK_EVENTS if event in events]


# --- what a notification says ---------------------------------------------------------


@dataclass
class Notice:
    """One event, reduced to what a notification may carry."""

    event: str  # block | warn | conflict_alert | join_request
    room: str
    e2e: bool
    timestamp: float
    summary: str
    # Cleartext rooms only (all empty in an e2e room):
    user: str = ""
    agent: str = ""
    file: str = ""
    decision: str = ""
    scope: str = ""
    others: list[str] = field(default_factory=list)
    reason: str = ""


def _clip(text: object, limit: int = MAX_TEXT) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def event_kind(payload: dict) -> Optional[str]:
    """Which webhook event a room event is, or None if none."""
    kind = payload.get("type")
    if kind == "edit_decision" and payload.get("decision") in ("block", "warn"):
        return str(payload["decision"])
    if kind == "conflict_alert" and payload.get("verdict") in ("overlapping", "related"):
        return "conflict_alert"
    if kind == "join_request":
        return "join_request"
    return None


_E2E_SUMMARIES = {
    "block": "a blocked edit in room {room}",
    "warn": "a warned edit in room {room}",
    "conflict_alert": "a conflict alert in room {room}",
    "join_request": "a join request waiting for approval in room {room}",
}


def build_notice(room: str, e2e: bool, payload: dict) -> Optional[Notice]:
    kind = event_kind(payload)
    if kind is None:
        return None
    raw_ts = payload.get("timestamp")
    timestamp = float(raw_ts) if isinstance(raw_ts, (int, float)) else time.time()
    if e2e:
        # The relay holds hashes, not names; and names, reasons and the join
        # requester stay out of third-party services too.
        return Notice(kind, room, True, timestamp, _E2E_SUMMARIES[kind].format(room=room))
    if kind in ("block", "warn"):
        user = _clip(payload.get("user_id"), 64)
        agent = _clip(payload.get("agent") or "unknown", 32)
        path = _clip(payload.get("file_path"), MAX_PATH)
        others = [_clip(o, 64) for o in (payload.get("others") or [])][:6]
        verb = "was blocked from editing" if kind == "block" else "was warned before editing"
        summary = f"{user}'s {agent} {verb} {path}"
        if others:
            summary += f" (held by {', '.join(others)})"
        return Notice(
            kind,
            room,
            False,
            timestamp,
            summary,
            user=user,
            agent=agent,
            file=path,
            decision=kind,
            scope=_clip(payload.get("scope") or "file", 16),
            others=others,
            reason=_clip(payload.get("reason")),
        )
    if kind == "conflict_alert":
        users = [_clip(u, 64) for u in (payload.get("users") or [])][:6]
        path = _clip(payload.get("file_path"), MAX_PATH)
        verdict = _clip(payload.get("verdict"), 16)
        return Notice(
            kind,
            room,
            False,
            timestamp,
            f"{verdict.capitalize()} edits to {path} by {' and '.join(users) or 'two agents'}",
            user=", ".join(users),
            file=path,
            decision=verdict,
            others=users,
            reason=_clip(payload.get("explanation")),
        )
    name = _clip(payload.get("name"), 64)
    agent = _clip(payload.get("agent") or "unknown", 32)
    return Notice(
        kind,
        room,
        False,
        timestamp,
        f"{name} ({agent}) is waiting for an owner to approve them into room {room}",
        user=name,
        agent=agent,
    )


_TITLES = {
    "block": "Edit blocked",
    "warn": "Edit warned",
    "conflict_alert": "Conflict alert",
    "join_request": "Join request",
}
_DISCORD_COLORS = {
    "block": 0xE5484D,
    "warn": 0xF5A524,
    "conflict_alert": 0xA78BFA,
    "join_request": 0x38BDF8,
}


def _slack_escape(text: str) -> str:
    # Slack mrkdwn control characters; also defuses <!channel> and links.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code(text: str) -> str:
    """Inline code in Slack mrkdwn / Discord markdown, so paths never render
    as links or formatting."""
    return "`" + text.replace("`", "'") + "`"


def _detail_pairs(notice: Notice) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if notice.event in ("block", "warn"):
        pairs = [
            ("Who", f"{notice.user} ({notice.agent})"),
            ("File", _code(notice.file)),
            ("Decision", f"{notice.decision} · {notice.scope}"),
        ]
        if notice.others:
            pairs.append(("With", ", ".join(notice.others)))
    elif notice.event == "conflict_alert":
        pairs = [("Who", notice.user), ("File", _code(notice.file)), ("Verdict", notice.decision)]
    elif notice.event == "join_request":
        pairs = [("Who", f"{notice.user} ({notice.agent})")]
    return pairs


def format_slack(notice: Notice) -> dict[str, Any]:
    title = _TITLES[notice.event]
    headline = f"*{title}* · {_slack_escape(notice.summary)}"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": headline}}
    ]
    pairs = _detail_pairs(notice)
    if pairs:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{name}*\n{_slack_escape(value)}"}
                    for name, value in pairs
                ],
            }
        )
    if notice.reason:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": _slack_escape(notice.reason)}}
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Synergo · room {_slack_escape(notice.room)}"
                    + (" · end-to-end encrypted" if notice.e2e else ""),
                }
            ],
        }
    )
    return {"text": f"{title}: {notice.summary}", "blocks": blocks}


def format_discord(notice: Notice) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": _TITLES[notice.event],
        "description": _clip(notice.summary, 2000),
        "color": _DISCORD_COLORS[notice.event],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(notice.timestamp)),
        "footer": {
            "text": f"Synergo · room {notice.room}"
            + (" · end-to-end encrypted" if notice.e2e else "")
        },
    }
    fields = [{"name": name, "value": _clip(value, 1000), "inline": True} for name, value in _detail_pairs(notice)]
    if notice.reason:
        fields.append({"name": "Reason", "value": _clip(notice.reason, 1000), "inline": False})
    if fields:
        embed["fields"] = fields
    # Never ping anyone, whatever a name or path contains.
    return {"username": "Synergo", "embeds": [embed], "allowed_mentions": {"parse": []}}


def format_generic(notice: Notice) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if not notice.e2e:
        if notice.event in ("block", "warn"):
            data = {
                "user": notice.user,
                "agent": notice.agent,
                "file": notice.file,
                "decision": notice.decision,
                "scope": notice.scope,
                "others": notice.others,
                "reason": notice.reason,
            }
        elif notice.event == "conflict_alert":
            data = {
                "users": notice.others,
                "file": notice.file,
                "verdict": notice.decision,
                "explanation": notice.reason,
            }
        else:
            data = {"name": notice.user, "agent": notice.agent}
    return {
        "source": "synergo",
        "version": 1,
        "event": notice.event,
        "room": notice.room,
        "e2e": notice.e2e,
        "timestamp": notice.timestamp,
        "text": notice.summary,
        "data": data,
    }


FORMATTERS: dict[str, Callable[[Notice], dict[str, Any]]] = {
    "slack": format_slack,
    "discord": format_discord,
    "generic": format_generic,
}


# --- the service: registry, delivery, breaker ----------------------------------------


@dataclass
class _Breaker:
    failures: int = 0
    open_until: Optional[float] = None
    last_error: str = ""


@dataclass
class _Cached:
    loaded_at: float
    hooks: list[Webhook]


class WebhookError(Exception):
    """A refused API call: `status` and a message safe to return."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


TransportFactory = Callable[[], Any]


class WebhookService:
    """Webhooks for every room: what is configured, and getting it delivered.

    `load` / `save` read and write a room's record in the room store (by
    room code); `owner_check(code, token)` says whether token is that room's
    owner token. Tests pass a fake `transport_factory` (an httpx transport)
    and `resolver`, so nothing touches the network.
    """

    def __init__(
        self,
        *,
        load: Callable[[str], Optional[dict]],
        save: Callable[[str, dict], None],
        owner_check: Callable[[str, Optional[str]], bool],
        transport_factory: Optional[TransportFactory] = None,
        resolver: Resolver = system_resolver,
        development: Optional[bool] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        retry_backoff: tuple[float, ...] = RETRY_BACKOFF_SECONDS,
    ) -> None:
        self._load = load
        self._save = save
        self._owner_check = owner_check
        self.transport_factory = transport_factory
        self.resolver = resolver
        self._development = development
        self._sleep = sleep
        self._clock = clock
        self.retry_backoff = retry_backoff
        self._cache: dict[str, _Cached] = {}
        self._breakers: dict[str, _Breaker] = {}
        self._budgets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    @property
    def development(self) -> bool:
        return config.IS_DEVELOPMENT if self._development is None else self._development

    # --- registry ------------------------------------------------------------

    def hooks(self, code: str, *, fresh: bool = False) -> list[Webhook]:
        cached = self._cache.get(code)
        now = self._clock()
        if fresh or cached is None or now - cached.loaded_at >= CACHE_SECONDS:
            try:
                record = self._load(code)
            except Exception:
                log.exception("Could not read webhooks for room %s", code)
                record = None if cached is None else hooks_record(cached.hooks)
            cached = _Cached(now, hooks_from_record(record))
            self._cache[code] = cached
            live = {hook.id for entry in self._cache.values() for hook in entry.hooks}
            for hook_id in [h for h in self._breakers if h not in live]:
                del self._breakers[hook_id]
        return list(cached.hooks)

    def _store(self, code: str, hooks: list[Webhook]) -> None:
        self._save(code, hooks_record(hooks))
        self._cache[code] = _Cached(self._clock(), list(hooks))

    def forget(self, code: str) -> None:
        """Drop what is cached for a room (it was evicted)."""
        entry = self._cache.pop(code, None)
        self._budgets.pop(code, None)
        if entry is not None:
            for hook in entry.hooks:
                self._breakers.pop(hook.id, None)

    def authorize(self, code: str, token: Optional[str]) -> None:
        """Raise 403 unless token is the room's owner token. An unknown room
        gets the same answer, so this API cannot tell anyone which codes exist."""
        if not token or not self._owner_check(code, token):
            raise WebhookError(403, "Unknown room or wrong owner token")

    def status_of(self, hook: Webhook) -> dict[str, Any]:
        breaker = self._breakers.get(hook.id)
        if breaker is None or breaker.failures == 0:
            state = "ok"
        elif breaker.open_until is not None and self._clock() < breaker.open_until:
            state = "paused"
        else:
            state = "failing"
        return {
            "status": state,
            "consecutive_failures": 0 if breaker is None else breaker.failures,
            "last_error": "" if breaker is None else breaker.last_error,
        }

    def public(self, hook: Webhook) -> dict[str, Any]:
        return {
            "id": hook.id,
            "kind": hook.kind,
            "events": list(hook.events),
            "url": mask_url(hook.url),
            "created_at": hook.created_at,
            **self.status_of(hook),
        }

    def listing(self, code: str) -> list[dict[str, Any]]:
        return [self.public(hook) for hook in self.hooks(code, fresh=True)]

    async def add(self, code: str, body: WebhookCreate) -> Webhook:
        hooks = self.hooks(code, fresh=True)
        if len(hooks) >= MAX_WEBHOOKS_PER_ROOM:
            raise WebhookError(409, f"A room has at most {MAX_WEBHOOKS_PER_ROOM} webhooks")
        url = body.url.strip()
        if any(hook.url == url for hook in hooks):
            raise WebhookError(409, "That webhook is already registered for this room")
        try:
            target = parse_url(url, development=self.development)
            await checked_address(target, resolver=self.resolver, development=self.development)
        except WebhookUrlError as exc:
            raise WebhookError(422, str(exc)) from None
        hook = Webhook(
            id=secrets.token_hex(6),
            url=url,
            kind=body.kind,
            events=tuple(body.events),
            created_at=time.time(),
        )
        # Re-read: another request may have added one while this one resolved.
        hooks = self.hooks(code, fresh=True)
        if len(hooks) >= MAX_WEBHOOKS_PER_ROOM:
            raise WebhookError(409, f"A room has at most {MAX_WEBHOOKS_PER_ROOM} webhooks")
        self._store(code, [*hooks, hook])
        log.info("Room %s: webhook %s added (%s, %s)", code, hook.id, hook.kind, ",".join(hook.events))
        return hook

    def remove(self, code: str, hook_id: str) -> bool:
        hooks = self.hooks(code, fresh=True)
        kept = [hook for hook in hooks if hook.id != hook_id]
        if len(kept) == len(hooks):
            return False
        self._store(code, kept)
        self._breakers.pop(hook_id, None)
        log.info("Room %s: webhook %s removed", code, hook_id)
        return True

    # --- delivery ------------------------------------------------------------

    def observe(self, room: Any, payload: dict) -> None:
        """Room event sink (see room.add_event_sink): synchronous and cheap.
        Anything that might match is handed to a background task."""
        code = getattr(room, "code", "")
        if not code or event_kind(payload) is None:
            return
        cached = self._cache.get(code)
        if (
            cached is not None
            and not cached.hooks
            and self._clock() - cached.loaded_at < CACHE_SECONDS
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if MAX_IN_FLIGHT > 0 and len(self._tasks) >= MAX_IN_FLIGHT:
            deliveries_total.inc("dropped")
            log.warning("Too many webhook deliveries in flight; dropping one for room %s", code)
            return
        e2e = bool(room.settings.e2e)
        task = loop.create_task(self.dispatch(code, e2e, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _budget(self, code: str) -> TokenBucket:
        bucket = self._budgets.get(code)
        if bucket is None:
            bucket = TokenBucket(ROOM_NOTICES_PER_MINUTE, clock=self._clock)
            self._budgets[code] = bucket
            while MAX_TRACKED_ROOMS > 0 and len(self._budgets) > MAX_TRACKED_ROOMS:
                self._budgets.popitem(last=False)
        else:
            self._budgets.move_to_end(code)
        return bucket

    async def dispatch(self, code: str, e2e: bool, payload: dict) -> int:
        """Deliver one room event to every webhook subscribed to it. Returns
        how many deliveries succeeded."""
        kind = event_kind(payload)
        if kind is None:
            return 0
        hooks = [hook for hook in self.hooks(code) if kind in hook.events]
        if not hooks:
            return 0
        if not self._budget(code).take():
            deliveries_total.inc("rate_limited")
            log.debug("Room %s over its webhook budget; dropping a %s notice", code, kind)
            return 0
        notice = build_notice(code, e2e, payload)
        if notice is None:
            return 0
        results = await asyncio.gather(
            *(self.deliver(hook, notice) for hook in hooks), return_exceptions=True
        )
        return sum(1 for result in results if result is True)

    async def deliver(self, hook: Webhook, notice: Notice) -> bool:
        """Post one notice to one webhook, with retries and the breaker."""
        breaker = self._breakers.setdefault(hook.id, _Breaker())
        if breaker.open_until is not None and self._clock() < breaker.open_until:
            deliveries_total.inc("circuit_open")
            return False
        body = json.dumps(FORMATTERS[hook.kind](notice), separators=(",", ":")).encode("utf-8")
        error = ""
        for attempt in range(1 + DELIVERY_RETRIES):
            if attempt:
                await self._sleep(self.retry_backoff[min(attempt - 1, len(self.retry_backoff) - 1)])
            ok, retryable, error = await self._post(hook, body)
            if ok:
                breaker.failures = 0
                breaker.open_until = None
                breaker.last_error = ""
                deliveries_total.inc("delivered")
                return True
            if not retryable:
                break
        breaker.failures += 1
        breaker.last_error = error
        deliveries_total.inc("failed")
        if breaker.failures >= BREAKER_THRESHOLD:
            breaker.open_until = self._clock() + BREAKER_COOLDOWN_SECONDS
            log.warning(
                "Webhook %s failed %d times in a row (%s); pausing it for %d minutes",
                hook.id,
                breaker.failures,
                error,
                int(BREAKER_COOLDOWN_SECONDS // 60),
            )
        else:
            log.info("Webhook %s delivery failed (%s)", hook.id, error)
        return False

    async def _post(self, hook: Webhook, body: bytes) -> tuple[bool, bool, str]:
        """One attempt: (delivered, worth retrying, error label). The label
        never contains the URL."""
        if httpx is None:
            return False, False, "httpx is not installed"
        try:
            target = parse_url(hook.url, development=self.development)
            addresses = await checked_addresses(
                target, resolver=self.resolver, development=self.development
            )
        except WebhookUrlError as exc:
            # Not retried: the answer will be the same in a second.
            return False, False, str(exc)
        headers = {
            "Host": target.host_header(),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        transport = self.transport_factory() if self.transport_factory is not None else None

        async def post_to_first_reachable() -> httpx.Response:
            # Every address was checked; one that refuses the connection (a
            # host with both IPv6 and IPv4, a receiver on only one) gives way
            # to the next. Anything after the connection is the answer.
            async with httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(DELIVERY_TIMEOUT_SECONDS),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for index, address in enumerate(addresses):
                    try:
                        return await client.post(
                            target.pinned_url(address),
                            content=body,
                            headers=headers,
                            extensions=extensions,
                        )
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if index == len(addresses) - 1:
                            raise
            raise httpx.ConnectError("no address")  # unreachable: addresses is never empty

        try:
            # One budget for the whole attempt, however many addresses it tries.
            response = await asyncio.wait_for(post_to_first_reachable(), DELIVERY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return False, True, "timed out"
        except httpx.HTTPError as exc:
            return False, True, type(exc).__name__
        status = response.status_code
        if 200 <= status < 300:
            return True, False, ""
        return False, status == 429 or status >= 500, f"HTTP {status}"

    async def drain(self) -> None:
        """Wait for every background delivery (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# --- HTTP endpoints -------------------------------------------------------------------


def install(app: FastAPI, service_ref: Callable[[], WebhookService]) -> None:
    """Add the owner API to app. `service_ref` is looked up per request, so
    tests can swap the service (and the room manager behind it)."""

    def _guard(request: Request, code: str, token: Optional[str]) -> WebhookService:
        allowance = api_limiter.check(client_ip(request))
        if not allowance.allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many webhook requests from this address. Try again later.",
                headers={"Retry-After": str(allowance.retry_after_seconds)},
            )
        service = service_ref()
        try:
            service.authorize(code, token)
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        return service

    @app.post("/rooms/{code}/webhooks", status_code=201)
    async def add_webhook(
        code: str,
        body: WebhookCreate,
        request: Request,
        x_synergo_owner_token: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        service = _guard(request, code, x_synergo_owner_token)
        try:
            hook = await service.add(normalize_room_code(code), body)
        except WebhookError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        return {"webhook": service.public(hook)}

    @app.get("/rooms/{code}/webhooks")
    async def list_webhooks(
        code: str,
        request: Request,
        x_synergo_owner_token: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        service = _guard(request, code, x_synergo_owner_token)
        return {
            "webhooks": service.listing(normalize_room_code(code)),
            "max": MAX_WEBHOOKS_PER_ROOM,
            "kinds": list(WEBHOOK_KINDS),
            "events": list(WEBHOOK_EVENTS),
        }

    @app.delete("/rooms/{code}/webhooks/{hook_id}", status_code=204)
    async def delete_webhook(
        code: str,
        hook_id: str,
        request: Request,
        x_synergo_owner_token: Optional[str] = Header(default=None),
    ) -> Response:
        service = _guard(request, code, x_synergo_owner_token)
        if not service.remove(normalize_room_code(code), hook_id):
            raise HTTPException(status_code=404, detail="No such webhook in this room")
        return Response(status_code=204)
