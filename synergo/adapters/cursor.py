"""Cursor (agent / Cmd+K / cursor-agent CLI) hook adapter.

Schema per https://cursor.com/docs/agent/hooks, fetched 2026-10-04. What the
docs say, and what this module relies on:

- Hooks live in `<repo>/.cursor/hooks.json` (project; commands run from the
  project root) or `~/.cursor/hooks.json` (user). The file is
  `{"version": 1, "hooks": {eventName: [{"command", "type"?, "timeout"?,
  "matcher"?, "failClosed"?}]}}` — a flat list of entries per event, NOT
  Claude Code's `[{matcher, hooks: [...]}]` groups, so install uses its own
  small merge here (same marker / never-overwrite rules as base).
- Every payload carries `conversation_id`, `generation_id`, `hook_event_name`,
  `workspace_roots`, ...; there is no `session_id`, so `conversation_id` is
  the session id. `preToolUse` additionally carries `cwd`.
- Events used:
  * `beforeSubmitPrompt` (`prompt`) -> intent. It can block the prompt via
    `{"continue": false}`; synergo always answers `{"continue": true}`.
  * `preToolUse` (`tool_name`, `tool_input`; `matcher` is a regex on the tool
    name, documented names include Shell/Read/Write/Grep/Delete/Task/MCP:*)
    -> pre_edit for `Write` / `Delete`. Output `{"permission": "allow"|"deny",
    "user_message", "agent_message"}`; the docs note `allow` is "accepted by
    the schema but not enforced for preToolUse today", so it never skips
    Cursor's own approval — a warn is an allow carrying messages. The docs do
    not show `tool_input` for Write, so the path is read defensively from
    `file_path` / `path` / `target_file`.
  * `afterFileEdit` (`file_path`, `edits: [{old_string, new_string}]`,
    observational) -> post_edit; the pre-edit text is rebuilt by undoing the
    edits when every `new_string` is found.
- Exit 0 + JSON is the contract; exit 2 means deny; other codes fail open.
  "Invalid JSON or a response that doesn't match the hook's schema blocks the
  action" for permission hooks, so an empty stdout is not a safe allow:
  `passthrough()` gives the explicit allow/continue response that hook_fast
  prints whenever nothing else was rendered.

Can Cursor block an edit before it happens? Yes, via `preToolUse` on the
agent's Write/Delete tools (there is no dedicated beforeFileEdit event, and
`afterFileEdit` is observational only). Tab completions and the user's own
typing are not covered by any blocking hook; the VS Code extension
(editors/vscode) reports those buffer edits.
"""

from __future__ import annotations

import os

from synergo import hook_json
from synergo.adapters import base

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Optional

AGENT = "cursor"
HOOK_COMMAND = "synergo hook cursor"

EDIT_TOOLS: tuple[str, ...] = ("Write", "Delete")
EDIT_MATCHER = "^(" + "|".join(EDIT_TOOLS) + ")$"
PATH_KEYS: tuple[str, ...] = ("file_path", "path", "target_file")

PRE_TOOL_TIMEOUT = 5
OTHER_TIMEOUT = 10

ALLOW = '{"permission": "allow"}'
CONTINUE = '{"continue": true}'


def hook_settings(command: str = HOOK_COMMAND) -> dict[str, list[dict[str, Any]]]:
    """The entries synergo merges into `.cursor/hooks.json` under `hooks`."""
    return {
        "beforeSubmitPrompt": [{"command": command, "type": "command", "timeout": OTHER_TIMEOUT}],
        "preToolUse": [
            {"command": command, "type": "command", "timeout": PRE_TOOL_TIMEOUT, "matcher": EDIT_MATCHER}
        ],
        "afterFileEdit": [{"command": command, "type": "command", "timeout": OTHER_TIMEOUT}],
    }


def _cwd(payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]
    return ""


def _absolute(path: str, cwd: str) -> str:
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _undo_edits(current: Optional[str], edits: Any) -> Optional[str]:
    """The file text before `edits`, or None when it cannot be rebuilt."""
    if current is None or not isinstance(edits, list) or not edits:
        return None
    text = current
    for edit in reversed(edits):
        if not isinstance(edit, dict):
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or (new and new not in text):
            return None
        if not new:
            return None  # a pure deletion has no anchor to put the text back
        text = text.replace(new, old, 1)
    return text


def normalize_payload(payload: Any) -> list[dict[str, Any]]:
    """Cursor hook stdin payload -> NormalizedEvent-shaped dicts ([] = ignore)."""
    if not isinstance(payload, dict):
        return []
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("conversation_id") or "")
    cwd = _cwd(payload)

    if event_name == "beforeSubmitPrompt":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return []
        return [base.make_event(AGENT, "intent", session_id, cwd, prompt=prompt)]

    if event_name == "preToolUse":
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if tool not in EDIT_TOOLS or not isinstance(tool_input, dict):
            return []
        raw = next((v for k in PATH_KEYS if isinstance(v := tool_input.get(k), str) and v), None)
        if raw is None:
            return []
        path = _absolute(raw, cwd)
        return [
            base.make_event(
                AGENT, "pre_edit", session_id, cwd, file_path=path, old_content=base.read_text(path), tool=tool
            )
        ]

    if event_name == "afterFileEdit":
        raw = payload.get("file_path")
        if not isinstance(raw, str) or not raw:
            return []
        path = _absolute(raw, cwd)
        new_content = base.read_text(path)
        return [
            base.make_event(
                AGENT,
                "post_edit",
                session_id,
                cwd,
                file_path=path,
                new_content=new_content,
                old_content=_undo_edits(new_content, payload.get("edits")),
                tool="afterFileEdit",
            )
        ]
    return []


def passthrough_for(payload: Any) -> str:
    """The explicit "carry on" answer for events whose empty output would block."""
    if not isinstance(payload, dict):
        return ""
    event_name = payload.get("hook_event_name")
    if event_name == "preToolUse":
        return ALLOW
    if event_name == "beforeSubmitPrompt":
        return CONTINUE
    return ""


def render_ack(event: dict[str, Any], ack: Any) -> str:
    """Stdout for Cursor; "" means "print the passthrough answer"."""
    if event.get("kind") != "pre_edit":
        return ""
    decision = base.ack_decision(ack)
    if decision == "allow":
        return ""
    message = base.ack_message(ack, decision, event.get("file_path"))
    if decision == "block":
        return hook_json.dumps(
            {
                "permission": "deny",
                "user_message": f"Synergo: {message}",
                "agent_message": f"Synergo blocked this edit: {message}",
            }
        )
    # warn: allow is not enforced for preToolUse, so it approves nothing.
    return hook_json.dumps(
        {
            "permission": "allow",
            "user_message": f"Synergo: {message}",
            "agent_message": f"Synergo warning: {message}",
        }
    )


# --- install: Cursor's flat {"version": 1, "hooks": {event: [entry, ...]}} -------


def _is_synergo(entry: Any) -> bool:
    return isinstance(entry, dict) and base.HOOK_MARKER in str(entry.get("command", ""))


def install_hooks(path: Path, desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    settings = base.load_settings(path)
    fresh = not settings
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            raise base.SettingsError(f"{path}: `hooks` is not an object — refusing to overwrite")
        hooks = {}
    changes: list[str] = []
    for event, entries in desired.items():
        existing = hooks.get(event)
        if existing is not None and not isinstance(existing, list):
            raise base.SettingsError(f"{path}: hooks.{event} is not a list — refusing to overwrite")
        existing = existing or []
        if any(_is_synergo(e) for e in existing):
            continue
        hooks[event] = existing + entries
        changes.append(event)
    if changes:
        if fresh:
            settings["version"] = 1
        settings["hooks"] = hooks
        base.save_settings(path, settings)
    return changes


def uninstall_hooks(path: Path) -> list[str]:
    if not path.exists():
        return []
    settings = base.load_settings(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    changes: list[str] = []
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _is_synergo(e)]
        if len(kept) == len(entries):
            continue
        changes.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if changes:
        if not hooks:
            del settings["hooks"]
        base.save_settings(path, settings)
    return changes


def hooks_installed(path: Path, events: Any) -> bool:
    try:
        hooks = base.load_settings(path).get("hooks")
    except (base.SettingsError, OSError, UnicodeDecodeError):
        return False
    if not isinstance(hooks, dict):
        return False
    return all(isinstance(hooks.get(e), list) and any(_is_synergo(x) for x in hooks[e]) for e in events)


class CursorAdapter(base.JsonHooksAdapter):
    id = AGENT
    label = "Cursor"
    executables = ("cursor-agent", "cursor")
    config_dirs = (".cursor",)
    capabilities = {"can_block": True, "has_intent": True, "has_post_edit": True}
    config_subpath = (".cursor", "hooks.json")

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        return hook_settings(HOOK_COMMAND)

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        try:
            return normalize_payload(payload)
        except Exception:  # noqa: BLE001 - fail open on any malformed payload
            return []

    def passthrough(self, payload: Any) -> str:
        try:
            return passthrough_for(payload)
        except Exception:  # noqa: BLE001
            return ""

    def render(self, event: dict[str, Any], ack: Any) -> base.HookOutput:
        try:
            return base.HookOutput(render_ack(event, ack))
        except Exception:  # noqa: BLE001
            return base.PROCEED

    def render_notice(self, event: dict[str, Any], message: str) -> base.HookOutput:
        kind = event.get("kind")
        if kind == "pre_edit":
            return base.HookOutput(hook_json.dumps({"permission": "allow", "user_message": message}))
        if kind == "intent":
            return base.HookOutput(hook_json.dumps({"continue": True, "user_message": message}))
        return base.PROCEED

    def install(self, scope: str, root: Any = None) -> list[str]:
        return install_hooks(self.settings_path(scope, root), self.hook_settings())

    def uninstall(self, scope: str, root: Any = None) -> list[str]:
        return uninstall_hooks(self.settings_path(scope, root))

    def is_installed(self, scope: str, root: Any = None) -> bool:
        return hooks_installed(self.settings_path(scope, root), tuple(self.hook_settings()))


ADAPTER = CursorAdapter()
