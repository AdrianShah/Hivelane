"""Claude Code hook adapter. Schema per https://code.claude.com/docs/en/hooks (fetched 2026-09-28).

The translation logic works on plain dicts and imports only the standard
library, because the hook runs as a fresh process on every edit and cannot
afford pydantic's import cost (see synergo/hook_fast.py). The pydantic-typed
`normalize` / `render_response` wrappers are the reference API the rest of the
package and the tests use; they delegate to the dict functions so the hot path
and the reference can never drift apart.
"""

from __future__ import annotations

from synergo import hook_json

# Not `from typing import TYPE_CHECKING`: typing itself is a measurable import
# on the hook path. Type checkers treat any name TYPE_CHECKING as True.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Optional

    from synergo.events import EventAck, NormalizedEvent

AGENT = "claude-code"

EDIT_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EDIT_MATCHER = "|".join(EDIT_TOOLS)

PRE_TOOL_TIMEOUT = 5
OTHER_TIMEOUT = 10

HOOK_COMMAND = "synergo hook claude-code"
HOOK_MARKER = "synergo hook"

# Every NormalizedEvent field, so a dict built here serializes to exactly the
# JSON the pydantic model would (None fields included, as model_dump_json does).
EVENT_FIELDS: tuple[str, ...] = (
    "kind",
    "agent",
    "session_id",
    "cwd",
    "prompt",
    "file_path",
    "old_content",
    "new_content",
    "tool",
)

DECISIONS: tuple[str, ...] = ("allow", "warn", "block")


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
    # open() rather than pathlib: pathlib is a measurable slice of the hook's
    # cold start, and this is the only file access on the hot path.
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _event(kind: str, session_id: str, cwd: str, **fields: Any) -> dict[str, Any]:
    event: dict[str, Any] = dict.fromkeys(EVENT_FIELDS)
    event.update(kind=kind, agent=AGENT, session_id=session_id, cwd=cwd, **fields)
    return event


def normalize_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Translate one hook stdin payload into NormalizedEvent-shaped JSON, or
    None if synergo does not care about it."""
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")

    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or payload.get("user_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return _event("intent", session_id, cwd, prompt=prompt)

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
        return _event(
            "pre_edit", session_id, cwd, file_path=file_path, old_content=_read(file_path), tool=tool
        )
    return _event(
        "post_edit", session_id, cwd, file_path=file_path, new_content=_read(file_path), tool=tool
    )


def normalize(payload: dict[str, Any]) -> Optional[NormalizedEvent]:
    """Translate one hook stdin payload, or None if synergo does not care."""
    from synergo.events import NormalizedEvent

    event = normalize_payload(payload)
    return None if event is None else NormalizedEvent(**event)


# A block stops the edit; a warn lets it through, so they must not read alike.
CALL_TO_ACTION = {
    "block": "wait, or pick another file",
    "warn": "check with them before you overlap",
}


def _ack_message(ack: dict[str, Any], decision: str, fallback_path: Optional[str]) -> str:
    """Human sentence naming who is in this file and what to do about it.

    The relay's reason is authoritative for the who-did-what clause because it
    distinguishes a peer who actually edited the file from one who has only
    declared intent — composing that here would misreport the second case.
    """
    others = ack.get("others") or []
    path = ack.get("file_path") or fallback_path or "this file"
    detail = ack.get("reason") or f"a teammate's agent is already working in {path}"
    if len(others) > 1:
        extra = len(others) - 1
        detail += f" (and {extra} other{'s' if extra > 1 else ''})"

    message = f"{detail} — {CALL_TO_ACTION[decision]}"
    first = others[0] if others else None
    if isinstance(first, dict) and first.get("prompt"):
        message += f' (their task: "{first["prompt"]}")'
    return message


def conflict_message(
    ack: EventAck, decision: str, fallback_path: Optional[str] = None
) -> str:
    """Human sentence naming who is in this file and what to do about it."""
    return _ack_message(ack.model_dump(), decision, fallback_path)


def render_ack(event: dict[str, Any], ack: Any) -> str:
    """Stdout for Claude Code from NormalizedEvent- and EventAck-shaped JSON.

    Empty string means "no output, proceed". `ack` is whatever the daemon sent
    back; anything that is not a well-formed warn/block is treated as allow,
    which is the fail-open reading.
    """
    if event.get("kind") != "pre_edit" or not isinstance(ack, dict):
        return ""
    decision = ack.get("decision", "allow")
    if decision not in DECISIONS or decision == "allow":
        return ""
    if not isinstance(ack.get("others", []), list):
        return ""

    message = _ack_message(ack, decision, event.get("file_path"))

    if decision == "block":
        return hook_json.dumps(
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
    return hook_json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Synergo warning: {message}",
            },
            "systemMessage": f"Synergo: {message}",
        }
    )


def render_response(event: NormalizedEvent, ack: EventAck) -> str:
    """Stdout for Claude Code. Empty string means "no output, proceed"."""
    return render_ack(event.model_dump(), ack.model_dump())


# Events whose JSON output Claude Code surfaces to the user via systemMessage
# without it changing what the agent does next.
NOTICE_EVENT_KINDS: tuple[str, ...] = ("intent", "pre_edit")


def render_notice(event: dict[str, Any], message: str) -> str:
    """A user-visible notice that never decides anything.

    Only `systemMessage`: a permissionDecision (or a non-zero exit) on an
    unreachable daemon would turn "Synergo is down" into "your agent is
    stuck", which is the one thing the hook must never do.
    """
    if event.get("kind") not in NOTICE_EVENT_KINDS:
        return ""
    return hook_json.dumps({"systemMessage": message})
