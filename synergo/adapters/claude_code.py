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
from synergo.adapters import base

# Not `from typing import TYPE_CHECKING`: typing itself is a measurable import
# on the hook path. Type checkers treat any name TYPE_CHECKING as True.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Optional

    from synergo.events import EventAck, NormalizedEvent

AGENT = "claude-code"

EDIT_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EDIT_MATCHER = "|".join(EDIT_TOOLS)
# PostToolUse for these yields a `plan` event: the agent's todo list / plan.
PLAN_TOOLS: tuple[str, ...] = ("TodoWrite", "ExitPlanMode")
POST_MATCHER = "|".join(EDIT_TOOLS + PLAN_TOOLS)

PRE_TOOL_TIMEOUT = 5
OTHER_TIMEOUT = 10

HOOK_COMMAND = "synergo hook claude-code"
HOOK_MARKER = base.HOOK_MARKER

EVENT_FIELDS = base.EVENT_FIELDS
DECISIONS = base.DECISIONS


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
                "matcher": POST_MATCHER,
                "hooks": [{"type": "command", "command": command, "timeout": OTHER_TIMEOUT}],
            }
        ],
    }


def _file_path(tool_input: dict[str, Any]) -> Optional[str]:
    value = tool_input.get("file_path") or tool_input.get("notebook_path")
    return value if isinstance(value, str) and value else None


_read = base.read_text

def _edit_hints(tool: Any, tool_input: dict[str, Any]) -> Optional[list[dict[str, str]]]:
    """old/new strings of an Edit / MultiEdit, so the daemon can tell which
    lines (and so which functions) the edit is about to touch."""
    if tool == "Edit":
        return base.string_hints(tool_input)
    if tool == "MultiEdit":
        return base.string_hints(tool_input.get("edits"))
    return None


def _plan_text(tool: Any, tool_input: dict[str, Any]) -> str:
    if tool == "ExitPlanMode":
        plan = tool_input.get("plan")
        return plan if isinstance(plan, str) else ""
    todos = tool_input.get("todos")
    if not isinstance(todos, list):
        return ""
    lines: list[str] = []
    for todo in todos:
        if isinstance(todo, dict) and todo.get("status") != "completed":
            content = todo.get("content")
            if isinstance(content, str) and content.strip():
                lines.append("- " + content.strip())
    return "\n".join(lines)


def _event(kind: str, session_id: str, cwd: str, **fields: Any) -> dict[str, Any]:
    return base.make_event(AGENT, kind, session_id, cwd, **fields)


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
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool in PLAN_TOOLS:
        if event_name != "PostToolUse":
            return None
        return base.plan_event(AGENT, session_id, cwd, _plan_text(tool, tool_input), tool=tool)
    if tool not in EDIT_TOOLS:
        return None
    file_path = _file_path(tool_input)
    if file_path is None:
        return None

    if event_name == "PreToolUse":
        return _event(
            "pre_edit", session_id, cwd, file_path=file_path, old_content=_read(file_path), tool=tool,
            edit_hints=_edit_hints(tool, tool_input),
        )
    return _event(
        "post_edit", session_id, cwd, file_path=file_path, new_content=_read(file_path), tool=tool
    )


def normalize(payload: dict[str, Any]) -> Optional[NormalizedEvent]:
    """Translate one hook stdin payload, or None if synergo does not care."""
    from synergo.events import NormalizedEvent

    event = normalize_payload(payload)
    return None if event is None else NormalizedEvent(**event)


CALL_TO_ACTION = base.CALL_TO_ACTION
_ack_message = base.ack_message


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
    if event.get("kind") == "plan":
        return render_plan_ack(ack)
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


def render_plan_ack(ack: Any) -> str:
    """A plan's overlaps as context for the agent; never a permission decision."""
    if not isinstance(ack, dict) or ack_decision(ack) == "allow":
        return ""
    reason = ack.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ""
    return hook_json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"Synergo heads-up: {reason}",
            },
            "systemMessage": f"Synergo: {reason}",
        }
    )


ack_decision = base.ack_decision


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


# --- the adapter contract (synergo.adapters.base) ------------------------------


class ClaudeCodeAdapter(base.JsonHooksAdapter):
    id = AGENT
    label = "Claude Code"
    executables = ("claude",)
    config_dirs = (".claude",)
    capabilities = {"can_block": True, "has_intent": True, "has_post_edit": True}
    config_subpath = (".claude", "settings.json")

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        return hook_settings(HOOK_COMMAND)

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        try:
            event = normalize_payload(payload)
        except Exception:  # noqa: BLE001 - fail open on any malformed payload
            return []
        return [] if event is None else [event]

    def render(self, event: dict[str, Any], ack: Any) -> base.HookOutput:
        try:
            return base.HookOutput(render_ack(event, ack))
        except Exception:  # noqa: BLE001
            return base.PROCEED

    def render_notice(self, event: dict[str, Any], message: str) -> base.HookOutput:
        return base.HookOutput(render_notice(event, message))


ADAPTER = ClaudeCodeAdapter()
