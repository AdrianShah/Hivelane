"""Relay configuration, read from the environment once at import.

A misconfigured deployment should refuse to boot rather than serve with
permissive CORS, so `load_settings` raises ConfigError in staging/production
when required values are missing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

VALID_ENVIRONMENTS = ("development", "staging", "production")

DEV_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

DEFAULT_ROOM_CREATE_LIMIT = 10
DEFAULT_ROOM_CREATE_WINDOW_SECONDS = 3600
DEFAULT_WS_ATTEMPT_LIMIT = 30
DEFAULT_WS_ATTEMPT_WINDOW_SECONDS = 60
DEFAULT_RATE_LIMIT_MAX_TRACKED_IPS = 4096
# Inbound WebSocket messages per minute per room, summed over every socket in
# it (a token bucket, so short bursts up to this many are fine).
DEFAULT_ROOM_MESSAGE_LIMIT = 600
# Proxies between the internet and the relay that each append to
# X-Forwarded-For (Render, a single nginx/Caddy/Traefik: 1).
DEFAULT_TRUSTED_PROXY_HOPS = 1

# --- capacity limits -----------------------------------------------------------
# Every limit below (and the rate limits above) reads 0 as "unlimited".
# docs/self-hosting.md#capacity-limits says which ones a public relay should
# set, sized from the load test in docs/performance.md.
#
# Live rooms per process and named members per room. Unlimited by default: a
# relay you run for your own team should not turn a teammate away. A public
# relay sets both (and keeps ROOM_CREATE_LIMIT) to bound memory.
DEFAULT_MAX_ROOMS = 0
DEFAULT_MAX_MEMBERS = 0
# Dashboards (spectator sockets) per room. Bounded by default, unlike
# members: each one receives every broadcast, so an unbounded count lets one
# code holder hold enough sockets to starve every room on the relay.
DEFAULT_MAX_SPECTATORS = 64
# Per-room bookkeeping, each bounded so one room cannot grow without limit.
DEFAULT_MAX_PENDING_JOINS = 10  # joiners waiting for an owner's approval
DEFAULT_MAX_TRACKED_DECISIONS = 1000  # warn/block decisions kept for votes (LRU)
DEFAULT_MAX_KNOWN_PATHS = 512  # paths remembered for suggestions (LRU)
# One file's wait line holds each member at most once, so with 0 it is bounded
# by the room's membership.
DEFAULT_FILE_QUEUE_MAX_LENGTH = 0
DEFAULT_FILE_QUEUE_MAX_FILES = 256  # files with a wait line at once, per room
# Opt-in replay history (server/history.py): per room, and process-wide.
# The byte caps count compact JSON; Python objects take roughly 3-8x that in
# resident memory, so the process-wide default keeps a 512 MB VM safe.
DEFAULT_HISTORY_MAX_EVENTS = 5000
DEFAULT_HISTORY_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_HISTORY_TOTAL_MAX_BYTES = 32 * 1024 * 1024
# Owner webhooks (server/webhooks.py). Webhooks per room is at least 1: each
# one is an outbound request per matching event.
DEFAULT_MAX_WEBHOOKS_PER_ROOM = 3
DEFAULT_WEBHOOK_MAX_IN_FLIGHT = 100  # deliveries running at once, process-wide
DEFAULT_WEBHOOK_MAX_TRACKED_ROOMS = 1024  # rooms with a notice budget (LRU)

# --- per-message safety caps (server/sanitize.py) ------------------------------
# These bound what one message can make the relay hold and fan out to every
# socket in the room, so they keep their defaults unless you have a reason.
DEFAULT_MAX_FRAME_BYTES = 512 * 1024  # any inbound frame; larger closes with 1009
DEFAULT_MAX_DIFF_BYTES = 64 * 1024  # one file's diff in an activity message
DEFAULT_MAX_TEXT_BYTES = 4 * 1024  # a prompt or a plan summary
DEFAULT_MAX_FILES_PER_MESSAGE = 50  # files per activity message or plan
DEFAULT_MAX_SYMBOLS_PER_MESSAGE = 64  # symbols per change, plan or edit check
DEFAULT_MAX_LINE_RANGES_PER_MESSAGE = 256  # line ranges per change or edit check
DEFAULT_MAX_PATH_CHARS = 1024  # longest path queued or remembered

# Origins no real dashboard has. A CORS_ORIGIN_REGEX that matches any of them
# matches everything, which production refuses (see _cors_wildcard_problem).
_CORS_PROBE_ORIGINS = (
    "https://cors-wildcard-probe.invalid",
    "http://cors-wildcard-probe.invalid",
    "null",
)

# Per-message caps that production refuses to switch off (0 = unlimited):
# without them one frame can make the relay buffer and fan out anything.
NONZERO_IN_PRODUCTION = ("max_frame_bytes", "max_diff_bytes", "max_text_bytes")

# Schemes redis-py's from_url understands. Checked here so a typo fails the boot
# instead of quietly leaving every instance on its own per-process budget.
REDIS_URL_SCHEMES = ("redis://", "rediss://", "unix://")


class ConfigError(RuntimeError):
    """Raised at import when the environment cannot serve traffic safely."""


@dataclass(frozen=True, slots=True)
class Settings:
    env: str
    cors_origins: tuple[str, ...]
    cors_origin_regex: str | None
    trust_proxy_header: bool
    room_create_limit: int
    room_create_window_seconds: int
    ws_attempt_limit: int
    ws_attempt_window_seconds: int
    rate_limit_max_tracked_ips: int
    redis_url: str | None
    trusted_proxy_hops: int = DEFAULT_TRUSTED_PROXY_HOPS
    trust_cloudflare: bool = False
    behind_fly: bool = False
    room_message_limit: int = DEFAULT_ROOM_MESSAGE_LIMIT
    # Capacity limits; 0 = unlimited (see the DEFAULT_* constants above).
    max_rooms: int = DEFAULT_MAX_ROOMS
    max_members: int = DEFAULT_MAX_MEMBERS
    max_spectators: int = DEFAULT_MAX_SPECTATORS
    max_pending_joins: int = DEFAULT_MAX_PENDING_JOINS
    max_tracked_decisions: int = DEFAULT_MAX_TRACKED_DECISIONS
    max_known_paths: int = DEFAULT_MAX_KNOWN_PATHS
    file_queue_max_length: int = DEFAULT_FILE_QUEUE_MAX_LENGTH
    file_queue_max_files: int = DEFAULT_FILE_QUEUE_MAX_FILES
    history_max_events: int = DEFAULT_HISTORY_MAX_EVENTS
    history_max_bytes: int = DEFAULT_HISTORY_MAX_BYTES
    history_total_max_bytes: int = DEFAULT_HISTORY_TOTAL_MAX_BYTES
    max_webhooks_per_room: int = DEFAULT_MAX_WEBHOOKS_PER_ROOM
    webhook_max_in_flight: int = DEFAULT_WEBHOOK_MAX_IN_FLIGHT
    webhook_max_tracked_rooms: int = DEFAULT_WEBHOOK_MAX_TRACKED_ROOMS
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES
    max_files_per_message: int = DEFAULT_MAX_FILES_PER_MESSAGE
    max_symbols_per_message: int = DEFAULT_MAX_SYMBOLS_PER_MESSAGE
    max_line_ranges_per_message: int = DEFAULT_MAX_LINE_RANGES_PER_MESSAGE
    max_path_chars: int = DEFAULT_MAX_PATH_CHARS
    # Webhooks may target private, loopback and link-local addresses (and
    # http://localhost). Off unless HIVELANE_WEBHOOKS_ALLOW_PRIVATE is set, in
    # every environment; production refuses it.
    webhooks_allow_private: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_development(self) -> bool:
        return self.env == "development"

    @property
    def hsts_enabled(self) -> bool:
        """Strict-Transport-Security only where TLS is certain: a dev relay on
        http://localhost would otherwise pin the browser to https for it."""
        return self.env in ("staging", "production")


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, got {value}")
    return value


def _limit(env: Mapping[str, str], name: str, default: int) -> int:
    """A capacity or rate limit: a whole number, 0 meaning unlimited."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (0 = unlimited), got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} must be 0 (unlimited) or more, got {value}")
    return value


# (Settings field, environment variable, default) for every capacity limit
# that reads 0 as unlimited. Webhooks per room is not here: it is at least 1.
LIMITS: tuple[tuple[str, str, int], ...] = (
    ("max_rooms", "HIVELANE_MAX_ROOMS", DEFAULT_MAX_ROOMS),
    ("max_members", "HIVELANE_MAX_MEMBERS", DEFAULT_MAX_MEMBERS),
    ("max_spectators", "HIVELANE_MAX_SPECTATORS", DEFAULT_MAX_SPECTATORS),
    ("max_pending_joins", "HIVELANE_MAX_PENDING_JOINS", DEFAULT_MAX_PENDING_JOINS),
    ("max_tracked_decisions", "HIVELANE_MAX_TRACKED_DECISIONS", DEFAULT_MAX_TRACKED_DECISIONS),
    ("max_known_paths", "HIVELANE_MAX_KNOWN_PATHS", DEFAULT_MAX_KNOWN_PATHS),
    ("file_queue_max_length", "HIVELANE_FILE_QUEUE_MAX_LENGTH", DEFAULT_FILE_QUEUE_MAX_LENGTH),
    ("file_queue_max_files", "HIVELANE_FILE_QUEUE_MAX_FILES", DEFAULT_FILE_QUEUE_MAX_FILES),
    ("history_max_events", "HIVELANE_HISTORY_MAX_EVENTS", DEFAULT_HISTORY_MAX_EVENTS),
    ("history_max_bytes", "HIVELANE_HISTORY_MAX_BYTES", DEFAULT_HISTORY_MAX_BYTES),
    (
        "history_total_max_bytes",
        "HIVELANE_HISTORY_TOTAL_MAX_BYTES",
        DEFAULT_HISTORY_TOTAL_MAX_BYTES,
    ),
    ("webhook_max_in_flight", "HIVELANE_WEBHOOK_MAX_IN_FLIGHT", DEFAULT_WEBHOOK_MAX_IN_FLIGHT),
    (
        "webhook_max_tracked_rooms",
        "HIVELANE_WEBHOOK_MAX_TRACKED_ROOMS",
        DEFAULT_WEBHOOK_MAX_TRACKED_ROOMS,
    ),
    ("max_frame_bytes", "HIVELANE_MAX_FRAME_BYTES", DEFAULT_MAX_FRAME_BYTES),
    ("max_diff_bytes", "HIVELANE_MAX_DIFF_BYTES", DEFAULT_MAX_DIFF_BYTES),
    ("max_text_bytes", "HIVELANE_MAX_TEXT_BYTES", DEFAULT_MAX_TEXT_BYTES),
    ("max_files_per_message", "HIVELANE_MAX_FILES_PER_MESSAGE", DEFAULT_MAX_FILES_PER_MESSAGE),
    (
        "max_symbols_per_message",
        "HIVELANE_MAX_SYMBOLS_PER_MESSAGE",
        DEFAULT_MAX_SYMBOLS_PER_MESSAGE,
    ),
    (
        "max_line_ranges_per_message",
        "HIVELANE_MAX_LINE_RANGES_PER_MESSAGE",
        DEFAULT_MAX_LINE_RANGES_PER_MESSAGE,
    ),
    ("max_path_chars", "HIVELANE_MAX_PATH_CHARS", DEFAULT_MAX_PATH_CHARS),
)


def _redis_url(env: Mapping[str, str]) -> str | None:
    """Optional shared store for the rate limiters (see limits.py).

    Unset keeps the limiters in process memory, which is right for a single
    relay instance and needs no extra service.
    """
    raw = (env.get("HIVELANE_REDIS_URL") or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith(REDIS_URL_SCHEMES):
        # The value is not echoed: it usually carries a password.
        raise ConfigError(
            f"HIVELANE_REDIS_URL must start with {', '.join(REDIS_URL_SCHEMES)}"
        )
    return raw


# A quantifier (*, +, {n,m}) in an origin regex. What follows it must be a
# literal dot, or the pattern also matches look-alike registrable domains:
# r"https://.*example\.com" accepts https://evilexample.com.
_QUANTIFIER = re.compile(r"(?<!\\)(?:[*+]|\{\d+(?:,\d*)?\})\??")
# What a quantified digit run looks like (a port or a PR number): harmless.
_DIGITS_BEFORE = (r"\d", "[0-9]")


def _unanchored_wildcard(regex: str) -> bool:
    """True when a wildcard in `regex` is not followed by a literal dot, so
    a host label can run straight into the domain the author meant."""
    for match in _QUANTIFIER.finditer(regex):
        before = regex[: match.start()]
        if before.endswith(_DIGITS_BEFORE):
            continue
        rest = regex[match.end() :].lstrip(")")
        if rest.startswith(r"\."):
            continue
        # Anything else, a trailing wildcard included (https://app-.*), lets
        # the host run on into a domain somebody else can register.
        return True
    return False


def _cors_wildcard_problem(
    origins: tuple[str, ...], regex: str | None
) -> str | None:
    """Why this CORS policy amounts to "any origin", or None if it does not.

    Probed rather than pattern-matched against ".*": "https?://.*",
    "^.+$" and friends are all wildcards, and the only reliable test is
    whether they accept an origin nobody listed.
    """
    if "*" in origins:
        return "CORS_ORIGINS contains '*'"
    if regex is None:
        return None
    for probe in _CORS_PROBE_ORIGINS:
        if re.fullmatch(regex, probe):
            return f"CORS_ORIGIN_REGEX {regex!r} matches any origin (e.g. {probe!r})"
    if _unanchored_wildcard(regex):
        return (
            f"CORS_ORIGIN_REGEX {regex!r} has a wildcard not followed by a literal "
            r"dot, so it also matches look-alike domains (e.g. https://.*example\.com "
            "accepts https://evilexample.com)"
        )
    return None


def load_settings(env: Mapping[str, str]) -> Settings:
    name = (env.get("HIVELANE_ENV") or "development").strip().lower()
    if name not in VALID_ENVIRONMENTS:
        raise ConfigError(
            f"HIVELANE_ENV must be one of {', '.join(VALID_ENVIRONMENTS)}, got {name!r}"
        )

    origins = _csv(env.get("CORS_ORIGINS"))
    if not origins:
        if name == "development":
            origins = DEV_CORS_ORIGINS
        else:
            raise ConfigError(
                f"CORS_ORIGINS is required when HIVELANE_ENV={name}. Set it to the "
                "comma-separated list of exact dashboard origins allowed to call "
                "this relay, e.g. CORS_ORIGINS=https://app.example.com"
            )

    # Opt-in wildcard for preview deploys (e.g. r"https://.*\.example\.com").
    # Empty by default: a regex that silently matches nothing, or too much,
    # is worse than an explicit origin list.
    regex = (env.get("CORS_ORIGIN_REGEX") or "").strip() or None
    if regex is not None:
        try:
            re.compile(regex)
        except re.error as exc:
            raise ConfigError(f"CORS_ORIGIN_REGEX is not a valid regex: {exc}") from exc
    if name == "production":
        problem = _cors_wildcard_problem(origins, regex)
        if problem is not None:
            raise ConfigError(
                f"{problem}. A production relay must name the dashboard origins it "
                "serves; a wildcard lets any web page drive it from a visitor's "
                "browser. List exact origins in CORS_ORIGINS, or scope the regex "
                r"to your own domain (e.g. https://.*\.example\.com)."
            )

    allow_private = _bool(env.get("HIVELANE_WEBHOOKS_ALLOW_PRIVATE"), default=False)
    if allow_private and name == "production":
        raise ConfigError(
            "HIVELANE_WEBHOOKS_ALLOW_PRIVATE is for testing a local webhook receiver; "
            "a production relay must not post to private or loopback addresses"
        )
    limits = {field: _limit(env, var, default) for field, var, default in LIMITS}
    if name == "production":
        for field in NONZERO_IN_PRODUCTION:
            if limits[field] == 0:
                var = next(v for f, v, _ in LIMITS if f == field)
                raise ConfigError(
                    f"{var}=0 (unlimited) is refused in production: it lets one "
                    "message make the relay buffer and fan out anything"
                )

    return Settings(
        env=name,
        cors_origins=origins,
        cors_origin_regex=regex,
        # Read the client IP from X-Forwarded-For. Behind a proxy (Render,
        # Cloudflare) the socket peer is the proxy, so leaving this off puts
        # every user in one rate-limit bucket. Turning it on when NOT behind a
        # proxy lets any client set the header and bypass rate limiting
        # entirely, so it stays off until a trusted proxy is known to be there.
        trust_proxy_header=_bool(env.get("TRUST_PROXY_HEADER"), default=False),
        # Rate limits: 0 switches one off (a private relay, a load test).
        room_create_limit=_limit(env, "ROOM_CREATE_LIMIT", DEFAULT_ROOM_CREATE_LIMIT),
        room_create_window_seconds=_positive_int(
            env, "ROOM_CREATE_WINDOW_SECONDS", DEFAULT_ROOM_CREATE_WINDOW_SECONDS
        ),
        ws_attempt_limit=_limit(env, "WS_ATTEMPT_LIMIT", DEFAULT_WS_ATTEMPT_LIMIT),
        ws_attempt_window_seconds=_positive_int(
            env, "WS_ATTEMPT_WINDOW_SECONDS", DEFAULT_WS_ATTEMPT_WINDOW_SECONDS
        ),
        rate_limit_max_tracked_ips=_limit(
            env, "RATE_LIMIT_MAX_TRACKED_IPS", DEFAULT_RATE_LIMIT_MAX_TRACKED_IPS
        ),
        redis_url=_redis_url(env),
        # How many X-Forwarded-For entries, counted from the right, were added
        # by proxies you run or rent. Only read with TRUST_PROXY_HEADER on.
        trusted_proxy_hops=_positive_int(
            env, "TRUSTED_PROXY_HOPS", DEFAULT_TRUSTED_PROXY_HOPS
        ),
        # Read CF-Connecting-IP. Only safe when the origin accepts traffic from
        # Cloudflare alone; otherwise anyone can send the header directly.
        trust_cloudflare=_bool(env.get("TRUST_CLOUDFLARE"), default=False),
        # Fly.io sets FLY_APP_NAME on every machine, and its proxy overwrites
        # Fly-Client-IP on every request, so that header cannot be forged.
        behind_fly=bool((env.get("FLY_APP_NAME") or "").strip()),
        room_message_limit=_limit(env, "ROOM_MESSAGE_LIMIT", DEFAULT_ROOM_MESSAGE_LIMIT),
        max_webhooks_per_room=_positive_int(
            env, "HIVELANE_MAX_WEBHOOKS_PER_ROOM", DEFAULT_MAX_WEBHOOKS_PER_ROOM
        ),
        webhooks_allow_private=allow_private,
        **limits,
    )


SETTINGS = load_settings(os.environ)

ENV: str = SETTINGS.env
IS_PRODUCTION: bool = SETTINGS.is_production
IS_DEVELOPMENT: bool = SETTINGS.is_development
CORS_ORIGINS: tuple[str, ...] = SETTINGS.cors_origins
CORS_ORIGIN_REGEX: str | None = SETTINGS.cors_origin_regex
TRUST_PROXY_HEADER: bool = SETTINGS.trust_proxy_header
ROOM_CREATE_LIMIT: int = SETTINGS.room_create_limit
ROOM_CREATE_WINDOW_SECONDS: int = SETTINGS.room_create_window_seconds
WS_ATTEMPT_LIMIT: int = SETTINGS.ws_attempt_limit
WS_ATTEMPT_WINDOW_SECONDS: int = SETTINGS.ws_attempt_window_seconds
RATE_LIMIT_MAX_TRACKED_IPS: int = SETTINGS.rate_limit_max_tracked_ips
REDIS_URL: str | None = SETTINGS.redis_url
TRUSTED_PROXY_HOPS: int = SETTINGS.trusted_proxy_hops
TRUST_CLOUDFLARE: bool = SETTINGS.trust_cloudflare
BEHIND_FLY: bool = SETTINGS.behind_fly
ROOM_MESSAGE_LIMIT: int = SETTINGS.room_message_limit
HSTS_ENABLED: bool = SETTINGS.hsts_enabled
# Capacity limits (0 = unlimited). Each module copies the ones it enforces
# into a constant of its own at import, so a test can patch that constant.
MAX_ROOMS: int = SETTINGS.max_rooms
MAX_MEMBERS: int = SETTINGS.max_members
MAX_SPECTATORS: int = SETTINGS.max_spectators
MAX_PENDING_JOINS: int = SETTINGS.max_pending_joins
MAX_TRACKED_DECISIONS: int = SETTINGS.max_tracked_decisions
MAX_KNOWN_PATHS: int = SETTINGS.max_known_paths
FILE_QUEUE_MAX_LENGTH: int = SETTINGS.file_queue_max_length
FILE_QUEUE_MAX_FILES: int = SETTINGS.file_queue_max_files
HISTORY_MAX_EVENTS: int = SETTINGS.history_max_events
HISTORY_MAX_BYTES: int = SETTINGS.history_max_bytes
HISTORY_TOTAL_MAX_BYTES: int = SETTINGS.history_total_max_bytes
MAX_WEBHOOKS_PER_ROOM: int = SETTINGS.max_webhooks_per_room
WEBHOOK_MAX_IN_FLIGHT: int = SETTINGS.webhook_max_in_flight
WEBHOOK_MAX_TRACKED_ROOMS: int = SETTINGS.webhook_max_tracked_rooms
MAX_FRAME_BYTES: int = SETTINGS.max_frame_bytes
MAX_DIFF_BYTES: int = SETTINGS.max_diff_bytes
MAX_TEXT_BYTES: int = SETTINGS.max_text_bytes
MAX_FILES_PER_MESSAGE: int = SETTINGS.max_files_per_message
MAX_SYMBOLS_PER_MESSAGE: int = SETTINGS.max_symbols_per_message
MAX_LINE_RANGES_PER_MESSAGE: int = SETTINGS.max_line_ranges_per_message
MAX_PATH_CHARS: int = SETTINGS.max_path_chars
WEBHOOKS_ALLOW_PRIVATE: bool = SETTINGS.webhooks_allow_private


def limits_summary(settings: Settings = SETTINGS) -> dict[str, int]:
    """Every capacity and rate limit in force (0 = unlimited), for /readyz."""
    summary = {field: getattr(settings, field) for field, _, _ in LIMITS}
    summary.update(
        max_webhooks_per_room=settings.max_webhooks_per_room,
        room_create_limit=settings.room_create_limit,
        ws_attempt_limit=settings.ws_attempt_limit,
        room_message_limit=settings.room_message_limit,
        rate_limit_max_tracked_ips=settings.rate_limit_max_tracked_ips,
    )
    return summary
