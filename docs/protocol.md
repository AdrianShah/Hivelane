# Wire protocol

Everything a client and the Synergo relay say to each other: the room HTTP
API, the WebSocket handshake, every message in both directions, delivery
rules, limits, close codes and how the protocol is versioned. It is written
for anyone building a client in another language, and for reviewing changes
to the protocol.

This page describes protocol version **1**. The contract
itself is code, and wins wherever this page disagrees with it:

- [`synergo/protocol.py`](../synergo/protocol.py), the client's models;
- [`server/models.py`](../server/models.py), the relay's copy;
- [`protocol/schema.json`](../protocol/schema.json), a JSON Schema (draft
  2020-12) generated from `synergo/protocol.py` by `protocol/generate.py`.
  Its `x-client-to-relay` and `x-relay-to-client` maps list every message by
  direction.

`server/tests/test_protocol_contract.py` fails when these disagree. You do not
need any of this to support a new coding agent: agents talk to the local
daemon, not the relay, and [an adapter](adapters.md) is the way to add one.

## Overview

| | |
|---|---|
| Transport | WebSocket, `wss://` in production (`ws://` for a local relay) |
| URL | `<relay>/ws/<ROOM>`, for example `wss://relay.example.com/ws/K7M2PQXR` |
| Frames | UTF-8 text, one JSON object per frame |
| Discriminator | Every message has a string `type` |
| Room code | 8 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`; matched case-insensitively, spaces and dashes ignored. Older 6-character codes still resolve. |

A connection is one of two roles:

- A **member** joins with a name. Each daemon connection (one agent in one
  repo) is a member. Members send activity, intent, plans and edit checks,
  and receive everything the room broadcasts.
- A **spectator** joins without a name. The dashboard and the editor
  extension are spectators. They receive broadcasts and a catch-up snapshot,
  and may vote on decisions.

Either role becomes a room **owner** by presenting the room's owner token in
its `join`. Owners can approve joiners and change the room's join policy.

## HTTP endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/rooms` | Create a room. Optional body `{"settings": {"block_window_seconds", "warn_window_seconds"}}`. Returns `{"room_id", "settings", "owner_token"}`. `owner_token` is returned only here, once; the relay keeps only its hash. `422` for settings out of range, `429` (with `Retry-After`) when this address has created too many rooms, `503` when the relay is full. |
| `GET` | `/health` | Liveness: `{"status": "ok", "rooms": N}`. |
| `GET` | `/readyz` | Readiness and configuration, including `protocol_version` and `min_supported_protocol`. |
| `GET` | `/metrics` | Prometheus counters and gauges. Aggregates only, no room codes or names. |

Room creation is not authenticated: the room code is the shared secret. See
[Security](security.md) for the trust model.

## Connection lifecycle

```text
client                                        relay
  │ ── WebSocket upgrade /ws/<ROOM> ───────────► │
  │ ◄── rate_limited, close 4429 ─────────────── │  too many attempts from this IP
  │ ◄── room_rejected, close 4404 ────────────── │  unknown or expired code
  │ ── join ───────────────────────────────────► │
  │ ◄── name_rejected ────────────────────────── │  bad name: send another join
  │ ◄── version_rejected, close 4426 ─────────── │  member protocol too old
  │ ◄── join_pending ─────────────────────────── │  approval required: wait
  │ ◄── room_rejected, close 4403 ────────────── │  declined / timed out / queue full
  │ ◄── catch-up (user_joined, ...) ──────────── │  admitted
  │ ◄─► traffic ◄─────────────────────────────►  │
```

1. **Connect** to `/ws/<ROOM>`. Before anything else the relay checks the
   per-address connection budget, then the room code. Either failure arrives
   as a JSON message followed by a close, never a bare close, so a client
   always learns why.
2. **Send `join`** as the first frame. Until the join is accepted, nothing
   else is read.
3. **Handle the reply.**
   - `name_rejected`: the socket stays open. Send another `join` with a
     different name.
   - `version_rejected`: this client speaks a protocol older than the relay's
     `MIN_SUPPORTED_PROTOCOL`. The socket closes with 4426. Spectators are
     never version-checked.
   - `join_pending`: the room requires approval and this join had no owner
     token. Keep the socket open. Within five minutes it is either admitted,
     and the normal catch-up follows, or refused with `room_rejected` and
     closed with 4403. A name can be taken while waiting, in which case a
     `name_rejected` follows the approval.
4. **Catch-up.** A member first receives a `user_joined` for every member
   already present; then the whole room, the new member included, gets a
   `user_joined` for it, and a `room_settings` if its join carried settings.
   A spectator receives, in order: `room_settings`, `room_policy_update`, a
   `user_joined` per member, the latest `intent_update` per member, every
   active `plan_update`, and the latest `activity_update` per member. A
   connection that joined with the owner token also receives the open
   `join_request`s (and, as a member, a `room_policy_update`).
5. **Traffic** until either side closes. When a member disconnects, the room
   gets a `user_left`. A room whose last connection goes stays in memory for
   30 minutes; its code stays valid for a week from creation.

## Delivery

Two kinds of frames come from the relay:

- **Point-to-point, sent at once:** replies to the sender
  (`edit_check_response`, `plan_conflicts`), handshake results
  (`name_rejected`, `version_rejected`, `room_rejected`, `rate_limited`,
  `join_pending`), owner notifications (`join_request`, `join_resolved`) and
  the catch-up frames above.
- **Room broadcasts, batched:** every other relay-to-client message is queued
  and delivered to every member and spectator, the sender included, inside a
  `synergo_feed` frame about every 500 ms. A feed's `events` are complete
  messages in the order they happened.

A client must therefore handle each message both bare and inside a feed.
Unwrap `synergo_feed` first, then dispatch every event exactly as if it had
arrived on its own.

## Client to relay

| Message | Sent by | Answer |
|---------|---------|--------|
| [`join`](#join) | everyone, first | handshake replies above |
| [`activity`](#activity) | members | `activity_update` broadcast |
| [`intent`](#intent) | members | `intent_update` broadcast |
| [`edit_check`](#edit_check) | members | `edit_check_response` to the sender; an `edit_decision` broadcast for warn/block |
| [`plan`](#plan) | members | `plan_conflicts` to the sender; a `plan_update` broadcast |
| [`decision_feedback`](#decision_feedback) | members and spectators | `decision_feedback_update` broadcast |
| [`join_decision`](#join_decision) | owners | `join_resolved` to every owner |
| [`room_policy`](#room_policy) | owners | `room_policy_update` broadcast |

The relay ignores fields it does not know, frames that are not JSON objects,
messages with an unknown `type`, and messages that fail validation (they are
logged and dropped, never answered with an error). It never trusts identity
fields from a client: `user_id` is replaced with the connection's own name and
`timestamp` with server time.

### join

The first frame on every connection.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string or `null` | `null` | Display name. Omit or `null` to join as a spectator (a dashboard). See [Names](#names). |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `protocol_version` | integer | `1` | Protocol the client speaks. The relay reads an omitted value as `0`; members below `MIN_SUPPORTED_PROTOCOL` are refused. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque id of the repository the member works in; see [step 6](#writing-a-client-in-another-language) for how the reference client computes it. |
| `settings` | [RoomSettings](#roomsettings) or `null` | `null` | The team's committed conflict windows, applied room-wide when this member is admitted. Invalid settings are ignored, not fatal. |
| `owner_token` | string or `null` | `null` | The `owner_token` returned by `POST /rooms`. A matching token makes this socket a room owner; a wrong one is treated as none. |

#### RoomSettings

The team's conflict windows, as committed in `.synergo.toml`. The relay applies the settings of the most recently admitted member that sent any (last join wins), except that a join without `e2e` leaves the room's `e2e` flag alone.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `block_window_seconds` | number (0 to 600) | `20` | A peer's edit at most this old can block. |
| `warn_window_seconds` | number (0 to 3600) | `60` | A peer's edit at most this old can warn. |
| `e2e` | boolean | `false` | The room's members encrypt content (the team committed `e2e = true`). Informational for the relay. |

```json
{"type": "join", "name": "adrian", "agent": "claude-code", "protocol_version": 1,
 "repo_id": "3f2c9a1b7d4e5f60", "settings": {"block_window_seconds": 20, "warn_window_seconds": 60}}
```

A spectator: `{"type": "join"}`, or `{"type": "join", "name": null}`.

#### Names

The relay lowercases the name and strips everything but `a-z`, `0-9`, `_`
and `-`. The result must be 1 to 20 characters, must not contain a blocked
word, and must not already be in the room; a room holds at most 6 members.
The cleaned-up name is the member's `user_id` in everything the relay sends.

### activity

What a member changed: sent after edits land. Each accepted activity is
broadcast as an `activity_update` and becomes that member's latest snapshot
for spectator catch-up. The relay also compares it with recent peer activity
on the same files to produce `conflict_alert`s.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | Ignored: the relay substitutes the connection's own name. |
| `timestamp` | number | now | Ignored: the relay stamps server time. |
| `prompt` | string or `null` | `null` | The prompt that led to these changes, if any. |
| `changes` | [FileChange](#filechange)[] | required | One entry per changed file. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `base_commit` | string | `""` | Commit the working tree is based on; `""` if unknown. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

#### FileChange

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | required | Repo-relative path of the changed file. |
| `diff` | string | required | Unified diff of the change (`""` when the member does not share diffs). |
| `symbols` | string[] | `[]` | Function/class names touched (`"<imports>"` for an import block); empty if not extracted. |
| `line_ranges` | [integer, integer][] | `[]` | 1-based inclusive `[start, end]` line ranges in the new file; empty if not extracted. |

### intent

What a member is about to do, usually the prompt its agent just received.
Broadcast as an `intent_update`; the newest intent per member wins. A peer's
intent that mentions a file makes edit checks on that file warn.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | Ignored: the relay substitutes the connection's own name. |
| `prompt` | string | required | What the agent is about to do. Replaces the member's previous intent. |
| `timestamp` | number | now | Ignored: the relay stamps server time. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### edit_check

"May I edit this file?", asked before every edit. The relay answers the
sender immediately with an `edit_check_response` and never through the feed:
the agent is blocked until it arrives. Edit checks are not rate-limited.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | required | Client-chosen id, echoed in the reply. Must be unique among the client's in-flight checks. |
| `file_path` | string | required | The file about to be edited, repo-relative. |
| `symbol` | string or `null` | `null` | Legacy single symbol; merged into `symbols`. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `symbols` | string[] | `[]` | Functions/classes the edit is predicted to touch. |
| `line_ranges` | [integer, integer][] | `[]` | Line ranges the edit is predicted to touch. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

The decision rules (symbols, line ranges, windows, branches, plans) are in
[Conflict detection](conflict-detection.md). A client should give up waiting
after a short timeout (the daemon uses 1.5 s) and treat the edit as allowed.

### plan

A member's plan: the files and symbols it expects to touch. The relay
replies at once with a `plan_conflicts` listing files a peer is editing or
also plans to edit, stores the plan until it expires or is replaced, and
broadcasts it as a `plan_update`. Peers editing a planned file get a warning.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | Ignored: the relay substitutes the connection's own name. |
| `summary` | string | `""` | Plan text. |
| `files` | string[] | `[]` | Repo-relative paths the plan expects to touch. |
| `symbols` | string[] | `[]` | Symbols the plan expects to touch. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `ttl_seconds` | number (60 to 86400) | `1800` | How long the plan stays active. A new plan from the same member replaces it. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### decision_feedback

A vote on whether a warn/block decision was useful. One vote per socket per
decision; later votes from the same socket are ignored.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `decision_id` | string | required | The `decision_id` of an `edit_decision`. |
| `useful` | boolean | required | `true`: the decision helped; `false`: it was noise. One vote per socket per decision. |

### join_decision

An owner admits or declines a joiner waiting in an approval-required room.
Ignored from a socket without the owner token, and for requests already
settled.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | required | The `request_id` of a `join_request`. |
| `approve` | boolean | required | `true` admits the joiner, `false` declines. |

### room_policy

An owner switches join approval on or off. Persisted with the room and
broadcast as `room_policy_update`. Ignored from non-owners. Switching approval
off admits everyone waiting.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `require_approval` | boolean | required | `true`: every new joiner without the owner token waits for an owner's approval. |

## Relay to client

### edit_check_response

The answer to an `edit_check`, sent only to the member who asked.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | required | The `request_id` of the `edit_check` this answers. |
| `decision` | `"allow"`, `"warn"`, `"block"` | required | `allow`, `warn` (go ahead, but tell the user) or `block` (do not edit now). |
| `file_path` | string | required | The checked path, as sent. |
| `others` | [PeerTouch](#peertouch)[] | `[]` | The peers that drove the decision, most relevant first. |
| `reason` | string | `""` | One sentence about the peer that drove the decision; `""` for allow. |
| `scope` | `"symbol"`, `"lines"`, `"file"`, `"intent"`, `"plan"`, `"branch"` | `"file"` | Granularity of the decision: `symbol`, `lines`, `file`, `intent`, `plan` or `branch`. |
| `decision_id` | string | `""` | Id of a warn/block decision, for `decision_feedback`; `""` for allow. |

#### PeerTouch

One peer's recent involvement with the checked file.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The peer. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `seconds_ago` | number | required | How long ago the peer touched the file. |
| `prompt` | string or `null` | `null` | The peer's intent, if known. |
| `symbols` | string[] | `[]` | Symbols the peer touched. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `kind` | `"edited"`, `"planned"`, `"intent"` | `"edited"` | `edited` the file, `planned` to, or only mentioned it in its `intent`. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

```json
{"type": "edit_check_response", "request_id": "c3f1a2", "decision": "block",
 "file_path": "server/room.py", "scope": "symbol", "decision_id": "9b1c...",
 "reason": "sam's claude-code is editing Room.broadcast in server/room.py (4s ago)",
 "others": [{"user_id": "sam", "agent": "claude-code", "seconds_ago": 4.2,
             "prompt": "Rewrite broadcast fan-out", "symbols": ["Room.broadcast"],
             "branch": "main", "kind": "edited", "e2e": ""}]}
```

### plan_conflicts

The reply to a `plan`, sent only to its sender.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `conflicts` | [PlanConflict](#planconflict)[] | `[]` | Files in the plan that a peer is editing or also plans to edit. Empty when there are none. |

#### PlanConflict

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | required | The contested file. |
| `user_id` | string | required | The peer. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `kind` | `"edited"`, `"planned"` | required | The peer `edited` the file recently, or `planned` it. |
| `summary` | string | `""` | The peer's plan summary, for `planned`. |
| `seconds_ago` | number | required | Age of the peer's edit or plan. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### activity_update

A member's accepted `activity`, broadcast.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The member who made the changes. |
| `timestamp` | number | required | Server time the activity was accepted. |
| `prompt` | string or `null` | `null` | As sent. |
| `changes` | [FileChange](#filechange)[] | required | As sent, after the relay's size caps. |
| `caption` | string or `null` | `null` | One-line AI caption, or a placeholder when the relay has no model configured. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### intent_update

A member's latest `intent`, broadcast and replayed in catch-up.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The member whose intent changed. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `prompt` | string | required | The new intent. Newest wins. |
| `timestamp` | number | required | Server time. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### plan_update

A member's active plan, broadcast and replayed in catch-up while it has not
expired.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The member whose plan this is. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `summary` | string | `""` | Plan text. |
| `files` | string[] | `[]` | Repo-relative paths the plan expects to touch. |
| `symbols` | string[] | `[]` | Symbols the plan expects to touch. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `timestamp` | number | required | Server time the plan was accepted. |
| `expires_at` | number | required | Unix time after which the plan no longer counts. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### edit_decision

An `edit_check` that came back warn or block, broadcast so dashboards can
show it. Allowed edits are not broadcast.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The member whose edit was checked. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |
| `file_path` | string | required | The checked path. |
| `decision` | `"warn"`, `"block"` | required | `warn` or `block`. Allowed edits are not broadcast. |
| `others` | string[] | `[]` | Names of the peers that drove the decision. |
| `reason` | string | `""` | Human-readable explanation, safe to show verbatim. |
| `timestamp` | number | now | Unix time in seconds. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `scope` | `"symbol"`, `"lines"`, `"file"`, `"intent"`, `"plan"`, `"branch"` | `"file"` | Granularity of the decision: `symbol`, `lines`, `file`, `intent`, `plan` or `branch`. |
| `decision_id` | string | `""` | Id of a warn/block decision, for `decision_feedback`; `""` for allow. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### conflict_alert

Two members edited the same file within a short window. Best-effort and
asynchronous: it can arrive seconds after the activity, and never blocks
anything.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `users` | string[] | required | The members involved. |
| `file_path` | string | required | The file both edited. |
| `verdict` | `"overlapping"`, `"related"`, `"unrelated"` | required | `overlapping`, `related` or `unrelated`. |
| `explanation` | string | required | Why, in a sentence (AI-written when the relay has a model, heuristic otherwise). |
| `timestamp` | number | now | Unix time in seconds. |

### room_settings

The conflict windows in force. Broadcast when a joining member's settings
change them, and sent first in every spectator's catch-up.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `block_window_seconds` | number | required | The block window now in force. |
| `warn_window_seconds` | number | required | The warn window now in force. |
| `e2e` | boolean | `false` | Whether the room's members encrypt content. |
| `set_by` | string or `null` | `null` | The member whose join set these, or `null` for the room's creation settings. |
| `timestamp` | number | now | Unix time in seconds. |

### room_policy_update

Whether the room requires approval to join. Broadcast when an owner changes
it, and sent in every spectator's (and every owner's) catch-up.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `require_approval` | boolean | required | Whether new joiners need an owner's approval. |

### user_joined / user_left

Membership changes, broadcast. A joining member also receives a
`user_joined` for each member already present.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The member who joined (or, in catch-up, who is already here). |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The agent behind the member. Labelling only; never used for decisions. |

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | The member who left. |

### decision_feedback_update

The running tally of votes on one decision, broadcast after each vote.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `decision_id` | string | required | The decision voted on. |
| `useful_count` | integer | `0` | Votes that it was useful. |
| `noise_count` | integer | `0` | Votes that it was noise. |

### join_pending

Sent to a joiner held for approval. Keep the socket open and wait.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | required | Id of the pending request (owners see the same id). |
| `reason` | string | required | What is happening, for the user. |

### join_request / join_resolved

Sent to every owner, immediately: someone is waiting (`join_request`), and
later how the request ended (`join_resolved`), so every owner's list stays
current. Owners who connect while requests are waiting receive the open
`join_request`s in their catch-up.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | required | Answer with a `join_decision` carrying this id. |
| `name` | string | required | The waiting member's name, or `dashboard viewer` for a spectator. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The waiting member's agent kind. |
| `timestamp` | number | now | When the request was opened. |

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | required | The settled request. |
| `outcome` | `"approved"`, `"declined"`, `"timed_out"`, `"withdrawn"` | required | `approved`, `declined`, `timed_out` or `withdrawn` (the joiner left). |

### name_rejected

The `join` cannot be accepted under this name. The socket stays open for
another `join`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `reason` | string | required | `Invalid name`, `Name not allowed`, `Name taken` or `Room full`. Send another `join` on the same socket. |

### version_rejected

The member's `protocol_version` is below the relay's minimum. Followed by
close 4426.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `reason` | string | required | What to do (upgrade). The socket closes with 4426. |
| `server_protocol` | integer | `1` | The relay's `PROTOCOL_VERSION`. |
| `min_supported` | integer | `1` | The relay's `MIN_SUPPORTED_PROTOCOL`. |

### room_rejected

The room code is unknown or expired (close 4404), or an approval-required
room did not admit this joiner (close 4403).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `reason` | string | required | Why the room cannot be joined. The socket closes (4404 or 4403). |

### rate_limited

Too many connection attempts from this address. Sent before any `join`, so
spectators must handle it too. Followed by close 4429.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `reason` | string | required | Why. The socket closes with 4429. |
| `retry_after` | integer | required | Seconds to wait before reconnecting. |

### synergo_feed

A batch of room broadcasts. See [Delivery](#delivery).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `events` | object[] | required | Room broadcasts queued since the last feed, oldest first; each is a complete relay-to-client message. |

## Agent kinds

`agent` fields take one of: `cursor`, `claude-code`, `codex`, `antigravity`, `gemini-cli`, `devin`, `grok`, `aider`, `human`, `unknown`. The value labels lanes and
reasons; it never changes a decision. See [Versioning](#versioning) before
adding one.

## Limits

The relay bounds what one client can make it store and fan out. These are
the current defaults; operators can change some of them, and clients should
not depend on the exact numbers.

| Limit | Default | Over the limit |
|-------|---------|----------------|
| One frame | 512 KB | The socket is closed with 1009 |
| One file's `diff` | 64 KB | Truncated, ending in `…[truncated]` |
| A `prompt` or plan `summary` | 4 KB | Truncated the same way |
| Files per `activity` or `plan` | 50 | The first 50 are kept |
| Symbols per file change, plan or edit check | 64 | The first 64 are kept |
| Line ranges per file change or edit check | 256 | The first 256 are kept |
| An `e2e` blob | 262,144 characters | The message fails validation and is dropped |
| `activity`, `intent`, `plan` from one member | one of each per 0.5 s | Dropped silently |
| Inbound frames per room (`ROOM_MESSAGE_LIMIT`) | 600 per minute | Dropped silently |
| Connection attempts per address (`WS_ATTEMPT_LIMIT`) | 30 per minute | `rate_limited`, close 4429 |
| Members per room | 6 | `name_rejected` "Room full" |
| Joiners waiting for approval, per room | 10 | `room_rejected`, close 4403 |

## Close codes

| Code | Meaning | Sent after | What a client should do |
|------|---------|------------|-------------------------|
| `4404` | Room not found or expired | `room_rejected` | Stop; the room code is wrong or the room is gone |
| `4403` | Join refused by an approval-required room | `room_rejected` | Stop; ask an owner, then reconnect |
| `4426` | Client protocol too old | `version_rejected` | Stop; upgrade the client |
| `4429` | Too many connection attempts | `rate_limited` | Reconnect after `retry_after` seconds |
| `1009` | Frame too large | nothing | A client bug: do not resend that frame |
| `1000`, `1001`, `1006` | Normal close, going away, connection lost | nothing | Reconnect with backoff |

The `4xxx` codes are in the WebSocket private-use range; the rejection message
before each close carries the human-readable reason. Behind some proxies the
close code itself is lost, so rely on the message.

## Versioning

The relay and every client carry two numbers:

- `PROTOCOL_VERSION` (now 1): what this build speaks. A
  member sends it in its `join`.
- `MIN_SUPPORTED_PROTOCOL` (now 1): the oldest member
  protocol the relay accepts.

Both are in `/readyz`, `version_rejected` and `protocol/schema.json`
(`x-protocol-version`, `x-min-supported-protocol`).

**Additive changes do not bump the version.** These are safe in both
directions, so they ship without touching either number:

- a new field with a default (older peers ignore it, and a peer that does
  not send it gets the default);
- a new message type that older peers can ignore (they drop unknown types).

That is why the protocol is still at version 1 after
gaining symbols, line ranges, branches, plans, decision feedback, join
approval and encrypted payloads: every one of them was an optional field or an
ignorable message.

**Anything an older peer cannot satisfy is a breaking change:** removing or
renaming a field, changing a field's type or meaning, making an optional
field required, or a new obligation a client must meet for coordination to
stay correct. Bump `PROTOCOL_VERSION` in `synergo/protocol.py` and
`server/models.py` together. Raise `MIN_SUPPORTED_PROTOCOL` when the relay can
no longer serve the old clients safely; those members are then refused with
`version_rejected` instead of connecting and silently skipping checks.

**New enum values need the relay first.** The relay validates literal fields
strictly, so a value it does not know (a new agent kind, a new scope) makes
the whole message invalid there: an unknown `agent` in `join` is answered with
`name_rejected`, and an `activity` carrying one is dropped. Deploy the relay
that knows the new value before clients start sending it. In the other
direction, clients should read an unknown value they receive as the generic
one (`unknown` for an agent, a file-level reason for a scope) rather than
dropping the message.

So that this all works, every client must:

- ignore fields it does not recognise;
- ignore message types it does not recognise, inside feeds too;
- send only fields and values the relay it talks to knows, when it has to
  talk to older relays.

## End-to-end encrypted rooms

In a room whose committed `.synergo.toml` sets `e2e = true`, members hide
content from the relay while keeping overlap detection working, because the
relay only ever compares values for equality:

- every file path, symbol and branch in a cleartext field is replaced with a
  keyed hash, `h:` followed by 32 hex digits of HMAC-SHA256 under the room's
  MAC key, so equal values still collide;
- content (diffs, prompts, plan summaries) is sent as empty strings;
- the real values travel in the message's `e2e` field, AES-256-GCM under the
  room's encryption key, which the relay copies untouched onto the matching
  broadcasts (and onto `PeerTouch` and `PlanConflict` entries derived from
  that member's messages) for key holders to decrypt.

Keys come from a passphrase that never reaches the relay or the repository.
The key schedule and the blob format are specified in
[`synergo/crypto.py`](../synergo/crypto.py), with test vectors in
[`protocol/e2e-test-vectors.json`](../protocol/e2e-test-vectors.json) that
both the Python and the dashboard implementations are tested against. What
this does and does not protect is in [Security](security.md).

## Writing a client in another language

1. **Generate types** from `protocol/schema.json` with any JSON Schema code
   generator, or write them by hand from the tables above. Validate the frames
   your tests produce against the schema.
2. **Connect and join.** Open `<relay>/ws/<ROOM>` and send `join`. As a member,
   send your `protocol_version`. Handle `name_rejected` by joining again with
   another name, and treat `version_rejected`, `room_rejected` and close codes
   4403, 4404 and 4426 as final.
3. **Dispatch on `type`.** Unwrap `synergo_feed` and handle each event as if it
   had arrived alone. Ignore unknown types and fields.
4. **Correlate edit checks** by a `request_id` you generate. Time out quickly
   (the daemon waits 1.5 s) and treat a timeout, a dropped connection or a
   malformed reply as allow: coordination must never stop the user's work.
5. **Reconnect** with exponential backoff (the daemon starts at 1 s and caps at
   30 s), wait at least `retry_after` after `rate_limited`, and send `join`
   again on every new connection. Nothing survives a reconnect except the room.
6. **Match the reference client's identities** if you share rooms with
   Synergo daemons: send paths relative to the repository root with forward
   slashes, and compute `repo_id` the same way. `repo_id` is the first 16 hex
   digits of the SHA-256 of the normalized `origin` URL (host lowercased, with
   scheme, credentials, port and a trailing `.git` dropped, so ssh and https
   clones agree), or, without an origin, the first 16 characters of the
   smallest root commit id; see `compute_repo_id` and `normalize_remote_url`
   in [`synergo/daemon.py`](../synergo/daemon.py).
7. **Test against a real relay.** From a checkout:

   ```bash
   uvicorn server.main:app --port 8000
   curl -s -X POST http://localhost:8000/rooms     # {"room_id": "...", ...}
   ```

A minimal spectator, in JavaScript (Node 22 or a browser, no dependencies):

```js
const room = process.argv[2];
const socket = new WebSocket(`ws://localhost:8000/ws/${room}`);

socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'join' })));
socket.addEventListener('message', ({ data }) => {
  const message = JSON.parse(data);
  const events = message.type === 'synergo_feed' ? message.events : [message];
  for (const event of events) {
    if (event.type === 'edit_decision') {
      console.log(`${event.decision}: ${event.user_id} on ${event.file_path} (${event.reason})`);
    } else if (event.type === 'room_rejected' || event.type === 'rate_limited') {
      console.error(event.reason);
    }
  }
});
socket.addEventListener('close', ({ code }) => console.log(`closed (${code})`));
```

The daemon's connection code, `RoomConnection` in
[`synergo/daemon.py`](../synergo/daemon.py), and the dashboard's, in
`frontend/src/`, are complete reference clients for a member and a spectator.
