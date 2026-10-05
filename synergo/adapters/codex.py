"""OpenAI Codex CLI hook adapter.

Schema per https://developers.openai.com/codex/hooks (308 → https://learn.chatgpt.com/docs/hooks),
fetched 2026-10-04. What the docs say, and what this module relies on:

- Hooks live in `~/.codex/hooks.json` / `~/.codex/config.toml` (user) or
  `<repo>/.codex/hooks.json` / `<repo>/.codex/config.toml` (project; loaded only
  once the project's `.codex/` layer is trusted). hooks.json has the same
  `{"hooks": {Event: [{matcher, hooks: [{type: "command", command, timeout}]}]}}`
  shape as Claude Code's settings.json, so install writes `.codex/hooks.json`
  (JSON, merged with the shared logic) and never touches config.toml. Codex
  asks the user to review/trust new or changed hooks (`/hooks`) before they run.
- Events used: UserPromptSubmit (`prompt`; matcher ignored), PreToolUse and
  PostToolUse (`tool_name`, `tool_input`; matcher is a regex on tool_name).
  Every payload carries `session_id`, `cwd`, `hook_event_name`.
- File edits go through the `apply_patch` tool; `tool_input.command` holds the
  patch text (`*** Begin Patch` / `*** Add File:` / `*** Update File:` /
  `*** Delete File:` / `*** Move to:`). One patch can touch several files, so
  normalize returns one event per file. Shell (`Bash`) edits are invisible.
  The matcher also accepts `Edit` / `Write` as aliases for apply_patch.
- PreToolUse can block: exit 2 + stderr, or JSON
  `hookSpecificOutput.permissionDecision = "deny"` with
  `permissionDecisionReason`. `permissionDecision: "allow"` exists too (used
  for input rewriting), so a warn must not emit it. `additionalContext`
  (model-visible) and top-level `systemMessage` (UI notice) are supported on
  PreToolUse, PostToolUse and UserPromptSubmit.

Hence: can_block True, has_intent True, has_post_edit True, and the JSON
response contract is the same as Claude Code's.
"""

from __future__ import annotations

import os

from synergo import hook_json
from synergo.adapters import base

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Optional

AGENT = "codex"
HOOK_COMMAND = "synergo hook codex"

PATCH_TOOL = "apply_patch"
EDIT_TOOLS: tuple[str, ...] = (PATCH_TOOL, "Edit", "Write")
EDIT_MATCHER = "^(" + "|".join(EDIT_TOOLS) + ")$"

PRE_TOOL_TIMEOUT = 5
OTHER_TIMEOUT = 10

# Header prefixes in Codex's patch format that name a file.
PATCH_FILE_HEADERS: tuple[str, ...] = (
    "*** Add File:",
    "*** Update File:",
    "*** Delete File:",
    "*** Move to:",
)


def hook_settings(command: str = HOOK_COMMAND) -> dict[str, list[dict[str, Any]]]:
    """The hooks block synergo merges into `.codex/hooks.json`."""
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


def patch_paths(patch: str) -> list[str]:
    """Every file a Codex patch touches, in order, without duplicates."""
    paths: list[str] = []
    for line in patch.splitlines():
        stripped = line.strip()
        for header in PATCH_FILE_HEADERS:
            if stripped.startswith(header):
                path = stripped[len(header) :].strip()
                if path and path not in paths:
                    paths.append(path)
                break
    return paths


NEWLINE = chr(10)


def patch_hints(patch: str) -> dict[str, list[dict[str, str]]]:
    """Per updated file, the old/new text of each change in a Codex patch:
    each run of removed lines (with the lines added in its place), or, for a
    pure insertion, the hunk's context lines."""
    out: dict[str, list[dict[str, str]]] = {}
    current: Optional[list[dict[str, str]]] = None
    context: list[str] = []
    removed: list[str] = []
    added: list[str] = []

    def flush_run() -> None:
        if current is not None and (removed or added):
            old = removed or context[-3:]
            if old and len(current) < base.MAX_HINTS:
                current.append({"old": NEWLINE.join(old), "new": NEWLINE.join(added)})
        removed.clear()
        added.clear()

    for line in patch.splitlines():
        if line.startswith("*** "):
            flush_run()
            context.clear()
            stripped = line.strip()
            current = None
            if stripped.startswith("*** Update File:"):
                path = stripped[len("*** Update File:") :].strip()
                current = out.setdefault(path, []) if path else None
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            flush_run()
            context.clear()
        elif line.startswith("-"):
            if added and not removed:
                flush_run()
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            flush_run()
            context.append(line[1:] if line.startswith(" ") else line)
    flush_run()
    return {path: hints for path, hints in out.items() if hints}


def _edit_hints(tool_input: dict[str, Any], cwd: str) -> dict[str, list[dict[str, str]]]:
    """Absolute path -> edit hints for a Codex Edit or apply_patch call."""
    command = tool_input.get("command")
    patches = [command] if isinstance(command, str) else [c for c in command if isinstance(c, str)] if isinstance(command, list) else []
    hints: dict[str, list[dict[str, str]]] = {}
    for patch in patches:
        for path, found in patch_hints(patch).items():
            hints.setdefault(_absolute(path, cwd), []).extend(found)
    if not hints:
        value = tool_input.get("file_path") or tool_input.get("path")
        found = base.string_hints(tool_input)
        if isinstance(value, str) and value and found:
            hints[_absolute(value, cwd)] = found
    return hints


def _absolute(path: str, cwd: str) -> str:
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _tool_paths(tool_input: dict[str, Any], cwd: str) -> list[str]:
    raw: list[str] = []
    command = tool_input.get("command")
    if isinstance(command, str):
        raw = patch_paths(command)
    elif isinstance(command, list):  # argv form: ["apply_patch", "<patch>"]
        for part in command:
            if isinstance(part, str):
                raw.extend(p for p in patch_paths(part) if p not in raw)
    if not raw:
        for key in ("file_path", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                raw = [value]
                break
    out: list[str] = []
    for path in raw:
        full = _absolute(path, cwd)
        if full not in out:
            out.append(full)
    return out


def normalize_payload(payload: Any) -> list[dict[str, Any]]:
    """Codex hook stdin payload -> NormalizedEvent-shaped dicts ([] = ignore)."""
    if not isinstance(payload, dict):
        return []
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or "")

    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return []
        return [base.make_event(AGENT, "intent", session_id, cwd, prompt=prompt)]

    if event_name not in ("PreToolUse", "PostToolUse"):
        return []
    tool = payload.get("tool_name")
    if tool not in EDIT_TOOLS:
        return []
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []

    events: list[dict[str, Any]] = []
    hints = _edit_hints(tool_input, cwd) if event_name == "PreToolUse" else {}
    for path in _tool_paths(tool_input, cwd):
        if event_name == "PreToolUse":
            fields = {"old_content": base.read_text(path), "edit_hints": hints.get(path)}
            kind = "pre_edit"
        else:
            fields = {"new_content": base.read_text(path)}
            kind = "post_edit"
        events.append(base.make_event(AGENT, kind, session_id, cwd, file_path=path, tool=tool, **fields))
    return events


def render_ack(event: dict[str, Any], ack: Any) -> str:
    """Stdout for Codex; "" means proceed. Same JSON contract as Claude Code."""
    if event.get("kind") != "pre_edit":
        return ""
    decision = base.ack_decision(ack)
    if decision == "allow":
        return ""
    message = base.ack_message(ack, decision, event.get("file_path"))
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
    # warn: no permissionDecision — "allow" would skip Codex's own approval flow.
    return hook_json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"Synergo warning: {message}",
            },
            "systemMessage": f"Synergo: {message}",
        }
    )


NOTICE_EVENT_KINDS: tuple[str, ...] = ("intent", "pre_edit")


class CodexAdapter(base.JsonHooksAdapter):
    id = AGENT
    label = "Codex"
    executables = ("codex",)
    config_dirs = (".codex",)
    capabilities = {"can_block": True, "has_intent": True, "has_post_edit": True}
    config_subpath = (".codex", "hooks.json")

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        return hook_settings(HOOK_COMMAND)

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        try:
            return normalize_payload(payload)
        except Exception:  # noqa: BLE001 - fail open on any malformed payload
            return []

    def render(self, event: dict[str, Any], ack: Any) -> base.HookOutput:
        try:
            return base.HookOutput(render_ack(event, ack))
        except Exception:  # noqa: BLE001
            return base.PROCEED

    def render_notice(self, event: dict[str, Any], message: str) -> base.HookOutput:
        if event.get("kind") not in NOTICE_EVENT_KINDS:
            return base.PROCEED
        return base.HookOutput(hook_json.dumps({"systemMessage": message}))


ADAPTER = CodexAdapter()
