# `synergo` command reference

```text
synergo [-h] [--version] COMMAND ...
```

Most of the time you need two commands. Run `synergo up` in a repo to start,
and `synergo doctor` when something seems off. Everything else is for
managing the daemon, or for advanced and scripted use.

| Command | What it does |
|---------|--------------|
| [`up`](#synergo-up) | Set up this repo: agent hooks, room, background daemon. Start here; safe to re-run. |
| [`down`](#synergo-down) | Stop the background daemon. Hooks stay installed. |
| [`leave`](#synergo-leave) | Disconnect this repo from its room. |
| [`status`](#synergo-status) | Show the daemon and the repos it is connected for. |
| [`doctor`](#synergo-doctor) | Check the setup and say how to fix what is wrong. |
| [`open`](#synergo-open) | Open this repo's room in the dashboard. |
| [`serve`](#synergo-serve) | Run the multi-repo daemon in the foreground. |
| [`service`](#synergo-service) | Start the daemon automatically at login (`install` / `uninstall`). |
| [`join`](#synergo-join--synergo-daemon) | Run a single-room daemon in the foreground (advanced; `daemon` is an alias). |
| [`hook`](#synergo-hook) | The agent hook entrypoint, plus `install` / `uninstall` for Claude Code. |
| [`mcp`](#synergo-mcp) | MCP server (stdio) for agents without native hooks. |
| [`watch`](#synergo-watch) | Observe-only file watcher for agents without hooks (e.g. Aider). |
| [`githook`](#synergo-githook) | Git pre-commit check against teammates' recent edits. |

Global options: `--version` prints `synergo X.Y.Z`; `-h`/`--help` works on
every command.

Commands that take `--root PATH` default to the git repository around the
current directory. Outside git there is no root every teammate agrees on, so
they refuse and ask for `--root`. Commands that take `--port PORT` default
to `4600`, or `SYNERGO_PORT`. That's the loopback port the daemon listens on
and the hooks call.

## synergo up

```text
synergo up [--room CODE] [--relay URL] [--name NAME] [--root PATH]
           [--port PORT] [-y] [--no-hooks] [--foreground]
```

Installs agent hooks, creates or joins this repo's room, and connects the repo
through the background daemon. Safe to re-run.

1. **Hooks**: for every detected agent Synergo has an adapter for, plus Claude
   Code even when it isn't detected (asks first). Hooks go into the repo's
   own settings, e.g. `.claude/settings.json`.
2. **Room**: `--room`, else the one in `.synergo.toml`, else a new room created
   on the relay. A new room, or an explicit `--relay`, is written to
   `.synergo.toml`. Commit that file so teammates join the same room.
3. **Daemon**: starts the per-machine background daemon if needed, registers
   this repo with it, and waits until it is connected. It then prints the
   room, relay, your name, the dashboard link and the line to share.

| Option | Description |
|--------|-------------|
| `--room CODE` | Join this room. Default: `.synergo.toml`, else create one. Asks before switching a repo that already has a different room. |
| `--relay URL` | Relay WebSocket base, e.g. `wss://relay.example.com/ws`. Default: config, `SYNERGO_RELAY`, else `ws://localhost:8000/ws`. |
| `--name NAME` | Display name. Default: config / `SYNERGO_NAME`, then git `user.name`, then the OS user. An explicit name is saved to `~/.synergo/config.toml`. |
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--port PORT` | Daemon loopback port (default `4600`, or `SYNERGO_PORT`). |
| `-y`, `--yes` | Answer yes to every question (scripts, CI). |
| `--no-hooks` | Do not install agent hooks. |
| `--foreground` | Run the daemon in this terminal instead of the background (debugging, CI). |

## synergo down

```text
synergo down [--port PORT]
```

Stops the background daemon. Hooks stay installed, and agents run
uncoordinated, failing open, until the next `synergo up`.

| Option | Description |
|--------|-------------|
| `--port PORT` | Daemon loopback port (default `4600`, or `SYNERGO_PORT`). |

## synergo leave

```text
synergo leave [--forget] [--root PATH] [--port PORT]
```

Disconnects this repo from its room. The daemon keeps serving your other repos.

| Option | Description |
|--------|-------------|
| `--forget` | Also remove the room from `.synergo.toml`. That file is shared, so this changes it for the whole team once committed. |
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--port PORT` | Daemon loopback port (default `4600`, or `SYNERGO_PORT`). |

## synergo status

```text
synergo status [--port PORT]
```

Shows whether the daemon is running and, for each connected repo, its room,
connection state and peers. Exits 1 when no daemon answers.

| Option | Description |
|--------|-------------|
| `--port PORT` | Daemon loopback port (default `4600`, or `SYNERGO_PORT`). |

## synergo doctor

```text
synergo doctor [--root PATH] [--port PORT]
```

Runs every check and prints a fix for each failure: Python version, git
repository, `.synergo.toml`, agent hooks, daemon running, repo connected, relay
reachable, protocol compatibility, and clock skew against the relay. Exits 1 if
any check fails.

| Option | Description |
|--------|-------------|
| `--root PATH` | Project directory (default: the git repo around the current directory). |
| `--port PORT` | Daemon loopback port (default `4600`, or `SYNERGO_PORT`). |

## synergo open

```text
synergo open [--root PATH]
```

Opens `<dashboard>/r/<ROOM>` in your browser. The dashboard base is
`SYNERGO_DASHBOARD` if set, else the local dev server (`http://localhost:5173`)
for a local relay, else `https://synergo.dev`. The URL is also printed for
headless machines.

| Option | Description |
|--------|-------------|
| `--root PATH` | Project directory (default: the git repo around the current directory). |

## synergo serve

```text
synergo serve [--port PORT]
```

Runs the multi-repo daemon in the foreground: the same process `synergo up`
starts in the background, for debugging or a process supervisor. Repos
connect to it with `synergo up`. Ctrl+C stops it.

| Option | Description |
|--------|-------------|
| `--port PORT` | Daemon loopback port (default `4600`, or `SYNERGO_PORT`). |

## synergo service

```text
synergo service install [--port PORT]
synergo service uninstall
```

`install` makes the daemon start at login, with no admin rights. That's a
launchd agent on macOS, a systemd user unit on Linux, and a Startup-folder
entry on Windows. `uninstall` removes it and leaves a running daemon alone
(use `synergo down` for that). Without autostart, the daemon starts on
`synergo up`, or lazily the first time an agent works in a repo that has a
`.synergo.toml`.

| Option | Description |
|--------|-------------|
| `--port PORT` | Port the autostarted daemon listens on (default `4600`, or `SYNERGO_PORT`). |

## synergo join / synergo daemon

```text
synergo join ROOM --name NAME [--server URL] [--agent KIND] [--port PORT] [--root PATH]
```

The pre-`up` way to run Synergo: a single-room daemon in the foreground for
one terminal session, configured entirely by flags. It ignores `.synergo.toml`.
It is still useful for scripts and demos. A single-room daemon on the port
blocks `synergo up` there until you stop it.

| Option | Description |
|--------|-------------|
| `ROOM` | Room code, e.g. `TEAM42`. |
| `--name NAME` | Required. Your display name in the room. |
| `--server URL` | Relay WebSocket base (default `ws://localhost:8000/ws`). |
| `--agent KIND` | The agent you drive: `cursor`, `claude-code` (default), `codex`, `antigravity`, `gemini-cli`, `devin`, `grok`, `unknown`. |
| `--port PORT` | Loopback port (default `4600`). |
| `--root PATH` | Directory file paths are keyed against (default: the git repo around each agent's working directory). Outside git, every teammate must pass one. |

## synergo hook

```text
synergo hook claude-code [--port PORT]
synergo hook install claude-code [--global]
synergo hook uninstall claude-code [--global]
```

- `hook claude-code` is what the installed hooks run: it handles one Claude
  Code hook invocation. It always fails open: if the daemon is missing or slow,
  it exits 0 with no decision, so it can never stall your agent.
- `hook install` / `hook uninstall` add or remove only Synergo's entries in
  `.claude/settings.json` in the current directory (`--global`:
  `~/.claude/settings.json`), leaving your other hooks untouched. `synergo up`
  does the install for you.

## Configuration files

**`.synergo.toml`** (in the repo, committed) is the team's settings:

```toml
room = "K7M2PQ2X"
relay = "wss://relay.example.com/ws"
block_window_seconds = 20.0   # a teammate's edit this recent blocks yours
warn_window_seconds = 60.0    # this recent warns
ignore = ["*.lock", "dist/"]  # gitignore-style globs kept out of coordination entirely
share_diffs = true            # false: send which files changed, keep the diffs local
```

**`~/.synergo/config.toml`** (personal; `SYNERGO_HOME` moves the directory)
holds `name`, `relay` and `agent`. It can't set the room or the windows, so
one person's file never splits them from the team.

**Precedence**, highest first: command-line flags, environment variables,
personal file, project file, built-in defaults. The room and windows come only
from flags or the project file.

## Environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `SYNERGO_RELAY` | `ws://localhost:8000/ws` | Relay WebSocket base when no `--relay` is given. |
| `SYNERGO_DASHBOARD` | see [`open`](#synergo-open) | Dashboard base for room links. |
| `SYNERGO_NAME` | git `user.name`, else the OS user | Display name. |
| `SYNERGO_PORT` | `4600` | Daemon loopback port. |
| `SYNERGO_HOME` | `~/.synergo` | Personal config, daemon state, logs (`logs/daemon.log`). |

## Exit codes

`0` success. `1` a check or command failed (no daemon for `status`, a failing
`doctor` check, a failed connection). `2` bad local configuration or usage (an
invalid flag, a malformed `.synergo.toml`). The `hook` entrypoint always exits
0, by design.

## synergo mcp

```text
synergo mcp [--agent ID] [--port PORT] [--print-config] [--agents-md]
```

Runs an MCP server over stdio (protocol `2025-11-25`; also accepts
`2025-06-18`, `2025-03-26`, `2024-11-05`) for agents that have no native
hooks. Tools: `check_file(path)` (asks whether a teammate is in the file),
`declare_intent(text, files?)`, `who_is_here()` and `report_edit(path)`. If the
daemon is down, tools return an error text and the server keeps running.
`--agent` sets the agent id sent to the room (default `unknown`).

`--print-config` prints a JSON snippet for your MCP client's config.
`--agents-md` prints an instruction block for `AGENTS.md` / `CLAUDE.md` that
tells the agent to call `declare_intent` first and `check_file` before edits.

## synergo watch

```text
synergo watch [--agent ID] [--interval SECONDS] [--root PATH] [--port PORT]
```

Polls the repo every second and shares each created, modified or deleted text
file as an edit. Observe only: it warns after the fact and cannot stop an
edit. Skips `.git`, `node_modules`, `.venv`, `dist`, `build`, the
`.synergo.toml` `ignore` globs, binaries and files over 256 KB. Ctrl+C stops.

## synergo githook

```text
synergo githook install|uninstall [--root PATH]
synergo githook run [--root PATH] [--port PORT] [--strict]
```

`install` writes a `pre-commit` hook into the hooks directory (honouring
`core.hooksPath`). An existing hook is moved unchanged to
`pre-commit.synergo-prev` and still runs after the check; `uninstall` puts it
back. Re-running `install` is a no-op. `run` warns about staged files a
teammate touched recently. It never fails the commit unless `--strict`, and
exits 0 silently when the daemon is not running.
