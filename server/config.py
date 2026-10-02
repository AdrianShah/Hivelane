"""Relay configuration, read from the environment once at import.

A misconfigured deployment should refuse to boot rather than serve with
permissive CORS, so `load_settings` raises ConfigError in staging/production
when required values are missing.
"""

from __future__ import annotations

import os
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

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_development(self) -> bool:
        return self.env == "development"


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


def _redis_url(env: Mapping[str, str]) -> str | None:
    """Optional shared store for the rate limiters (see limits.py).

    Unset keeps the limiters in process memory, which is right for a single
    relay instance and needs no extra service.
    """
    raw = (env.get("SYNERGO_REDIS_URL") or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith(REDIS_URL_SCHEMES):
        # The value is not echoed: it usually carries a password.
        raise ConfigError(
            f"SYNERGO_REDIS_URL must start with {', '.join(REDIS_URL_SCHEMES)}"
        )
    return raw


def load_settings(env: Mapping[str, str]) -> Settings:
    name = (env.get("SYNERGO_ENV") or "development").strip().lower()
    if name not in VALID_ENVIRONMENTS:
        raise ConfigError(
            f"SYNERGO_ENV must be one of {', '.join(VALID_ENVIRONMENTS)}, got {name!r}"
        )

    origins = _csv(env.get("CORS_ORIGINS"))
    if not origins:
        if name == "development":
            origins = DEV_CORS_ORIGINS
        else:
            raise ConfigError(
                f"CORS_ORIGINS is required when SYNERGO_ENV={name}. Set it to the "
                "comma-separated list of exact dashboard origins allowed to call "
                "this relay, e.g. CORS_ORIGINS=https://app.example.com"
            )

    # Opt-in wildcard for preview deploys (e.g. r"https://.*\.example\.com").
    # Empty by default: a regex that silently matches nothing — or too much —
    # is worse than an explicit origin list.
    regex = (env.get("CORS_ORIGIN_REGEX") or "").strip() or None

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
        room_create_limit=_positive_int(
            env, "ROOM_CREATE_LIMIT", DEFAULT_ROOM_CREATE_LIMIT
        ),
        room_create_window_seconds=_positive_int(
            env, "ROOM_CREATE_WINDOW_SECONDS", DEFAULT_ROOM_CREATE_WINDOW_SECONDS
        ),
        ws_attempt_limit=_positive_int(
            env, "WS_ATTEMPT_LIMIT", DEFAULT_WS_ATTEMPT_LIMIT
        ),
        ws_attempt_window_seconds=_positive_int(
            env, "WS_ATTEMPT_WINDOW_SECONDS", DEFAULT_WS_ATTEMPT_WINDOW_SECONDS
        ),
        rate_limit_max_tracked_ips=_positive_int(
            env, "RATE_LIMIT_MAX_TRACKED_IPS", DEFAULT_RATE_LIMIT_MAX_TRACKED_IPS
        ),
        redis_url=_redis_url(env),
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
