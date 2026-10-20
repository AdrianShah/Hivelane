# Self-hosting the relay

The relay (`server/`) is one small FastAPI process. It holds rooms in memory,
keeps the list of valid room codes in a SQLite file, and serves the dashboard
from the same address. Every option below runs the same code: the repo's
`Dockerfile`, published as `ghcr.io/adrianshah/hivelane-relay`, or
`hivelane relay` straight from the CLI.

| Option | Good for | Files |
|--------|----------|-------|
| [`hivelane relay`](#on-your-lan-hivelane-relay) | A LAN, a hackathon, trying it out | `hivelane/cli.py` |
| [Docker](#docker) | Any host that runs containers | `Dockerfile` |
| [Docker Compose + Caddy](#docker-compose--caddy) | Your own VM, with automatic HTTPS | `deploy/compose/` |
| [Fly.io](#flyio) | Managed, always-on, several regions if you want | `deploy/fly/` |
| [Render](#render) | Managed, Git-driven | `deploy/render/` |
| [Railway](#railway) | Managed, Git-driven | `deploy/railway/` |
| [Kubernetes (Helm)](#kubernetes-helm) | A cluster you already run | `deploy/helm/hivelane-relay/` |

## What every deployment needs

- **`HIVELANE_ENV=production`** and **`CORS_ORIGINS`**. Outside development the
  relay refuses to boot without `CORS_ORIGINS`. List the exact origin(s) of
  any dashboard hosted *elsewhere*, comma-separated. The bundled dashboard is
  served from the relay's own origin, so it needs no entry; if it is your only
  dashboard, list the relay's own URL (`https://relay.example.com`).
- **A persistent disk at `/data`** (`HIVELANE_STATE_PATH` points into it). The
  disk keeps room codes valid across restarts and deploys. Without it, every
  restart sends every team back to `hivelane up` with a new room.
  The relay runs as uid 10001. Hosts that mount the disk root-owned (Fly
  volumes, Render disks) are handled by the image: its entrypoint starts as
  root, chowns the state directory to 10001 and drops privileges before the
  relay starts. If your platform starts the container as a non-root user,
  mount the disk owned by uid/gid 10001 (Kubernetes: `fsGroup: 10001`, as the
  Helm chart does); with `HIVELANE_ENV=production` the container refuses to
  start on a state directory it cannot write rather than silently keeping
  rooms in memory.
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
# CLI: the first teammate records the relay in .hivelane.toml for everyone
hivelane up --relay wss://relay.example.com/ws
# or per machine, for every repo
export HIVELANE_RELAY=wss://relay.example.com/ws
```

The dashboard is at `https://relay.example.com/`. Set
`HIVELANE_DASHBOARD=https://relay.example.com` for the CLI too, so `hivelane open`
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

- **Serve the API only:** set `HIVELANE_STATIC_DIR=` (empty).
- **Host the dashboard elsewhere** (Vercel config: `frontend/vercel.json`):
  build it with `VITE_WS_URL=wss://relay.example.com/ws` and add its origin to
  the relay's `CORS_ORIGINS`.
- **Bake a different relay into the image's dashboard:**
  `docker build --build-arg VITE_WS_URL=wss://relay.example.com/ws .`

### The public site on GitHub Pages (hivelane.dev)

`.github/workflows/pages.yml` publishes the dashboard and the docs (at
`/docs/`) on every push to `main`. GitHub Pages only serves static files, so it
hosts no relay. The dashboard asks for one: room links carry `?relay=wss://…`,
and the Home page has a relay field. Set the repository variable
`PAGES_RELAY_URL=wss://relay.example.com/ws` to bake a default relay in, and
add the site's origin (`https://hivelane.dev`) to that relay's `CORS_ORIGINS`.

Without a custom domain, the site is at `https://adrianshah.github.io/Hivelane/`.
To serve it at `hivelane.dev`:

1. Buy the domain and add these DNS records at the registrar:
   - `A` records for `@`: `185.199.108.153`, `185.199.109.153`,
     `185.199.110.153` and `185.199.111.153`
   - `AAAA` records for `@`: `2606:50c0:8000::153`, `2606:50c0:8001::153`,
     `2606:50c0:8002::153` and `2606:50c0:8003::153`
   - `CNAME` for `www`: `adrianshah.github.io`
2. Verify the domain for your account: GitHub, then Settings > Pages >
   **Add a domain**. Add the TXT record it shows, so nobody else can claim it.
3. In the repository, go to Settings > Pages > **Custom domain**, enter
   `hivelane.dev` and save. When the certificate is issued (up to an hour),
   tick **Enforce HTTPS**.
4. Set the repository variable `PAGES_DOMAIN=hivelane.dev`, then re-run the
   Pages workflow. The site is then built for `/` instead of `/Hivelane/`.

## On your LAN: `hivelane relay`

For a hackathon table or an office network, run the relay from the CLI. It
needs the `relay` extra:

```bash
pipx install "hivelane[relay]"         # or: pip install "hivelane[relay]"
hivelane relay --state relay.db
```

```text
hivelane relay: listening on 0.0.0.0:8000 (Ctrl+C to stop)
  teammates run:  hivelane up --relay ws://192.168.1.23:8000/ws --insecure
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
  `$HIVELANE_STATIC_DIR`, or `frontend/dist` next to the relay in a source
  checkout (`cd frontend && npm ci && npm run build`). A wheel from PyPI does
  not include the dashboard; use the Docker image if you want both without
  building.
- Teammates' traffic is plain `ws://`, readable by anyone on the network,
  which is why the join line carries `--insecure`. For TLS on a LAN, put the
  relay behind [Caddy](#docker-compose--caddy) with `tls internal`, or use the
  Docker image with a proxy.
- AI captions: `pip install "hivelane[relay,gemini]"` and set `GEMINI_API_KEY`.

## Docker

```bash
docker run -d --name hivelane-relay --restart unless-stopped \
  -p 8000:8000 -v hivelane-data:/data \
  -e HIVELANE_ENV=production \
  -e CORS_ORIGINS=https://relay.example.com \
  ghcr.io/adrianshah/hivelane-relay
curl http://localhost:8000/readyz
```

Tags: `latest` (newest stable release), `1.2.3` and `1.2` (releases), `edge`
(the tip of `main`). Images are built for `linux/amd64` and `linux/arm64`. To
build it yourself: `docker build -t hivelane-relay .` from the repo root.

The container serves plain HTTP on `$PORT` (default 8000), as the non-root
user `hivelane`, so put a TLS proxy in front of it. The Compose setup below does
exactly that. For a quick local try, `docker run -p 8000:8000
ghcr.io/adrianshah/hivelane-relay` (development mode) serves the dashboard at
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
`HIVELANE_REDIS_URL=redis://redis:6379/0` in `.env` add a Redis container (with
its own volume) and move room codes, room leases and rate-limit counters into
it. That only pays off with [several relay instances](#scaling-out). To use
Redis for rate limits alone, also set `HIVELANE_ROOM_STORE=local`. If Redis goes
away, the relay logs a warning and falls back to what it holds itself.

For a local trial without a domain, set `RELAY_DOMAIN=localhost`; Caddy then
uses its own local CA.

## Fly.io

`deploy/fly/fly.toml` runs one always-on machine with a 1 GB volume. Health
checks hit `/health` and `/readyz`, and connection limits are sized for
long-lived WebSockets. In short, from the repo root:

```bash
fly apps create my-hivelane-relay            # and set `app` in fly.toml to match
fly volumes create hivelane_data --region iad --size 1 --yes --config deploy/fly/fly.toml
fly secrets set --stage --config deploy/fly/fly.toml CORS_ORIGINS=https://my-hivelane-relay.fly.dev
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
`HIVELANE_ENV`, `CORS_ORIGINS` and `TRUST_PROXY_HEADER`. Steps:
[deploy/railway/README.md](../deploy/railway/README.md). Keep it at one
replica, with app sleeping off.

## Kubernetes (Helm)

`deploy/helm/hivelane-relay/` is a chart: a Deployment (one replica by
default), a Service, an optional Ingress, and a PersistentVolumeClaim for
room codes. Liveness is `/health`, readiness `/readyz`.

```bash
helm install hivelane deploy/helm/hivelane-relay -n hivelane --create-namespace \
  --set relay.corsOrigins=https://relay.example.com \
  --set ingress.enabled=true --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=relay.example.com' \
  --set 'ingress.hosts[0].paths[0].path=/'
```

The chart refuses `replicaCount > 1` without Redis. Values, WebSocket timeouts
and multi-replica routing: [the chart's README](../deploy/helm/hivelane-relay/README.md).

## Scaling out

One relay instance holds hundreds of WebSockets on a small VM, so scale up
first. More than one instance is for availability or regions, and needs two
things:

1. **Redis**, via `HIVELANE_REDIS_URL` (the image includes the client;
   elsewhere `pip install "hivelane[redis]"`). With it:
   - room codes and settings live in Redis, so a code created on one instance
     is valid on every one. Each instance keeps a local copy (SQLite or
     memory) that it uses while Redis is unreachable;
   - rate limits are shared, so a caller gets one budget, not one per instance;
   - each room is **leased** to one instance: the key
     `hivelane:room:<code>:owner` holds that instance's id, set only if absent
     and expiring after 30 s. The owner renews it every 10 s while it has
     sockets in the room, and releases it when the room empties or the
     instance shuts down, so a crashed instance's rooms free themselves.
   Use a Redis that persists its data: it holds the room codes. The instance
   id is `FLY_MACHINE_ID` on Fly, else `HIVELANE_INSTANCE_ID`, else
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
upstream hivelane_relays {
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
        proxy_pass http://hivelane_relays;
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

Caddy (`uri_hash` hashes the path and query; Hivelane sends no query):

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
`RELAY_URL/readyz` until the relay reports the deployed commit (or times out
and fails the run). Render provides deploy hooks. Fly needs a `flyctl deploy`
step instead (see the Fly README). A Compose host can expose a small webhook,
or you can deploy it by hand.

`.github/workflows/image.yml` publishes the image to
`ghcr.io/adrianshah/hivelane-relay` on the same events (`edge` from `main`,
versions and `latest` from tags), so a Compose or Kubernetes host can also just
pull a new tag.

## Operating it

- **Health:** `/health` (liveness, room count) and `/readyz` (environment, CORS,
  protocol versions, rate-limit backend). `/metrics` serves Prometheus text
  with aggregate counts only, including `hivelane_room_leases` (rooms this
  instance owns when several share Redis).
- **Logs:** JSON lines on stdout outside development.
- **Upgrades:** deploy a new image. Daemons reconnect within seconds and
  rebuild room state from live activity. If a release raises the relay's
  minimum protocol version, older daemons are refused with an upgrade message
  (`/readyz` reports both numbers, and `hivelane doctor` compares them), so
  tell teams to upgrade the CLI at the same time.
- **Backups:** only room codes are durable, and they expire after seven days,
  so backups are optional. With one instance they are the SQLite file at
  `HIVELANE_STATE_PATH` (`/data/hivelane.db` in the image). Copy it consistently
  with `sqlite3 /data/hivelane.db ".backup /backup/hivelane.db"` (the file runs
  in WAL mode, so a plain `cp` of a live database can miss recent writes), or
  snapshot the volume. With Redis, they are in Redis (`hivelane:rooms*` keys),
  so back Redis up instead. Losing them means teams run `hivelane up` again and
  commit a new `.hivelane.toml`.

## Capacity limits

Every capacity limit is an environment variable, and `0` always means
unlimited. Out of the box a relay accepts **any number of rooms and any number
of members per room**: a relay you run for your own team should never turn a
teammate away. `/readyz` lists every limit in force under `limits`, and the
startup log line shows `max_rooms` and `max_members`.

A relay **open to the internet** should set the first group. The numbers below
come from the load test in [docs/performance.md](performance.md) (one relay
process on a 6-core desktop, every member sending 2 messages a second, which
is 10-20x a real agent; target: edit-check p95 under 50 ms):

| Variable | Default | Public relay | Why |
|----------|---------|--------------|-----|
| `HIVELANE_MAX_MEMBERS` | `0` (unlimited) | `25` | One room stayed under 50 ms p95 up to 25 busy members and failed at 35-50: fan-out is O(members x sockets). |
| `HIVELANE_MAX_ROOMS` | `0` (unlimited) | `500` | Bounds memory (about 0.2 MB per connected socket, 52 MB idle) and ghost-room flooding. A process saturated at ~200 busy members (stress rate), so size instances by members, not rooms. |
| `ROOM_CREATE_LIMIT` | `10` per IP per hour | keep | Without it one script fills `HIVELANE_MAX_ROOMS`. `0` turns it off. |
| `WS_ATTEMPT_LIMIT` | `30` per IP per minute | keep | Makes room codes impractical to brute-force. `0` turns it off (load tests). |
| `ROOM_MESSAGE_LIMIT` | `600` per room per minute | keep, or `100 x HIVELANE_MAX_MEMBERS` for big rooms | Shared by everyone in the room; 600 is ample for 25 real agents. `0` turns it off. |
| `RATE_LIMIT_MAX_TRACKED_IPS` | `4096` | keep | Bounds the limiters' memory. `0` is unbounded. |

Per-message safety caps bound what one message can make the relay hold and copy
to every socket in the room. Keep the defaults unless you have a reason; on a
public relay never set them to `0`:

| Variable | Default | Over the limit |
|----------|---------|----------------|
| `HIVELANE_MAX_FRAME_BYTES` | 524288 (512 KiB) | Socket closed with 1009. Also run uvicorn with `--ws-max-size 1048576` so oversized frames are not read at all. |
| `HIVELANE_MAX_DIFF_BYTES` | 65536 | Diff truncated |
| `HIVELANE_MAX_TEXT_BYTES` | 4096 | Prompt or plan summary truncated |
| `HIVELANE_MAX_FILES_PER_MESSAGE` | 50 | First 50 kept |
| `HIVELANE_MAX_SYMBOLS_PER_MESSAGE` | 64 | First 64 kept |
| `HIVELANE_MAX_LINE_RANGES_PER_MESSAGE` | 256 | First 256 kept |
| `HIVELANE_MAX_PATH_CHARS` | 1024 | Longer paths are not queued or remembered |

Per-room and process bookkeeping (defaults are fine for any size of room):

| Variable | Default | Bounds |
|----------|---------|--------|
| `HIVELANE_MAX_PENDING_JOINS` | 10 | Joiners waiting for an owner's approval, per room |
| `HIVELANE_MAX_TRACKED_DECISIONS` | 1000 | Warn/block decisions kept for votes, per room (LRU) |
| `HIVELANE_MAX_KNOWN_PATHS` | 512 | Paths remembered for suggestions, per room (LRU) |
| `HIVELANE_FILE_QUEUE_MAX_LENGTH` | 0 | Members in one file's wait line; 0 = bounded by the room (each member once) |
| `HIVELANE_FILE_QUEUE_MAX_FILES` | 256 | Files with a wait line, per room |
| `HIVELANE_HISTORY_MAX_EVENTS` / `HIVELANE_HISTORY_MAX_BYTES` | 5000 / 8 MiB | Replay history per room |
| `HIVELANE_HISTORY_TOTAL_MAX_BYTES` | 128 MiB | Replay history across the process |
| `HIVELANE_MAX_WEBHOOKS_PER_ROOM` | 3 | Owner webhooks per room (at least 1) |
| `HIVELANE_WEBHOOK_MAX_IN_FLIGHT` | 100 | Webhook deliveries running at once |
| `HIVELANE_WEBHOOK_MAX_TRACKED_ROOMS` | 1024 | Rooms with a webhook notice budget (LRU) |

On the client side, `HIVELANE_OUTBOX_LIMIT` (default 500, `0` = unlimited) is
how many messages each daemon keeps per room while the relay is unreachable.

## Environment variables for hosting

| Variable | Default | Meaning |
|----------|---------|---------|
| `HIVELANE_STATE_PATH` | memory (`/data/hivelane.db` in the image) | SQLite file for room codes. |
| `HIVELANE_STATIC_DIR` | unset (`/app/static` in the image) | Built dashboard to serve at `/`. Empty serves the API only. |
| `HIVELANE_REDIS_URL` | unset | Redis for room codes, room leases and rate limits shared by several instances. |
| `HIVELANE_ROOM_STORE` | `redis` when `HIVELANE_REDIS_URL` is set | `local` keeps room codes on this instance even with Redis. |
| `HIVELANE_INSTANCE_ID` | `FLY_MACHINE_ID`, else hostname-pid | This instance's name in room leases. |
| `PORT` | `8000` | Port the image listens on. |
