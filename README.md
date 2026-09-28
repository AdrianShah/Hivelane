# Synergo

**Live multiplayer AI agent viewer for hackathons and parallel coding sessions.**

Synergo streams agent intent, prompts, AI captions, and file diffs from every teammate into one shared board — and answers each agent's "is anyone else in this file?" before the edit lands.

---

## Why Synergo

When several people (or several AI agents) work in the same repo at once, you usually find out about collisions at PR time. Synergo turns that into a glanceable, real-time signal:

- **Per-agent lanes** — declared intent, prompts, captions, and expandable git-style diffs
- **Pre-edit checks** — an agent asks the relay before it writes and is warned, or blocked, when a teammate is already in that file
- **Conflict radar** — overlapping file edits called out within a short window
- **Shared rooms** — one code to join; no accounts, Docs-style trust model

---

## Architecture

```
┌─────────────┐  intent / edit_check  ┌──────────────────┐     broadcast      ┌──────────────┐
│    Daemon   │ ────────────────────► │  Synergo Relay   │ ─────────────────► │   Dashboard  │
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
| **`server/`** | In-memory room relay. Accepts agents + spectators, broadcasts activity, answers pre-edit checks, runs Gemini captioning and conflict classification. |
| **`synergo/`** | Local CLI. Installs agent hooks, runs the daemon that holds the room connection, and answers the hooks on `127.0.0.1:4600`. |
| **`frontend/`** | Spectator dashboard. Create/join rooms, live lanes, conflict banner, analytics. |

---

## Quick start

### Prerequisites

- Python **3.12+**
- Node.js **18+**
- (Optional) [Gemini API key](https://aistudio.google.com/apikey) for live captions and smarter conflict labels

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

### 3. Synergo CLI (on each contributor machine)

From the repo you are working in:

```bash
uv tool install .                   # or: pip install -e .

synergo hook install claude-code    # writes hook entries into .claude/settings.json
synergo join K7M2PQ --name adrian   # create the room in the UI first
```

`synergo join` runs a local daemon that holds the relay connection for this repo; the installed hooks talk to it on `127.0.0.1:4600`, so the agent's intent and pre-edit checks reach the room without a second terminal. Useful flags: `--server ws://localhost:8000/ws`, `--agent claude-code`, `--port 4600`. Run `synergo status` to see whether the daemon is up and which room it joined.

Hooks replaced the file watcher because a watcher can only notice a collision after both agents have already written to the file. Now the agent declares what it is working on and asks before each edit, so Synergo can warn it — or block the write outright — and the conflict is prevented rather than reported.

**Do not tunnel or publicly expose port 4600.**

---

## Environment variables

### Relay (`server/`)

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Enables AI captions and conflict explanations |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model override |
| `GEMINI_CAPTION_TIMEOUT` | `4.0` | Seconds before caption fallback |
| `GEMINI_CONFLICT_TIMEOUT` | `5.0` | Seconds before heuristic conflict label |
| `CORS_ORIGINS` | permissive / configured | Comma-separated allowed origins (set in production) |

### Dashboard (`frontend/`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_WS_URL` | `ws://localhost:8000/ws` | Relay WebSocket **base** (no room code). Room is appended as `/ws/{ROOM}`. |

---

## Synergo CLI

```text
synergo hook install AGENT   Write hook entries into .claude/settings.json
                             AGENT: claude-code

synergo join ROOM            Run the local daemon for this repo
  --name NAME                Display name in the room (prompted if omitted)
  --server URL               Relay WS base, e.g. ws://localhost:8000/ws
  --agent KIND               cursor | claude-code | codex | antigravity |
                             gemini-cli | devin | grok | unknown
  --port PORT                Local daemon port the hooks call (default: 4600)

synergo status               Inspect the running daemon (room, agent, port)
```

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

**Server broadcasts:** batched as `synergo_feed` every ~500ms (`events`: `user_joined`, `user_left`, `activity_update`, `intent_update`, `conflict_alert`, `edit_decision`). Point-to-point messages (`edit_check_response`, `name_rejected`, spectator catch-up) still send immediately.

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

---

## Deployment

### Relay (e.g. Render)

- Python **3.12** (`server/runtime.txt`)
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT` (with `rootDir=server`) or `uvicorn server.main:app` from repo root
- Set `GEMINI_API_KEY` and `CORS_ORIGINS` to your Vercel origin
- Expose WebSockets (`wss://…/ws/{room}`)

### Dashboard (e.g. Vercel)

- Root: `frontend/`
- Build: `npm run build` (see `frontend/vercel.json`)
- Env: `VITE_WS_URL=wss://<your-relay>/ws`

### Local tunnel (demo)

```bash
ngrok http 8000
# Use the https URL as wss://…/ws for VITE_WS_URL and synergo join --server
```

---

## Project layout

```text
├── server/          # FastAPI relay + Gemini helpers
├── synergo/         # CLI: hook installer, local daemon, status
├── frontend/        # Synergo dashboard (React + Vite)
├── demo_proj/       # Tiny sample tree for demos
├── pyproject.toml   # Installs the `synergo` CLI
├── .env.example
└── README.md
```

---

## Demo tips

1. Create a workspace in the UI and copy the room code.
2. Run `synergo hook install claude-code` then `synergo join CODE --name …` on two machines with different names.
3. Point both agents at the **same file** — the second one gets warned or blocked, and its lane shows the decision badge.
4. Without `GEMINI_API_KEY`, captions fall back to placeholders and conflicts use heuristics so the demo still works.

---

## License

Hackathon project — use and modify freely for your team.
