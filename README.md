# Synergo

**Live multiplayer AI agent viewer for hackathons and parallel coding sessions.**

Synergo streams IDE activity, prompts, AI captions, and file diffs from every teammate into one shared board — and flags overlapping edits before they become merge conflicts.

---

## Why Synergo

When several people (or several AI agents) work in the same repo at once, you usually find out about collisions at PR time. Synergo turns that into a glanceable, real-time signal:

- **Per-agent lanes** — prompts, captions, and expandable git-style diffs
- **Conflict radar** — overlapping file edits called out within a short window
- **Shared rooms** — one code to join; no accounts, Docs-style trust model
- **Optional companion** — start watchers from the dashboard without a terminal

---

## Architecture

```
┌─────────────┐     activity       ┌──────────────────┐     broadcast      ┌──────────────┐
│   Watcher   │ ─────────────────► │  Synergo Relay   │ ─────────────────► │   Dashboard  │
│  (per laptop)│   WebSocket       │  FastAPI + rooms │   WebSocket        │  React / Vite│
└─────────────┘                    └────────┬─────────┘                    └──────────────┘
       ▲                                    │
       │ spawn (optional)                   │ Gemini captions / conflicts
┌──────┴──────┐                             ▼
│  Companion  │                      (best-effort AI layer)
│  :4500 only │
└─────────────┘
```

| Component | Role |
|-----------|------|
| **`server/`** | In-memory room relay. Accepts watchers + spectators, broadcasts activity, runs Gemini captioning and conflict classification. |
| **`watcher/`** | Local CLI. Watches a project folder, batches saves (~1.5s debounce), sends unified diffs + prompts. |
| **`frontend/`** | Spectator dashboard. Create/join rooms, live lanes, conflict banner, analytics. |
| **`companion/`** | Localhost-only helper that starts/stops watchers for the UI. Never expose publicly. |

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

### 3. Watcher (on each contributor machine)

```bash
cd watcher
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a room in the UI first, then:
python watcher.py \
  --path /path/to/project \
  --name adrian \
  --room K7M2PQ \
  --server ws://localhost:8000/ws \
  --agent cursor
```

Save a file under `--path`. Within ~1.5s it should appear in that user’s lane on the dashboard.

### 4. Companion (optional)

Lets the dashboard start watchers for you:

```bash
./setup.sh                         # macOS / Linux
# powershell -ExecutionPolicy Bypass -File setup.ps1   # Windows

python -m companion.main           # http://127.0.0.1:4500 only
```

See [`companion/README.md`](companion/README.md) for security notes and the full local API. **Do not tunnel or publicly expose port 4500.**

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

### Companion

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPANION_PORT` | `4500` | Local bind port (`127.0.0.1` only) |
| `COMPANION_CORS_ORIGINS` | — | Extra production origins allowed to call the companion |

---

## Watcher CLI

```text
python watcher/watcher.py
  --path PATH          Project directory to watch (default: .)
  --server URL         Relay WS base, e.g. ws://localhost:8000/ws
  --room CODE          Room id from POST /rooms → connects to /ws/{CODE}
  --name NAME          Display name (prompted if omitted)
  --agent KIND         cursor | claude-code | codex | unknown
  --prompt TEXT        Override prompt for every batch; empty string → null
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
{ "type": "join", "name": "adrian", "agent": "cursor" }
```

Spectator (dashboard):

```json
{ "type": "join", "name": null, "role": "spectator" }
```

**Activity (watchers)**

```json
{
  "type": "activity",
  "user_id": "adrian",
  "prompt": "Refactor the WebSocket handler",
  "agent": "cursor",
  "changes": [
    { "file_path": "server/main.py", "diff": "@@ ..." }
  ]
}
```

**Server broadcasts:** batched as `synergo_feed` every ~500ms (`events`: `user_joined`, `user_left`, `activity_update`, `conflict_alert`). Point-to-point messages (`name_rejected`, spectator catch-up) still send immediately.

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
# Use the https URL as wss://…/ws for VITE_WS_URL and watcher --server
```

---

## Project layout

```text
├── server/          # FastAPI relay + Gemini helpers
├── watcher/         # File watcher CLI
├── frontend/        # Synergo dashboard (React + Vite)
├── companion/       # Optional localhost watcher launcher
├── demo_proj/       # Tiny sample tree for demos
├── setup.sh         # Install companion + watcher deps
├── .env.example
└── README.md
```

---

## Demo tips

1. Create a workspace in the UI and copy the room code.
2. Start two watchers with different `--name` values on the same `--room`.
3. Edit the **same file** from both sides within ~60–120s — the conflict banner should fire.
4. Without `GEMINI_API_KEY`, captions fall back to placeholders and conflicts use heuristics so the demo still works.

---

## License

Hackathon project — use and modify freely for your team.
