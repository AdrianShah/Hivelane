# Relay on Render

[`render.yaml`](render.yaml) is a Blueprint for one Docker web service built
from the repo's `Dockerfile` (the relay plus the bundled dashboard), with a
1 GB disk at `/data` for room codes.

## Setup

1. In the Render dashboard: **New → Blueprint**, connect this repository, and
   set **Blueprint Path** to `deploy/render/render.yaml`.
2. Render prompts for `GEMINI_API_KEY` (optional): it turns on AI captions
   and conflict summaries. `CORS_ORIGINS` is preset to the GitHub Pages
   dashboard (`https://adrianshah.github.io`); add any other dashboard origin
   there, comma-separated.
3. Apply. When the deploy finishes, check
   `https://<service>.onrender.com/readyz`.
4. Set the repository variable `PAGES_RELAY_URL=wss://<service>.onrender.com/ws`
   and re-run the Pages workflow, so the Pages dashboard uses this relay by
   default.

Render deploys each commit on `main` after its GitHub checks pass
(`autoDeployTrigger: checksPass`). To drive deploys from
`.github/workflows/deploy.yml` instead, set it to `"off"` and copy the
service's **Deploy Hook** URL (Settings → Deploy Hook) into the
`RELAY_DEPLOY_HOOK_URL` secret of the `Staging`/`Production` environments.

The dashboard is at `https://<service>.onrender.com/`. Point the CLI at
`wss://<service>.onrender.com/ws` with `hivelane up --relay …` (or
`HIVELANE_RELAY`); a dashboard hosted elsewhere takes the same URL as
`VITE_WS_URL` at build time. For a custom
domain, add it under the service's Settings → Custom Domains and create the
CNAME Render shows.

## Things to know

- **No free tier.** A disk needs a paid instance, and free instances sleep
  when idle, which drops every connection.
- **One instance.** Render cannot scale a service that has a disk, and its
  load balancer cannot route by room code, which several relay instances
  need (rooms live in a relay's memory). Scale the instance up instead; see
  [docs/self-hosting.md](../../docs/self-hosting.md#scaling-out).
- **Short downtime on deploy.** Services with a disk don't get zero-downtime
  deploys. Daemons reconnect on their own, and pre-edit checks fail open
  meanwhile.
- **Client IPs.** `TRUST_PROXY_HEADER=true` gives each user their own
  rate-limit bucket behind Render's proxy. The caveat is in
  [docs/self-hosting.md](../../docs/self-hosting.md#client-ips-behind-a-proxy).
