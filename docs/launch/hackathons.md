# Hackathon playbook: free relays

Hivelane began at a hackathon, and a hackathon is the best place to show it:
many teams, several agents per team, one shared deadline. This playbook is
for offering organisers a free relay for their event. It covers what to
offer, a 5-minute setup, the limits that matter at a venue, the day-of
runbook, teardown, and a one-page sheet to print for every table.

Nothing here has been sent to any organiser. Reaching out is a human task.

## What to offer an organiser

> We would like to run a free Hivelane relay for your event. Teams install one
> CLI, run `hivelane up` in their repo, and their AI coding agents (Claude
> Code, Codex CLI, Cursor, Gemini CLI) check with each other before every
> edit, so two teammates' agents stop rewriting the same function. We bring
> the relay, a printed one-page guide per table, and a person at the help
> desk for the first hour. The relay keeps nothing after the event: we delete
> its database at the end, and we do not turn on the optional AI captions,
> so prompts and code never leave the relay machine.

Ask for: the venue network details (is client-to-client traffic allowed?),
expected team count and size, a table near power for the relay laptop, and
five minutes at the opening session.

## Choose a setup

| | A. Laptop on the venue network | B. Small cloud VM |
|---|---|---|
| Time | 5 minutes on the day | 10 to 15 minutes, done the day before |
| Needs | Client-to-client traffic on the venue Wi-Fi | A domain or a host with TLS (e.g. Fly's `*.fly.dev`) |
| Transport | Plain `ws://` on the venue network (`--insecure`) | `wss://` (TLS) |
| Remote team members | No | Yes |
| Rate limits | Each laptop has its own IP: defaults are fine | The whole venue may share one public IP: raise them (below) |

Many venue networks isolate clients from each other. Test before choosing A:
start the relay, then from a second laptop on the same network run
`curl http://<relay-ip>:8000/health`. If that times out, use B, or a
dedicated router you bring.

### A. Laptop relay in 5 minutes

With Docker (relay and dashboard in one image; needs the public image, P3
in [README.md](README.md), pending):

```bash
docker run -d --name hivelane-relay --restart unless-stopped \
  -p 8000:8000 -v hivelane-event:/data \
  ghcr.io/adrianshah/hivelane-relay
curl http://localhost:8000/readyz
```

Without Docker, from a checkout (the PyPI wheel does not include the
dashboard, so build it once beforehand):

```bash
git clone https://github.com/AdrianShah/Hivelane && cd Hivelane
pipx install ".[relay]"                    # after the PyPI release: pipx install "hivelane[relay]"
(cd frontend && npm ci && npm run build)   # the night before
hivelane relay --state event.db --dashboard frontend/dist
```

`hivelane relay` prints the exact line teams should run, with your LAN
address, for example:

```text
  teammates run:  hivelane up --relay ws://192.168.1.23:8000/ws --insecure
  dashboard:      http://192.168.1.23:8000
```

Copy both into the one-page sheet below before printing, or write them on a
whiteboard.

### B. Cloud relay

Use the Fly setup in `deploy/fly/` (one always-on machine, 1 GB volume):
`fly apps create`, `fly volumes create`, set `CORS_ORIGINS` to the app's own
URL, `fly deploy --config deploy/fly/fly.toml`. Full steps:
`docs/self-hosting.md`. Teams then use `wss://<app>.fly.dev/ws` and no
`--insecure`. Set the venue-NAT limits below as Fly secrets.

## Limits that matter at a venue

| Limit | Value | What to do |
|-------|-------|------------|
| Members per room | 6, hard-coded (`MAX_MEMBERS` in `server/room_manager.py`) | Each agent checkout connected to the room is one member. A team of three with two checkouts each is six. Larger teams split into two rooms (`hivelane up --room` with a new code for the second group). |
| Live rooms per relay | 100, hard-coded (`MAX_ROOMS`) | Beyond 100 teams, run a second relay on another port or machine and assign tables to each. |
| Room creations per IP | 10 per hour (`ROOM_CREATE_LIMIT`) | Fine for setup A. For setup B behind venue NAT, set `ROOM_CREATE_LIMIT=500`. |
| WebSocket attempts per IP | 30 per minute (`WS_ATTEMPT_LIMIT`) | Fine for setup A. For setup B behind venue NAT, set `WS_ATTEMPT_LIMIT=1000`; every daemon reconnect counts. |
| Messages per room | 600 per minute (`ROOM_MESSAGE_LIMIT`) | Default is fine. |

For setup B, also check how the relay sees client IPs
(`TRUST_PROXY_HEADER`; automatic on Fly), as described in
`docs/security.md`.

## Data and trust

- Whoever runs the relay can read every cleartext room: prompts, file paths
  and diffs. Say this in the opening announcement and on the sheet.
- Do not set `GEMINI_API_KEY`. Without it, nothing leaves the relay machine.
- Teams that want privacy can use an end-to-end encrypted room
  (`hivelane up --e2e`, needs `pipx install "hivelane[e2e]"`), in which the
  relay sees only hashes. On setup A the dashboard is served over plain
  http, and browsers only allow the dashboard to decrypt on https or
  localhost, so e2e teams will see placeholders on the dashboard; their
  agents are still coordinated.
- Room codes are on every screen at a hackathon. A team can stop strangers
  joining with `hivelane room require-approval on` (run by whoever created
  the room).
- At the end, delete the database (`event.db`, or the `hivelane-event`
  volume). Room codes would otherwise stay valid for seven days.

## Day-of runbook

| When | Do |
|------|----|
| T-1 day | Rehearse the full sheet on two laptops with two agents; build the dashboard; print one sheet per table plus spares |
| T-60 min | Start the relay; check `/readyz` from a second laptop on the venue network; open the dashboard on a big screen if there is one |
| Opening | Two minutes: the problem (agents rewriting each other's work), the three commands, where the help desk is, and the data note above |
| First hour | Help desk. Most problems are `hivelane` not on PATH (`pipx ensurepath`, new terminal) or a missing `--insecure`; `hivelane doctor` names both |
| Hourly | Glance at `http://<relay>:8000/metrics`: `hivelane_rooms_live`, `hivelane_members`, and `hivelane_edit_checks_total{decision="block"}` |
| Closing | Ask teams for one sentence each: did a block or warning save them work, or get in their way? Paper is fine. Note the relay's block count from `/metrics` |
| After | `docker rm -f hivelane-relay && docker volume rm hivelane-event`, or Ctrl+C and delete `event.db`. Remind teams to remove the event relay from their repos (sheet, last section) |

What to record for the launch metrics: number of rooms, number of members,
blocks and warnings from `/metrics`, and the closing sentences. There is no
telemetry, so this is the only data the event produces.

---

## One-page sheet (print one per table)

Fill in the three blanks before printing. Fits on one A4 or Letter page.

> # Hivelane at [EVENT NAME]
>
> Your AI coding agents check with your teammates' agents before every edit,
> so two of you never rewrite the same function at once.
>
> **Relay:** `ws://[RELAY-ADDRESS]:8000/ws`
> **Dashboard:** `http://[RELAY-ADDRESS]:8000`
> **Help desk:** [WHERE]
>
> ### 1. Install (everyone, once)
>
> Needs Python 3.12+ (uv brings it if you lack it).
>
>     pipx install hivelane          (or: uv tool install hivelane)
>     hivelane --version             (in a NEW terminal: must work)
>
> ### 2. Set up your repo (one person per team)
>
>     cd your-repo
>     hivelane up --relay ws://[RELAY-ADDRESS]:8000/ws --insecure
>
> Then add the line `insecure = true` to the `.hivelane.toml` it wrote, and
> commit it together with the hook files `hivelane up` listed
> (`.claude/settings.json`, `.codex/hooks.json`, `.cursor/hooks.json` or
> `.gemini/settings.json`):
>
>     git add .hivelane.toml .claude/settings.json      (plus any other hook file it listed)
>     git commit -m "Join the Hivelane room" && git push
>
> ### 3. Everyone else on the team
>
>     git pull
>     hivelane up
>
> ### 4. Work as usual
>
> Use Claude Code, Codex CLI, Cursor or Gemini CLI. If a teammate's agent is
> changing the same function, yours is stopped before the edit with a message
> like "sam's claude-code is editing parse_args in src/cli.py (8s ago) —
> wait, or pick another file". A different function in the same file only
> warns. The dashboard (`hivelane open`, or the address above) shows every
> agent's prompt, diffs and decisions.
>
> ### If something is wrong
>
>     hivelane doctor                (checks everything, says how to fix it)
>     hivelane status                (who your daemon is connected to)
>
> If Hivelane is down, your agents keep working, uncoordinated.
>
> ### Good to know
>
> - The relay's operators can see prompts and code in your room, and the
>   venue network is unencrypted. Do not paste secrets.
> - A room holds 6 members. Each agent checkout counts as one.
> - Keep strangers out: `hivelane room require-approval on` (room creator).
> - Never share or tunnel port 4600 on your laptop.
>
> ### Before you leave
>
> The event relay goes away tonight. In your repo, run
> `hivelane leave --forget` and commit the change to `.hivelane.toml`, then
> `hivelane down`. To remove the hooks: `hivelane hook uninstall <agent>`.
>
> Hivelane is open source: github.com/AdrianShah/Hivelane

Before printing: if the PyPI release does not exist yet (pending), replace
step 1's install line with `pipx install git+https://github.com/AdrianShah/Hivelane`.
For setup B, replace `ws://` with `wss://`, drop `--insecure` and the
`insecure = true` step, and delete the "venue network is unencrypted" clause.
