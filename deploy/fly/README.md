# Relay on Fly.io

Runs the relay image from the repo's `Dockerfile` (the relay plus the bundled
dashboard) on one always-on Fly machine, with a 1 GB volume so room codes
survive deploys. Config: [`fly.toml`](fly.toml). More machines, in one region
or several, are covered under [Scaling out](#scaling-out).

## One-time setup

Install [flyctl](https://fly.io/docs/flyctl/install/), then from the **repo root**:

```bash
fly auth login

# 1. Create the app. Names are global; use your own, and put the same name in
#    deploy/fly/fly.toml (`app = "..."`) so later commands find it.
fly apps create my-synergo-relay

# 2. A volume for room codes, in the same region as primary_region.
fly volumes create synergo_data --region iad --size 1 --yes \
  --config deploy/fly/fly.toml

# 3. Secrets. CORS_ORIGINS is required in production: the exact origin(s) of
#    any dashboard hosted elsewhere, comma-separated, no trailing slash. The
#    bundled dashboard is same-origin and needs no entry, so if it is the only
#    one, list the app's own URL. --stage applies them on the next deploy.
fly secrets set --stage --config deploy/fly/fly.toml \
  CORS_ORIGINS=https://my-synergo-relay.fly.dev
# Optional AI captions and conflict summaries:
fly secrets set --stage --config deploy/fly/fly.toml GEMINI_API_KEY=...

# 4. Deploy.
fly deploy --config deploy/fly/fly.toml

# 5. Check it.
curl https://my-synergo-relay.fly.dev/readyz
```

`fly launch --config deploy/fly/fly.toml --copy-config --no-deploy --name
my-synergo-relay` can stand in for step 1. It scans the repository and may write
a second `fly.toml` at the root, though. `fly apps create` doesn't touch any files.

## Point clients at it

```bash
# CLI, per repo: written to .synergo.toml so teammates use the same relay
synergo up --relay wss://my-synergo-relay.fly.dev/ws
# or per machine
export SYNERGO_RELAY=wss://my-synergo-relay.fly.dev/ws
```

The dashboard is at `https://my-synergo-relay.fly.dev/`, served by the relay
itself. A dashboard hosted elsewhere is built with
`VITE_WS_URL=wss://my-synergo-relay.fly.dev/ws`, and its origin goes in
`CORS_ORIGINS`.

## Custom domain

```bash
fly certs add relay.example.com --config deploy/fly/fly.toml
# then create the DNS records it prints (CNAME to my-synergo-relay.fly.dev, or A/AAAA)
```

## Deploying from CI

`.github/workflows/deploy.yml` triggers deploys through a host's deploy-hook
URL, and Fly has none. Use `fly deploy` by hand, or add a step to that
workflow that runs `flyctl deploy --config deploy/fly/fly.toml --remote-only`
with a `FLY_API_TOKEN` secret, created with `fly tokens create deploy`.

## Things to know

- **One machine by default.** Rooms live in the relay process's memory, and
  room codes on that machine's volume. More machines need Redis; see
  [Scaling out](#scaling-out). Until then, scale up the machine (`[[vm]]`
  memory and CPUs) instead.
- **Brief restarts on deploy.** A machine with a volume is replaced in place,
  so daemons reconnect after a few seconds. Pre-edit checks fail open meanwhile,
  so agents are never blocked by a deploy.
- **Client IPs.** On Fly the relay takes each client's address from
  `Fly-Client-IP`, which Fly's edge sets on every request, so users get their
  own rate-limit buckets. See [docs/security.md](../../docs/security.md).
- **Connection limits.** `[http_service.concurrency]` raises Fly's default
  (25 connections per machine), because every daemon and dashboard tab keeps a
  WebSocket open.

## Scaling out

Each relay process holds its rooms in memory, so several machines work by
pinning every room to one of them:

```bash
# 1. Redis, reachable from every machine (Upstash, through Fly):
fly redis create
fly secrets set --stage --config deploy/fly/fly.toml SYNERGO_REDIS_URL=redis://default:...@fly-....upstash.io:6379

# 2. A volume per machine, then the machines:
fly volumes create synergo_data --region fra --size 1 --yes --config deploy/fly/fly.toml
fly scale count 2 --region iad,fra --config deploy/fly/fly.toml
fly deploy --config deploy/fly/fly.toml
```

With `SYNERGO_REDIS_URL` set:

- **Room codes live in Redis**, so a code created on one machine is valid on
  all of them. Each machine's volume keeps only its local copy, used while
  Redis is unreachable.
- **Each room is leased to one machine**, the one its first member reached.
  The lease is a Redis key that the owner renews every 10 s while the room has
  members, and that expires 30 s after the owner stops renewing it.
- **Fly replays connections to the owner.** Fly sends each client to its
  nearest machine. When that machine does not own the room, it answers the
  WebSocket upgrade with `fly-replay: instance=<owner>;fallback=prefer_self`,
  and Fly's proxy replays the same request to the owner, which accepts it. If
  the owner cannot be reached, Fly sends the request back and the first
  machine takes the room over.
- **Deploys move rooms cleanly.** A stopping machine releases its leases, so
  reconnecting daemons claim each room on whichever machine they reach next.

To check it, `/metrics` on each machine reports `synergo_room_leases`, and the
logs name each replayed room (`Replaying room ... to instance ...`). Redis has
to keep its data, because it holds the room codes now.
