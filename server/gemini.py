"""Gemini helpers for captioning and conflict classification.

All network failures fall back so the WebSocket broadcast path never breaks.
Requires GEMINI_API_KEY in the environment (or a .env loaded by main).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from google import genai
from google.genai import types

from .models import ActivityMessage, ConflictAlert, FileChange, Verdict, placeholder_caption
from .room import FileTouch

log = logging.getLogger(__name__)

# Fast tier for live captions / conflict checks during the demo.
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
CAPTION_TIMEOUT_SECONDS = float(os.getenv("GEMINI_CAPTION_TIMEOUT", "4.0"))
CONFLICT_TIMEOUT_SECONDS = float(os.getenv("GEMINI_CONFLICT_TIMEOUT", "5.0"))
MAX_DIFF_CHARS = 2500

_client: Optional[genai.Client] = None


def _get_client() -> Optional[genai.Client]:
    global _client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


def _truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def _format_changes(changes: list[FileChange]) -> str:
    parts: list[str] = []
    for change in changes:
        parts.append(f"### {change.file_path}\n{_truncate(change.diff)}")
    return "\n\n".join(parts)


async def generate_caption(message: ActivityMessage) -> str:
    """One short third-person sentence describing the batch. Falls back on any error."""
    fallback = placeholder_caption(message)
    client = _get_client()
    if client is None:
        log.debug("GEMINI_API_KEY missing; using placeholder caption")
        return fallback

    prompt_line = message.prompt or "(no prompt captured)"
    contents = (
        "Write ONE short sentence in third person summarizing what this coding agent's "
        f"batch of edits is doing for user '{message.user_id}'. "
        "Mention the intent if a prompt is present. No quotes, no markdown, under 25 words.\n\n"
        f"User prompt: {prompt_line}\n\n"
        f"File changes:\n{_format_changes(message.changes)}"
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=64,
                ),
            ),
            timeout=CAPTION_TIMEOUT_SECONDS,
        )
        text = (response.text or "").strip().strip('"')
        return text or fallback
    except Exception as exc:
        log.warning("Caption generation failed; using placeholder: %s", exc)
        return fallback


_CONFLICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["overlapping", "related", "unrelated"],
        },
        "explanation": {"type": "string"},
    },
    "required": ["verdict", "explanation"],
}


async def classify_conflict(
    file_path: str,
    current_user: str,
    current_diff: str,
    current_prompt: Optional[str],
    other: FileTouch,
) -> Optional[ConflictAlert]:
    """Ask Gemini whether two users' edits to the same file conflict.

    Returns a ConflictAlert for overlapping/related, or None for unrelated /
    missing key / any failure (so we never emit false alarms on errors).
    """
    client = _get_client()
    if client is None:
        return None

    contents = (
        "Two teammates' AI coding agents edited the SAME file recently. "
        "Classify the relationship of their edits as exactly one of: "
        "overlapping (same lines/logic, likely merge conflict), "
        "related (same area/feature but not a direct clash), "
        "unrelated (coincidental same file, independent changes). "
        "Respond with JSON only.\n\n"
        f"File: {file_path}\n\n"
        f"User A ({current_user}) prompt: {current_prompt or '(none)'}\n"
        f"User A diff:\n{_truncate(current_diff)}\n\n"
        f"User B ({other.user_id}) prompt: {other.prompt or '(none)'}\n"
        f"User B diff:\n{_truncate(other.diff)}"
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=120,
                    response_mime_type="application/json",
                    response_json_schema=_CONFLICT_SCHEMA,
                ),
            ),
            timeout=CONFLICT_TIMEOUT_SECONDS,
        )
        raw = (response.text or "").strip()
        data = json.loads(raw)
        verdict: Verdict = data.get("verdict", "unrelated")
        if verdict not in ("overlapping", "related", "unrelated"):
            return None
        if verdict == "unrelated":
            log.info("Conflict check %s vs %s on %s: unrelated", current_user, other.user_id, file_path)
            return None
        explanation = str(data.get("explanation") or "Potential conflict detected.").strip()
        return ConflictAlert(
            users=[current_user, other.user_id],
            file_path=file_path,
            verdict=verdict,
            explanation=explanation,
        )
    except Exception as exc:
        log.warning(
            "Conflict classification failed for %s (%s vs %s): %s",
            file_path,
            current_user,
            other.user_id,
            exc,
        )
        return None
