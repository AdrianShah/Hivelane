"""Google Gemini CLI hook adapter.

Schema per https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
and docs/reference/tools.md in the same repo, fetched 2026-10-04. What the docs
say, and what this module relies on:

- Hooks live in `settings.json` under `hooks`: `~/.gemini/settings.json` (user)
  or `<repo>/.gemini/settings.json` (project), with the same
  `{"hooks": {Event: [{matcher, hooks: [{type: "command", command, timeout}]}]}}`
  shape as Claude Code, except `timeout` is in milliseconds (default 60000).
  For tool events the matcher is a regex on the tool name.
- Events used: BeforeAgent (`prompt`), BeforeTool and AfterTool (`tool_name`,
  `tool_input`). Every payload carries `session_id`, `cwd`, `hook_event_name`,
  `transcript_path`, `timestamp`.
- File edits go through `write_file` (`file_path`, `content`) and `replace`
  (`file_path`, `old_string`, `new_string`). Shell edits are invisible.
- BeforeTool can block: exit 0 with stdout JSON `{"decision": "deny",
  "reason": ..., "systemMessage": ...}` (reason goes to the model,
  systemMessage to the terminal), or exit 2 with the reason on stderr. A
  `systemMessage` alone decides nothing, which is how a warn is rendered.
  Limitation: that message is user-visible only, so unlike Claude Code and
  Codex (`additionalContext`) the Gemini model itself never hears of a warn.
- settings.json may carry comments (Gemini CLI accepts them): synergo reads
  such a file but refuses to rewrite it, printing the entries to add by hand.
- AfterTool with `tool_response.error` set (a failed `replace`) is not an
  edit and posts nothing.

Hence: can_block True, has_intent True, has_post_edit True.
"""

from __future__ import annotations

from synergo import hook_json
from synergo.adapters import base

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

AGENT = "gemini-cli"
HOOK_COMMAND = "synergo hook gemini-cli"

EDIT_TOOLS: tuple[str, ...] = ("write_file", "replace")
EDIT_MATCHER = "^(" + "|".join(EDIT_TOOLS) + ")$"

# Milliseconds, per the Gemini CLI docs.
PRE_TOOL_TIMEOUT_MS = 5000
OTHER_TIMEOUT_MS = 10000


def hook_settings(command: str = HOOK_COMMAND) -> dict[str, list[dict[str, Any]]]:
    """The hooks block synergo merges into `.gemini/settings.json`."""

    def entry(timeout: int) -> dict[str, Any]:
        return {"type": "command", "name": "synergo", "command": command, "timeout": timeout}

    return {
        "BeforeAgent": [{"hooks": [entry(OTHER_TIMEOUT_MS)]}],
        "BeforeTool": [{"matcher": EDIT_MATCHER, "hooks": [entry(PRE_TOOL_TIMEOUT_MS)]}],
        "AfterTool": [{"matcher": EDIT_MATCHER, "hooks": [entry(OTHER_TIMEOUT_MS)]}],
    }


def normalize_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")

    if event_name == "BeforeAgent":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return []
        return [base.make_event(AGENT, "intent", session_id, cwd, prompt=prompt)]

    if event_name not in ("BeforeTool", "AfterTool"):
        return []
    tool = payload.get("tool_name")
    if tool not in EDIT_TOOLS:
        return []
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return []
    path = base.absolute(path, cwd)
    if event_name == "BeforeTool":
        hints = base.string_hints(tool_input) if tool == "replace" else None
        return [base.make_event(AGENT, "pre_edit", session_id, cwd, file_path=path, tool=tool,
                                old_content=base.read_text(path), edit_hints=hints)]
    response = payload.get("tool_response")
    if isinstance(response, dict) and response.get("error"):
        return []  # the tool failed (say, old_string not found): nothing was edited
    return [base.make_event(AGENT, "post_edit", session_id, cwd, file_path=path, tool=tool,
                            new_content=base.read_text(path))]


def render_ack(event: dict[str, Any], ack: Any) -> str:
    if event.get("kind") != "pre_edit":
        return ""
    decision = base.ack_decision(ack)
    if decision == "allow":
        return ""
    message = base.ack_message(ack, decision, event.get("file_path"))
    if decision == "block":
        return hook_json.dumps(
            {"decision": "deny", "reason": f"Synergo: {message}", "systemMessage": f"Synergo: {message}"}
        )
    # warn: no decision at all, so Gemini's own confirmation flow still runs.
    return hook_json.dumps({"systemMessage": f"Synergo warning: {message}"})


class GeminiCliAdapter(base.JsonHooksAdapter):
    id = AGENT
    label = "Gemini CLI"
    executables = ("gemini",)
    config_dirs = (".gemini",)
    capabilities = {"can_block": True, "has_intent": True, "has_post_edit": True}
    config_subpath = (".gemini", "settings.json")

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        return hook_settings(self.hook_command)

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        try:
            return normalize_payload(payload)
        except Exception:  # noqa: BLE001 - fail open
            return []

    def render(self, event: dict[str, Any], ack: Any) -> base.HookOutput:
        try:
            return base.HookOutput(render_ack(event, ack))
        except Exception:  # noqa: BLE001
            if event.get("kind") == "pre_edit" and base.ack_decision(ack) == "block":
                return base.HookOutput(render_ack(event, {"decision": "block", "reason": base.GENERIC_BLOCK}))
            return base.PROCEED

    def render_notice(self, event: dict[str, Any], message: str) -> base.HookOutput:
        if event.get("kind") not in ("intent", "pre_edit"):
            return base.PROCEED
        return base.HookOutput(hook_json.dumps({"systemMessage": message}))


ADAPTER = GeminiCliAdapter()
