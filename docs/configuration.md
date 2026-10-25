# Configuration

Every environment variable read by the CLI, the relay and the dashboard.

## CLI (`hivelane`)

Flags win over these, and these win over `~/.hivelane/config.toml` and the repo's
`.hivelane.toml`. The room and conflict windows are the exception: those come
from `.hivelane.toml`, so one person's settings cannot split them from the team.
Full precedence and every command: [docs/cli.md](cli.md).

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVELANE_RELAY` | `ws://localhost:8000/ws` | Relay WebSocket base used when no `--relay` is given. |
| `HIVELANE_DASHBOARD` | local dev server for a local relay, else `https://adrianshah.github.io/Hivelane` | Dashboard base for room links (`<dashboard>/r/<CODE>`) and `hivelane open`. |
| `HIVELANE_NAME` | git `user.name`, else the OS user | Your display name in rooms. |
| `HIVELANE_PORT` | `4600` | Loopback port the daemon listens on and the hooks call. |
| `HIVELANE_HOME` | `~/.hivelane` | Personal config, daemon state and logs (`logs/daemon.log`). |
| `HIVELANE_OUTBOX_LIMIT` | `500` | Messages the daemon keeps per room while the relay is unreachable (oldest dropped). `0` = unlimited. |

## Relay (`server/`)

| Variable | Default | Description |
|----------|---------|-------------|
| `HIVELANE_ENV` | `development` | `development` \| `staging` \| `production`. Outside development the relay refuses to start without `CORS_ORIGINS`. |
| `CORS_ORIGINS` | localhost dev server | Comma-separated exact origins. **Required** in staging/production. |
| `CORS_ORIGIN_REGEX` | - | Opt-in wildcard for preview deploys. Empty by default; a regex here widens who can call the relay. |
| `HIVELANE_STATE_PATH` | - (`/data/hivelane.db` in the image) | SQLite file on a persistent disk. Without it, room codes live only in memory and every deploy invalidates them. |
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
| `HIVELANE_REDIS_URL` | - | `redis://`, `rediss://` or `unix://` URL. Shares rate-limit counters, room codes and room-ownership leases across relay instances. Needs the `redis` package (included in the Docker image). |
| `HIVELANE_ROOM_STORE` | - | Set to `local` to keep room codes in SQLite/memory even when `HIVELANE_REDIS_URL` is set (Redis for rate limits only). |
| `HIVELANE_INSTANCE_ID` | `FLY_MACHINE_ID`, else hostname-pid | This instance's id in room-ownership leases. |
| `HIVELANE_STATIC_DIR` | - (`/app/static` in the image) | Built dashboard to serve from the relay, same-origin, so the bundled dashboard needs no CORS entry. |
| `GEMINI_API_KEY` | - | Enables AI captions and conflict explanations |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model override |
| `GEMINI_CAPTION_TIMEOUT` | `4.0` | Seconds before caption fallback |
| `GEMINI_CONFLICT_TIMEOUT` | `5.0` | Seconds before heuristic conflict label |

> **`TRUST_PROXY_HEADER` cuts both ways.** Behind Render or Cloudflare the socket
> peer is the proxy, so with it off every user shares one rate-limit bucket. With
> it on but no proxy in front, a client can spoof `X-Forwarded-For` and bypass the
> limits. Set it, and `TRUSTED_PROXY_HOPS`, to match your actual topology; see
> [docs/security.md](security.md).

> Rate limits are **per process** unless `HIVELANE_REDIS_URL` is set. Run more
> than one relay instance without it and each one grants the full budget. If
> Redis becomes unreachable the relay logs a warning and falls back to
> per-process limits rather than refusing traffic.

## Dashboard (`frontend/`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_WS_URL` | `ws://localhost:8000/ws` | Relay WebSocket **base** (no room code). Room is appended as `/ws/{ROOM}`. |
