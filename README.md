<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/mascot-clear-dark.svg">
    <img src="docs/brand/mascot-clear.svg" alt="The Hivelane guard bee" width="120">
  </picture>
</p>

# Hivelane

**Live coordination for parallel AI coding agents.**

Hivelane streams agent intent, prompts, AI captions, and file diffs from every teammate into one shared board — and answers each agent's "is anyone else in this file?" before the edit lands.

```bash
pipx install git+https://github.com/AdrianShah/Hivelane   # not on PyPI yet
hivelane up                # in your repo: hooks, room, background daemon
```

> **Not published yet.** The repository is private and `hivelane` is not on
> PyPI yet, so for now install from a checkout you have access to
> (`pipx install .` or `uv tool install --editable .`). The install-script and
> `raw.githubusercontent.com` URLs in [docs/install.md](docs/install.md) 404
> for anyone without repo access until the repository is public or the
> packages are published. The dashboard and docs are public at
> <https://adrianshah.github.io/Hivelane/> and
> <https://adrianshah.github.io/Hivelane/docs/>.

---

## Why Hivelane

When several people (or several AI agents) work in the same repo at once, you usually find out about collisions at PR time. Hivelane turns that into a glanceable, real-time signal:

- **Per-agent lanes** — declared intent, prompts, captions, and expandable git-style diffs
- **Pre-edit checks** — an agent asks the relay before it writes and is warned, or blocked, when a teammate is already in that file
- **Conflict radar** — overlapping file edits called out within a short window
- **Shared rooms** — one code to join; no accounts, Docs-style trust model

---

## Quick start

You need Python 3.12+ (uv installs it for you if you don't have it) and a
relay: your team's, or [your own](https://github.com/AdrianShah/Hivelane/blob/main/docs/self-hosting.md). Without one, Hivelane
uses `ws://localhost:8000/ws`, which suits [developing locally](#develop-locally).

**1. Install the CLI** — once per machine:

```bash
pipx install git+https://github.com/AdrianShah/Hivelane   # not on PyPI yet
```

Other channels (Homebrew, Scoop, a one-line install script, `npx`) are in
[docs/install.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/install.md). Install it persistently, so `hivelane` is on
your PATH: agent hooks call it before every edit.

**2. Set up the repo** — the first teammate:

```bash
cd your-repo
hivelane up --relay wss://relay.example.com/ws
git add .hivelane.toml && git commit -m "Join the Hivelane room"
```

`hivelane up` installs the agent hooks, creates a room on the relay, records it
(and the relay) in `.hivelane.toml`, and connects the repo through a background
daemon. It finishes by printing the room's dashboard link and the one line to
share with teammates.

**3. Everyone else** pulls the repo and runs:

```bash
hivelane up
```

The room comes from the committed `.hivelane.toml`, so there is no code to pass
around. `hivelane open` opens the room's dashboard, `hivelane status` shows what
the daemon is connected to, and `hivelane doctor` explains whatever is wrong.

**Do not tunnel or publicly expose the daemon's port (4600).** It answers hooks
on loopback only.

Hooks replaced the old file watcher because a watcher only notices a collision
after both agents have written to the file. Now each agent declares what it is
working on and asks before each edit, so Hivelane can warn it, or block the
write outright. The conflict is prevented instead of reported.

---

**Editor extension:** a VS Code / Cursor / Windsurf extension (status bar, badges on files peers are editing, pre-edit warnings) lives in [`editors/vscode/`](editors/vscode/README.md).

## Architecture

```
┌─────────────┐  intent / edit_check  ┌──────────────────┐     broadcast      ┌──────────────┐
│    Daemon   │ ────────────────────► │  Hivelane Relay   │ ─────────────────► │   Dashboard  │
│ (per laptop)│ ◄──────────────────── │  FastAPI + rooms │   WebSocket        │  React / Vite│
└─────────────┘  allow / warn / block └────────┬─────────┘                    └──────────────┘
       ▲                                       │
       │ hook call, before each edit           │ Gemini captions / conflicts
┌──────┴──────┐                                ▼
│ Agent hooks │                        (best-effort AI layer)
│  :4600 only │
└─────────────┘
```

| Component | Role |
|-----------|------|
| **`server/`** | Room relay. Accepts agents + spectators, broadcasts activity, answers pre-edit checks, runs Gemini captioning and conflict classification. Room codes persist to SQLite when `HIVELANE_STATE_PATH` is set. |
| **`hivelane/`** | The `hivelane` CLI (published to PyPI). Installs agent hooks, and runs one background daemon per machine that holds a relay connection for each repo you `hivelane up` in and answers hooks on `127.0.0.1:4600`. |
| **`frontend/`** | Spectator dashboard. Create/join rooms, live lanes, conflict banner, analytics. Room links are `<dashboard>/r/<CODE>`. |
| **`.hivelane.toml`** | Per-repo, committed: the room, the relay, conflict windows. What lets a teammate's plain `hivelane up` land in the same room. |

---

## Environment variables

### CLI (`hivelane`)

Flags win over these, and these win over `~/.hivelane/config.toml` and the repo's
`.hivelane.toml`. The room and conflict windows are the exception: those come
from `.hivelane.toml`, so one person's settings cannot split them from the team.
Full precedence and every command: [docs/cli.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/cli.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVELANE_RELAY` | `ws://localhost:8000/ws` | Relay WebSocket base used when no `--relay` is given. |
| `HIVELANE_DASHBOARD` | local dev server for a local relay, else `https://adrianshah.github.io/Hivelane` | Dashboard base for room links (`<dashboard>/r/<CODE>`) and `hivelane open`. |
| `HIVELANE_NAME` | git `user.name`, else the OS user | Your display name in rooms. |
| `HIVELANE_PORT` | `4600` | Loopback port the daemon listens on and the hooks call. |
| `HIVELANE_HOME` | `~/.hivelane` | Personal config, daemon state and logs (`logs/daemon.log`). |
| `HIVELANE_OUTBOX_LIMIT` | `500` | Messages the daemon keeps per room while the relay is unreachable (oldest dropped). `0` = unlimited. |

### Relay (`server/`)

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVELANE_ENV` | `development` | `development` \| `staging` \| `production`. Outside development the relay refuses to start without `CORS_ORIGINS`. |
| `CORS_ORIGINS` | localhost dev server | Comma-separated exact origins. **Required** in staging/production. |
| `CORS_ORIGIN_REGEX` | — | Opt-in wildcard for preview deploys. Empty by default; a regex here widens who can call the relay. |
| `HIVELANE_STATE_PATH` | — (`/data/hivelane.db` in the image) | SQLite file on a persistent disk. Without it, room codes live only in memory and every deploy invalidates them. |
| `TRUST_PROXY_HEADER` | `false` | Read the client IP from `X-Forwarded-For`. See the warning below. |
| `TRUSTED_PROXY_HOPS` | `1` | With `TRUST_PROXY_HEADER`, how many `X-Forwarded-For` entries from the right your own proxies appended. The client IP is the entry that many hops in, never the client-supplied leftmost one. |
| `TRUST_CLOUDFLARE` | `false` | Read the client IP from `CF-Connecting-IP`. On Fly (`FLY_APP_NAME` set) `Fly-Client-IP` is used automatically. |
| `ROOM_CREATE_LIMIT` / `ROOM_CREATE_WINDOW_SECONDS` | `10` / `3600` | Room creations allowed per IP per window. `0` = unlimited. |
| `WS_ATTEMPT_LIMIT` / `WS_ATTEMPT_WINDOW_SECONDS` | `30` / `60` | WebSocket connection attempts per IP per window. `0` = unlimited. |
| `ROOM_MESSAGE_LIMIT` | `600` | Inbound messages per minute per room, summed over every socket in it (token bucket). Raise it for rooms bigger than ~6 busy agents; `0` = unlimited. |
| `RATE_LIMIT_MAX_TRACKED_IPS` | `4096` | Bound on tracked IPs so the limiter cannot leak memory. `0` = unbounded. |
| `HIVELANE_MAX_ROOMS` | `0` (unlimited) | Valid room codes this relay holds at once; `POST /rooms` answers 503 at the cap. |
| `HIVELANE_MAX_MEMBERS` | `0` (unlimited) | Named members (agent connections) per room; the next joiner gets `Room full`. |
| `HIVELANE_MAX_FRAME_BYTES`, `HIVELANE_MAX_DIFF_BYTES`, `HIVELANE_MAX_TEXT_BYTES`, `HIVELANE_MAX_FILES_PER_MESSAGE`, `HIVELANE_MAX_SYMBOLS_PER_MESSAGE`, `HIVELANE_MAX_LINE_RANGES_PER_MESSAGE`, `HIVELANE_MAX_PATH_CHARS` | 512 KiB, 64 KiB, 4 KiB, 50, 64, 256, 1024 | Per-message safety caps. Keep the defaults on a public relay. |
| `HIVELANE_MAX_PENDING_JOINS`, `HIVELANE_MAX_TRACKED_DECISIONS`, `HIVELANE_MAX_KNOWN_PATHS`, `HIVELANE_FILE_QUEUE_MAX_LENGTH`, `HIVELANE_FILE_QUEUE_MAX_FILES` | 10, 1000, 512, 0, 256 | Per-room bookkeeping bounds. |
| `HIVELANE_HISTORY_MAX_EVENTS`, `HIVELANE_HISTORY_MAX_BYTES`, `HIVELANE_HISTORY_TOTAL_MAX_BYTES` | 5000, 8 MiB, 128 MiB | Replay history bounds (per room, process-wide). |
| `HIVELANE_MAX_WEBHOOKS_PER_ROOM`, `HIVELANE_WEBHOOK_MAX_IN_FLIGHT`, `HIVELANE_WEBHOOK_MAX_TRACKED_ROOMS` | 3, 100, 1024 | Webhook bounds (webhooks per room is at least 1). |
| `HIVELANE_REDIS_URL` | — | `redis://`, `rediss://` or `unix://` URL. Shares rate-limit counters, room codes and room-ownership leases across relay instances. Needs the `redis` package (included in the Docker image). |
| `HIVELANE_ROOM_STORE` | — | Set to `local` to keep room codes in SQLite/memory even when `HIVELANE_REDIS_URL` is set (Redis for rate limits only). |
| `HIVELANE_INSTANCE_ID` | `FLY_MACHINE_ID`, else hostname-pid | This instance's id in room-ownership leases. |
| `HIVELANE_STATIC_DIR` | — (`/app/static` in the image) | Built dashboard to serve from the relay, same-origin, so the bundled dashboard needs no CORS entry. |
| `GEMINI_API_KEY` | — | Enables AI captions and conflict explanations |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model override |
| `GEMINI_CAPTION_TIMEOUT` | `4.0` | Seconds before caption fallback |
| `GEMINI_CONFLICT_TIMEOUT` | `5.0` | Seconds before heuristic conflict label |

> **`TRUST_PROXY_HEADER` cuts both ways.** Behind Render or Cloudflare the socket
> peer is the proxy, so with it off every user shares one rate-limit bucket. With
> it on but no proxy in front, a client can spoof `X-Forwarded-For` and bypass the
> limits. Set it, and `TRUSTED_PROXY_HOPS`, to match your actual topology; see
> [docs/security.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/security.md).

> Rate limits are **per process** unless `HIVELANE_REDIS_URL` is set. Run more
> than one relay instance without it and each one grants the full budget. If
> Redis becomes unreachable the relay logs a warning and falls back to
> per-process limits rather than refusing traffic.

### Dashboard (`frontend/`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_WS_URL` | `ws://localhost:8000/ws` | Relay WebSocket **base** (no room code). Room is appended as `/ws/{ROOM}`. |

---

## Hivelane CLI

```text
hivelane up          set up this repo: hooks, room, background daemon (start here)
hivelane down        stop the background daemon (hooks stay installed)
hivelane leave       disconnect this repo from its room (--forget: also drop it from .hivelane.toml)
hivelane status      show the daemon and its connected repos
hivelane doctor      check the setup and say how to fix what is wrong
hivelane open        open this repo's room in the dashboard
hivelane serve       run the multi-repo daemon in the foreground
hivelane service     install | uninstall: start the daemon automatically at login
hivelane join ROOM   run a single-room daemon in the foreground (advanced)
hivelane hook ...    agent hook entrypoint, and install | uninstall for Claude Code
hivelane --version
```

Every flag, `.hivelane.toml` and the personal config: [docs/cli.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/cli.md).

---

## HTTP & WebSocket API

### HTTP

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/rooms` | Create a room → `{ "room_id": "K7M2PQ" }` |
| `GET` | `/health` | Liveness → `{ "status": "ok", "rooms": N }` |
| `GET` | `/docs` | OpenAPI / Swagger UI |

### WebSocket

Connect to:

```text
ws://<host>/ws/{room_id}
```

**Join (first message)**

```json
{ "type": "join", "name": "adrian", "agent": "claude-code" }
```

`agent` is one of `cursor | claude-code | codex | antigravity | gemini-cli | devin | grok | unknown`.

Spectator (dashboard):

```json
{ "type": "join", "name": null, "role": "spectator" }
```

**Activity (agents)**

```json
{
  "type": "activity",
  "user_id": "adrian",
  "prompt": "Refactor the WebSocket handler",
  "agent": "claude-code",
  "changes": [
    { "file_path": "server/main.py", "diff": "@@ ..." }
  ]
}
```

**Intent (agents)** — what this agent is currently trying to do, declared up front:

```json
{
  "type": "intent",
  "user_id": "adrian",
  "prompt": "Refactor the WebSocket handler"
}
```

**Edit check (agents)** — asked before writing a file; `request_id` correlates the reply on a blocking hook:

```json
{
  "type": "edit_check",
  "request_id": "c3f1a2",
  "file_path": "server/room.py",
  "symbol": "Room.broadcast"
}
```

The relay answers point-to-point and immediately — never through the debounced feed, because the agent is blocked waiting on it:

```json
{
  "type": "edit_check_response",
  "request_id": "c3f1a2",
  "decision": "block",
  "file_path": "server/room.py",
  "others": [
    { "user_id": "sam", "agent": "claude-code", "seconds_ago": 12.4, "prompt": "Rewrite broadcast fan-out" }
  ],
  "reason": "sam's agent is editing this file right now"
}
```

`decision` is `allow`, `warn`, or `block`; a Claude Code `PreToolUse` hook maps `block` to a denied tool call.

**Server broadcasts:** batched as `hivelane_feed` every ~500ms (`events`: `user_joined`, `user_left`, `activity_update`, `intent_update`, `conflict_alert`, `edit_decision`). Point-to-point messages (`edit_check_response`, `name_rejected`, spectator catch-up) still send immediately.

`intent_update` carries the latest intent per user — newest wins — and is replayed to spectators on connect, like `activity_update`:

```json
{
  "type": "intent_update",
  "user_id": "adrian",
  "agent": "claude-code",
  "prompt": "Refactor the WebSocket handler",
  "timestamp": 1730000000.0
}
```

`edit_decision` records a pre-edit check that came back `warn` or `block`. Allowed edits are not broadcast, so a busy agent's file probes don't drown the dashboard:

```json
{
  "type": "edit_decision",
  "user_id": "adrian",
  "agent": "claude-code",
  "file_path": "server/room.py",
  "decision": "block",
  "others": ["sam"],
  "reason": "sam's agent is editing this file right now",
  "timestamp": 1730000000.0
}
```

Room codes are the shared secret — anyone with the code can join.

### Protocol version

The join frame carries `protocol_version`. A daemon older than the relay's
`MIN_SUPPORTED_PROTOCOL` is refused with `version_rejected` and told to upgrade,
because such a client would connect happily and simply never send `edit_check` —
conflict prevention would be silently off. Spectators are exempt: the dashboard
only consumes broadcasts. `/readyz` reports both numbers. When the wire contract
changes incompatibly, bump `PROTOCOL_VERSION` in **both** `server/models.py` and
`hivelane/protocol.py`; a test fails if they drift.

### Logs

Human-readable in development, JSON lines everywhere else, so stdout can be
picked up by an aggregator without re-instrumenting anything.

---

## Develop locally

Run all three pieces from a checkout. You need Python **3.12+**, Node.js
**18+**, and optionally a [Gemini API key](https://aistudio.google.com/apikey)
for live captions and smarter conflict labels.

### 1. Relay server

From the **repo root**:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt

cp .env.example .env               # optional — AI captions / smarter conflicts
# edit .env → set GEMINI_API_KEY=

uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check: [http://localhost:8000/health](http://localhost:8000/health)

### 2. Dashboard

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173) → **Create workspace** (or join with a room code).

Point the frontend at a remote relay by setting:

```bash
# frontend/.env.local
VITE_WS_URL=wss://your-relay.example.com/ws
```

### 3. The CLI from your checkout

```bash
pip install -e ".[dev]"            # in the venv above; or: uv tool install --editable .
cd path/to/a/test-repo
hivelane up                         # the default relay is the local one from step 1
```

`./setup.sh` (or `setup.ps1` on Windows) does the venv and editable install in
one step and enables the repo's git hooks. Tests: `pip install -r
server/requirements-dev.txt` then `pytest -q`.

---

## Deployment

### Environments

| Environment | Deploys from | Gate |
|-------------|--------------|------|
| `Staging` | pushes to `main` | CI must pass |
| `Production` | `v*` tags | CI must pass; tagging is the deliberate step |

`.github/workflows/deploy.yml` runs when `ci.yml` completes successfully for a
push to `main` or a `v*` tag, so nothing deploys on code that has not passed
tests and the suite is not run a second time. After POSTing the deploy hook it
waits until the relay's `/readyz` reports the deployed commit (`DEPLOY_HOOK_SENDS_REF=true`
as a repository variable makes Render build that exact commit). Required-reviewer gates need a paid GitHub plan on private
repos, so the tag stands in for approval — cut a release with `git tag v1.2.3 &&
git push origin v1.2.3`.

Deploys are host-agnostic: each environment supplies a deploy-hook URL as a
secret and `scripts/deploy_hook.sh` POSTs it (Render, Cloudflare Pages and
Netlify all offer these). Steps skip with a notice when unconfigured.

| Scope | Name | Kind | Purpose |
|-------|------|------|---------|
| Environment secret | `RELAY_DEPLOY_HOOK_URL` | secret | Triggers the relay deploy |
| Environment secret | `DASHBOARD_DEPLOY_HOOK_URL` | secret | Triggers the dashboard deploy |
| Environment variable | `RELAY_URL` | variable | Base URL polled at `/readyz` after deploy |

### Releasing the CLI

The same `v*` tag also runs `.github/workflows/release.yml`, independently of
the deploy. It checks that the tag matches `hivelane/__init__.py`'s
`__version__` (the single source of the version), builds the sdist and wheel,
runs the tests, smoke-installs the wheel on Linux, macOS and Windows, then:

1. publishes to PyPI through trusted publishing (no API token);
2. creates the GitHub release with the artifacts and `SHA256SUMS`;
3. bumps the Homebrew tap (stable releases only).

So a release is: bump `__version__`, merge, `git tag vX.Y.Z && git push origin
vX.Y.Z`. Run the workflow by hand (`workflow_dispatch`) for a dry run that
builds and smoke-tests without publishing.

| Where | Name | Purpose |
|-------|------|---------|
| GitHub environment | `pypi` | Trusted-publishing gate. Register the publisher on PyPI: owner `AdrianShah`, repo `Hivelane`, workflow `release.yml`, environment `pypi`. |
| Repository secret | `HOMEBREW_TAP_TOKEN` | Optional. Push access to `AdrianShah/homebrew-hivelane`; without it the tap bump is skipped. See [packaging/homebrew](https://github.com/AdrianShah/Hivelane/blob/main/packaging/homebrew/README.md). |

The Scoop bucket follows PyPI on its own (`checkver`/`autoupdate`), and the npm
launcher always runs the latest release; see [packaging/](https://github.com/AdrianShah/Hivelane/tree/main/packaging).

### Container

The relay ships as an image so the host stays a choice:

```bash
docker build -t hivelane-relay .
docker run -p 8000:8000 -v hivelane-data:/data \
  -e HIVELANE_ENV=production -e CORS_ORIGINS=https://your-dashboard \
  hivelane-relay
```

`$PORT` is honoured when the host injects one, and `/data` is where room codes
persist — mount a volume or they die with the container. CI builds and boots
this image on every push, so it cannot rot.

Ready-made setups for Docker Compose (with Caddy for automatic HTTPS), Fly.io
and Render live in [`deploy/`](https://github.com/AdrianShah/Hivelane/tree/main/deploy); the walkthrough is
[docs/self-hosting.md](https://github.com/AdrianShah/Hivelane/blob/main/docs/self-hosting.md).

### Relay

- Python **3.12** (`server/runtime.txt`)
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT` (with `rootDir=server`) or `uvicorn server.main:app` from repo root
- Set `CORS_ORIGINS` to your dashboard origin and `HIVELANE_ENV` to `staging`/`production`
- Set `HIVELANE_STATE_PATH` to a file on a persistent disk, or every deploy invalidates all room codes
- Turn on `TRUST_PROXY_HEADER` if the host terminates TLS in front of you
- Expose WebSockets (`wss://…/ws/{room}`), and pick a host that does **not** sleep — an idle-suspended relay drops every connection
- Run **one** instance: rooms live in the relay process's memory
- `/health` is liveness; `/readyz` reports configuration readiness

### Dashboard

- Root: `frontend/`
- Build: `npm run build`
- Env: `VITE_WS_URL=wss://<your-relay>/ws`

### Local tunnel (demo)

```bash
ngrok http 8000
# Use the https URL as wss://…/ws for VITE_WS_URL and `hivelane up --relay`
```

---

## Project layout

```text
├── server/          # FastAPI relay + Gemini helpers
├── hivelane/         # The `hivelane` CLI: hooks, multi-repo daemon, doctor
├── frontend/        # Hivelane dashboard (React + Vite)
├── deploy/          # Relay deployment: compose + Caddy, Fly.io, Render
├── packaging/       # Homebrew formula, Scoop manifest, npm launcher
├── docs/            # Install, CLI reference, self-hosting
├── scripts/         # install.sh / install.ps1, smoke and deploy helpers
├── examples/        # demo_proj/: tiny sample tree for demos
├── tests/e2e/       # Relay + two daemons in one process, cross-cwd collision
├── Dockerfile       # The relay image
├── pyproject.toml   # Packages the `hivelane` CLI (version from hivelane/__init__.py)
├── .env.example
└── README.md
```

---

## Demo tips

1. Run `hivelane up` in the demo repo and commit the `.hivelane.toml` it writes.
2. On a second machine (or as a second user), pull and run `hivelane up`. Same room, no code to type.
3. Point both agents at the **same file**: the second one gets warned or blocked, and its lane shows the decision badge.
4. `hivelane open` brings up the room's dashboard.
5. Without `GEMINI_API_KEY`, captions fall back to placeholders and conflicts use heuristics, so the demo still works.

---

## License

Apache-2.0. See [LICENSE](https://github.com/AdrianShah/Hivelane/blob/main/LICENSE)
and [NOTICE](https://github.com/AdrianShah/Hivelane/blob/main/NOTICE).
