"""Display-name sanitization. The server is authoritative; clients may
pre-validate with the same rules purely for UX."""

from __future__ import annotations

import re

# Best-effort layer only. The charset strip below is the real safety mechanism.
BLOCKLIST = {
    "admin",
    "root",
    "system",
    "moderator",
    "fuck",
    "shit",
    "bitch",
    "cunt",
    "nigg",
    "rape",
}

_UNSAFE = re.compile(r"[^a-z0-9_-]")

MAX_LENGTH = 20


def sanitize_name(raw: str) -> str:
    """Return a safe display name or raise ValueError with a client-facing reason."""
    name = _UNSAFE.sub("", raw.strip().lower())
    if not name or len(name) > MAX_LENGTH:
        raise ValueError("Invalid name")
    if any(bad in name for bad in BLOCKLIST):
        raise ValueError("Name not allowed")
    return name


def is_probably_valid(raw: str) -> bool:
    """Non-raising variant for client-side pre-checks."""
    try:
        sanitize_name(raw)
    except ValueError:
        return False
    return True
