# Relay on Railway

[`railway.json`](railway.json) builds the repo's `Dockerfile` (relay plus the
bundled dashboard), health-checks `/readyz`, and restarts the service if it
crashes. Railway injects `$PORT`, which the image listens on.

## Setup

1. **New Project → Deploy from GitHub repo**, and pick this repository.
2. In the service's **Settings**:
   - **Config-as-code → Railway config file**: `/deploy/railway/railway.json`
     (Railway only looks for `railway.json` at the repo root by default).
   - **Networking → Generate Domain** (or add your own domain).
3. **Volume**: right-click the service → **Attach volume**, mount path
   `/data`. Room codes live there (`HIVELANE_STATE_PATH=/data/hivelane.db` is
   set in the image), so they survive deploys.
4. **Variables**:
   - `HIVELANE_ENV=production`
   - `CORS_ORIGINS=https://<your-service>.up.railway.app` (required in
     production; add any dashboard hosted elsewhere, comma-separated)
   - `TRUST_PROXY_HEADER=true` (Railway's proxy terminates TLS; see
     [docs/security.md](../../docs/security.md) for the proxy-hop settings)
   - optional `GEMINI_API_KEY` for AI captions
5. Deploy, then check `https://<your-service>.up.railway.app/readyz`.

`https://<your-service>.up.railway.app/` is the dashboard, and teammates run
`hivelane up --relay wss://<your-service>.up.railway.app/ws`.

## Things to know

- **One replica.** Keep the replica count at 1: rooms live in the relay
  process's memory, and Railway balances connections across replicas without
  regard to the room. A service with a volume also cannot run more than one
  replica. Scaling out needs Redis and room-affine routing
  ([docs/self-hosting.md](../../docs/self-hosting.md#scaling-out)), which
  Railway's proxy does not offer.
- **Never sleep.** Leave **Serverless** (app sleeping) off: a sleeping relay
  drops every daemon's WebSocket.
- **Short downtime on deploy.** A service with a volume is stopped before the
  new deployment starts. Daemons reconnect on their own, and pre-edit checks
  fail open meanwhile.
- **Deploys from CI.** `watchPatterns` limits automatic deploys to changes
  under `server/`, `frontend/` and the `Dockerfile`. To deploy only after CI
  passes, turn on **Wait for CI** in the service settings.
