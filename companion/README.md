# Companion Service

A small local process that lets the dashboard start and stop `watcher/watcher.py`
subprocesses on **your own machine**, so teammates never have to touch a terminal.

The relay server (`server/`) is unchanged and unaware of the companion. The
companion only spawns the same watcher CLI you would run by hand.

## Security model

The companion **spawns local processes from HTTP requests**, so it is bound to
`127.0.0.1` and nothing else:

- Host is hard-coded to `127.0.0.1` in `main.py` — never `0.0.0.0`.
- CORS allows any `http://localhost:*` / `http://127.0.0.1:*` origin (Vite dev
  ports move around) plus explicit production origins from
  `COMPANION_CORS_ORIGINS`.
- Methods are limited to `GET`, `POST`, `DELETE`; only `Content-Type` is allowed
  as a request header.
- Never expose this port through ngrok, a reverse proxy, or a tunnel.

## Setup

From the repo root:

```bash
./setup.sh                      # macOS / Linux
powershell -ExecutionPolicy Bypass -File setup.ps1   # Windows
```

This creates `.venv` and installs both `companion/requirements.txt` and
`watcher/requirements.txt`.

## Running

Foreground (easiest for debugging):

```bash
python -m companion.main
```

Background:

```bash
companion/scripts/start_mac.sh     # writes companion/.pid, logs to companion/companion.log
companion/scripts/stop_mac.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File companion\scripts\start_windows.ps1
powershell -ExecutionPolicy Bypass -File companion\scripts\stop_windows.ps1
```

Default port is `4500`; override with `COMPANION_PORT`.

## API

Base URL: `http://127.0.0.1:4500`

### `GET /health`

```json
{ "status": "ok" }
```

Used by the dashboard to decide whether to show the manual-CLI fallback.

### `GET /detect`

Reports install and running state for Cursor, Claude Code, Codex, and
Antigravity.

```json
{
  "platform": "darwin",
  "tools": [
    {
      "id": "cursor",
      "label": "Cursor",
      "installed": true,
      "install_path": "/Applications/Cursor.app",
      "running": true
    }
  ]
}
```

`running` is `null` when `psutil` is missing or its process scan fails — the
endpoint degrades instead of erroring.

### `POST /watchers`

```json
{
  "path": "/Users/you/projects/my-app",
  "room_id": "K7M2PQ",
  "name": "adrian",
  "agent": "cursor",
  "server_url": "ws://localhost:8000/ws"
}
```

Returns `201` with `{"watcher_id": "<uuid>"}`.

| Status | Meaning |
| --- | --- |
| `400` | `path` missing or not a directory |
| `409` | A watcher for the same `(path, agent)` pair is already running |
| `500` | The subprocess could not be spawned |

`agent` accepts `antigravity` for bookkeeping, but the watcher CLI is invoked
with `--agent unknown` for values it does not support.

### `GET /watchers`

Lists supervised subprocesses with their status and the last 50 lines of merged
stdout/stderr:

```json
[
  {
    "watcher_id": "0f0c…",
    "path": "/Users/you/projects/my-app",
    "agent": "cursor",
    "room_id": "K7M2PQ",
    "name": "adrian",
    "server_url": "ws://localhost:8000/ws",
    "status": "running",
    "pid": 48211,
    "exit_code": null,
    "started_at": 1761414000.12,
    "logs": ["Watching /Users/you/projects/my-app (debounce 1.5s)…"]
  }
]
```

Status is derived from `proc.poll()`:

- `running` — still alive
- `stopped` — exited cleanly, or we asked it to stop
- `errored` — exited non-zero on its own (logs are retained so you can see why)

### `DELETE /watchers/{watcher_id}`

Sends `SIGTERM`, waits up to 3 seconds, then `SIGKILL`. Returns the final
watcher record, or `404` if the id is unknown.

## Lifecycle guarantees

- Every spawned watcher is terminated on companion shutdown (FastAPI lifespan
  hook plus explicit `SIGINT`/`SIGTERM` handlers).
- Process state is in-memory only. Restarting the companion loses the registry,
  which is why shutdown terminates children rather than orphaning them.
- Output is kept in a bounded 50-line ring buffer per process, so a chatty
  watcher cannot grow memory without bound.

## Future work: run as a managed service

Today the companion is started manually or via the background scripts. Two
natural next steps:

**macOS (launchd)** — add `~/Library/LaunchAgents/dev.hackday.companion.plist`
with `ProgramArguments` set to the venv Python plus `-m companion.main`,
`RunAtLoad` true, and `KeepAlive` true; load with
`launchctl load -w <plist>`. This replaces `start_mac.sh` and survives logout.

**Linux (systemd user unit)** — add
`~/.config/systemd/user/companion.service` with `ExecStart` pointing at the venv
Python, `Restart=on-failure`, and `WorkingDirectory` set to the repo root; enable
with `systemctl --user enable --now companion`. Using a *user* unit (not system)
keeps the process off the network boundary and under the right uid.

**Windows** — the current `Start-Process -WindowStyle Hidden` approach is
sufficient; a Scheduled Task with "At log on" would make it persistent.
