# Relay on Render

[`render.yaml`](render.yaml) is a Blueprint for one Docker web service built
from the repo's `Dockerfile`, with a 1 GB disk at `/data` for room codes.

## Setup

1. In the Render dashboard: **New → Blueprint**, connect this repository, and
   set **Blueprint Path** to `deploy/render/render.yaml`.
2. Render prompts for the `sync: false` variables:
   - `CORS_ORIGINS` (required): your dashboard's exact origin(s), comma-separated,
     e.g. `https://synergo-dashboard.example.com`.
   - `GEMINI_API_KEY` (optional): enables AI captions and conflict summaries.
3. Apply. When the deploy finishes, check
   `https://<service>.onrender.com/readyz`.
4. Optional, for CI-driven deploys: copy the service's **Deploy Hook** URL
   (Settings → Deploy Hook) into the `RELAY_DEPLOY_HOOK_URL` secret of the
   `Staging`/`Production` GitHub environments. `.github/workflows/deploy.yml`
   then deploys after CI passes. The Blueprint sets `autoDeployTrigger: "off"`
   so Render does not also deploy every push on its own.

Point clients at `wss://<service>.onrender.com/ws`: `synergo up --relay …` (or
`SYNERGO_RELAY`) for the CLI, `VITE_WS_URL` for the dashboard. For a custom
domain, add it under the service's Settings → Custom Domains and create the
CNAME Render shows.

## Things to know

- **No free tier.** A disk needs a paid instance, and free instances sleep
  when idle, which drops every connection.
- **One instance.** Render cannot scale a service that has a disk. The relay
  needs a single instance anyway, because rooms live in its memory.
- **Short downtime on deploy.** Services with a disk don't get zero-downtime
  deploys. Daemons reconnect on their own, and pre-edit checks fail open
  meanwhile.
- **Client IPs.** `TRUST_PROXY_HEADER=true` gives each user their own
  rate-limit bucket behind Render's proxy. The caveat is in
  [docs/self-hosting.md](../../docs/self-hosting.md#client-ips-behind-a-proxy).
