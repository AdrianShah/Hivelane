# `hivelane` command reference

```text
hivelane [-h] [--version] COMMAND ...
```

Most of the time you need two commands. Run `hivelane up` in a repo to start,
and `hivelane doctor` when something seems off. Everything else is for
managing the daemon, or for advanced and scripted use.

| Command | What it does |
|---------|--------------|
| [`up`](#hivelane-up) | Set up this repo: agent hooks, room, background daemon. Start here; safe to re-run. |
| [`invite`](#hivelane-invite) | Print a shareable invite link (`/j/<ROOM>`) for this repo's room. |
| [`join`](#hivelane-join) | Join a teammate's room from an invite link or room code. |
| [`down`](#hivelane-down) | Stop the background daemon. Hooks stay installed. |
| [`leave`](#hivelane-leave) | Disconnect this repo from its room. |
| [`status`](#hivelane-status) | Show the daemon and the repos it is connected for. |
| [`doctor`](#hivelane-doctor) | Check the setup and say how to fix what is wrong. |
| [`open`](#hivelane-open) | Open this repo's room in the dashboard. |
| [`serve`](#hivelane-serve) | Run the multi-repo daemon in the foreground. |
| [`service`](#hivelane-service) | Start the daemon automatically at login (`install` / `uninstall`). |
| [`daemon`](#hivelane-daemon) | Run a single-room daemon in the foreground (advanced). |
| [`hook`](#hivelane-hook) | The agent hook entrypoint, plus `install` / `uninstall` for Claude Code. |
| [`mcp`](#hivelane-mcp) | MCP server (stdio) for agents without native hooks. |
| [`watch`](#hivelane-watch) | Observe-only file watcher for agents without hooks (e.g. Aider). |
| [`run`](#hivelane-run) | Run a hookless agent (e.g. `hivelane run aider`) with the watcher. |
| [`githook`](#hivelane-githook) | Git pre-commit check against teammates' recent edits. |
| [`task`](#hivelane-task) | The room's shared task list: add, claim, finish (a claimed task reserves its files). |

Global options: `--version` prints `hivelane X.Y.Z`; `-h`/`--help` works on
every command.

Commands that take `--root PATH` default to the git repository around the
current directory. Outside git there is no root every teammate agrees on, so
they refuse and ask for `--root`. Commands that take `--port PORT` default
to `4600`, or `HIVELANE_PORT`. That's the loopback port the daemon listens on
and the hooks call.

## hivelane up

```text
hivelane up [--room CODE_OR_LINK] [--relay URL] [--name NAME] [--root PATH]
           [--port PORT] [-y] [--no-hooks] [--no-repo-lookup]
           [--foreground] [--insecure] [--e2e] [--save-project]
```

Installs agent hooks, creates or joins this repo's room, and connects the repo
through the background daemon. Safe to re-run.

1. **Hooks**: for every detected agent Hivelane has an adapter for, plus Claude
   Code even when it isn't detected (asks first). Hooks go into the repo's
   own settings, e.g. `.claude/settings.json`.
2. **Room**: `--room`, else `.hivelane.toml` (when present), else the room
   remembered for this repo on this machine (`~/.hivelane/config.toml`), else an
   automatic lookup on the relay by hashed repository identity
   (`GET /rooms/by-repo/<key>`), else a new room created on the relay
   (automatically bound to that repo lookup key unless `--no-repo-lookup` is
   given). When a teammate finds a repo-bound room via lookup, their join is
   always held for the room owner to approve from the desktop bee or
   `hivelane room approve`. `hivelane up` does not wait for that answer: it
   says the join is waiting and returns, and the daemon finishes joining in
   the background (the bee and `hivelane status` show "waiting for the owner
   to let you in" until then). If no owner answers within the relay's
   5-minute hold, the daemon asks again a minute later, up to 6 times (about
   half an hour); a decline or no answer at all stops it with a message, and
   `hivelane up` asks again. The resolved room and relay are remembered on this
   machine automatically—`.hivelane.toml` is optional and is not written unless
   `--save-project` is passed. `--room` also takes the full invite link a
   teammate sent: its `#invite=` secret (and `#key=`, if any) is saved to
   `~/.hivelane/config.toml`, never `.hivelane.toml`, and lets you in without
   waiting for approval. Creating a room saves its owner token and invite
   secret there too. When `up` writes the room into `.hivelane.toml` it warns
   that the committed code alone does not let anyone in: teammates with only
   the code wait for an owner's approval, so send them the link.
3. **Daemon**: starts the per-machine background daemon if needed, registers
   this repo with it, and waits until it is connected. It then prints the
   room, relay, your name, the dashboard link and **the line to share**: the
   invite link (`<dashboard>/j/<ROOM>#invite=...`), when this machine has the
   room's invite secret. For a room you own, that is the relay's current one
   (a rotation from the dashboard shows up here).

| Option | Description |
|--------|-------------|
| `--room CODE_OR_LINK` | Join this room: a code, or the invite link a teammate sent (lets you in without approval). Default: `.hivelane.toml`, remembered room on this machine, relay lookup by repo identity, else create one. Asks before switching a repo that already has a different room. |
| `--relay URL` | Relay WebSocket base, e.g. `wss://relay.example.com/ws`. Default: config, `HIVELANE_RELAY`, else `ws://localhost:8000/ws`. |
| `--name NAME` | Display name. Default: config / `HIVELANE_NAME`, then git `user.name`, then the OS user. An explicit name is saved to `~/.hivelane/config.toml`. |
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--port PORT` | Daemon loopback port (default `4600`, or `HIVELANE_PORT`). |
| `-y`, `--yes` | Answer yes to every question (scripts, CI). |
| `--no-hooks` | Do not install agent hooks. |
| `--no-repo-lookup` | Do not look up an existing room by repo identity or bind a newly created room to it. |
| `--status-line` / `--no-status-line` | Show Hivelane in Claude Code's status line (the project's `.claude/settings.json`). Without either flag `up` asks on a terminal (default no) and never replaces a status line you set; `--yes` alone does not turn it on. |
| `--bee` / `--no-bee` | Show the desktop bee (default on): room status in the menu bar, system tray or screen edge; see [`bee`](#hivelane-bee). |
| `--foreground` | Run the daemon in this terminal instead of the background (debugging, CI). |
| `--insecure` | Allow a plaintext `ws://` relay on another machine. Without it, `up` refuses `ws://` to anything but this machine. Also settable as `insecure = true` in `.hivelane.toml`. |
| `--e2e` | Turn on end-to-end encryption for this repo's room: if you have no key yet, generates one, saves it to `~/.hivelane/config.toml`, and prints it once. |
| `--save-project` | Write `room`, non-default `relay`, and `--e2e` into `.hivelane.toml` (optional; off by default). |

## hivelane invite

```text
hivelane invite [--root PATH] [--room CODE] [--relay URL] [--no-key] [--copy] [--json] [--rotate]
```

Prints a shareable `<dashboard>/j/<ROOM>` invite link for this repo's room
(reading `.hivelane.toml` or the room remembered on this machine). Includes
`?relay=<encoded>` when needed, `#invite=<secret>` so whoever opens it comes
straight in, and `#key=<key>` when end-to-end encryption is enabled (URL
fragments stay client-side and are never sent to any server). On a machine
that owns the room the invite secret comes from the relay
(`GET /rooms/<code>/invite`); otherwise it is the one saved from the link you
joined with. Without one, the link still works but its holder waits for an
owner's approval (the command says so).
Opening the link in a browser shows a `/j/<ROOM>` landing page with a one-click
**"Copy for your coding agent"** button.

| Option | Description |
|--------|-------------|
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--room CODE` | Room code override (default: this repo's room). |
| `--relay URL` | Relay URL override. |
| `--no-key` | Omit the `#key=` e2e passphrase fragment from the printed URL. |
| `--copy` | Copy the invite URL to the system clipboard. |
| `--json` | Print JSON with `room`, `relay`, `e2e`, `invite` (the link carries an invite secret), `owner` and `url`. |
| `--rotate` | Owner only: make a new invite link. Links sent before stop admitting (their holders wait for approval); people already let in stay. |

## hivelane join

```text
hivelane join <LINK_OR_ROOM> [--relay URL] [--name NAME] [--root PATH]
              [--port PORT] [-y] [--no-hooks] [--foreground] [--insecure]
```

Joins a teammate's room from an invite link (from `hivelane invite` or the
`/j/<ROOM>` page) or a bare room code:

1. Parses the room code, optional `?relay=`, the optional `#invite=` secret and
   the optional `#key=` e2e key from the URL fragment (the key also as
   `#k=<key>` or bare `#<key>`).
2. Installs agent hooks (honouring `--no-hooks`, `--yes`, `--status-line`).
3. Saves the invite secret into `~/.hivelane/config.toml` under
   `invite_secrets` (so the join is let in without approval), any e2e key under
   `room_keys`, and
   remembers the `(room, relay)` for this repo on this machine—without writing
   anything into `.hivelane.toml`.
4. Starts/connects the background daemon (or runs `--foreground`) and desktop bee.

## hivelane bee

```bash
hivelane bee [on|off|status]
```

The desktop bee shows the room's state where you already look, for people
who never open the dashboard:

- **macOS**: an item in the menu bar, beside your other background apps.
- **Windows**: the logo bee in the system tray, its wings beating while the
  room is connected (faster when amber or red). Instead of a coloured body it
  gets an amber or red dot in the corner, and a face to match, with a quiet
  heads-up when that happens. It follows the taskbar's light or dark mode.
- **Linux**: a thin colour tab tucked against the right edge of the screen;
  hover to slide it out, drag to move it up or down.

The bee's body is green when the room is clear, amber when someone is nearby
or you were pinged, and red when an edit of yours is held; amber and red add a
short label (`overlap`, `2 held`). Its menu shows the details and opens the
dashboard.

`hivelane up` starts it and `hivelane down` stops it. "Hide the bee" in its
menu, or `hivelane bee off`, keeps it off until `hivelane bee on`. It costs no
tokens: it is a separate process that reads the local daemon every 2 seconds
and never writes into an agent's context. It needs no extra installs (AppKit
through `osascript` on macOS, the Win32 tray through Python's own `ctypes` on
Windows, at a few MB, tkinter on Linux) and stays off in CI, over SSH, without a display, and when
`HIVELANE_BEE=0`.

## hivelane down

```text
hivelane down [--port PORT]
```

Stops the background daemon. Hooks stay installed, and agents run
uncoordinated, failing open, until the next `hivelane up`.

| Option | Description |
|--------|-------------|
| `--port PORT` | Daemon loopback port (default `4600`, or `HIVELANE_PORT`). |

## hivelane leave

```text
hivelane leave [--forget] [--root PATH] [--port PORT]
```

Disconnects this repo from its room. The daemon keeps serving your other repos.

| Option | Description |
|--------|-------------|
| `--forget` | Also remove the room from `.hivelane.toml`. That file is shared, so this changes it for the whole team once committed. |
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--port PORT` | Daemon loopback port (default `4600`, or `HIVELANE_PORT`). |

## hivelane status

```text
hivelane status [--port PORT] [--line [--plain]] [--prompt-snippet]
```

Shows whether the daemon is running and, for each connected repo, its room,
connection state and peers. Exits 1 when no daemon answers.

`--line` prints one line instead, for an agent app's status line or a shell
prompt: the hexagon mark, a state dot (green live, red when one of your edits
is held, grey when off) and the counts, e.g. `⬡ ● K7M2PQ2X · 3 agents · 1 held`.
It picks the repo you are in (Claude Code's session JSON on stdin, else the
working directory), gives up on the daemon after half a second and always exits
0, so a stopped daemon reads `⬡ ○ hivelane off` rather than an error.
`hivelane up` offers to put it in Claude Code's status line (see `--status-line`).

| Option | Description |
|--------|-------------|
| `--port PORT` | Daemon loopback port (default `4600`, or `HIVELANE_PORT`). |
| `--line` | One-line status for a status line or shell prompt. |
| `--plain` | With `--line`: no ANSI colours (for shells that do not render them in a prompt). |
| `--prompt-snippet` | Print zsh and bash prompt snippets that show `--line` (for Codex, Gemini CLI and other terminal agents). |

## hivelane doctor

```text
hivelane doctor [--root PATH] [--port PORT]
```

Runs every check and prints a fix for each failure: Python version, git
repository, `.hivelane.toml`, agent hooks, daemon running, repo connected, relay
reachable, protocol compatibility, and clock skew against the relay. Exits 1 if
any check fails.

| Option | Description |
|--------|-------------|
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--port PORT` | Daemon loopback port (default `4600`, or `HIVELANE_PORT`). |

## hivelane open

```text
hivelane open [--root PATH]
```

Opens `<dashboard>/r/<ROOM>` in your browser. The dashboard base is
`HIVELANE_DASHBOARD` if set, else the local dev server (`http://localhost:5173`)
for a local relay, else the hosted dashboard on GitHub Pages
(`https://adrianshah.github.io/Hivelane`). The URL is also printed for
headless machines. A dashboard that is not served by the relay itself needs to
be told which relay to use: add `?relay=<ws url>` to the link (for example
`https://adrianshah.github.io/Hivelane/r/K7M2PQ2X?relay=wss://relay.example.com/ws`);
the dashboard remembers it for the rest of the browser tab.

| Option | Description |
|--------|-------------|
| `--root PATH` | Project directory (default: the git repo around the current directory). |

## hivelane serve

```text
hivelane serve [--port PORT]
```

Runs the multi-repo daemon in the foreground: the same process `hivelane up`
starts in the background, for debugging or a process supervisor. Repos
connect to it with `hivelane up`. Ctrl+C stops it.

| Option | Description |
|--------|-------------|
| `--port PORT` | Daemon loopback port (default `4600`, or `HIVELANE_PORT`). |

## hivelane service

```text
hivelane service install [--port PORT]
hivelane service uninstall
```

`install` makes the daemon start at login, with no admin rights. That's a
launchd agent on macOS, a systemd user unit on Linux, and a Startup-folder
entry on Windows. `uninstall` removes it and leaves a running daemon alone
(use `hivelane down` for that). Without autostart, the daemon starts on
`hivelane up`, or lazily the first time an agent works in a repo that has a
`.hivelane.toml`.

| Option | Description |
|--------|-------------|
| `--port PORT` | Port the autostarted daemon listens on (default `4600`, or `HIVELANE_PORT`). |

## hivelane key

```text
hivelane key show [CODE] [--root PATH]
hivelane key set  [CODE] [KEY] [--root PATH]
```

Manages the passphrase of an end-to-end encrypted room (`e2e = true` in
`.hivelane.toml`). Keys live in `~/.hivelane/config.toml` under `room_keys`, or in
`HIVELANE_ROOM_KEY`; never in the repo. `show` prints the key so you can hand it
to a teammate privately; `set` stores a key a teammate gave you (prompting with
hidden input, or reading stdin, when `KEY` is omitted). `CODE` defaults to this
repo's room. See [security.md](security.md#end-to-end-encryption).

## hivelane room

```text
hivelane room approve <REQUEST_ID> [--port PORT]
hivelane room deny <REQUEST_ID> [--port PORT]
hivelane room require-approval on|off [--root PATH] [--port PORT]
```

Owner actions for a room this machine created (its owner token is stored in
`~/.hivelane/config.toml` under `owner_tokens`). In a room with an invite link,
anyone joining with only the room code already waits for an owner;
`require-approval on` holds invite-link holders too (people already let in, who
hold a member token, are not asked again). Pending requests and their ids
appear in `hivelane status`. Owners can also approve, remove remembered people
and rotate the invite link from the dashboard's Room access panel
(`hivelane invite --rotate` rotates it from here).

## hivelane wait

```text
hivelane wait <FILE> [--timeout S] [--port PORT]
```

Blocks until FILE is free for this repo's room, then exits 0; exits 1 if it is
still taken after `--timeout` seconds (default 90). A blocked agent is told to
run it ("run `hivelane wait server/room.py` to wait your turn"): the relay keeps
a first-come, first-served [wait queue](protocol.md#wait-queues) per file,
tells the teammate's agent someone is waiting, and hands the file over once
their edit ages past the block window (or they leave). The waiting agent's next
edit of the file is then let through with a note that it is its turn. FILE is
absolute, relative to the current directory, or relative to the repo root (the
form block messages print). With no daemon running, or a file hivelane does not
coordinate, there is nothing to wait for and it exits 0 at once.

## hivelane task

```text
hivelane task add TITLE [--body TEXT] [--files A,B] [--after T1,T2]
hivelane task list [--all] [--mine] [--json]
hivelane task show ID [--json]
hivelane task claim [ID | --next] [--wait DURATION] [--lease DURATION]
hivelane task renew ID [--lease DURATION]
hivelane task note ID TEXT
hivelane task done ID [RESULT]
hivelane task release ID
hivelane task cancel ID [REASON]
```

The room's shared task list. A claimed task reserves its files: teammates'
agents are blocked from editing them until the task is done, released, or its
lease (default 30 minutes, renewed by your own edits to those files) runs
out. Exit status is 0 on success and 1 when the relay refuses (for example,
nothing is ready, or the task is someone else's). Details and rules are in
[Room tasks](tasks.md).

## hivelane relay

```text
hivelane relay [--host HOST] [--port PORT] [--state PATH] [--dashboard DIR]
              [--log-level LEVEL]
```

Runs a relay in this terminal (plus the dashboard, when a build is found) for a
LAN or a hackathon, and prints the line teammates use to join it. Needs the
relay extra: `pip install "hivelane[relay]"`. `--state` keeps room codes in a
SQLite file across restarts. A LAN relay speaks plaintext `ws://`, so the
printed join line includes `--insecure`; for anything internet-facing see
[self-hosting.md](self-hosting.md).

## hivelane daemon

```text
hivelane daemon ROOM --name NAME [--server URL] [--agent KIND] [--port PORT] [--root PATH]
```

The pre-`up` way to run Hivelane: a single-room daemon in the foreground for
one terminal session, configured entirely by flags. It ignores `.hivelane.toml`.
It is still useful for scripts and demos. A single-room daemon on the port
blocks `hivelane up` there until you stop it.

| Option | Description |
|--------|-------------|
| `ROOM` | Room code, e.g. `TEAM42`. |
| `--name NAME` | Required. Your display name in the room. |
| `--server URL` | Relay WebSocket base (default `ws://localhost:8000/ws`). |
| `--agent KIND` | The agent you drive: `cursor`, `claude-code` (default), `codex`, `antigravity`, `gemini-cli`, `devin`, `grok`, `unknown`. |
| `--port PORT` | Loopback port (default `4600`). |
| `--root PATH` | Directory file paths are keyed against (default: the git repo around each agent's working directory). Outside git, every teammate must pass one. |

## hivelane hook

```text
hivelane hook claude-code [--port PORT]
hivelane hook install claude-code [--global]
hivelane hook uninstall claude-code [--global]
```

- `hook claude-code` is what the installed hooks run: it handles one Claude
  Code hook invocation. It always fails open: if the daemon is missing or slow,
  it exits 0 with no decision, so it can never stall your agent.
- `hook install` / `hook uninstall` add or remove only Hivelane's entries in
  `.claude/settings.json` in the current directory (`--global`:
  `~/.claude/settings.json`), leaving your other hooks untouched. `hivelane up`
  does the install for you.
- `gemini-cli` works the same way: `hivelane hook install gemini-cli` adds
  BeforeAgent/BeforeTool/AfterTool hooks (matching `write_file` and `replace`)
  to `.gemini/settings.json` (`--global`: `~/.gemini/settings.json`). A block
  is returned as `{"decision": "deny"}`; a warn as a `systemMessage`.

## Configuration files

**`.hivelane.toml`** (optional, in the repo, committed) holds team-shared settings
if you choose to commit one (`hivelane up --save-project`):

```toml
room = "K7M2PQ2X"
relay = "wss://relay.example.com/ws"
block_window_seconds = 20.0   # a teammate's edit this recent blocks yours
warn_window_seconds = 60.0    # this recent warns
ignore = ["*.lock", "dist/"]  # gitignore-style globs kept out of coordination entirely
share_diffs = true            # false: send which files changed, keep the diffs local
```

**`~/.hivelane/config.toml`** (personal; `HIVELANE_HOME` moves the directory)
holds `name`, `relay`, `agent`, per-repo remembered rooms (`rooms`), and the
per-room secrets that must never be committed: `room_keys`, `owner_tokens`,
`invite_secrets` (from invite links) and `member_tokens` (issued by the relay
once you were let in; the daemon writes these). It can't override `.hivelane.toml`'s room or conflict
windows, so one person's file never splits them from the team.

**Precedence**, highest first: command-line flags, environment variables,
personal file, project file, built-in defaults. The room and windows come only
from flags or the project file.

## Environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `HIVELANE_RELAY` | `ws://localhost:8000/ws` | Relay WebSocket base when no `--relay` is given. |
| `HIVELANE_DASHBOARD` | see [`open`](#hivelane-open) | Dashboard base for room links. |
| `HIVELANE_NAME` | git `user.name`, else the OS user | Display name. |
| `HIVELANE_PORT` | `4600` | Daemon loopback port. |
| `HIVELANE_BEE` | on | `0` keeps the desktop bee off (`hivelane up` starts it otherwise). |
| `HIVELANE_HOME` | `~/.hivelane` | Personal config, daemon state, logs (`logs/daemon.log`). |

## Exit codes

`0` success. `1` a check or command failed (no daemon for `status`, a failing
`doctor` check, a failed connection). `2` bad local configuration or usage (an
invalid flag, a malformed `.hivelane.toml`). The `hook` entrypoint always exits
0, by design.

## hivelane mcp

```text
hivelane mcp [--agent ID] [--port PORT] [--print-config] [--agents-md]
```

Runs an MCP server over stdio (protocol `2025-11-25`; also accepts
`2025-06-18`, `2025-03-26`, `2024-11-05`) for agents that have no native
hooks. Tools: `check_file(path)` (asks whether a teammate is in the file),
`declare_intent(text, files?)`, `who_is_here()` and `report_edit(path)`, plus
the [room task](tasks.md) tools `task_list(all?)`, `task_add(title, body?,
files?, after?)`, `task_claim(task_id? | next?, lease_minutes?)`,
`task_note(task_id, text)`, `task_done(task_id, result?)` and
`task_release(task_id)`. If the daemon is down, tools return an error text and the server keeps running.
`--agent` sets the agent id sent to the room (default `unknown`).

`--print-config` prints a JSON snippet for your MCP client's config.
`--agents-md` prints an instruction block for `AGENTS.md` / `CLAUDE.md` that
tells the agent to call `declare_intent` first and `check_file` before edits.

## hivelane watch

```text
hivelane watch [--agent ID] [--interval SECONDS] [--root PATH] [--port PORT]
```

Polls the repo every second and shares each created, modified or deleted text
file as an edit. Observe only: it warns after the fact and cannot stop an
edit. Skips `.git`, `node_modules`, `.venv`, `dist`, `build`, the
`.hivelane.toml` `ignore` globs, binaries and files over 256 KB. Ctrl+C stops.

## hivelane run

```text
hivelane run [--agent ID] [--interval SECONDS] [--root PATH] [--port PORT] [--] COMMAND [ARGS...]
```

Runs `COMMAND` in this terminal (stdin/stdout inherited, cwd = the repo) while
the `hivelane watch` poller runs in the background, then stops the watcher and
exits with the command's exit code (127 if it cannot be started). If `ARGS`
contains `-m TEXT` / `--message TEXT`, that text is posted as the agent's
intent before the command starts. `--agent` defaults to the command's name, so
`hivelane run aider -m "fix login"` reports as `aider`; an id that is not a
known agent kind reports as `unknown`. Use `--` to wrap any other tool:
`hivelane run --agent unknown -- mytool --flag`. Observe only, like `watch`:
edits are shared after they happen and cannot be blocked. This is how Aider
(which has no pre-edit hook) joins a room; `hivelane up` points to it when Aider
is detected.

## hivelane githook

```text
hivelane githook install|uninstall [--root PATH]
hivelane githook run [--root PATH] [--port PORT] [--strict]
```

`install` writes a `pre-commit` hook into the hooks directory (honouring
`core.hooksPath`). An existing hook is moved unchanged to
`pre-commit.hivelane-prev` and still runs after the check; `uninstall` puts it
back. Re-running `install` is a no-op. `run` warns about staged files a
teammate touched recently. It never fails the commit unless `--strict`, and
exits 0 silently when the daemon is not running.
