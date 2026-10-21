# Writing an agent adapter

An adapter teaches Synergo one coding agent's hooks. The agent runs
`synergo hook <id>` before and after its own actions. The adapter turns the
agent's native hook payload into Synergo events, and turns the daemon's
allow / warn / block answer back into whatever that agent expects to read.

Synergo ships adapters for Claude Code (`claude-code`), Codex (`codex`),
Cursor (`cursor`) and Gemini CLI (`gemini-cli`). Each one is a single module
in `synergo/adapters/`, and a shared conformance suite runs every module
against the same contract. This page walks through that contract and a
complete example.

## Before you start

You need an agent that can run a command **before a file edit**, with the file
path in its input. That is what lets Synergo check an edit before it lands.
The agent's hook documentation should also tell you:

- where hooks are configured, per user and per project;
- what the hook receives (almost always JSON on stdin) for a prompt, a
  pre-edit and a post-edit event;
- how a hook tells the agent to go ahead, to show the user a message, or to
  deny the action (an exit code, a field in JSON on stdout, or both).

If the agent has no hooks at all, it can still join a room without an
adapter, observe-only: `synergo run -- <command>` wraps it with a file
watcher, `synergo watch` watches a directory, and `synergo mcp` serves the
same checks as MCP tools. See [the CLI reference](cli.md#synergo-run). An
[adapter request](https://github.com/AdrianShah/Synergo/issues/new?template=adapter_request.yml)
issue is the place to collect the facts above if you want someone else to
write the adapter.

## How a hook call flows

```text
agent ──stdin JSON──► synergo hook <id>
                        │ adapter.normalize(payload) -> [event, ...]
                        │ POST each event to the daemon on 127.0.0.1:4600
                        │ the strictest ack wins (block > warn > allow)
                        │ adapter.render(event, ack) -> HookOutput
agent ◄─stdout + exit──┘
```

1. The agent starts `synergo hook <id>` as a new process and writes its
   payload to stdin.
2. `synergo/hook_fast.py` loads only your module and calls
   `normalize(payload)`. Each event it returns is posted to the local daemon,
   which asks the relay (for a `pre_edit`) and answers with an ack:
   `{"decision": "allow" | "warn" | "block", "reason", "file_path", "others"}`.
3. When one call yields several events (a patch that touches three files),
   every one is checked and the strictest ack decides.
4. `render(event, ack)` returns what to print and the exit status. Only exit
   codes 0 and 2 are passed through; anything else becomes 0.
5. If the daemon cannot be reached, `render_notice(event, message)` produces a
   one-off notice (at most once every ten minutes) and the edit goes ahead.
6. If nothing was printed, `passthrough(payload)` may print an explicit
   "carry on" for agents that read empty output as a refusal.

## The contract

The authoritative statement is the docstring at the top of
[`synergo/adapters/base.py`](../synergo/adapters/base.py). In short:

| Member | What it must do |
|--------|-----------------|
| `id` | The agent id, as in `synergo hook <id>`. Must be one of the protocol's `AgentKind` values. |
| `label` | Human name, shown by `synergo up` and `synergo doctor`. |
| `executables`, `config_dirs` | How `synergo.detect` spots the agent: commands on PATH, directories under the home directory. |
| `capabilities` | `{"can_block", "has_intent", "has_post_edit"}`, each a bool. `can_block` means a pre-edit hook can stop the edit. |
| `normalize(payload) -> list[dict]` | Native payload to event dicts with every key in `base.EVENT_FIELDS`. `[]` for anything Synergo does not care about or cannot parse. **Never raises.** |
| `render(event, ack) -> HookOutput` | The ack as stdout text and an exit code. Only a `pre_edit` may decide anything. Anything that is not a well-formed warn or block reads as allow. A block is never rendered as an allow, and a warn never auto-approves. **Never raises.** |
| `render_notice(event, message) -> HookOutput` | A user-visible notice that decides nothing (the daemon is offline). Defaults to printing nothing. |
| `passthrough(payload) -> str` | What to print when nothing else was. Defaults to `""`. |
| `settings_path(scope, root)` | The hook config file for `"user"` scope (`root` ignored) or `"project"` scope under `root`. |
| `install` / `uninstall` / `is_installed` | Idempotent and marker-based: Synergo's entries are found by `synergo hook` in their command, everything else in the file is left alone, and a file that does not parse raises `SettingsError` and is never overwritten. |

The event dict fields:

| Field | Meaning |
|-------|---------|
| `kind` | `intent` (a prompt), `pre_edit`, `post_edit` or `plan`. |
| `agent` | Your adapter's id. |
| `session_id`, `cwd` | The agent's session and working directory. Paths are keyed on the repo root found from `cwd`. |
| `prompt` | For `intent`: the user's prompt. |
| `file_path` | For edits: an absolute, normalized path. |
| `old_content` / `new_content` | The file before a `pre_edit` / after a `post_edit`, read from disk (`base.read_text`), so the daemon can work out which lines and functions change. |
| `tool` | The agent's tool name, for display. |
| `edit_hints` | Optional, for string-replacement edits: `[{"old", "new"}]` (`base.string_hints`), so the daemon can predict the touched lines before the edit happens. |
| `summary`, `files`, `symbols` | For `plan`: the plan text and the candidate files and symbols in it (`base.plan_event` fills these in). |

`base.make_event(agent, kind, session_id, cwd, **fields)` builds a dict with
every field present, and `base.ack_decision`, `base.ack_message` and
`base.strictest` do the ack handling every adapter needs.

### Two rules that override everything else

**Fail open.** Synergo must never stop an agent because Synergo itself went
wrong. A payload you do not understand normalizes to `[]`, an ack you do not
understand renders as allow, and any exception in `normalize` or `render`
becomes "do nothing". Wrap both in `try`/`except Exception`, as every
shipped adapter does.

**Stay on the fast path.** The agent starts the hook around every single edit,
so your module is imported on every file write. It may import only the
standard library's cheap modules (`os`, `sys`), `synergo.hook_json` for JSON,
and `synergo.adapters.base`. No pydantic, no `json`, `re`, `typing` or
`pathlib` at module level; use `TYPE_CHECKING = False` for type-only imports.
Install-time code can import whatever it needs inside the function.
`synergo/tests/test_adapter_hook_path.py` runs every registered adapter in a
subprocess and fails if anything heavier shows up in `sys.modules`.

## Worked example

Suppose an agent called "Example Agent" (command `example`, config under
`~/.example/`) documents hooks like this:

- Hooks live in `.example/hooks.json`, in the same
  `{"hooks": {Event: [{"matcher", "hooks": [{"type", "command", "timeout"}]}]}}`
  shape Claude Code uses, so the shared JSON helpers can install them.
- `BeforePrompt` receives `{"event", "session", "cwd", "prompt"}`.
- `BeforeEdit` and `AfterEdit` receive `{"event", "session", "cwd", "tool",
  "path", "find", "replace"}`, where `path` may be relative to `cwd`.
- A `BeforeEdit` hook denies the edit by printing `{"decision": "deny",
  "reason": "..."}` (the reason reaches both the user and the model), and
  shows the user a message without deciding anything with
  `{"message": "..."}`.

### 1. The module

`synergo/adapters/example.py`:

```python
"""Example Agent hook adapter.

Schema per <link to the agent's hook docs> (fetched YYYY-MM-DD). What the docs
say, and what this module relies on: [summarize the events, fields and
response format you depend on, so the next person can check them against a
newer version of the docs.]
"""

from __future__ import annotations

import os

from synergo import hook_json
from synergo.adapters import base

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

AGENT = "example"
HOOK_COMMAND = "synergo hook example"
EDIT_MATCHER = "edit_file|write_file"


def hook_settings(command: str = HOOK_COMMAND) -> dict[str, list[dict[str, Any]]]:
    def entry(timeout: int) -> dict[str, Any]:
        return {"type": "command", "command": command, "timeout": timeout}

    return {
        "BeforePrompt": [{"hooks": [entry(10)]}],
        # Short: the agent waits on this one before every edit.
        "BeforeEdit": [{"matcher": EDIT_MATCHER, "hooks": [entry(5)]}],
        "AfterEdit": [{"matcher": EDIT_MATCHER, "hooks": [entry(10)]}],
    }


def normalize_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    event = payload.get("event")
    session = str(payload.get("session") or "")
    cwd = str(payload.get("cwd") or "")

    if event == "BeforePrompt":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return []
        return [base.make_event(AGENT, "intent", session, cwd, prompt=prompt)]

    if event not in ("BeforeEdit", "AfterEdit"):
        return []
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        return []
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    path = os.path.normpath(path)
    tool = payload.get("tool") if isinstance(payload.get("tool"), str) else None

    if event == "BeforeEdit":
        hints = base.string_hints(payload, old_key="find", new_key="replace")
        return [base.make_event(AGENT, "pre_edit", session, cwd, file_path=path, tool=tool,
                                old_content=base.read_text(path), edit_hints=hints)]
    return [base.make_event(AGENT, "post_edit", session, cwd, file_path=path, tool=tool,
                            new_content=base.read_text(path))]


def render_ack(event: dict[str, Any], ack: Any) -> str:
    if event.get("kind") != "pre_edit":
        return ""
    decision = base.ack_decision(ack)
    if decision == "allow":
        return ""
    message = base.ack_message(ack, decision, event.get("file_path"))
    if decision == "block":
        return hook_json.dumps({"decision": "deny", "reason": f"Synergo: {message}"})
    # A warn shows the message and decides nothing, so the agent's own
    # approval flow still runs.
    return hook_json.dumps({"message": f"Synergo warning: {message}"})


class ExampleAdapter(base.JsonHooksAdapter):
    id = AGENT
    label = "Example Agent"
    executables = ("example",)
    config_dirs = (".example",)
    capabilities = {"can_block": True, "has_intent": True, "has_post_edit": True}
    config_subpath = (".example", "hooks.json")

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        return hook_settings(HOOK_COMMAND)

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        try:
            return normalize_payload(payload)
        except Exception:  # noqa: BLE001 - fail open
            return []

    def render(self, event: dict[str, Any], ack: Any) -> base.HookOutput:
        try:
            return base.HookOutput(render_ack(event, ack))
        except Exception:  # noqa: BLE001 - fail open
            return base.PROCEED

    def render_notice(self, event: dict[str, Any], message: str) -> base.HookOutput:
        if event.get("kind") not in ("intent", "pre_edit"):
            return base.PROCEED
        return base.HookOutput(hook_json.dumps({"message": message}))


ADAPTER = ExampleAdapter()
```

Adapt the shape to the agent rather than the agent to the shape:

- An agent that blocks by exit status returns `base.HookOutput(text, 2)` for
  a block (see `claude_code.py`).
- An agent whose config is not Claude-Code-shaped JSON subclasses
  `base.Adapter` and implements `settings_path`, `install`, `uninstall` and
  `is_installed` itself, keeping the same marker and never-overwrite rules
  (see `cursor.py`, whose `hooks.json` is a flat list per event).
- An agent that cannot deny an edit sets `can_block` to `False` and renders a
  block as a clearly worded warning.
- One native call that edits several files returns one `pre_edit` per file
  (see `codex.py`, which parses `apply_patch`).
- An agent that reads empty output from a permission hook as "deny" needs a
  `passthrough` that prints an explicit allow (see `cursor.py`).
- An agent that exposes its plan or todo list can return
  `base.plan_event(...)` from the matching hook (see `claude_code.py`).

### 2. Register it

- Add `"example": "synergo.adapters.example"` to `REGISTRY` in
  `synergo/adapters/__init__.py`.
- Add an `AgentSpec` to `KNOWN_AGENTS` in `synergo/detect.py`, with
  `adapter=registry_adapter("example")` and the same `label`, `executables` and
  `config_dirs` as the module (the conformance suite checks they agree).
  `synergo up` then installs the hooks whenever it detects the agent.
- If the id is new to the protocol, add it to `AgentKind` (and
  `AGENT_KINDS`) in `synergo/protocol.py` and `server/models.py`, and to the
  agent union in `frontend/src/types.ts`, then run
  `python protocol/generate.py`. A relay that predates the new id rejects
  joins and activity carrying it, so the relay has to be upgraded before
  clients start sending it; see [Versioning](protocol.md#versioning).

### 3. Golden fixtures

The conformance suite (`synergo/tests/test_adapter_conformance.py`) runs
every registered adapter against JSON fixtures in
`synergo/tests/fixtures/adapters/<id>/`. Each fixture holds a captured payload
and the events `normalize` must produce:

```json
{"payload": {"event": "BeforeEdit", "session": "e1", "cwd": "{tmp}", "tool": "edit_file",
             "path": "room.py", "find": "    pass", "replace": "    return 1"},
 "expect": [{"kind": "pre_edit", "session_id": "e1", "file_path": "{tmp}/room.py",
             "tool": "edit_file", "old_content": "def room():\n    pass\n"}]}
```

- `{tmp}` is replaced with a temporary directory that contains `room.py`
  (`def room():\n    pass\n`), so file reads are real.
- `expect` lists the events in order. Only the keys you list are compared
  (paths after normalization). `session_id` is compared against the payload's
  own `session_id` field unless you list it, which you must when the agent
  names its session something else, as Example Agent does.
- An empty `expect` means the payload must be ignored.

Five fixtures are required, and you can add more (Codex has
`pre_edit_multi.json`):

| Fixture | Payload |
|---------|---------|
| `prompt.json` | A prompt submission: one `intent` event. |
| `pre_edit.json` | An edit about to happen to `{tmp}/room.py`: one `pre_edit` event. The render tests use it. |
| `post_edit.json` | The same edit after it happened: one `post_edit` event. |
| `irrelevant_tool.json` | A hook Synergo ignores (a shell command, a file read): `[]`. |
| `malformed.json` | A payload with a broken field (a numeric path, a missing tool input): `[]`. |

Use real payloads captured from the agent whenever you can (run the agent
with a hook that copies stdin to a file), with anything private removed. The
fixture is the evidence that the adapter matches what the agent really sends.

### 4. Run the suite

```bash
pytest -q synergo/tests/test_adapter_conformance.py synergo/tests/test_adapter_hook_path.py
pytest -q                     # then everything
```

Without writing a single adapter-specific test, the suite checks that:

- the module, the registry and `synergo.detect` agree, and every required
  fixture exists;
- `normalize` produces exactly the fixture's events, each one valid against
  the daemon's `NormalizedEvent` model, and returns `[]` for garbage input
  (`None`, numbers, strings, another agent's payload);
- `render` prints nothing for allow, prints a warn that names the peer without
  approving or denying, denies a block when `can_block` is set (and never
  approves it otherwise), renders the daemon's real `EventAck` the same as a
  plain dict, and reads every malformed ack as allow;
- only `pre_edit` events decide anything, and `render_notice` never denies or
  approves;
- install is idempotent, keeps unrelated hooks and settings, refuses to touch
  a file it cannot parse, and uninstall restores the file exactly, in both
  user and project scope; `synergo hook install|uninstall <id>` works end to
  end;
- `synergo hook <id>` runs in a subprocess without importing anything from
  the daemon stack or the slow parts of the standard library, and produces
  the right output for allow, block and daemon-offline.

The render checks recognise a deny as exit status 2, `"decision": "block"`
or `"deny"`, `"permission": "deny"`, or
`"hookSpecificOutput": {"permissionDecision": "deny"}`, and an approval as
`"decision": "approve"` or `"permissionDecision": "allow"`. If your agent
spells either one differently, teach `_denies` and `_approves` in
`test_adapter_conformance.py` the new shape in the same pull request;
otherwise the suite cannot see your adapter's decisions and fails.

Add tests of your own for anything agent-specific, such as a patch format,
next to the others in `synergo/tests/`.

### 5. Try it for real

```bash
synergo hook install example          # writes .example/hooks.json in this repo
```

`scripts/bench_hook.py` times one hook invocation from a cold start. It
builds Claude Code and Codex payloads today; add your agent's pre-edit
payload to its `payload()` function and run it with `--agent example` to
confirm the new module costs no more than the others.

Then run the agent in two checkouts joined to the same room, edit the same
function from both, and watch the second edit get blocked. Add the agent to
the README's list and to `docs/cli.md` in the same pull request.

Issues labelled
[`good first adapter`](https://github.com/AdrianShah/Synergo/labels/good%20first%20adapter)
are agents people have asked for.
