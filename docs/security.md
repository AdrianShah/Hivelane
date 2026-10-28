# Security

What Hivelane protects, from whom, and how. Hivelane moves coordination
metadata (file paths, symbol names, branches, diffs, prompts) between
teammates' machines through a relay; this page says who can see which of it.

## Relay

The relay (`server/`) is the one shared component: every daemon and dashboard
in a room connects to it over a WebSocket. Whoever runs it is trusted with
everything a room sends in cleartext, so run your own
([self-hosting](self-hosting.md)) or use end-to-end encryption (below) if that
operator should not see your code.

### Trust model

- **The room code is the key to the room.** Codes are 8 characters from a
  32-letter alphabet (about 40 bits), valid for 7 days, and removed about 30
  minutes after the last socket leaves. Anyone holding the code (or a dashboard
  link, which contains it) can join as a member or watch as a dashboard, the
  same trust model as a document share link. Guessing is throttled per client
  IP: 30 WebSocket attempts a minute, 10 rooms created an hour (see
  [client IPs](#client-ips-behind-a-proxy)).
- **Optionally, an owner approves each newcomer.** The room's creator holds an
  owner token and can switch on *require approval*; from then on a leaked code
  alone gets nobody in.
- **The relay cannot authenticate people.** Display names are chosen by
  whoever joins; the relay only keeps them unique within a room. Approval is
  where a human checks who is knocking.
- **Clients do not take the relay's word on encryption.** A daemon decides to
  encrypt from its own `.hivelane.toml`, never from the relay's `e2e` flag, so a
  hostile relay cannot talk it into cleartext. The relay keeps the flag one-way
  anyway: a join can switch `e2e` on, never off.

### Owner tokens

`POST /rooms` returns `owner_token` alongside `room_id`: 32 random bytes,
URL-safe base64, returned this once. The relay keeps only its SHA-256 (in
memory and in the room store: the SQLite `access` column, or the Redis
`access` hash), never the token, and compares hashes in constant time.

A join (member or dashboard) that carries `owner_token` makes that socket an
owner: it may send `room_policy` and `join_decision`, and it receives
`join_request` / `join_resolved`. A wrong or stale token is treated exactly
like no token (no error, nothing revealed). Several sockets can be owners at
once (the dashboard and the creator's daemon, two tabs).

Where tokens live: the CLI keeps the one for rooms it creates in the personal
config (see [Daemon and hooks](#daemon-and-hooks)). The dashboard keeps the one
for rooms it creates in `localStorage` under `hivelane:owner-token:<CODE>`, so
anyone using that browser profile owns the room, and clearing site data gives
the ownership up. There is no recovery or rotation: a lost token means a new
room. Rooms created before owner tokens existed have no owner and cannot
require approval.

### Join approval

When an owner sends `{"type": "room_policy", "require_approval": true}`, the
relay persists the policy with the room (it survives restarts), broadcasts
`room_policy_update` to everyone, and from then on holds every joiner that does
not present the owner token, members and dashboards alike:

1. The joiner gets `join_pending {request_id, reason}`. Its socket stays open;
   anything it sends while waiting is read and discarded.
2. Every owner socket gets `join_request {request_id, name, agent, timestamp}`
   (`name` is `"dashboard viewer"` for a dashboard). An owner who connects
   later gets the open requests in its catch-up.
3. An owner answers `join_decision {request_id, approve}`. Approved: the joiner
   gets `join_resolved {request_id, outcome: "approved"}` and then the room's
   normal traffic; it must not send anything before that frame. Declined:
   `room_rejected` "an owner declined your request" and close code `4403`.
4. Every owner gets `join_resolved` with the outcome (`approved`, `declined`,
   `timed_out`, or `withdrawn` when the joiner disconnected), so other owners'
   lists stay current.

Limits and edge cases:

- At most 10 joiners wait per room; the 11th gets `room_rejected` "too many
  join requests are waiting for approval; try again later" (close `4403`).
- A request expires after 5 minutes: `room_rejected` "approval timed out"
  (close `4403`). With no owner connected, joiners simply wait until then.
- People already connected stay connected when approval is switched on.
  Switching it off admits everyone waiting.
- Approval is per connection: a daemon that reconnects (network blip, laptop
  sleep, relay redeploy) asks again. Owners and their daemons, which present
  the token, are never held.
- If the requested name was taken while the joiner waited, it gets
  `name_rejected` after approval and joins again under another name (which
  needs approval again).
- `join_decision` and `room_policy` from a socket that is not an owner are
  ignored.

Refusals are counted in `hivelane_ws_rejections_total{reason}` as
`approval_declined`, `approval_timeout` and `approval_queue_full`.

### Payload caps and quotas

Everything a member sends is kept in memory and fanned out to every socket in
the room, so the relay bounds it:

| What | Cap | Over the cap |
| --- | --- | --- |
| One WebSocket frame | 512 KB | socket closed with `1009` ("Frame larger than 512 KB") |
| A diff, per file | 64 KB | truncated, ending in `…[truncated]` |
| A prompt, plan summary or intent | 4 KB | truncated, ending in `…[truncated]` |
| Files per activity message or plan | 50 | first 50 kept |
| Symbols per file change, plan or edit check | 64 | first 64 kept |
| Line ranges per file change or edit check | 256 | first 256 kept |
| `e2e` blob | 256 KB | message dropped (ciphertext cannot be truncated) |
| Messages per room, all sockets together | 600 a minute (`ROOM_MESSAGE_LIMIT`), bursts up to that | dropped |
| Activity, intent and plan messages per member | one each per 0.5 s | dropped |

Sizes are UTF-8 bytes. Dropped messages are counted in
`hivelane_messages_dropped_total{reason}`: `room_quota`, `invalid` (fails
validation, including an oversized blob, or is not JSON) and `frame_too_large`.
A malformed frame is dropped without ending the session.

The frame cap applies after uvicorn has read the frame, and uvicorn accepts
frames up to 16 MiB by default, with up to 32 of them queued per socket; to
stop larger ones before they are buffered, also start uvicorn with
`--ws-max-size 600000 --ws-max-queue 4` (keep the size in step with
`HIVELANE_MAX_FRAME_BYTES`). A production relay refuses to start with
`HIVELANE_MAX_FRAME_BYTES`, `HIVELANE_MAX_DIFF_BYTES` or `HIVELANE_MAX_TEXT_BYTES`
set to 0 (unlimited).

### Client IPs behind a proxy

Per-IP limits are only as good as the IP. `client_ip()` uses, in order:

1. `Fly-Client-IP` when `FLY_APP_NAME` is set (Fly sets it on every machine,
   and its edge overwrites the header on every request);
2. `CF-Connecting-IP` when `TRUST_CLOUDFLARE=true`;
3. with `TRUST_PROXY_HEADER=true`, the `X-Forwarded-For` entry
   `TRUSTED_PROXY_HOPS` (default 1) from the **right**;
4. otherwise the TCP peer.

Each proxy appends the address it received the connection from, so the
rightmost entries are written by your proxies and everything left of them by
the client, who can put anything there. Taking the leftmost entry (as the relay
did before) let any caller pick a fresh IP per request and skip every limit.
With N proxies of yours in front, the entry N from the right is the address
the outermost one saw. A header with fewer entries than that yields its
leftmost entry.

| Deployment | Set |
| --- | --- |
| Fly.io | nothing (automatic) |
| Behind Cloudflare, origin reachable only through Cloudflare | `TRUST_CLOUDFLARE=true` |
| One proxy that appends or replaces `X-Forwarded-For`: Render, Railway, Caddy (the Compose file), nginx, Traefik, a Kubernetes ingress | `TRUST_PROXY_HEADER=true` |
| Two proxies of yours, each appending (e.g. a CDN in front of a load balancer) | `TRUST_PROXY_HEADER=true`, `TRUSTED_PROXY_HOPS=2` |
| Nothing in front (the relay terminates connections itself) | nothing: none of these headers can be trusted |

A header value that is not an IP address is ignored (the next source is
used). Every per-IP limit counts an IPv6 client by its **/64**, not its full
address: one host is normally given a whole /64, and could otherwise rotate
through 2^64 addresses with a fresh budget each. An IPv4-mapped IPv6 address
counts as the IPv4 address it carries.

Turning on a header with no proxy in front that sets it lets callers forge
their IP, and so does `TRUST_CLOUDFLARE` when the origin also accepts traffic
that bypasses Cloudflare. `/readyz` reports what is in use as `client_ip_from`
(e.g. `x-forwarded-for[-1],socket`).

### HTTP headers and CORS

Every HTTP response from the relay carries `X-Content-Type-Options: nosniff`
and `Referrer-Policy: no-referrer` (a dashboard URL contains the room code,
which must not leak to other sites). With `HIVELANE_ENV=staging` or
`production` it also sends `Strict-Transport-Security: max-age=31536000;
includeSubDomains`, so only serve those environments over HTTPS (on a host
whose subdomains are HTTPS too). HTML responses, which is the bundled
dashboard, also get `X-Frame-Options: DENY` and:

```
default-src 'self'; script-src 'self' 'sha256-…' 'sha256-…' https://cdn.tailwindcss.com;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com;
img-src 'self' data:; connect-src 'self' ws: wss: https:; object-src 'none'; base-uri 'self';
form-action 'self'; frame-ancestors 'none'
```

The dashboard loads Tailwind's browser build and Google Fonts, and has two
inline scripts (the theme-before-paint snippet and the Tailwind config).
Rather than `'unsafe-inline'`, the relay hashes the inline scripts of the
`index.html` it serves and allows exactly those (`'sha256-…'`, recomputed when
the file changes), so injected markup cannot run script. Other HTML the relay
might serve falls back to `'unsafe-inline'` in `script-src`. `style-src` keeps
`'unsafe-inline'` because the Tailwind Play CDN injects its CSS as `<style>`
elements at runtime. `connect-src` allows any `ws:`, `wss:` and `https:`
target because the relay URL is baked in at build time (`VITE_WS_URL`) and may
differ from the page's origin. An unhandled error's `500` carries the same
headers. `frame-ancestors 'none'` keeps other sites from framing an owner's
Approve button.

Still to do, once the redesigned dashboard compiles Tailwind at build time
instead of loading the Play CDN (which Tailwind says not to use in production,
and which cannot carry Subresource Integrity): drop
`https://cdn.tailwindcss.com` from `script-src`, drop `'unsafe-inline'` from
`style-src`, move the theme snippet into a bundled file, and narrow
`connect-src` to `'self'` plus the configured relay origin. Until then a
compromise of that CDN runs script on the dashboard's origin, where owner
tokens sit in `localStorage`. A dashboard hosted elsewhere (GitHub Pages, a static host) gets its headers from that
host, not from the relay.

CORS: `CORS_ORIGINS` is required outside development. In production the relay
refuses to start when `CORS_ORIGINS` contains `*` or `CORS_ORIGIN_REGEX` is a
wildcard. The regex is tested against origins nobody would list
(`https://cors-wildcard-probe.invalid`, `null`), so `.*`, `https?://.*` and
`^.+$` are all refused while `https://[a-z0-9-]+\.example\.com` is fine. A
wildcard must also be followed by a literal dot: `https://.*example\.com` is
refused, since it accepts `https://evilexample.com`. An invalid regex fails
the boot in every environment.

### Several instances on Fly

With Redis shared by several Fly machines, a room's WebSocket upgrades are
replayed (`fly-replay`) to the machine holding its lease. Fly documents the
headers its proxy adds on a replay (`fly-replay-src`) and on a fallback
(`fly-replay-failed`), but not that it strips them from client requests, so
the relay does not trust them as sent:

- each replay carries `state=<time>.<HMAC>` keyed by `HIVELANE_REPLAY_SECRET`
  (or, unset, a key derived from `HIVELANE_REDIS_URL`, which every instance
  sharing leases has). `fly-replay-src` ("already replayed, serve it here")
  counts only with a valid state less than 30 s old; otherwise the request is
  routed normally. Set the same `HIVELANE_REPLAY_SECRET` on every machine, or
  none.
- `fly-replay-failed` moves a room's lease only when its `replay_source` is
  this machine **and** the owner fails a 1 s health probe over Fly's private
  network (`<owner>.vm.<app>.internal`), and at most once per room every 30 s.
  A forged header can at most get its own sender served on the wrong machine.

Lease and store calls to Redis made while routing an upgrade run in a worker
thread, so a slow Redis does not stall every room on the instance.

### What the relay stores, and for how long

- **Durable** (SQLite at `HIVELANE_STATE_PATH`, or Redis): per room code, its
  creation time, its settings (conflict windows, `e2e` flag, `history_hours`),
  its access record (owner-token SHA-256, `require_approval`) and the owner's
  webhooks (URL, format, events; see [History and
  webhooks](#history-and-webhooks)). Removed when the room expires (7 days) or
  about 30 minutes after it empties.
- **In memory only, lost on restart:** each connected member's latest activity
  (prompt and diffs, within the caps above), latest intent and current plan
  (all dropped when the member leaves; a plan also expires after its TTL, 30
  minutes by default and at most 24 hours); recent file touches (paths,
  symbols, line ranges, diffs), swept once older than the room's warn window
  (at least 60 s); the last 1,000 warn/block decision ids with their vote
  tallies; pending join requests (at most 5 minutes); in a room with
  `history_hours` set, the feed events it broadcast over that many hours
  ([History and webhooks](#history-and-webhooks)).
- **Logs:** never prompts, diffs, plan summaries or intent text (an intent is
  logged as its length, at DEBUG). Messages that fail validation are logged by
  field and error, without the submitted values. Logs do contain room codes,
  display names, agent kinds, counts, and the file paths of warn/block
  decisions and conflicts (hashes in an e2e room). On stdout, room codes in
  messages and in the access log (`/ws/<code>`, `/rooms/<code>`) are shown as
  two characters and a short hash; each message is capped at 2,000
  characters, and one message template is logged at most 120 times a minute
  (errors always; the next line says how many were suppressed).
- **Metrics:** aggregate counters and gauges only; no codes, names or paths.
- **Third parties:** with `GEMINI_API_KEY` set, the relay sends prompts and
  diffs of cleartext rooms (trimmed) to Google's Gemini API for captions and
  conflict explanations. That text is wrapped in delimiters with a random
  per-call nonce (anything delimiter-like in it is removed first), and what
  comes back is treated as untrusted: one line, capped, URLs and markup
  stripped, explanations prefixed "AI assessment:". The model can add detail
  to a conflict alert but never suppress one (an "unrelated" verdict keeps the
  same-file alert). Calls are capped process-wide (`GEMINI_MAX_CONCURRENCY`,
  default 8; `GEMINI_MAX_CALLS_PER_MINUTE`, 120; `GEMINI_DAILY_CALL_BUDGET`,
  20,000 per UTC day), with placeholders past any cap. Never from an e2e room; without the key, nothing
  leaves the relay. A room owner's webhooks send that room's blocks, warnings,
  conflict alerts and join requests to the URLs the owner chose (from an e2e
  room, only that something happened).

### What the relay can see

In a cleartext room, everything members send: display names, agent kinds, repo
ids (a hash of the remote URL), file paths, symbols, line ranges, branches,
prompts, plans, intents and diffs (unless the repo sets `share_diffs = false`,
or the file is withheld or redacted by the daemon, see below). In an e2e room
it sees the room's shape but not its content: paths, symbols and branches as
keyed hashes, empty prompts and diffs, and ciphertext it copies onto its
broadcasts without opening; the details are in
[End-to-end encryption](#end-to-end-encryption). Either way it sees who is
connected, when, from which IP, and how much they send.

### History and webhooks

Both are off until someone turns them on: history by the team's committed
`.hivelane.toml`, webhooks by the room's owner. Setup is in
[notifications.md](notifications.md).

**Room history** (`history_hours = 4` in `.hivelane.toml`, 0 by default, at
most 24) makes the relay keep the room's feed so a dashboard can replay it.

- *What:* exactly the feed events the relay broadcast to the room (activity
  updates with prompts, diffs and captions as relayed, intents, plans,
  warn/block decisions with their reasons, conflict alerts, joins and leaves,
  settings changes, vote tallies), nothing else and nothing derived. In an
  e2e room that means hashes and the members' ciphertext blobs only.
- *Where:* the relay process's memory. Never the room store, SQLite, Redis,
  disk or logs.
- *How long:* at most `history_hours`, and at most 5,000 events and 4 MB per
  room (oldest dropped first), and 32 MB across all rooms in the process (the
  room adding events gives up its own oldest first). These count compact JSON;
  resident memory is several times that. It is gone when the
  relay restarts, when the room empties and is evicted (about 30 minutes after
  the last socket leaves), and at once when `history_hours` goes back to 0.
  A join whose settings do not mention `history_hours` (an older daemon, a
  file without the key) leaves the room's setting alone.
- *Who can read it:* any member or dashboard admitted to the room, with
  `history_request`, answered to the requester only in chunks of at most 200
  events and about 256 KB, once per socket every 10 seconds, 12 times a
  minute per client address, and at most 16 MB of replay per room a minute. This changes what a newcomer
  learns: without history they see the room's current state; with it, up to
  `history_hours` of the past. In an approval-required room, approving someone
  now also shows them that history. In an e2e room, anyone with the room key
  can decrypt the replay, just as they could the live feed.
- *Metrics:* `hivelane_history_events` and `hivelane_history_bytes`, process
  totals (no per-room series, which would publish room codes).

**Webhooks** are configured by the room's owner over HTTP
(`POST`/`GET /rooms/{code}/webhooks`, `DELETE /rooms/{code}/webhooks/{id}`).

- *Authentication:* the `X-Hivelane-Owner-Token` header, hashed and compared in
  constant time with the stored owner-token SHA-256 (read from the store when
  the room has no live state, so managing webhooks does not revive a room). A
  missing or wrong token and an unknown room code get the same `403`, and the
  API takes 30 calls a minute per client IP, so it is no faster a room-code
  oracle than the WebSocket endpoint. CORS lets the configured dashboard
  origins send that header and `DELETE`.
- *What is stored:* per webhook its URL, format (`slack`, `discord`,
  `generic`), events and creation time, at most 3 per room, in the room store
  next to the room's access record (the SQLite `webhooks` column, or the
  Redis `<prefix>:webhooks` hash), in plain text: a Slack or Discord URL is
  itself the credential, so whoever can read the store or Redis can post to
  those channels. They are deleted with the room code. The relay never logs a
  URL (only the webhook's id) and `GET` returns them masked (scheme and host
  only: every character of a Slack or Discord path is part of the secret).
- *SSRF protection.* The relay makes requests to URLs a room owner (that is,
  anyone who can create a room) typed in, so:
  - only `https://` (plus `http://localhost` and other loopback names with
    `HIVELANE_WEBHOOKS_ALLOW_PRIVATE=1`), with no user name or password in the
    URL;
  - in every environment, development included, the host is resolved and
    **every** address it resolves to must be globally routable unicast: no
    private (10/8, 172.16/12, 192.168/16, fc00::/7), loopback, link-local
    (169.254/16, including cloud metadata at 169.254.169.254, and fe80::/10),
    CGNAT (100.64/10), multicast, unspecified, reserved or documentation
    ranges. IPv6 forms that embed an IPv4 address (IPv4-mapped,
    IPv4-compatible `::/96`, IPv4-translated `::ffff:0:0:0/96`, 6to4, Teredo,
    NAT64 `64:ff9b::/96`) are judged by that address, and IP literals are
    checked the same way. Only `HIVELANE_WEBHOOKS_ALLOW_PRIVATE=1` turns this
    off, for testing a receiver on your own machine; a production relay
    refuses to start with it, and a relay that has it logs a warning at
    startup;
  - this is checked when the webhook is added and again before every delivery,
    and the delivery connects only to the addresses that were just checked (in
    resolver order, the next one when a connection is refused; with the
    original host in `Host` and in TLS SNI, and the certificate verified
    against it), so DNS rebinding between check and connection cannot point
    it at an internal service;
  - redirects are never followed, `HTTP(S)_PROXY` variables are ignored, and
    each attempt times out after 5 s;
  - the receiver's answer is its status line alone: the response body is
    never read (or decompressed; the request sends `Accept-Encoding:
    identity`), so a receiver cannot make the relay buffer a large or
    gzip-bombed reply.
- *Delivery:* off the event path (background tasks), at most 30 notifications
  a minute per room, two retries with backoff on network errors, `429` and
  `5xx`, and a webhook is paused for 15 minutes after 10 failed deliveries in
  a row. Outcomes are counted in `hivelane_webhook_deliveries_total{outcome}`.
- *What a notification says:* in a cleartext room the who (display name and
  agent kind), what (block, warning, conflict verdict, join request), file
  path, decision scope, the people it collided with, the relay's reason or
  conflict explanation, and the room code. **The room code is the room's
  key**: post only to channels whose members may join the room. In an e2e room
  the payload says only "a blocked edit in room CODE" (or "a warned edit", "a
  conflict alert", "a join request waiting for approval"): no paths, hashes,
  names, reasons or prompts. Text from members and from Gemini is escaped for
  the target: Slack mrkdwn and Discord markdown (masked links included) are
  escaped, paths sit in code spans, and `://` and `www.` are defanged
  (`https[:]//`), so a code holder cannot plant a clickable link in the
  owner's channel.

Browser notifications (the dashboard's **Notifications** view) are local: the
dashboard that already receives the feed shows them, the preference sits in
that browser's `localStorage` (`hivelane:notify:<CODE>`), and nothing about
them reaches the relay.

## Daemon and hooks

Every machine runs one Hivelane daemon on `127.0.0.1:4600` (or `HIVELANE_PORT`).
Agent hooks, the CLI, `hivelane mcp`, `hivelane watch`, the git pre-commit hook
and the VS Code extension all talk to it over loopback HTTP.

**Who can talk to the daemon.** Binding to loopback keeps other machines out,
but not other local processes or web pages: a browser tab can reach
`127.0.0.1` too, directly or through DNS rebinding. So every endpoint except a
bare `GET /health` requires a per-install secret:

- `HIVELANE_HOME/token` (default `~/.hivelane/token`): 32 random bytes,
  url-safe base64, created the first time the daemon or `hivelane up` runs.
  It is created owner-only (mode `0600`) on macOS and Linux; on Windows it
  inherits your user profile's ACL (you and administrators).
- Clients send it as `X-Hivelane-Token`; the daemon compares it in constant
  time and answers `401` otherwise. Whoever can read the file is, by
  definition, the user the daemon belongs to.
- The daemon also checks the `Host` header: anything but `127.0.0.1:<port>`
  or `localhost:<port>` gets `421`. A DNS-rebinding page reaches the socket
  under its own hostname and cannot forge that header.
- Admin routes (`/repos`, `/repos/remove`, `/rooms/decide`, `/rooms/policy`,
  `/shutdown`) additionally refuse any request carrying an `Origin` header,
  which browsers always send cross-site.
- `GET /health` is the one unauthenticated route and says only
  `{"ok": true, "service": "hivelane-daemon"}`, so tools can tell "no daemon"
  from "a daemon that refuses my token".

**The daemon proves itself first.** The token also has to protect clients from
the port: on a shared machine, another user's program could bind `4600` while
your daemon is stopped and wait for the token, prompts and file contents. So
before sending anything, every client (the hooks, `hivelane mcp`, the CLI and
the VS Code extension) asks `GET /health?nonce=<32 hex>` without its token, and
only a listener that answers with `HMAC-SHA256(token, "hivelane-daemon-proof:"
+ nonce)` gets the real request. Anything else is treated as offline: the hook
fails open with a notice that nothing was sent, and nothing about you reaches
that process. A daemon under another `HIVELANE_HOME` cannot answer either, so
it gets the same notice; `hivelane down` then `hivelane up` replaces a daemon
from before this check.

**Token lifecycle.** Clients read the file on every call (the hook with a plain
`open()`, nothing cached across processes), so nothing needs restarting when it
changes. If the file is deleted and recreated while the daemon runs, the
daemon re-reads the file it started from on the first mismatch and adopts the
new token (for the proof too). A daemon and a client using different `HIVELANE_HOME`s fail the proof:
`hivelane status` and `hivelane doctor` ("Daemon token readable", "Daemon
accepts this token") say so, and the VS Code extension takes a
`hivelane.tokenPath` setting for that case.

**Hooks fail open.** A hook that cannot reach the daemon, or whose token is
refused, lets the edit through (a coordination tool must never stall the
agent) and shows the agent's user a notice at most once every ten minutes.
A missing token file is treated like a stopped daemon. This costs about one
small file read per hook; with the daemon up the hook still takes well under
150 ms p95 (`scripts/bench_hook.py`).

**What leaves the machine: secret redaction.** Before a diff, prompt, plan
summary or intent is sent (and, in an end-to-end room, before it is
encrypted), the daemon replaces likely secrets with `[REDACTED:<kind>]`:

| Kind | What |
| --- | --- |
| `aws-access-key`, `aws-secret-key` | `AKIA…`/`ASIA…` key ids; 40-character values assigned to an `aws…secret` name |
| `github-token`, `gitlab-token` | `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`, `github_pat_`; `glpat-`, `gldt-`, `glrt-`, … |
| `slack-token` | `xox[abposr]-…`, `xoxe-…`, `xapp-…`, Slack webhook URLs |
| `stripe-key` | `sk_live_…`, `rk_live_…`, `sk_test_…`, `rk_test_…`, `whsec_…` |
| `google-api-key` | `AIza…` |
| `openai-key`, `anthropic-key` | `sk-…`, `sk-proj-…`, `sk-ant-…` |
| `npm-token`, `pypi-token`, `huggingface-token` | `npm_…`, `_authToken=`/`_auth=`/`_password=` values; `pypi-AgE…`; `hf_…` |
| `sendgrid-key`, `twilio-key` | `SG.….…`; `AC…`/`SK…` + 32 hex |
| `azure-key` | `AccountKey=` / `SharedAccessKey=` values, a SAS URL's `sig=` |
| `url-password` | the password in `scheme://user:password@host` (`postgres://`, `mongodb+srv://`, `https://`, …) |
| `connection-password` | `Password=…;` / `Pwd=…;` in JDBC/ODBC/ADO.NET connection strings |
| `docker-auth` | `"auth": "<base64>"` in a Docker config |
| `auth-header` | the value after `Bearer`, `Basic` or `token` (with a digit in it) |
| `mysql-password` | `mysql … -pSECRET` / `--password=SECRET` |
| `private-key` | `-----BEGIN … PRIVATE KEY-----` and PGP `PRIVATE KEY BLOCK` armour (to the END line, or the end of a truncated diff); a PuTTY key's `Private-Lines` |
| `jwt` | `eyJ….eyJ….…` |
| `secret` | a quoted literal of 8+ characters (optionally `f"…"`, `b"…"`, `r"…"`) assigned to a secret-sounding name (`api_key = "…"`, `"password": "…"`, `const apiKey: string = "…"`); for password/passphrase/secret names the value may contain spaces. Unquoted `key: value` too in `.yaml`, `.toml`, `.ini`, `.cfg`, `.conf`, `.properties` files and in prompts and plans |
| `env-value` | every value of a `KEY=value` line in a file named `.env*` or `*.env` |
| `custom` | anything matching the repo's `redact_patterns` |

A name is secret-sounding by its **words**, split on `_`, `-`, `.` and
camelCase: one of password, passwd, passphrase, pass, pwd, secret(s), token,
credential(s), creds, bearer, or a pair such as api key, access key, private
key, client secret. A name whose last word says it is *about* a secret is not
one: `token_url`, `secret_arn`, `credentials_path`, `password_hash_algo`,
`auth_key_id`, `pwd_hint`, `tokenType`. So `tokenizer`, `max_tokens`,
`bypass_mode`, `compass` and `passenger_class` are left alone.

Ordinary code is left alone: `token = get_token()`, comparisons
(`token == "…"`), keyword arguments (`password=password`), environment
lookups, placeholders (`"${DB_PASSWORD}"`, `"changeme"`, `"********"`), URLs
without a password, file paths, and labels that spell out what they are
without any digit (`"X-Hivelane-Token"`). File paths are never rewritten: the
`diff --git` / `--- a/…` / `+++ b/…` header lines of a diff are skipped, but
only where a header can be (before the first `@@` hunk, or after a hunk's
line counts are used up). A content line that merely looks like a header (a
removed SQL comment `-- a/…` shows as `--- a/…`) is redacted like any other.

Some files are never sent at all: the diff of `.env*`, `*.env`, `*.pem`,
`*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.ppk`, `*.kdbx`,
`id_rsa*` (and the other ssh key names), `credentials*.json`,
`service-account*.json`, `*-key.json`, `secrets.{yml,yaml,json,toml}`,
`.npmrc`, `.pypirc`, `.netrc`/`_netrc`, `.git-credentials`, `.pgpass`,
`.htpasswd`, `.vault-token`, `*.tfstate[.backup]`, `*.tfvars`, `kubeconfig*`,
`.kube/config`, `.docker/config.json`, `.aws/credentials` and `.aws/config` is
replaced by `[withheld: sensitive file]`. The path still goes, so a teammate
touching the same file is still warned.

Configure it in `.hivelane.toml`:

```toml
redact = true                                # default; false turns the patterns off
redact_patterns = ["ACME-[0-9]{6}", "corp_[a-z0-9]{32}"]   # extra regexes (Python syntax)
```

`redact = false` never un-withholds sensitive files. Redaction is a safety net
for the patterns above, not a guarantee: a secret in a shape it does not know
still goes out. Use `share_diffs = false` to send paths and symbols only, and
`ignore = [...]` to keep files out of coordination entirely. Not covered:
Discord bot tokens and other formats without a fixed prefix, secrets in file
paths, symbol names or branch names. Redacting a 200 KB diff takes a few
milliseconds (tested: under 10 ms), and no rule backtracks super-linearly on
hostile input.

**Transport.** The CLI refuses a plaintext `ws://` relay on any host but this
machine (`localhost`, `127.0.0.0/8`, `::1`, `*.localhost`): every path, diff,
prompt and the room code would cross the network unencrypted. Use `wss://`, or
opt in on a network you trust with `hivelane up --insecure` (or
`insecure = true` in `.hivelane.toml`). A repo the daemon adopts on its own,
such as a fresh clone, is refused when its committed file sets `insecure = true`
or `redact = false`: run `hivelane up` in it to accept the team's choice, or
set the same key in your personal config. `hivelane doctor` fails a plaintext
remote relay and warns about one you opted into.

**Room ownership.** `hivelane up` keeps the owner token `POST /rooms` returns
when it creates a room in the personal config (`owner_tokens`, file mode
`0600`) and presents it when it joins that room; it is never written to the
project file, never printed and never in `hivelane status` output. As the
owner you can make newcomers wait for approval (`hivelane room
require-approval on`); waiting requests appear in `hivelane status` and as one
line in the daemon log, and `hivelane room approve|deny <request_id>` answers
them. A teammate whose join is held sees "waiting for an owner to approve" in
`hivelane up` (which keeps waiting for up to the relay's five minutes).

## End-to-end encryption

With `e2e = true` in the committed `.hivelane.toml`, the relay coordinates the
room without being able to read it.

**What the relay sees.** It matches overlaps on equality only, so it gets:

- every file path, symbol and branch as `h:` + the first 32 hex digits of
  `HMAC-SHA256(mac_key, value)`. Equal names hash equal, so two teammates
  editing `server/room.py` still collide; without the key the relay can only
  test guesses by brute-forcing the passphrase (see **Keys**). `<imports>`
  (the import-block symbol) stays as is.
- empty diffs, prompts, plan summaries and intents;
- the real values in each message's opaque `e2e` blob, which it copies onto
  its broadcasts (and onto the records it derives from them) untouched.

It still sees: who is in the room and when, display names and agent kinds,
which hashed files and symbols each person touches and how often, line-range
numbers, timing and message sizes (blobs are padded to size buckets, so
sizes are coarse: 256 B, powers of two to 16 KiB, then 16 KiB steps). The
repo id is a keyed hash in e2e rooms (`hash("repo:" + repo_id)`): the plain
repo id is only a truncated SHA-256 of the remote URL, or a public root
commit, which a relay could confirm by guessing. Equal hashes are visible as
equal: a file's hash is stable for the life of the room key, and the same
string hashes the same whether it is a path, a symbol or a branch. A
malicious relay can also replay an old blob or attach one teammate's blob to
another's message; blobs are authenticated as coming from *a* key holder,
not from a particular one. AI captions and Gemini conflict summaries are off in e2e
rooms (the relay has nothing to send them).

**Keys.** One passphrase per room, known to every teammate:

```
ROOM    = room code with every space and "-" removed, upper case
          (the relay's own normalisation: k7q2-xm9p and K7Q2XM9P are one room, one key)
salt    = "synergo-e2e-v2:" + ROOM
ikm     = PBKDF2-HMAC-SHA256(NFC(passphrase) (UTF-8), salt, 600000 iterations, 32 bytes)
enc_key = HKDF-SHA256(ikm, salt, info = "enc", length = 32)
mac_key = the same with info = "mac"
blob    = "2." + base64url_nopad(nonce (12 random bytes) || AES-256-GCM ciphertext || tag)
          associated data = the client message type ("activity", "intent", "plan", "edit_check")
          plaintext = compact JSON padded with trailing spaces to a size bucket
```

PBKDF2 rather than scrypt or Argon2 because the dashboard derives the same
key with WebCrypto alone, which has PBKDF2 and HKDF but no memory-hard KDF;
600000 iterations is OWASP's floor for PBKDF2-HMAC-SHA256. That makes each
guess cost a relay about 1.2 million SHA-256 compressions instead of ~10,
which matters because the relay holds known-plaintext tokens (the `main`
branch hash, public commit SHAs) to test guesses against. It does not save a
dictionary passphrase: use a generated key (256 random bits). `hivelane key
set` and a `HIVELANE_ROOM_KEY` that is shorter than 16 characters or looks
guessable get a warning (`crypto.passphrase_problem`).

Version 1 (no PBKDF2, salt `"synergo-e2e-v1:" + code.strip().upper()`, no
`2.` prefix) is still read, so stored history stays readable, but no longer
written. v1 and v2 clients do not match each other's hashes: upgrade every
teammate and the dashboard together.

`protocol/e2e-test-vectors.json` pins both versions (`vectors` and
`vectors_v2`); the daemon (`hivelane/crypto.py`) and the dashboard
(`frontend/src/e2e.ts`, WebCrypto) are both tested against it. AES-GCM needs the optional extra: `pipx install 'hivelane[e2e]'` (or
`pipx inject hivelane cryptography`); without it an e2e repo refuses to connect
with that instruction rather than falling back to cleartext.

**Sharing the key.** The passphrase is never in the committed file (a
`room_keys` or `owner_tokens` entry there is an error), never in the share
line and never sent to the relay. It comes from `HIVELANE_ROOM_KEY` or the
personal config's `room_keys = { CODE = "…" }` (mode `0600`).

1. The first teammate runs `hivelane up --e2e` (or adds `e2e = true` and runs
   `hivelane up`). With no key configured, it generates one (256 random bits),
   saves it and prints it once.
2. Send it to each teammate over a different channel than the room code (a
   password manager share, a DM): `hivelane key show` prints it again.
3. Teammates run `hivelane key set CODE` and paste it (input hidden), then
   `hivelane up`.
4. Dashboard viewers open the room link with `#k=<key>` appended, or paste the
   key when the dashboard asks. Browsers never send the fragment to a server,
   and the dashboard removes it from the address bar and keeps the key in
   that tab's sessionStorage for that room only. The browser's own history
   (and history sync) may still record the URL as first opened, key
   included, so pasting the key is safer than a `#k=` link. Sharing the link
   *with* the fragment shares the key: send them separately.

**Daemon behaviour.** Outgoing messages are rewritten as above (the real
values, already redacted, go into the blob; a blob that would exceed the
relay's 256 KB limit has its diffs cut, then dropped). Incoming blobs on edit
check replies, plan conflicts and the room feed are decrypted, so what the
agent reads names real files, symbols and branches, the teammate's task
appears in the decision, and `h:` tokens in the relay's reason text are
rewritten back to names (the file being asked about is always known; others
once their blob has been seen). A blob that does not open (a teammate with the
wrong key) is logged and ignored. If the room is e2e and the key is missing,
the repo reports a clear error and sends nothing; if a teammate's
`.hivelane.toml` turns e2e on while yours does not say so, your daemon stops
coordinating that repo instead of sending them cleartext. A join never
switches the room's e2e flag off.

**Always-warn files.** The relay judges manifests, lockfiles and config
(`package.json`, `pyproject.toml`, `*.lock`, `.env*`, `Dockerfile`, CI
workflows...) at file level whatever symbols an edit touches. It recognises
them by name, which it cannot read in an e2e room, so the daemon sends edits to
those files with no symbols and no line ranges: the relay then has nothing
finer than the file to judge by and reaches the same file-level decision.

**Dashboard.** With the key, the dashboard shows real paths, diffs, prompts,
plans and decisions, and a lock in the header. Without it: "Encrypted room,
paste the room key to view", with hashes shown as short `🔒 3f9a1c`
placeholders. With a wrong key: "Can't decrypt this room with that key" and
`can't decrypt·3f9a1c` placeholders, never ciphertext. Browsers only expose
WebCrypto on `https://` and `localhost` pages, so a dashboard served over
plain http on a LAN address cannot decrypt (it says so).

**Limits.** E2E hides content from the relay operator and from anyone who
breaks into it; it does not hide the room's shape (who, when, how much), and
it trusts every key holder fully. Rotating a key means a new room (`hivelane
leave --forget`, `hivelane up --e2e`) and a new key for everyone; turning e2e
off likewise needs a new room, since joins never clear the flag. Anyone with
the key and access to the relay's traffic can read the room's history. The VS
Code extension's file decorations come from the relay feed and show nothing
for hashed paths in e2e rooms.
