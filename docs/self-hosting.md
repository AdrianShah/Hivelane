# Self-hosting the relay

The relay (`server/`) is one small FastAPI process. It holds rooms in
memory and keeps the list of valid room codes in a SQLite file. Everything
here deploys the repo's `Dockerfile`, which CI builds and boots on every
change.

| Option | Good for | Files |
|--------|----------|-------|
| [Docker](#docker) | Any host that runs containers | `Dockerfile` |
| [Docker Compose + Caddy](#docker-compose--caddy) | Your own VM, with automatic HTTPS | `deploy/compose/` |
| [Fly.io](#flyio) | Managed, always-on, global edge | `deploy/fly/` |
| [Render](#render) | Managed, Git-driven | `deploy/render/` |

## What every deployment needs

- **`SYNERGO_ENV=production`** and **`CORS_ORIGINS`**: the exact origin(s) of
  the dashboard that will call this relay, comma-separated. Outside development
  the relay refuses to boot without `CORS_ORIGINS`.
- **A persistent disk at `/data`** (`SYNERGO_STATE_PATH` points into it). The
  disk keeps room codes valid across restarts and deploys. Without it, every
  restart sends every team back to `synergo up` with a new room.
- **WebSockets over TLS.** Clients connect to `wss://<host>/ws/<ROOM>`.
- **A host that never sleeps.** An idle-suspended relay drops every daemon's
  connection. Pre-edit checks fail open, so agents keep working, but
  uncoordinated.
- **Exactly one instance.** Rooms live in the relay process's memory. Two
  instances behind a load balancer would split a team between them. Scale up,
  not out.

Optional: `GEMINI_API_KEY` for AI captions and conflict summaries. Every
variable is listed in the [README](../README.md#relay-server).

## Connecting clients

Once `https://<host>/readyz` answers:

```bash
# CLI: the first teammate records the relay in .synergo.toml for everyone
synergo up --relay wss://relay.example.com/ws
# or per machine, for every repo
export SYNERGO_RELAY=wss://relay.example.com/ws
```

The dashboard is a static build: set `VITE_WS_URL=wss://relay.example.com/ws`
when building `frontend/` (Vercel config: `frontend/vercel.json`). If you host
your own dashboard, also set `SYNERGO_DASHBOARD=https://dashboard.example.com`
for the CLI, so `synergo open` and the links it prints point at it. Then add
that dashboard's origin to the relay's `CORS_ORIGINS`.

## Docker

```bash
docker build -t synergo-relay .
docker run -d --name synergo-relay --restart unless-stopped \
  -p 8000:8000 -v synergo-data:/data \
  -e SYNERGO_ENV=production \
  -e CORS_ORIGINS=https://dashboard.example.com \
  synergo-relay
curl http://localhost:8000/readyz
```

The container serves plain HTTP on `$PORT` (default 8000), so put a TLS proxy
in front of it. The Compose setup below does exactly that.

## Docker Compose + Caddy

`deploy/compose/` runs the relay behind [Caddy](https://caddyserver.com/),
which obtains and renews a certificate for your domain by itself.

1. Point a DNS name (say `relay.example.com`) at the server, and open ports 80
   and 443.
2. Configure and start:

   ```bash
   cd deploy/compose
   cp .env.example .env     # set RELAY_DOMAIN and CORS_ORIGINS
   docker compose up -d --build
   docker compose logs -f caddy   # watch the certificate being issued
   ```

3. Check `https://relay.example.com/readyz`.

To upgrade, run `git pull && docker compose up -d --build`. Room codes are
kept in the `relay-data` volume, and certificates in `caddy-data`.

**Optional Redis.** Setting `COMPOSE_PROFILES=redis` and
`SYNERGO_REDIS_URL=redis://redis:6379/0` in `.env` adds a Redis container
and moves the rate-limit counters into it (the image includes the `redis`
client). This only pays off once several relay instances share one budget.
With the single instance the relay needs today, in-process counters work the
same. If Redis goes away, the relay logs a warning and falls back to them.

For a local trial without a domain, set `RELAY_DOMAIN=localhost`; Caddy then
uses its own local CA.

## Fly.io

`deploy/fly/fly.toml` runs one always-on machine with a 1 GB volume. Health
checks hit `/health` and `/readyz`, and connection limits are sized for
long-lived WebSockets. In short, from the repo root:

```bash
fly apps create my-synergo-relay            # and set `app` in fly.toml to match
fly volumes create synergo_data --region iad --size 1 --yes --config deploy/fly/fly.toml
fly secrets set --stage --config deploy/fly/fly.toml CORS_ORIGINS=https://dashboard.example.com
fly deploy --config deploy/fly/fly.toml
```

Full steps, custom domains and CI deploys: [deploy/fly/README.md](../deploy/fly/README.md).
The config includes a two-region example, commented out on purpose. Read the
note before enabling it.

## Render

`deploy/render/render.yaml` is a Blueprint: one Docker web service with a 1 GB
disk, health-checked on `/readyz`. Create a Blueprint from the repo with
**Blueprint Path** `deploy/render/render.yaml`. Render then prompts for
`CORS_ORIGINS` (and optionally `GEMINI_API_KEY`). Details:
[deploy/render/README.md](../deploy/render/README.md). A disk needs a paid
instance; the free tier also sleeps.

## Wiring a host into CI

`.github/workflows/deploy.yml` deploys staging on pushes to `main` and
production on `v*` tags, after CI passes, by POSTing a host's deploy-hook URL
(`RELAY_DEPLOY_HOOK_URL`, set per GitHub environment). It then polls
`RELAY_URL/readyz`. Render provides deploy hooks. Fly needs a `flyctl deploy`
step instead (see the Fly README). A Compose host can expose a small webhook,
or you can deploy it by hand.

## Client IPs behind a proxy

Rate limits (room creation, WebSocket attempts) are per client IP. Behind a
TLS proxy the relay only sees the proxy's address, unless
`TRUST_PROXY_HEADER=true` tells it to read `X-Forwarded-For`. It uses the
**leftmost** entry.

- **Caddy (Compose):** safe. By default Caddy discards a client-sent
  `X-Forwarded-For` and sets the real address.
- **Fly.io, Render, Cloudflare:** these append the real address to whatever the
  client sent. Leftmost-wins lets a client that forges the header pick its own
  bucket. The configs here still turn the header on, because leaving it off
  puts every user in one bucket (10 room creations an hour, for everyone). The
  limits slow casual abuse; they don't stop a determined client.

## Operating it

- **Health:** `/health` (liveness, room count) and `/readyz` (environment, CORS,
  protocol versions). `/metrics` serves Prometheus text with aggregate counts only.
- **Logs:** JSON lines on stdout outside development.
- **Upgrades:** deploy a new image. Daemons reconnect within seconds and
  rebuild room state from live activity. If a release raises the relay's
  minimum protocol version, older daemons are refused with an upgrade message
  (`/readyz` reports both numbers, and `synergo doctor` compares them), so
  tell teams to upgrade the CLI at the same time.
- **Backups:** only `/data` matters, and only for room codes (seven-day TTL).
  Losing it means teams run `synergo up` again and commit a new
  `.synergo.toml`.
