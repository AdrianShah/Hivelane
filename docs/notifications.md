# Notifications and replay

Synergo can tell you about blocked edits and conflicts without anyone
watching the board, and can replay what happened in a room over the last few
hours:

- **Webhooks** post a room's events to Slack, Discord or any HTTPS endpoint.
  The relay sends them, so they work with every browser closed. Owner only.
- **Browser notifications** pop up on your desktop while a dashboard tab is
  open. Per person, per room.
- **Replay** scrubs through the room's recent history on the dashboard.
  Opt-in per room with `history_hours`.

What the relay keeps for each, and the protections around it, are in
[security.md](security.md#history-and-webhooks).

## Webhooks (Slack, Discord, generic)

Webhooks belong to the room's **owner**: the dashboard (or `synergo up`) that
created the room and holds its owner token. Up to three per room.

### From the dashboard

1. Open the room you created and go to **Notifications** in the sidebar.
2. Under *Slack, Discord and webhooks*, paste the webhook URL. The format is
   guessed from the host (Slack, Discord, otherwise generic JSON); change it
   if you need to.
3. Pick the events: **Blocked edits**, **Warnings**, **Conflict alerts**,
   **Join requests** (approval-required rooms). Then **Add webhook**.

The list shows each webhook masked (`https://hooks.slack.com/…a1B2`) with its
delivery state: *Delivering*, *Last delivery failed (HTTP 404)*, or *Paused*
after ten failures in a row (retried after 15 minutes). **Remove** deletes it.

Other people's dashboards see a note that only the owner can configure
webhooks: the owner token lives in the browser that created the room
(`localStorage`, see [security.md](security.md#owner-tokens)).

### Slack

1. In Slack, create an app (or open yours) at <https://api.slack.com/apps>,
   enable **Incoming Webhooks**, and **Add New Webhook to Workspace** for the
   channel the team watches.
2. Copy the URL (`https://hooks.slack.com/services/T…/B…/…`) and add it with
   the format **Slack**.

Messages use Block Kit: a headline (*Edit blocked · priya's claude-code was
blocked from editing server/room.py (held by sam)*), fields for who, file,
decision and scope, the relay's reason, and a footer with the room code.
Text from the room is escaped, so a path or reason containing `<!channel>`
cannot ping anyone.

### Discord

1. In the Discord channel: **Edit Channel → Integrations → Webhooks → New
   Webhook**, then **Copy Webhook URL**
   (`https://discord.com/api/webhooks/…/…`).
2. Add it with the format **Discord**.

Messages are one embed per event (colour-coded: red block, amber warning,
purple conflict, blue join request), posted as "Synergo" with
`allowed_mentions` empty, so nothing in a message can `@everyone`.

### Generic JSON

Anything else that accepts a JSON `POST` (an automation service, your own
endpoint). The body is:

```json
{
  "source": "synergo",
  "version": 1,
  "event": "block",
  "room": "K7Q2XMAB",
  "e2e": false,
  "timestamp": 1760000000.0,
  "text": "priya's claude-code was blocked from editing server/room.py (held by sam)",
  "data": {
    "user": "priya", "agent": "claude-code", "file": "server/room.py",
    "decision": "block", "scope": "symbol", "others": ["sam"],
    "reason": "sam's cursor is editing Room.join in server/room.py (3s ago)"
  }
}
```

`event` is `block`, `warn`, `conflict_alert` (`data`: `users`, `file`,
`verdict`, `explanation`) or `join_request` (`data`: `name`, `agent`).
Requests come with `Content-Type: application/json` and `User-Agent:
Synergo-Relay-Webhooks/1`; any 2xx counts as delivered. They are not signed:
treat the URL itself as the secret, and use a hard-to-guess path.

### End-to-end encrypted rooms

The relay cannot read an e2e room, and sends nothing readable to a third
party: every format says only what happened, e.g. *a blocked edit in room
K7Q2XMAB*, with no file paths, names, reasons or prompts (`data` is `{}`).
Use browser notifications (below) for the details: the dashboard decrypts
them with the room key.

### The API

The dashboard uses three endpoints; scripts can too. Every call needs the
owner token in `X-Synergo-Owner-Token` (an unknown room and a wrong token get
the same `403`, so the API cannot be used to probe room codes):

```sh
# Add (201): url, kind (slack | discord | generic), events
curl -X POST "$RELAY/rooms/$ROOM/webhooks" \
  -H "X-Synergo-Owner-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://hooks.slack.com/services/…", "kind": "slack", "events": ["block", "conflict_alert"]}'

# List (URLs masked)
curl "$RELAY/rooms/$ROOM/webhooks" -H "X-Synergo-Owner-Token: $TOKEN"

# Remove (204)
curl -X DELETE "$RELAY/rooms/$ROOM/webhooks/$ID" -H "X-Synergo-Owner-Token: $TOKEN"
```

`$RELAY` is the relay's HTTPS origin (`https://relay.example.com`, the
`--relay` URL without `/ws`). The CLI keeps the token of a room it created in
your personal config (`owner_tokens` in `~/.synergo/config.toml`).

| Answer | Why |
| --- | --- |
| `403` | missing or wrong owner token, or no such room |
| `409` | the room already has 3 webhooks, or this URL is already registered |
| `422` | not `https://` (or `http://localhost` on a development relay), a user name or password in the URL, an unknown kind or event, a host that does not resolve, or one that resolves to a private, loopback or link-local address |
| `429` | more than 30 calls a minute from your IP |

### Limits and delivery

- At most **30 notifications a minute per room**; beyond that they are dropped
  (and counted in `synergo_webhook_deliveries_total{outcome="rate_limited"}`).
- Each delivery has a **5 s timeout** and is **retried twice** (after 1 s and
  4 s) on network errors, `429` and `5xx`. Other `4xx` answers and redirects
  are not retried (redirects are never followed).
- After **10 failed deliveries in a row** a webhook is paused for 15 minutes,
  then tried again; one success resets the count.
- Webhooks live as long as the room code: they are deleted when the room
  expires (7 days) or about 30 minutes after everyone leaves.

## Browser notifications

On the dashboard, **Notifications → Browser notifications**:

1. Turn the switch on. The browser asks for permission (only then: the
   dashboard never asks on its own). If you denied it before, allow
   notifications for the site in the browser's settings first.
2. Choose which **edit blocks** notify you: anyone's, **only blocks involving
   me** (enter the name you joined with, e.g. `ada`; it matches blocks of your
   agent and blocks your edits caused), or none.
3. Keep **Conflict alerts** on or off. Warnings never notify.

**Send a test notification** checks that it works. The setting is stored per
room in this browser (`localStorage`, key `synergo:notify:<ROOM>`) and only
works while a dashboard tab for the room is open (it does not use a service
worker or push service). In an e2e room the notification shows real paths
when the dashboard has the room key, and placeholders when it does not.

## Replay

Replay needs the relay to keep the room's history, which is **off by
default**. Turn it on for the team in the committed `.synergo.toml`:

```toml
history_hours = 4   # 0 (default) keeps nothing; at most 24
```

The next member whose `synergo up` (or reconnect) carries the setting applies
it to the room, like the conflict windows. A teammate on an older checkout
without the key leaves the room's setting alone; `history_hours = 0` turns it
off and drops what was kept. A room created over the API can start with it:
`POST /rooms {"settings": {"history_hours": 4}}`.

Then open **Replay** in the dashboard sidebar. It loads the kept events and
shows the board at the time on the scrubber: lanes (who was online, their
intent, prompt or caption, the files they had changed, their plan), each
lane's latest decisions, the decisions so far and the conflicts active at
that moment. Ticks above the scrubber mark blocks (red), warnings (amber) and
conflicts (purple). **Play** runs it forward at 1×, 10× or 60×; drag the
scrubber to jump. **Reload history** fetches it again (at most once every
10 seconds).

Things to know:

- History starts when it is switched on and lives only in the relay's memory:
  a relay restart, or the room emptying out, starts it over.
- It is bounded: the last `history_hours`, at most 5,000 events and 8 MB per
  room, oldest dropped first.
- Someone who joined before the oldest kept event appears when they first act.
- In an e2e room the relay keeps only hashes and ciphertext; the dashboard
  decrypts the replay with the room key exactly as it does the live feed.
- When history is off, the Replay view says how to turn it on.

Under the hood the dashboard sends `{"type": "history_request", "since": 0}`
on its spectator socket (members may ask too; `since` is a relay timestamp,
0 for everything kept) and receives `history_chunk {events, done}` frames: at
most 200 feed events each, in order, exactly as the feed carried them, with
`done: true` on the last. Events the feed carries without a `timestamp`
(`user_joined`, `user_left`, ...) get the relay's broadcast time as
`timestamp`. A room without history answers with one empty, done chunk, and
a socket that asks again within 10 seconds is ignored. The frames are in
[protocol/schema.json](../protocol/schema.json).
