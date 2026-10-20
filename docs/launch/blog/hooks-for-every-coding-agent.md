# Hooks for every coding agent

*Draft. Publish on D+2 (see the launch checklist). Sources: `docs/adapters.md`
and the docstrings in `hivelane/adapters/`, each of which names the agent's
hook documentation and the date it was read.*

Hivelane prevents collisions by asking a question before every edit: is a
teammate's agent already working on this code? That only works if the agent
asks. Four agents let us ask from their own hooks today: Claude Code, Codex
CLI, Cursor and Gemini CLI. Each one does it differently. This post walks
through how Hivelane plugs into each, what it can and cannot see, and how to
add the next one.

## One command, one contract

`hivelane up` detects the agents installed on your machine and writes
Hivelane's hook entries into each agent's project config. Every entry runs the
same command, `hivelane hook <agent-id>`, which:

1. reads the agent's JSON payload from stdin;
2. hands it to that agent's adapter, whose `normalize()` turns it into
   Hivelane events: `intent` (a prompt), `pre_edit`, `post_edit` or `plan`;
3. posts each event to the local daemon on `127.0.0.1:4600`, which asks the
   relay for a `pre_edit` and gets back allow, warn or block;
4. takes the strictest answer when one call touched several files;
5. hands the answer to the adapter's `render()`, which prints whatever this
   agent understands as "go ahead", "show this message" or "deny".

Two rules sit above everything else in the adapter contract
(`hivelane/adapters/base.py`):

- **Fail open.** A payload the adapter does not understand produces no
  events; an answer it does not understand renders as allow; any exception
  means "do nothing". If the daemon cannot be reached, the agent shows a
  one-line notice at most every ten minutes and the edit proceeds.
- **Stay on the fast path.** The hook is a new process on every edit, so an
  adapter may import only cheap standard-library modules and Hivelane's own
  JSON and base helpers. A test runs every adapter in a subprocess and fails
  if anything heavier appears in `sys.modules`. Measured with
  `scripts/bench_hook.py` against a local stub daemon, a Claude Code hook
  call is 117 ms at p95 on Windows.

And one rule about warnings: **a warning never approves anything.** Several
agents read an explicit "allow" from a hook as permission that skips the
user's own confirmation, so every adapter renders a warning as a message
only.

## The four adapters

| | Claude Code | Codex CLI | Cursor | Gemini CLI |
|---|---|---|---|---|
| Config written | `.claude/settings.json` | `.codex/hooks.json` | `.cursor/hooks.json` | `.gemini/settings.json` |
| Prompt event | `UserPromptSubmit` | `UserPromptSubmit` | `beforeSubmitPrompt` | `BeforeAgent` |
| Pre-edit event | `PreToolUse` on `Edit\|Write\|MultiEdit\|NotebookEdit` | `PreToolUse` on `apply_patch` (and `Edit`, `Write`) | `preToolUse` on `Write\|Delete` | `BeforeTool` on `write_file\|replace` |
| Post-edit event | `PostToolUse` | `PostToolUse` | `afterFileEdit` | `AfterTool` |
| Block | `permissionDecision: "deny"` with a reason | same JSON as Claude Code | `{"permission": "deny", "user_message", "agent_message"}` | `{"decision": "deny", "reason", "systemMessage"}` |
| Warn | `additionalContext` plus `systemMessage`, no decision | same as Claude Code | `permission: "allow"` with messages (Cursor does not enforce `allow` on this hook) | `systemMessage` only |
| Hook docs read | [code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks), 2026-09-28 | [developers.openai.com/codex/hooks](https://developers.openai.com/codex/hooks), 2026-10-04 | [cursor.com/docs/agent/hooks](https://cursor.com/docs/agent/hooks), 2026-10-04 | [gemini-cli docs/hooks/reference.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md), 2026-10-04 |

### Claude Code

Claude Code's hooks live in `settings.json`, and Hivelane writes the project
copy, `.claude/settings.json`, so the whole team gets them with the repo.
Three entries:

- `UserPromptSubmit` shares your prompt as your current intent. Teammates'
  dashboards show it, and a teammate whose agent is blocked by you sees it as
  "their task".
- `PreToolUse`, matched to `Edit`, `Write`, `MultiEdit` and `NotebookEdit`,
  with a 5-second timeout. For `Edit` and `MultiEdit` the payload includes
  the old and new strings, which Hivelane passes on as edit hints so the
  daemon can predict which lines and functions the edit will touch before it
  happens. A block is a `deny` with a `permissionDecisionReason` that both you
  and the model read.
- `PostToolUse`, matched to the edit tools plus `TodoWrite` and
  `ExitPlanMode`. Edits are shared with the room. Todo lists and plans become
  a Hivelane **plan**: its summary plus the files and symbols it names, so a
  teammate's agent is warned before it edits a file your plan claims.

### Codex CLI

Codex reads hooks from `hooks.json` with the same shape as Claude Code's
settings, so the shared install logic writes `.codex/hooks.json` and never
touches `config.toml`. Two Codex-specific details:

- File edits arrive through the `apply_patch` tool as patch text, and one
  patch can add, update, delete and move several files. The adapter parses
  the patch headers and produces one `pre_edit` per file; every one is
  checked and the strictest answer wins, so a patch that touches one
  contested file is blocked as a whole.
- Codex asks you to review and trust new or changed hooks (`/hooks`) before
  it runs them, and loads project hooks only once the project's `.codex/`
  layer is trusted. `hivelane up` prints a reminder.

### Cursor

Cursor's `hooks.json` is a flat list of entries per event, not Claude Code's
grouped shape, so this adapter has its own small merge with the same rules:
entries found by their `hivelane hook` command, everything else untouched.

- `preToolUse` on the agent's `Write` and `Delete` tools is where a block
  happens. Cursor treats invalid or empty output from a permission hook as a
  refusal, so Hivelane always prints an explicit allow when it has nothing to
  say.
- `afterFileEdit` is observational. It reports the replaced strings, and the
  adapter rebuilds the pre-edit text by undoing them, so the daemon can work
  out which functions changed.
- What Cursor's hooks do not cover: Tab completions and your own typing.
  For those, the VS Code-compatible extension (it runs in Cursor too) shows
  which files teammates' agents are in, warns the first time you change one,
  and shares your saved edits. It never blocks.

### Gemini CLI

Gemini CLI's hooks live in `settings.json` with the Claude Code shape, except
that timeouts are in milliseconds (Hivelane uses 5,000 for the pre-edit hook).
Edits go through `write_file` and `replace`; `replace` carries old and new
strings, which become edit hints. A block is `{"decision": "deny"}` with a
reason for the model and a `systemMessage` for the terminal.

## What no hook sees

Every adapter shares one blind spot: an edit made through a shell command
(`sed -i`, a code generator, `git checkout -- file`) does not go through the
agent's edit tool, so no pre-edit hook runs. Hivelane does not try to guess at
shell commands.

## Agents without hooks

Some agents have no pre-edit hook. They can still join a room, honestly
labelled as observe-only:

| Path | What it does | Can it stop an edit? |
|------|--------------|----------------------|
| `hivelane run aider -m "fix login"` | Runs the agent with a file poller beside it (every second) and posts `-m` as its intent | No: edits are shared after they happen |
| `hivelane watch` | The same poller on its own, for any tool | No |
| `hivelane mcp` | An MCP server with `check_file`, `declare_intent`, `who_is_here` and `report_edit`; `--agents-md` prints instructions telling the agent to call `check_file` before edits | Only if the agent calls the tool |
| `hivelane githook install` | A pre-commit check against teammates' recent edits | Warns; fails the commit only with `--strict` |

## Adding the next agent

An adapter is one module in `hivelane/adapters/` plus a registry entry. The
agent has to be able to run a command before a file edit, with the path in
its input. The module implements `normalize()` and `render()` (both must
never raise), names the agent's config file, and declares what it can do
(`can_block`, `has_intent`, `has_post_edit`). Its docstring records which
hook documentation it was written against and when, so the next person can
check it against a newer version.

The shared conformance suite then needs five golden fixtures, real captured
payloads where possible: a prompt, a pre-edit, a post-edit, an irrelevant
tool call and a malformed payload. Without any adapter-specific test, the
suite checks that the module agrees with the registry and detection, that
`normalize()` produces exactly the fixtures' events and ignores garbage,
that a block is never rendered as an allow and a warning never approves,
that install is idempotent and uninstall restores the file exactly, and that
the hook runs in a subprocess without importing anything slow.

The walkthrough, with a complete example adapter, is
[docs/adapters.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/adapters.md),
and issues labelled `good first adapter` collect agents people have asked
for.

---

Hivelane is open source (Apache-2.0):
[github.com/AdrianShah/Hivelane](https://github.com/AdrianShah/Hivelane).
