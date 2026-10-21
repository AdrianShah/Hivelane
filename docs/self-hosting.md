# Self-hosting the relay

The relay (`server/`) is one small FastAPI process. It holds rooms in memory,
keeps the list of valid room codes in a SQLite file, and serves the dashboard
from the same address. Every option below runs the same code: the repo's
`Dockerfile`, published as `ghcr.io/adrianshah/synergo-relay`, or
`synergo relay` straight from the CLI.

| Option | Good for | Files |
|--------|----------|-------|
| [`synergo relay`](#on-your-lan-synergo-relay) | A LAN, a hackathon, trying it out | `synergo/cli.py` |
| [Docker](#docker) | Any host that runs containers | `Dockerfile` |
| [Docker Compose + Caddy](#docker-compose--caddy) | Your own VM, with automatic HTTPS | `deploy/compose/` |
| [Fly.io](#flyio) | Managed, always-on, several regions if you want | `deploy/fly/` |
| [Render](#render) | Managed, Git-driven | `deploy/render/` |
| [Railway](#railway) | Managed, Git-driven | `deploy/railway/` |
| [Kubernetes (Helm)](#kubernetes-helm) | A cluster you already run | `deploy/helm/synergo-relay/` |

## What every deployment needs

- **`SYNERGO_ENV=production`** and **`CORS_ORIGINS`**. Outside development the
  relay refuses to boot without `CORS_ORIGINS`. List the exact origin(s) of
  any dashboard hosted *elsewhere*, comma-separated. The bundled dashboard is
  served from the relay's own origin, so it needs no entry; if it is your only
  dashboard, list the relay's own URL (`https://relay.example.com`).
- **A persistent disk at `/data`** (`SYNERGO_STATE_PATH` points into it). The
  disk keeps room codes valid across restarts and deploys. Without it, every
  restart sends every team back to `synergo up` with a new room.
- **WebSockets over TLS.** Clients connect to `wss://<host>/ws/<ROOM>`. The CLI
  refuses a plain `ws://` relay on another machine unless you pass
  `--insecure`.
- **A host that never sleeps.** An idle-suspended relay drops every daemon's
  connection. Pre-edit checks fail open, so agents keep working, but
  uncoordinated.
- **One instance, unless you set up [scaling out](#scaling-out).** Rooms live
  in the relay process's memory, so several instances need Redis and routing
  by room code.

Optional: `GEMINI_API_KEY` for AI captions and conflict summaries. Every
variable is listed in the [README](../README.md#relay-server); the ones for
hosting are [below](#environment-variables-for-hosting).

## Connecting clients

Once `https://<host>/readyz` answers:

```bash
# CLI: the first teammate records the relay in .synergo.toml for everyone
synergo up --relay wss://relay.example.com/ws
# or per machine, for every repo
export SYNERGO_RELAY=wss://relay.example.com/ws
```

The dashboard is at `https://relay.example.com/`. Set
`SYNERGO_DASHBOARD=https://relay.example.com` for the CLI too, so `synergo open`
and the links it prints point at it.

## The bundled dashboard

The image builds `frontend/` and serves it at `/`. Any path that is not an API
route or a file gets `index.html`, so room links (`/r/<code>`) work. The API
always wins: `/ws`, `/rooms`, `/health`, `/readyz` and `/metrics` are never
answered with the dashboard.

A dashboard built without `VITE_WS_URL` connects to the relay on the origin it
was loaded from (`wss://` on https, `ws://` on http). Its requests are then
same-origin, so CORS does not apply to them. The exceptions are `npm run dev`
and `vite preview`, which use `ws://localhost:8000/ws`.

- **Serve the API only:** set `SYNERGO_STATIC_DIR=` (empty).
- **Host the dashboard elsewhere** (Vercel config: `frontend/vercel.json`):
  build it with `VITE_WS_URL=wss://relay.example.com/ws` and add its origin to
  the relay's `CORS_ORIGINS`.
- **Bake a different relay into the image's dashboard:**
  `docker build --build-arg VITE_WS_URL=wss://relay.example.com/ws .`

## On your LAN: `synergo relay`

For a hackathon table or an office network, run the relay from the CLI. It
needs the `relay` extra:

```bash
pipx install "synergo[relay]"         # or: pip install "synergo[relay]"
synergo relay --state relay.db
```

```text
synergo relay: listening on 0.0.0.0:8000 (Ctrl+C to stop)
  teammates run:  synergo up --relay ws://192.168.1.23:8000/ws --insecure
                  (--insecure: ws:// is unencrypted, so only on a network you trust;
                  for TLS, put the relay behind Caddy and use wss://, see docs/self-hosting.md)
  dashboard:      http://192.168.1.23:8000  (served from .../frontend/dist)
  room codes:     saved in /home/ada/relay.db
```

- `--host` (default `0.0.0.0`, every interface) and `--port` (default `8000`).
  `--host 127.0.0.1` keeps it to your own machine.
- `--state PATH` keeps room codes in a SQLite file across restarts. Without it
  they are in memory.
- `--dashboard DIR` serves a built dashboard. By default it uses
  `$SYNERGO_STATIC_DIR`, or `frontend/dist` next to the relay in a source
  checkout (`cd frontend && npm ci && npm run build`). A wheel from PyPI does
  not include the dashboard; use the Docker image if you want both without
  building.
- Teammates' traffic is plain `ws://`, readable by anyone on the network,
  which is why the join line carries `--insecure`. For TLS on a LAN, put the
  relay behind [Caddy](#docker-compose--caddy) with `tls internal`, or use the
  Docker image with a proxy.
- AI captions: `pip install "synergo[relay,gemini]"` and set `GEMINI_API_KEY`.

## Docker

```bash
docker run -d --name synergo-relay --restart unless-stopped \
  -p 8000:8000 -v synergo-data:/data \
  -e SYNERGO_ENV=production \
  -e CORS_ORIGINS=https://relay.example.com \
  ghcr.io/adrianshah/synergo-relay
curl http://localhost:8000/readyz
```

Tags: `latest` (newest stable release), `1.2.3` and `1.2` (releases), `edge`
(the tip of `main`). Images are built for `linux/amd64` and `linux/arm64`. To
build it yourself: `docker build -t synergo-relay .` from the repo root.

The container serves plain HTTP on `$PORT` (default 8000), as the non-root
user `synergo`, so put a TLS proxy in front of it. The Compose setup below does
exactly that. For a quick local try, `docker run -p 8000:8000
ghcr.io/adrianshah/synergo-relay` (development mode) serves the dashboard at
`http://localhost:8000/`.

## Docker Compose + Caddy

`deploy/compose/` runs the image behind [Caddy](https://caddyserver.com/),
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

3. Check `https://relay.example.com/readyz`, and open
   `https://relay.example.com/` for the dashboard.

To upgrade, run `git pull && docker compose up -d --build`. Room codes are
kept in the `relay-data` volume, and certificates in `caddy-data`.

**Optional Redis.** `COMPOSE_PROFILES=redis` and
`SYNERGO_REDIS_URL=redis://redis:6379/0` in `.env` add a Redis container (with
its own volume) and move room codes, room leases and rate-limit counters into
it. That only pays off with [several relay instances](#scaling-out). To use
Redis for rate limits alone, also set `SYNERGO_ROOM_STORE=local`. If Redis goes
away, the relay logs a warning and falls back to what it holds itself.

For a local trial without a domain, set `RELAY_DOMAIN=localhost`; Caddy then
uses its own local CA.

## Fly.io

`deploy/fly/fly.toml` runs one always-on machine with a 1 GB volume. Health
checks hit `/health` and `/readyz`, and connection limits are sized for
long-lived WebSockets. In short, from the repo root:

```bash
fly apps create my-synergo-relay            # and set `app` in fly.toml to match
fly volumes create synergo_data --region iad --size 1 --yes --config deploy/fly/fly.toml
fly secrets set --stage --config deploy/fly/fly.toml CORS_ORIGINS=https://my-synergo-relay.fly.dev
fly deploy --config deploy/fly/fly.toml
```

Fly is also the easiest place to run [several machines](#scaling-out): with
Redis, the relay pins each room to one machine and has Fly's proxy replay
connections there. Full steps, custom domains, CI deploys and the multi-region
setup: [deploy/fly/README.md](../deploy/fly/README.md).

## Render

`deploy/render/render.yaml` is a Blueprint: one Docker web service with a 1 GB
disk, health-checked on `/readyz`. Create a Blueprint from the repo with
**Blueprint Path** `deploy/render/render.yaml`. Render then prompts for
`CORS_ORIGINS` (and optionally `GEMINI_API_KEY`). Details:
[deploy/render/README.md](../deploy/render/README.md). A disk needs a paid
instance; the free tier also sleeps.

## Railway

`deploy/railway/railway.json` builds the `Dockerfile`, health-checks
`/readyz` and restarts on failure. Point the service's config-as-code path at
`/deploy/railway/railway.json`, attach a volume at `/data`, and set
`SYNERGO_ENV`, `CORS_ORIGINS` and `TRUST_PROXY_HEADER`. Steps:
[deploy/railway/README.md](../deploy/railway/README.md). Keep it at one
replica, with app sleeping off.

## Kubernetes (Helm)

`deploy/helm/synergo-relay/` is a chart: a Deployment (one replica by
default), a Service, an optional Ingress, and a PersistentVolumeClaim for
room codes. Liveness is `/health`, readiness `/readyz`.

```bash
helm install synergo deploy/helm/synergo-relay -n synergo --create-namespace \
  --set relay.corsOrigins=https://relay.example.com \
  --set ingress.enabled=true --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=relay.example.com' \
  --set 'ingress.hosts[0].paths[0].path=/'
```

The chart refuses `replicaCount > 1` without Redis. Values, WebSocket timeouts
and multi-replica routing: [the chart's README](../deploy/helm/synergo-relay/README.md).

## Scaling out

One relay instance holds hundreds of WebSockets on a small VM, so scale up
first. More than one instance is for availability or regions, and needs two
things:

1. **Redis**, via `SYNERGO_REDIS_URL` (the image includes the client;
   elsewhere `pip install "synergo[redis]"`). With it:
   - room codes and settings live in Redis, so a code created on one instance
     is valid on every one. Each instance keeps a local copy (SQLite or
     memory) that it uses while Redis is unreachable;
   - rate limits are shared, so a caller gets one budget, not one per instance;
   - each room is **leased** to one instance: the key
     `synergo:room:<code>:owner` holds that instance's id, set only if absent
     and expiring after 30 s. The owner renews it every 10 s while it has
     sockets in the room, and releases it when the room empties or the
     instance shuts down, so a crashed instance's rooms free themselves.
   Use a Redis that persists its data: it holds the room codes. The instance
   id is `FLY_MACHINE_ID` on Fly, else `SYNERGO_INSTANCE_ID`, else
   hostname-pid.
2. **Every connection for a room reaching its owner.** A room's live state
   (its members' sockets and recent edits) is in one process, so all of a
   room's WebSockets (`/ws/<code>`) must land on the same instance.

**On Fly.io** the relay does this itself. When a connection for a room
reaches a machine that does not own it, that machine answers the WebSocket
upgrade with a bare response carrying
`fly-replay: instance=<owner>;fallback=prefer_self`. Fly's proxy then replays
the original request to the owner, which accepts it. If the owner cannot be
reached, Fly sends the request back with `fly-replay-failed`, and the first
machine takes the room over and serves it. A request is never replayed twice.
See [deploy/fly/README.md](../deploy/fly/README.md#scaling-out).

**Everywhere else** the load balancer has to route by room code. Clients send
the code in upper case and with no query string, so hashing the path is
enough. A relay that receives a connection for a room another instance owns
serves it anyway and logs a warning naming the room ("route /ws/&lt;code&gt;
by room code at the load balancer"). Its members then cannot see the owner's
members until they reconnect, and the lease moves to whichever instance still
has members.

nginx:

```nginx
upstream synergo_relays {
    hash $uri consistent;          # the path, so /ws/<code> picks one relay
    server relay-1:8000;
    server relay-2:8000;
}

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name relay.example.com;
    # ssl_certificate ... ;

    location / {
        proxy_pass http://synergo_relays;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        # Replace, not append, so clients cannot choose their rate-limit bucket.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}
```

On Kubernetes with ingress-nginx, the same is one annotation:
`nginx.ingress.kubernetes.io/upstream-hash-by: "$uri"`.

Caddy (`uri_hash` hashes the path and query; Synergo sends no query):

```caddyfile
relay.example.com {
	reverse_proxy relay-1:8000 relay-2:8000 {
		lb_policy uri_hash
		stream_close_delay 5m
	}
}
```

Traefik has no hash-by-path balancer (its sticky sessions use cookies, which
daemons do not keep), so split the code space between instances with path
rules instead. Room codes are drawn from `A-Z` and `2-9`, without `I` and `O`:

```yaml
# dynamic configuration (file provider)
http:
  routers:
    rooms-a-m:
      rule: "Host(`relay.example.com`) && PathRegexp(`^/ws/[A-M]`)"
      service: relay-1
      priority: 100
    rooms-rest:
      rule: "Host(`relay.example.com`) && PathPrefix(`/ws/`)"
      service: relay-2
      priority: 90
    everything-else:   # dashboard, /rooms, /health: any instance will do
      rule: "Host(`relay.example.com`)"
      service: relays
      priority: 1
  services:
    relay-1:
      loadBalancer:
        servers: [{ url: "http://relay-1:8000" }]
    relay-2:
      loadBalancer:
        servers: [{ url: "http://relay-2:8000" }]
    relays:
      loadBalancer:
        servers: [{ url: "http://relay-1:8000" }, { url: "http://relay-2:8000" }]
```

HAProxy: `balance uri` with `hash-type consistent` in the backend.

Render and Railway balance connections without regard to the path, so keep
them at one instance.

## Client IPs behind a proxy

Rate limits (room creation, WebSocket attempts) are per client IP. Behind a TLS
proxy the relay only sees the proxy's address unless told which header to
trust: `Fly-Client-IP` on Fly.io (automatic), `CF-Connecting-IP` behind
Cloudflare (`TRUST_CLOUDFLARE`), or `X-Forwarded-For` (`TRUST_PROXY_HEADER`,
with `TRUSTED_PROXY_HOPS` saying how many proxies of yours append to it).
Each of these headers is client-settable when no proxy of yours rewrites it,
so turn on only the one that matches your setup. The full rules, and what
each host does: [docs/security.md](security.md).

- **Caddy (Compose), nginx with `proxy_set_header X-Forwarded-For
  $remote_addr`:** one hop that replaces the header. `TRUST_PROXY_HEADER=true`.
- **Render, Railway, an ingress controller:** these append to the header.
  `TRUST_PROXY_HEADER=true` with the hop count from docs/security.md.

## Wiring a host into CI

`.github/workflows/deploy.yml` deploys staging on pushes to `main` and
production on `v*` tags, after CI passes, by POSTing a host's deploy-hook URL
(`RELAY_DEPLOY_HOOK_URL`, set per GitHub environment). It then polls
`RELAY_URL/readyz`. Render provides deploy hooks. Fly needs a `flyctl deploy`
step instead (see the Fly README). A Compose host can expose a small webhook,
or you can deploy it by hand.

`.github/workflows/image.yml` publishes the image to
`ghcr.io/adrianshah/synergo-relay` on the same events (`edge` from `main`,
versions and `latest` from tags), so a Compose or Kubernetes host can also just
pull a new tag.

## Operating it

- **Health:** `/health` (liveness, room count) and `/readyz` (environment, CORS,
  protocol versions, rate-limit backend). `/metrics` serves Prometheus text
  with aggregate counts only, including `synergo_room_leases` (rooms this
  instance owns when several share Redis).
- **Logs:** JSON lines on stdout outside development.
- **Upgrades:** deploy a new image. Daemons reconnect within seconds and
  rebuild room state from live activity. If a release raises the relay's
  minimum protocol version, older daemons are refused with an upgrade message
  (`/readyz` reports both numbers, and `synergo doctor` compares them), so
  tell teams to upgrade the CLI at the same time.
- **Backups:** only room codes are durable, and they expire after seven days,
  so backups are optional. With one instance they are the SQLite file at
  `SYNERGO_STATE_PATH` (`/data/synergo.db` in the image). Copy it consistently
  with `sqlite3 /data/synergo.db ".backup /backup/synergo.db"` (the file runs
  in WAL mode, so a plain `cp` of a live database can miss recent writes), or
  snapshot the volume. With Redis, they are in Redis (`synergo:rooms*` keys),
  so back Redis up instead. Losing them means teams run `synergo up` again and
  commit a new `.synergo.toml`.

## Environment variables for hosting

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYNERGO_STATE_PATH` | memory (`/data/synergo.db` in the image) | SQLite file for room codes. |
| `SYNERGO_STATIC_DIR` | unset (`/app/static` in the image) | Built dashboard to serve at `/`. Empty serves the API only. |
| `SYNERGO_REDIS_URL` | unset | Redis for room codes, room leases and rate limits shared by several instances. |
| `SYNERGO_ROOM_STORE` | `redis` when `SYNERGO_REDIS_URL` is set | `local` keeps room codes on this instance even with Redis. |
| `SYNERGO_INSTANCE_ID` | `FLY_MACHINE_ID`, else hostname-pid | This instance's name in room leases. |
| `PORT` | `8000` | Port the image listens on. |
