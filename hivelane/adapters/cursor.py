"""Cursor (agent / Cmd+K / cursor-agent CLI) hook adapter.

Schema per https://cursor.com/docs/agent/hooks, re-fetched 2026-10-18. What
the docs say, and what this module relies on:

- Hooks live in `<repo>/.cursor/hooks.json` (project; commands run from the
  project root) or `~/.cursor/hooks.json` (user). The file is
  `{"version": 1, "hooks": {eventName: [{"command", "type"?, "timeout"?,
  "matcher"?, "failClosed"?}]}}`, a flat list of entries per event, NOT
  Claude Code's `[{matcher, hooks: [...]}]` groups, so install uses its own
  small merge here (same ownership / never-overwrite rules as base).
- Every payload carries `conversation_id`, `generation_id`, `hook_event_name`,
  `workspace_roots`, ...; there is no `session_id`, so `conversation_id` is
  the session id. `preToolUse` additionally carries `cwd`.
- Events used:
  * `beforeSubmitPrompt` (`prompt`) -> intent. Output `{"continue": bool,
    "user_message"}`; hivelane always answers `{"continue": true}`. The docs
    show `user_message` only "when the prompt is blocked", so an offline
    notice attached here is not displayed today (the preToolUse one is the
    one users see).
  * `preToolUse` (`tool_name`, `tool_input`; `matcher` is a regex on the tool
    name, documented names include Shell/Read/Write/Grep/Delete/Task/MCP:*)
    -> pre_edit for `Write` / `Delete`. Output `{"permission", "user_message",
    "agent_message"}`. The docs: `"allow"` to proceed, `"deny"` to block,
    and `"ask"` "is accepted by the schema but not enforced for preToolUse
    today". The docs do not show `tool_input` for Write/Delete, so the path is
    read defensively from `file_path` / `path` / `target_file`.
  * `afterFileEdit` (`file_path`, `edits: [{old_string, new_string}]`,
    observational) -> post_edit; the pre-edit text is rebuilt by undoing the
    edits when every `new_string` is found exactly once.
- Exit 0 + JSON is the contract; exit 2 means deny; other codes fail open.
  For permission hooks "invalid JSON or a response that doesn't match the
  hook's schema blocks the action" even with `failClosed: false`, and "no
  output" counts as a hook failure, so an empty stdout is not a safe answer.

The decision hivelane makes in Cursor: it only ever *denies* (a block). Every
other preToolUse answer, an allow ack, a warn, the daemon offline, an
internal error, is `{"permission": "ask"}` (with messages for a warn or a
notice), never `"allow"`. There is no documented "no decision" form, and an
explicit `allow` could skip Cursor's own approval (file-deletion protection
included), which a coordination tool must never do. `ask` is schema-valid (so
it does not trip the invalid-response block) and, per the docs, not enforced
for preToolUse today, so the action proceeds through Cursor's normal flow. If
Cursor starts enforcing `ask`, the user is prompted: the safe direction.

Can Cursor block an edit before it happens? Yes, via `preToolUse` on the
agent's Write/Delete tools (there is no dedicated beforeFileEdit event, and
`afterFileEdit` is observational only). Tab completions and the user's own
typing are not covered by any blocking hook; the VS Code extension
(editors/vscode) reports those buffer edits.
"""

from __future__ import annotations

import os

from hivelane import hook_json
from hivelane.adapters import base

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Optional

AGENT = "cursor"
HOOK_COMMAND = "hivelane hook cursor"

EDIT_TOOLS: tuple[str, ...] = ("Write", "Delete")
EDIT_MATCHER = "^(" + "|".join(EDIT_TOOLS) + ")$"
PATH_KEYS: tuple[str, ...] = ("file_path", "path", "target_file")

PRE_TOOL_TIMEOUT = 5
OTHER_TIMEOUT = 10

# Never "allow": see the module docstring.
NO_DECISION = "ask"
ASK = '{"permission": "ask"}'
CONTINUE = '{"continue": true}'
CONFIG_VERSION = 1


def hook_settings(command: str = HOOK_COMMAND) -> dict[str, list[dict[str, Any]]]:
    """The entries hivelane merges into `.cursor/hooks.json` under `hooks`."""
    return {
        "beforeSubmitPrompt": [{"command": command, "type": "command", "timeout": OTHER_TIMEOUT}],
        "preToolUse": [
            {"command": command, "type": "command", "timeout": PRE_TOOL_TIMEOUT, "matcher": EDIT_MATCHER}
        ],
        "afterFileEdit": [{"command": command, "type": "command", "timeout": OTHER_TIMEOUT}],
    }


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # different drives, or mixed absolute/relative
        return False


def _cwd(payload: dict[str, Any], file_path: Any = None) -> str:
    """The payload's cwd, else the workspace root holding `file_path` (the
    longest match in a multi-root workspace), else the first root."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    roots = [r for r in payload.get("workspace_roots") or [] if isinstance(r, str) and r] if isinstance(
        payload.get("workspace_roots"), list
    ) else []
    if not roots:
        return ""
    if isinstance(file_path, str) and os.path.isabs(file_path):
        holding = [r for r in roots if _within(file_path, r)]
        if holding:
            return max(holding, key=len)
    return roots[0]


def _undo_edits(current: Optional[str], edits: Any) -> Optional[str]:
    """The file text before `edits`, or None when it cannot be rebuilt
    unambiguously (a `new_string` missing, or found more than once)."""
    if current is None or not isinstance(edits, list) or not edits:
        return None
    text = current
    for edit in reversed(edits):
        if not isinstance(edit, dict):
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or not new:
            return None  # a pure deletion has no anchor to put the text back
        if text.count(new) != 1:
            return None
        text = text.replace(new, old, 1)
    return text


def normalize_payload(payload: Any) -> list[dict[str, Any]]:
    """Cursor hook stdin payload -> NormalizedEvent-shaped dicts ([] = ignore)."""
    if not isinstance(payload, dict):
        return []
    event_name = payload.get("hook_event_name")
    session_id = str(payload.get("conversation_id") or "")

    if event_name == "beforeSubmitPrompt":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return []
        return [base.make_event(AGENT, "intent", session_id, _cwd(payload), prompt=prompt)]

    if event_name == "preToolUse":
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if tool not in EDIT_TOOLS or not isinstance(tool_input, dict):
            return []
        raw = next((v for k in PATH_KEYS if isinstance(v := tool_input.get(k), str) and v), None)
        if raw is None:
            return []
        cwd = _cwd(payload, raw)
        path = base.absolute(raw, cwd)
        return [
            base.make_event(
                AGENT, "pre_edit", session_id, cwd, file_path=path, old_content=base.read_text(path), tool=tool
            )
        ]

    if event_name == "afterFileEdit":
        raw = payload.get("file_path")
        if not isinstance(raw, str) or not raw:
            return []
        cwd = _cwd(payload, raw)
        path = base.absolute(raw, cwd)
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
    """The well-formed "no decision from hivelane" answer for events whose
    empty output Cursor would treat as a failure."""
    if not isinstance(payload, dict):
        return ""
    event_name = payload.get("hook_event_name")
    if event_name == "preToolUse":
        return ASK
    if event_name == "beforeSubmitPrompt":
        return CONTINUE
    return ""


def _deny(message: str) -> str:
    return hook_json.dumps(
        {
            "permission": "deny",
            "user_message": f"Hivelane: {message}",
            "agent_message": f"Hivelane blocked this edit: {message}",
        }
    )


def render_ack(event: dict[str, Any], ack: Any) -> str:
    """Stdout for Cursor; "" means "print the passthrough answer"."""
    if event.get("kind") != "pre_edit":
        return ""
    decision = base.ack_decision(ack)
    if decision == "allow":
        return ""
    message = base.ack_message(ack, decision, event.get("file_path"))
    if decision == "block":
        return _deny(message)
    # warn: "ask", never "allow", a warn must not approve anything.
    return hook_json.dumps(
        {
            "permission": NO_DECISION,
            "user_message": f"Hivelane: {message}",
            "agent_message": f"Hivelane warning: {message}",
        }
    )


# --- install: Cursor's flat {"version": 1, "hooks": {event: [entry, ...]}} -------


def install_hooks(path: Path, desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    settings, fmt = base.read_settings(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            raise base.SettingsError(f"{path}: `hooks` is not an object, refusing to overwrite")
        hooks = {}
    changes: list[str] = []
    for event, entries in desired.items():
        existing = hooks.get(event)
        if existing is not None and not isinstance(existing, list):
            raise base.SettingsError(f"{path}: hooks.{event} is not a list, refusing to overwrite")
        existing = existing or []
        current = base._existing_command(existing)
        if current is not None:
            # Upgrade an older hivelane entry (matcher, timeout), keeping a
            # command the user customised.
            wanted = [
                {**e, "command": base.keep_user_command(current, e["command"])} if base._is_hivelane(e) else e
                for e in entries
            ]
            if all(e in existing for e in wanted):
                continue
            existing = [e for e in existing if not base._is_hivelane(e)]
            entries = wanted
        hooks[event] = existing + entries
        changes.append(event)
    if changes:
        if fmt.commented:
            base._refuse_commented(path, desired)
        settings.setdefault("version", CONFIG_VERSION)
        settings["hooks"] = hooks
        base.save_settings(path, settings, fmt)
    return changes


def uninstall_hooks(path: Path) -> list[str]:
    if not path.exists():
        return []
    settings, fmt = base.read_settings(path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    changes: list[str] = []
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not base._is_hivelane(e)]
        if len(kept) == len(entries):
            continue
        changes.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if changes:
        if fmt.commented:
            base._refuse_commented(path)
        if not hooks:
            del settings["hooks"]
        base.save_settings(path, settings, fmt)
    return changes


def hooks_installed(path: Path, events: Any) -> bool:
    try:
        hooks = base.load_settings(path).get("hooks")
    except (base.SettingsError, OSError, UnicodeDecodeError):
        return False
    if not isinstance(hooks, dict):
        return False
    return all(isinstance(hooks.get(e), list) and any(base._is_hivelane(x) for x in hooks[e]) for e in events)


class CursorAdapter(base.JsonHooksAdapter):
    id = AGENT
    label = "Cursor"
    executables = ("cursor-agent", "cursor")
    config_dirs = (".cursor",)
    capabilities = {"can_block": True, "has_intent": True, "has_post_edit": True}
    config_subpath = (".cursor", "hooks.json")

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        return hook_settings(self.hook_command)

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
            if event.get("kind") == "pre_edit" and base.ack_decision(ack) == "block":
                return base.HookOutput(_deny(base.GENERIC_BLOCK))
            return base.PROCEED

    def render_notice(self, event: dict[str, Any], message: str) -> base.HookOutput:
        kind = event.get("kind")
        if kind == "pre_edit":
            return base.HookOutput(hook_json.dumps({"permission": NO_DECISION, "user_message": message}))
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
