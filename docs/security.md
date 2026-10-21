# Security

What Synergo protects, from whom, and how. Synergo moves coordination
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
  encrypt from its own `.synergo.toml`, never from the relay's `e2e` flag, so a
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
for rooms it creates in `localStorage` under `synergo:owner-token:<CODE>`, so
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

Refusals are counted in `synergo_ws_rejections_total{reason}` as
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
`synergo_messages_dropped_total{reason}`: `room_quota`, `invalid` (fails
validation, including an oversized blob, or is not JSON) and `frame_too_large`.
A malformed frame is dropped without ending the session.

The frame cap applies after uvicorn has read the frame, and uvicorn accepts
frames up to 16 MiB by default; to stop larger ones before they are buffered,
also start uvicorn with `--ws-max-size 524288`.

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

Turning on a header with no proxy in front that sets it lets callers forge
their IP, and so does `TRUST_CLOUDFLARE` when the origin also accepts traffic
that bypasses Cloudflare. `/readyz` reports what is in use as `client_ip_from`
(e.g. `x-forwarded-for[-1],socket`).

### HTTP headers and CORS

Every HTTP response from the relay carries `X-Content-Type-Options: nosniff`
and `Referrer-Policy: no-referrer` (a dashboard URL contains the room code,
which must not leak to other sites). With `SYNERGO_ENV=staging` or
`production` it also sends `Strict-Transport-Security: max-age=31536000;
includeSubDomains`, so only serve those environments over HTTPS (on a host
whose subdomains are HTTPS too). HTML responses, which is the bundled
dashboard, also get `X-Frame-Options: DENY` and:

```
default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com;
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com;
img-src 'self' data:; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self';
form-action 'self'; frame-ancestors 'none'
```

The dashboard loads Tailwind's browser build and Google Fonts and configures
Tailwind in an inline script, hence those origins and `'unsafe-inline'`.
`frame-ancestors 'none'` keeps other sites from framing an owner's Approve
button. A dashboard hosted elsewhere (e.g. Vercel) gets its headers from that
host (`frontend/vercel.json`), not from the relay.

CORS: `CORS_ORIGINS` is required outside development. In production the relay
refuses to start when `CORS_ORIGINS` contains `*` or `CORS_ORIGIN_REGEX` is a
wildcard. The regex is tested against origins nobody would list
(`https://cors-wildcard-probe.invalid`, `null`), so `.*`, `https?://.*` and
`^.+$` are all refused while `https://[a-z0-9-]+\.example\.com` is fine. An
invalid regex fails the boot in every environment.

### What the relay stores, and for how long

- **Durable** (SQLite at `SYNERGO_STATE_PATH`, or Redis): per room code, its
  creation time, its settings (conflict windows, `e2e` flag) and its access
  record (owner-token SHA-256, `require_approval`). Removed when the room
  expires (7 days) or about 30 minutes after it empties.
- **In memory only, lost on restart:** each connected member's latest activity
  (prompt and diffs, within the caps above), latest intent and current plan
  (all dropped when the member leaves; a plan also expires after its TTL, 30
  minutes by default and at most 24 hours); recent file touches (paths,
  symbols, line ranges, diffs), swept once older than the room's warn window
  (at least 60 s); the last 1,000 warn/block decision ids with their vote
  tallies; pending join requests (at most 5 minutes).
- **Logs:** never prompts, diffs, plan summaries or intent text (an intent is
  logged as its length, at DEBUG). Messages that fail validation are logged by
  field and error, without the submitted values. Logs do contain room codes,
  display names, agent kinds, counts, and the file paths of warn/block
  decisions and conflicts (hashes in an e2e room).
- **Metrics:** aggregate counters and gauges only; no codes, names or paths.
- **Third parties:** with `GEMINI_API_KEY` set, the relay sends prompts and
  diffs of cleartext rooms (trimmed) to Google's Gemini API for captions and
  conflict explanations. Never from an e2e room; without the key, nothing
  leaves the relay.

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

## Daemon and hooks

Every machine runs one Synergo daemon on `127.0.0.1:4600` (or `SYNERGO_PORT`).
Agent hooks, the CLI, `synergo mcp`, `synergo watch`, the git pre-commit hook
and the VS Code extension all talk to it over loopback HTTP.

**Who can talk to the daemon.** Binding to loopback keeps other machines out,
but not other local processes or web pages: a browser tab can reach
`127.0.0.1` too, directly or through DNS rebinding. So every endpoint except a
bare `GET /health` requires a per-install secret:

- `SYNERGO_HOME/token` (default `~/.synergo/token`): 32 random bytes,
  url-safe base64, created the first time the daemon or `synergo up` runs.
  It is created owner-only (mode `0600`) on macOS and Linux; on Windows it
  inherits your user profile's ACL (you and administrators).
- Clients send it as `X-Synergo-Token`; the daemon compares it in constant
  time and answers `401` otherwise. Whoever can read the file is, by
  definition, the user the daemon belongs to.
- The daemon also checks the `Host` header: anything but `127.0.0.1:<port>`
  or `localhost:<port>` gets `421`. A DNS-rebinding page reaches the socket
  under its own hostname and cannot forge that header.
- Admin routes (`/repos`, `/repos/remove`, `/rooms/decide`, `/rooms/policy`,
  `/shutdown`) additionally refuse any request carrying an `Origin` header,
  which browsers always send cross-site.
- `GET /health` is the one unauthenticated route and says only
  `{"ok": true, "service": "synergo-daemon"}`, so tools can tell "no daemon"
  from "a daemon that refuses my token".

**Token lifecycle.** Clients read the file on every call (the hook with a plain
`open()`, nothing cached across processes), so nothing needs restarting when it
changes. If the file is deleted and recreated while the daemon runs, the
daemon re-reads the file it started from on the first mismatch and adopts the
new token. A daemon and a client using different `SYNERGO_HOME`s get `401`:
`synergo status` and `synergo doctor` ("Daemon token readable", "Daemon
accepts this token") say so, and the VS Code extension takes a
`synergo.tokenPath` setting for that case.

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
| `github-token` | `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`, `github_pat_` |
| `slack-token` | `xox[abposr]-…`, Slack webhook URLs |
| `stripe-key` | `sk_live_…`, `rk_live_…` |
| `google-api-key` | `AIza…` |
| `openai-key`, `anthropic-key` | `sk-…`, `sk-proj-…`, `sk-ant-…` |
| `private-key` | `-----BEGIN … PRIVATE KEY-----` blocks (to the END line, or the end of a truncated diff) |
| `jwt` | `eyJ….eyJ….…` |
| `secret` | a quoted literal of 8+ characters assigned to a name containing password, secret, token, api_key, access_key, client_secret, private_key, credential(s) or bearer (`api_key = "…"`, `"password": "…"`); unquoted `key: value` too in `.yaml`, `.toml`, `.ini`, `.cfg`, `.conf`, `.properties` files |
| `env-value` | every value of a `KEY=value` line in a file named `.env*` |
| `custom` | anything matching the repo's `redact_patterns` |

Ordinary code is left alone: `token = get_token()`, comparisons
(`token == "…"`), environment lookups, placeholders (`"${DB_PASSWORD}"`,
`"changeme"`, `"********"`), URLs without credentials, file paths, and labels
that spell out what they are without any digit (`"X-Synergo-Token"`). File
paths are never rewritten, including the `--- a/…` / `+++ b/…` lines of a diff.

Some files are never sent at all: the diff of `.env*`, `*.pem`, `*.key`,
`*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `id_rsa*` (and the other ssh key
names) and `credentials*.json` is replaced by `[withheld: sensitive file]`.
The path still goes, so a teammate touching the same file is still warned.

Configure it in `.synergo.toml`:

```toml
redact = true                                # default; false turns the patterns off
redact_patterns = ["ACME-[0-9]{6}", "corp_[a-z0-9]{32}"]   # extra regexes (Python syntax)
```

`redact = false` never un-withholds sensitive files. Redaction is a safety net
for the patterns above, not a guarantee: a secret in a shape it does not know
still goes out. Use `share_diffs = false` to send paths and symbols only, and
`ignore = [...]` to keep files out of coordination entirely. Redacting a 200 KB
diff takes a few milliseconds.

**Transport.** The CLI refuses a plaintext `ws://` relay on any host but this
machine (`localhost`, `127.0.0.0/8`, `::1`, `*.localhost`): every path, diff,
prompt and the room code would cross the network unencrypted. Use `wss://`, or
opt in on a network you trust with `synergo up --insecure` (or
`insecure = true` in `.synergo.toml`, which the daemon also honours for repos
it adopts on its own). `synergo doctor` fails a plaintext remote relay and
warns about one you opted into.

**Room ownership.** `synergo up` keeps the owner token `POST /rooms` returns
when it creates a room in the personal config (`owner_tokens`, file mode
`0600`) and presents it when it joins that room; it is never written to the
project file, never printed and never in `synergo status` output. As the
owner you can make newcomers wait for approval (`synergo room
require-approval on`); waiting requests appear in `synergo status` and as one
line in the daemon log, and `synergo room approve|deny <request_id>` answers
them. A teammate whose join is held sees "waiting for an owner to approve" in
`synergo up` (which keeps waiting for up to the relay's five minutes).

## End-to-end encryption

With `e2e = true` in the committed `.synergo.toml`, the relay coordinates the
room without being able to read it.

**What the relay sees.** It matches overlaps on equality only, so it gets:

- every file path, symbol and branch as `h:` + the first 32 hex digits of
  `HMAC-SHA256(mac_key, value)`. Equal names hash equal, so two teammates
  editing `server/room.py` still collide; without the key the relay cannot
  test guesses. `<imports>` (the import-block symbol) stays as is.
- empty diffs, prompts, plan summaries and intents;
- the real values in each message's opaque `e2e` blob, which it copies onto
  its broadcasts (and onto the records it derives from them) untouched.

It still sees: who is in the room and when, display names and agent kinds,
which hashed files and symbols each person touches and how often, line-range
numbers, timing, message sizes and the repo id (already a hash of the remote
URL). Equal hashes are visible as equal: a file's hash is stable for the life
of the room key. AI captions and Gemini conflict summaries are off in e2e
rooms (the relay has nothing to send them).

**Keys.** One passphrase per room, known to every teammate:

```
enc_key = HKDF-SHA256(ikm = passphrase (UTF-8),
                      salt = "synergo-e2e-v1:" + ROOM_CODE (uppercase),
                      info = "enc", length = 32)
mac_key = the same with info = "mac"
blob    = base64url_nopad(nonce (12 random bytes) || AES-256-GCM ciphertext || tag)
          associated data = the client message type ("activity", "intent", "plan", "edit_check")
```

`protocol/e2e-test-vectors.json` pins this; the daemon (`synergo/crypto.py`)
and the dashboard (`frontend/src/e2e.ts`, WebCrypto) are both tested against
it. AES-GCM needs the optional extra: `pipx install 'synergo[e2e]'` (or
`pipx inject synergo cryptography`); without it an e2e repo refuses to connect
with that instruction rather than falling back to cleartext.

**Sharing the key.** The passphrase is never in the committed file (a
`room_keys` or `owner_tokens` entry there is an error), never in the share
line and never sent to the relay. It comes from `SYNERGO_ROOM_KEY` or the
personal config's `room_keys = { CODE = "…" }` (mode `0600`).

1. The first teammate runs `synergo up --e2e` (or adds `e2e = true` and runs
   `synergo up`). With no key configured, it generates one (192 random bits),
   saves it and prints it once.
2. Send it to each teammate over a different channel than the room code (a
   password manager share, a DM): `synergo key show` prints it again.
3. Teammates run `synergo key set CODE` and paste it (input hidden), then
   `synergo up`.
4. Dashboard viewers open the room link with `#k=<key>` appended, or paste the
   key when the dashboard asks. Browsers never send the fragment to a server;
   the dashboard removes it from the address bar and history at once and keeps
   the key in that tab's sessionStorage for that room only. Sharing the link
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
`.synergo.toml` turns e2e on while yours does not say so, your daemon stops
coordinating that repo instead of sending them cleartext. A join never
switches the room's e2e flag off.

**Always-warn files.** The relay judges manifests, lockfiles and config
(`package.json`, `pyproject.toml`, `*.lock`, `.env*`, `Dockerfile`, CI
workflows...) at file level whatever symbols an edit touches. It recognises
them by name, which it cannot read in an e2e room, so the daemon sends edits to
those files with no symbols and no line ranges: the relay then has nothing
finer than the file to judge by and reaches the same file-level decision.

**Dashboard.** With the key, the dashboard shows real paths, diffs, prompts,
plans and decisions, and a lock in the header. Without it: "Encrypted room —
paste the room key to view", with hashes shown as short `🔒 3f9a1c`
placeholders. With a wrong key: "Can't decrypt this room with that key" and
`can't decrypt·3f9a1c` placeholders, never ciphertext. Browsers only expose
WebCrypto on `https://` and `localhost` pages, so a dashboard served over
plain http on a LAN address cannot decrypt (it says so).

**Limits.** E2E hides content from the relay operator and from anyone who
breaks into it; it does not hide the room's shape (who, when, how much), and
it trusts every key holder fully. Rotating a key means a new room (`synergo
leave --forget`, `synergo up --e2e`) and a new key for everyone; turning e2e
off likewise needs a new room, since joins never clear the flag. Anyone with
the key and access to the relay's traffic can read the room's history. The VS
Code extension's file decorations come from the relay feed and show nothing
for hashed paths in e2e rooms.
