"""Claude Code hook adapter. Schema per https://code.claude.com/docs/en/hooks (fetched 2026-09-28)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from synergo.events import EventAck, NormalizedEvent

AGENT = "claude-code"

EDIT_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EDIT_MATCHER = "|".join(EDIT_TOOLS)

PRE_TOOL_TIMEOUT = 5
OTHER_TIMEOUT = 10

HOOK_COMMAND = "synergo hook claude-code"
HOOK_MARKER = "synergo hook"


def hook_settings(command: str = HOOK_COMMAND) -> dict[str, list[dict[str, Any]]]:
    """The hooks block synergo wants merged into a Claude Code settings.json.

    UserPromptSubmit has no matcher support, so the entry omits it.
    """
    return {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": command, "timeout": OTHER_TIMEOUT}]}
        ],
        "PreToolUse": [
            {
                "matcher": EDIT_MATCHER,
                "hooks": [{"type": "command", "command": command, "timeout": PRE_TOOL_TIMEOUT}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": EDIT_MATCHER,
                "hooks": [{"type": "command", "command": command, "timeout": OTHER_TIMEOUT}],
            }
        ],
    }


def _file_path(tool_input: dict[str, Any]) -> Optional[str]:
    value = tool_input.get("file_path") or tool_input.get("notebook_path")
    return value if isinstance(value, str) and value else None


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def normalize(payload: dict[str, Any]) -> Optional[NormalizedEvent]:
    """Translate one hook stdin payload, or None if synergo does not care."""
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")

    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or payload.get("user_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return NormalizedEvent(
            kind="intent", agent=AGENT, session_id=session_id, cwd=cwd, prompt=prompt
        )

    if event_name not in ("PreToolUse", "PostToolUse"):
        return None

    tool = payload.get("tool_name")
    if tool not in EDIT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = _file_path(tool_input)
    if file_path is None:
        return None

    if event_name == "PreToolUse":
        return NormalizedEvent(
            kind="pre_edit",
            agent=AGENT,
            session_id=session_id,
            cwd=cwd,
            file_path=file_path,
            old_content=_read(file_path),
            tool=tool,
        )
    return NormalizedEvent(
        kind="post_edit",
        agent=AGENT,
        session_id=session_id,
        cwd=cwd,
        file_path=file_path,
        new_content=_read(file_path),
        tool=tool,
    )


# A block stops the edit; a warn lets it through, so they must not read alike.
CALL_TO_ACTION = {
    "block": "wait, or pick another file",
    "warn": "check with them before you overlap",
}


def conflict_message(
    ack: EventAck, decision: str, fallback_path: Optional[str] = None
) -> str:
    """Human sentence naming who is in this file and what to do about it.

    The relay's reason is authoritative for the who-did-what clause because it
    distinguishes a peer who actually edited the file from one who has only
    declared intent — composing that here would misreport the second case.
    """
    path = ack.file_path or fallback_path or "this file"
    detail = ack.reason or f"a teammate's agent is already working in {path}"
    if len(ack.others) > 1:
        extra = len(ack.others) - 1
        detail += f" (and {extra} other{'s' if extra > 1 else ''})"

    message = f"{detail} — {CALL_TO_ACTION[decision]}"
    if ack.others and ack.others[0].prompt:
        message += f' (their task: "{ack.others[0].prompt}")'
    return message


def render_response(event: NormalizedEvent, ack: EventAck) -> str:
    """Stdout for Claude Code. Empty string means "no output, proceed"."""
    if event.kind != "pre_edit" or ack.decision == "allow":
        return ""

    message = conflict_message(ack, ack.decision, event.file_path)

    if ack.decision == "block":
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Synergo: {message}",
                }
            }
        )

    # warn: no permissionDecision, so Claude Code's normal permission flow still
    # applies — emitting "allow" here would silently auto-approve the edit.
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Synergo warning: {message}",
            },
            "systemMessage": f"Synergo: {message}",
        }
    )
