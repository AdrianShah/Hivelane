# Relay on Fly.io

Runs the relay image from the repo's `Dockerfile` on one always-on Fly machine,
with a 1 GB volume so room codes survive deploys. Config: [`fly.toml`](fly.toml).

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

# 3. Secrets. CORS_ORIGINS is required: the exact origin(s) of your dashboard,
#    comma-separated, no trailing slash. --stage applies them on the next deploy.
fly secrets set --stage --config deploy/fly/fly.toml \
  CORS_ORIGINS=https://synergo-dashboard.example.com
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

Build the dashboard with `VITE_WS_URL=wss://my-synergo-relay.fly.dev/ws`.

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

- **One machine.** Rooms live in the relay process's memory, and room codes
  live on that machine's volume. Two machines would split teams; see the
  comment in `fly.toml`. Scale up the machine (`[[vm]]` memory and CPUs) instead.
- **Brief restarts on deploy.** A machine with a volume is replaced in place,
  so daemons reconnect after a few seconds. Pre-edit checks fail open meanwhile,
  so agents are never blocked by a deploy.
- **Client IPs.** `TRUST_PROXY_HEADER=true` keeps users in separate
  rate-limit buckets. The relay uses the leftmost `X-Forwarded-For` entry, and
  Fly appends the real address after any value the client sent. A client that
  forges the header can choose its own bucket. The limits slow casual abuse;
  they don't stop a determined client.
- **Connection limits.** `[http_service.concurrency]` raises Fly's default
  (25 connections per machine), because every daemon and dashboard tab keeps a
  WebSocket open.
