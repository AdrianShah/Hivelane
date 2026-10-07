"""What the relay accepts from clients: display names, and the size of every
field it stores and fans out.

Names: the server is authoritative; clients may pre-validate with the same
rules purely for UX.

Payload caps: a member's message is held in memory (latest activity per
member, recent touches, plans) and copied to every socket in the room, so one
oversized prompt or diff costs memory and bandwidth for everyone. Text over a
cap is truncated with TRUNCATION_MARKER rather than dropped, so the dashboard
still shows that something happened; lists over a cap keep their first items.
The e2e blob is capped by the wire model itself (models.E2E_MAX_CHARS):
ciphertext cannot be truncated meaningfully, so an oversized one fails
validation and the message is dropped. Frames are capped before any of this,
in main.receive_frame.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    try:
        from .models import ActivityMessage, EditCheckRequest, IntentMessage, PlanMessage
    except ImportError:  # Render rootDir=server
        from models import ActivityMessage, EditCheckRequest, IntentMessage, PlanMessage

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


# --- payload caps ---------------------------------------------------------------

# Any single WebSocket frame. Larger frames close the socket (1009).
MAX_FRAME_BYTES = 512 * 1024
# One file's diff in an activity message.
MAX_DIFF_BYTES = 64 * 1024
# A prompt (activity or intent) or a plan summary.
MAX_TEXT_BYTES = 4 * 1024
# Files per activity message or plan.
MAX_FILES = 50
# Symbols per file change, plan or edit check.
MAX_SYMBOLS = 64
# Line ranges per file change or edit check.
MAX_LINE_RANGES = 256

TRUNCATION_MARKER = "…[truncated]"


def truncate_utf8(text: str, limit: int) -> str:
    """text if it fits in `limit` UTF-8 bytes, else a prefix that does once
    TRUNCATION_MARKER is appended. Never splits a multi-byte character."""
    # Cheap exits first: a character is at most 4 bytes.
    if len(text) * 4 <= limit:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    room = max(0, limit - len(TRUNCATION_MARKER.encode("utf-8")))
    return encoded[:room].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _cap_optional(text: str | None, limit: int) -> str | None:
    return None if text is None else truncate_utf8(text, limit)


def cap_activity(message: ActivityMessage) -> None:
    """Trim an activity message in place to the caps above."""
    message.prompt = _cap_optional(message.prompt, MAX_TEXT_BYTES)
    del message.changes[MAX_FILES:]
    for change in message.changes:
        change.diff = truncate_utf8(change.diff, MAX_DIFF_BYTES)
        del change.symbols[MAX_SYMBOLS:]
        del change.line_ranges[MAX_LINE_RANGES:]


def cap_intent(message: IntentMessage) -> None:
    message.prompt = truncate_utf8(message.prompt, MAX_TEXT_BYTES)


def cap_plan(message: PlanMessage) -> None:
    message.summary = truncate_utf8(message.summary, MAX_TEXT_BYTES)
    del message.files[MAX_FILES:]
    del message.symbols[MAX_SYMBOLS:]


def cap_edit_check(message: EditCheckRequest) -> None:
    del message.symbols[MAX_SYMBOLS:]
    del message.line_ranges[MAX_LINE_RANGES:]
