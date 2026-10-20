# Wire protocol

Everything a client and the Hivelane relay say to each other: the room HTTP
API, the WebSocket handshake, every message in both directions, delivery
rules, limits, close codes and how the protocol is versioned. It is written
for anyone building a client in another language, and for reviewing changes
to the protocol.

This page describes protocol version **1**. The contract
itself is code, and wins wherever this page disagrees with it:

- [`hivelane/protocol.py`](../hivelane/protocol.py), the client's models;
- [`server/models.py`](../server/models.py), the relay's copy;
- [`protocol/schema.json`](../protocol/schema.json), a JSON Schema (draft
  2020-12) generated from `hivelane/protocol.py` by `protocol/generate.py`.
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
| `POST` | `/rooms` | Create a room. Optional body `{"settings": {"block_window_seconds", "warn_window_seconds", "history_hours"}}` ([RoomSettings](#roomsettings)). Returns `{"room_id", "settings", "owner_token"}`. `owner_token` is returned only here, once; the relay keeps only its hash. `422` for settings out of range, `429` (with `Retry-After`) when this address has created too many rooms, `503` when the relay is full. |
| `GET` | `/health` | Liveness: `{"status": "ok", "rooms": N}`. |
| `GET` | `/readyz` | Readiness and configuration, including `version` (the hivelane release), `commit` (the build's commit when the deploy sets `HIVELANE_COMMIT`, else `""`), `protocol_version` and `min_supported_protocol`. |
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
   gets a `user_left`. A room whose last connection goes is kept for 30
   minutes after the *latest* departure; if nobody is connected (or joining,
   or waiting for approval) by then, the room and its code are removed. A room
   created with `POST /rooms` that nobody joins within 10 minutes
   (`HIVELANE_UNCLAIMED_ROOM_TTL_SECONDS`) is removed the same way. A code is
   otherwise valid for at most a week from creation.

A connection must send a valid `join` within 15 s, and at most 5 joins in
all (re-prompts after `name_rejected` included); otherwise it is closed with
4408. Time spent waiting for an owner's approval does not count.

## Delivery

Two kinds of frames come from the relay:

- **Point-to-point, sent at once:** replies to the sender
  (`edit_check_response`, `plan_conflicts`, `history_chunk`), handshake
  results (`name_rejected`, `version_rejected`, `room_rejected`,
  `rate_limited`, `join_pending`), owner notifications (`join_request`,
  `join_resolved`), [wait-queue](#wait-queues) notifications
  (`file_requested`, `file_released`) and the catch-up frames above.
- **Room broadcasts, batched:** every other relay-to-client message is queued
  and delivered to every member and spectator, the sender included, inside a
  `hivelane_feed` frame about every 500 ms. A feed's `events` are complete
  messages in the order they happened. A batch too big for one frame (over
  768 KB) is split across consecutive feeds.

A socket that does not accept a frame within 2 s (`HIVELANE_SEND_TIMEOUT_SECONDS`)
is closed with 1013 rather than allowed to hold up the room; reconnect as after
any drop. Records that copy a peer's `e2e` blob (`PeerTouch`, `PlanConflict`)
may arrive with `e2e: ""` when the reply would otherwise exceed the frame
budget; the least relevant entries lose theirs first.

A client must therefore handle each message both bare and inside a feed.
Unwrap `hivelane_feed` first, then dispatch every event exactly as if it had
arrived on its own.

## Client to relay

| Message | Sent by | Answer |
|---------|---------|--------|
| [`join`](#join) | everyone, first | handshake replies above |
| [`activity`](#activity) | members | `activity_update` broadcast |
| [`intent`](#intent) | members | `intent_update` broadcast |
| [`edit_check`](#edit_check) | members | `edit_check_response` to the sender; an `edit_decision` broadcast for warn/block |
| [`file_request`](#file_request) | members | `file_released` to the sender when the file is (or becomes) theirs; `file_requested` to its holders |
| [`plan`](#plan) | members | `plan_conflicts` to the sender; a `plan_update` broadcast |
| [`decision_feedback`](#decision_feedback) | members and spectators | `decision_feedback_update` broadcast |
| [`join_decision`](#join_decision) | owners | `join_resolved` to every owner |
| [`room_policy`](#room_policy) | owners | `room_policy_update` broadcast |
| [`history_request`](#history_request) | members and spectators | `history_chunk` frames to the sender |

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

The team's conflict windows, as committed in `.hivelane.toml`. The relay applies the settings of the most recently admitted member that sent any (last join wins), except that a join without `e2e` leaves the room's `e2e` flag alone.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `block_window_seconds` | number (0 to 600) | `20` | A peer's edit at most this old can block. |
| `warn_window_seconds` | number (0 to 3600) | `60` | A peer's edit at most this old can warn. |
| `e2e` | boolean | `false` | The room's members encrypt content (the team committed `e2e = true`). Informational for the relay. |
| `history_hours` | number (0 to 24) | `0` | Hours of feed events the relay keeps in memory for [`history_request`](#history_request) (the dashboard's replay). `0` keeps none. A join that omits it leaves the room's value alone. |

```json
{"type": "join", "name": "adrian", "agent": "claude-code", "protocol_version": 1,
 "repo_id": "3f2c9a1b7d4e5f60", "settings": {"block_window_seconds": 20, "warn_window_seconds": 60}}
```

A spectator: `{"type": "join"}`, or `{"type": "join", "name": null}`.

#### Names

The relay lowercases the name and strips everything but `a-z`, `0-9`, `_`
and `-`. The result must be 1 to 20 characters, must not contain a blocked
word, and must not already be in the room. A relay may cap members per room
(`HIVELANE_MAX_MEMBERS`; unlimited by default), and then answers `Room full`.
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
the agent is blocked until it arrives. Edit checks do not count against
the room's message quota; each member has its own budget (the room's
`ROOM_MESSAGE_LIMIT` per minute), and a check over it is still answered,
with `warn` and a reason saying it was not checked.

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
A blocked check also puts the sender in the file's [wait queue](#wait-queues),
and the file's queue can block a check no touch blocks.

### file_request

"Let me have this file next." Joins (or keeps the sender's place in) the
file's [wait queue](#wait-queues) and tells the members holding it, once per
requester, with `file_requested`. When nothing keeps the sender out and
nobody is ahead of it, the relay answers at once with `file_released`.
Re-sending keeps the sender's place and restarts its expiry; `hivelane wait`
re-sends every few seconds while it waits.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | required | The file wanted, repo-relative (a keyed hash in an encrypted room). |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), copied through untouched; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |
| `branch` | string | `""` | Git branch the requester works on; holders on another known branch do not hold the file for it. |
| `symbols` | string[] | `[]` | What the requester means to edit; holders are judged at that granularity, as an `edit_check` is. |

### history_request

Replay the room's kept feed events (see `history_hours` in
[RoomSettings](#roomsettings)) newer than `since`. Answered, to the sender
only, with `history_chunk` frames of at most 200 events in timestamp order,
the last with `done: true`; a room keeping no history answers with one empty,
done chunk. One request per socket per 10 s; more are ignored. Kept events
are exactly what spectators were sent, so an encrypted room's history holds
only hashes and ciphertext.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `since` | number | `0` | Unix time; only events newer than this. `0` for everything kept. |

### plan

A member's plan: the files and symbols it expects to touch. The relay
replies at once with a `plan_conflicts` listing files a peer is editing or
also plans to edit, stores the plan until it expires or is replaced, and
broadcasts it as a `plan_update`. Peers editing a planned file get a warning.
Every `plan` gets exactly one `plan_conflicts`, even one the relay did not
record (rate-limited or invalid: see its `reason`). A plan's `symbols` belong
to its `files`; they are matched on their own only when a plan names no
files, and `"<imports>"` is never matched across files.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | required | Ignored: the relay substitutes the connection's own name. |
| `summary` | string | `""` | Plan text. |
| `files` | string[] | `[]` | Repo-relative paths the plan expects to touch. |
| `symbols` | string[] | `[]` | Symbols the plan expects to touch. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `branch` | string | `""` | Git branch the work is on; `""` if unknown. Different known branches turn a block into a merge-conflict warning. |
| `ttl_seconds` | number (60 to 86400) | `1800` | How long the plan stays active. A new plan from the same member in the same `repo_id` replaces it (plans in different repos are kept side by side). |
| `request_id` | string (at most 128 chars) | `""` | Client-chosen id, echoed in the `plan_conflicts` reply. |
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
| `queue_position` | integer | `0` | The sender's place in the file's [wait queue](#wait-queues) after this check, `1` being next; `0` when not queued (allowed, or the queue was full). |
| `suggestion` | string | `""` | For warn/block, one short thing to do instead, built from room state: another file nobody active is in (this module's tests, the sender's own plan, a neighbour in the same directory) and, for a block, how long to wait. `""` when there is nothing to offer. Paths are never suggested in encrypted rooms. |

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
 "queue_position": 1,
 "suggestion": "try server/tests/test_room.py (tests for this module) meanwhile, or wait ~16s (they edited it 4s ago)",
 "others": [{"user_id": "sam", "agent": "claude-code", "seconds_ago": 4.2,
             "prompt": "Rewrite broadcast fan-out", "symbols": ["Room.broadcast"],
             "branch": "main", "kind": "edited", "e2e": ""}]}
```

### plan_conflicts

The reply to a `plan`, sent only to its sender.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `conflicts` | [PlanConflict](#planconflict)[] | `[]` | Files in the plan that a peer is editing or also plans to edit. Empty when there are none. |
| `request_id` | string | `""` | The `request_id` of the `plan` this answers. |
| `reason` | string | `""` | `""` when the plan was recorded; `"rate_limited"` or `"invalid"` when it was not (then `conflicts` is empty). |

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
| `queue_position` | integer | `0` | As in `edit_check_response`: the member's place in the file's wait queue. |
| `suggestion` | string | `""` | As in `edit_check_response`. |
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
| `history_hours` | number | `0` | Hours of feed events the relay keeps for `history_request`; `0` when it keeps none. |
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

### file_requested

Sent, point-to-point, to each member holding a file when someone is waiting
for it (a blocked `edit_check`, or a `file_request`): once per requester per
stay in the queue. A member holds a file while its latest edit of it still
blocks the waiting member, or while it holds a claim on it (see
[Wait queues](#wait-queues)). The daemon passes it on to the holding agent as
a note on its next edit check.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | required | The file, as the requester named it. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `by` | string | required | The waiting member. |
| `agent` | [agent kind](#agent-kinds) | `"unknown"` | The waiting member's agent. |
| `timestamp` | number | now | Unix time in seconds. |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), the requester's, copied from its `edit_check` or `file_request`; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### file_released

Sent, point-to-point, to the member first in a file's wait queue when the
file frees up: every blocking edit aged past the block window, the holders
left, or (for a `file_request`) it was free already. The file is held for
that member for a 10 s grace, during which every other member's
`edit_check` on it is blocked behind it; if it does not take the file in
time, the next in line is released. If a holder edits the file again first,
the release is withdrawn and a new `file_released` follows later.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | required | The file, as the recipient named it. |
| `repo_id` | string (at most 64 chars) | `""` | Opaque repository id; the relay only compares overlaps between equal ids. `""` means unknown and matches only other `""`. |
| `timestamp` | number | now | When the file was released to the recipient (the grace starts here). |
| `e2e` | string (at most 262,144 chars) | `""` | End-to-end encrypted payload (base64url), the recipient's own, copied from the request that queued it; `""` outside [encrypted rooms](#end-to-end-encrypted-rooms). |

### history_chunk

The answer to a `history_request`, sent only to its sender.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `events` | object[] | required | Kept feed events, oldest first, each exactly as broadcast (events without a `timestamp` of their own get the broadcast time). |
| `done` | boolean | required | `true` on the last chunk of the answer. |

### hivelane_feed

A batch of room broadcasts. See [Delivery](#delivery).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `events` | object[] | required | Room broadcasts queued since the last feed, oldest first; each is a complete relay-to-client message. |

## Wait queues

A blocked agent is not left to guess when to retry. Every file
(`repo_id`, `file_path`) can have a line of members waiting for it, and the
relay works it:

- **Joining.** A blocked `edit_check` or a `file_request` puts the sender at
  the back of the line, or keeps its place if it is already in it. One place
  per member; at most 8 members per line and 256 lines per room (a full line
  answers `queue_position: 0`).
- **Holders.** The members whose edits still block the member at the head of
  the line (judged with the symbols and line ranges it last asked about), and
  anyone holding a claim. Each holder is told once per requester with
  `file_requested`.
- **Release.** When the head has no holders left, it is sent
  `file_released` and has a 10 s grace to take the file. During the grace an
  `edit_check` from anyone else is blocked behind it (`scope: "file"`, a
  reason naming who is next), even though no edit blocks it any more. A
  holder editing the file again withdraws the release.
- **Taking.** An `edit_check` that passes takes the sender out of the line.
  If others are still waiting, the sender holds a claim: its edit was allowed
  but its activity has not arrived yet, so the next in line must not be
  released into the middle of it. A claim blocks the others (a reason saying
  who just took the file) until the claimer's activity on the file arrives,
  the block window passes, or the claimer leaves.
- **Expiry.** A place lasts the room's warn window from its latest request
  (`hivelane wait` re-requests every few seconds), a released head lasts its
  grace, and a member that leaves loses its places and claims. A holder that
  leaves releases its files at once.

The relay sweeps every line on each member message and once a second while
any line or claim exists. Nothing can wait forever: each state that makes
someone wait ends on a clock (an edit after the block window, a claim after
the block window, a release after the grace, a place after the warn window),
and a lapsed grace releases the next in line in the same sweep. Two agents
each holding the file the other wants are both blocked, so neither edits,
both edits lapse, and both lines move.

## Agent kinds

`agent` fields take one of: `cursor`, `claude-code`, `codex`, `antigravity`, `gemini-cli`, `devin`, `grok`, `aider`, `human`, `unknown`. The value labels lanes and
reasons; it never changes a decision. See [Versioning](#versioning) before
adding one.

## Limits

The relay bounds what one client can make it store and fan out. These are
the current defaults; operators can change all of them but the e2e blob
and the per-member 0.5 s spacing ([docs/self-hosting.md](self-hosting.md#capacity-limits)),
and clients should not depend on the exact numbers.

| Limit | Default | Over the limit |
|-------|---------|----------------|
| One frame | 512 KB | The socket is closed with 1009 |
| One file's `diff` | 64 KB | Truncated, ending in `…[truncated]` |
| A `prompt` or plan `summary` | 4 KB | Truncated the same way |
| Files per `activity` or `plan` | 50 | The first 50 are kept |
| Symbols per file change, plan or edit check | 64 | The first 64 are kept |
| Line ranges per file change or edit check | 256 | The first 256 are kept |
| An `e2e` blob | 262,144 characters | The message fails validation and is dropped |
| `activity` from one member, per `repo_id` | one broadcast per 0.5 s | Always recorded for conflict checks; the broadcast is coalesced and sent when the 0.5 s are up |
| `intent` from one member, per `repo_id` | one per 0.5 s | Dropped silently |
| `plan` from one member, per `repo_id` | one per 0.5 s | `plan_conflicts` with `reason: "rate_limited"` |
| Inbound member frames per room (`ROOM_MESSAGE_LIMIT`), edit checks excluded | 600 per minute | Dropped silently (a `plan` is answered with `reason: "rate_limited"`) |
| `edit_check` per member | `ROOM_MESSAGE_LIMIT` per minute | Answered `warn`, not checked |
| Inbound frames per dashboard socket | 120 per minute | Dropped silently |
| Joiners waiting for approval, per address | 2 | `room_rejected`, close 4403 |
| Tracked file touches per room (`HIVELANE_MAX_TOUCHES_PER_ROOM`, `HIVELANE_MAX_TOUCH_BYTES_PER_ROOM`) | 5,000 touches, 64 MB | Least recently updated touches are forgotten |
| Plans and intents per member | one per repo, up to 8 repos | Oldest dropped |
| Connection attempts per address (`WS_ATTEMPT_LIMIT`) | 30 per minute | `rate_limited`, close 4429 |
| Members per room (`HIVELANE_MAX_MEMBERS`) | unlimited by default | `name_rejected` "Room full" |
| Joiners waiting for approval, per room | 10 | `room_rejected`, close 4403 |
| Members waiting in one file's queue (`HIVELANE_FILE_QUEUE_MAX_LENGTH`) | unlimited by default (each member once) | Not queued (`queue_position: 0`); still blocked |
| Files with a wait queue, per room | 256 | Not queued (`queue_position: 0`); still blocked |
| `history_request` per socket | one per 10 s | Ignored |
| Kept history per room | 24 h, 5,000 events, 8 MB | Oldest events dropped |

## Close codes

| Code | Meaning | Sent after | What a client should do |
|------|---------|------------|-------------------------|
| `4404` | Room not found or expired | `room_rejected` | Stop; the room code is wrong or the room is gone |
| `4403` | Join refused by an approval-required room | `room_rejected` | Stop; ask an owner, then reconnect |
| `4426` | Client protocol too old | `version_rejected` | Stop; upgrade the client |
| `4429` | Too many connection attempts | `rate_limited` | Reconnect after `retry_after` seconds |
| `4408` | No valid `join` in time, or too many invalid ones | nothing | Fix the join, then reconnect with backoff |
| `1013` | Too slow to keep up: a frame was not accepted in time | nothing | Reconnect with backoff |
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
approval, encrypted payloads, wait queues and replayable history: every one of
them was an optional field or an ignorable message. A client that never sends
`file_request` still gets queued by its blocked checks; it simply ignores the
`file_requested` and `file_released` frames it does not know.

**Anything an older peer cannot satisfy is a breaking change:** removing or
renaming a field, changing a field's type or meaning, making an optional
field required, or a new obligation a client must meet for coordination to
stay correct. Bump `PROTOCOL_VERSION` in `hivelane/protocol.py` and
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

In a room whose committed `.hivelane.toml` sets `e2e = true`, members hide
content from the relay while keeping overlap detection working, because the
relay only ever compares values for equality:

- every file path, symbol and branch in a cleartext field is replaced with a
  keyed hash, `h:` followed by 32 hex digits of HMAC-SHA256 under the room's
  MAC key, so equal values still collide;
- content (diffs, prompts, plan summaries) is sent as empty strings;
- the real values travel in the message's `e2e` field, AES-256-GCM under the
  room's encryption key, which the relay copies untouched onto the matching
  broadcasts (and onto `PeerTouch` and `PlanConflict` entries derived from
  that member's messages) for key holders to decrypt. A `file_request`
  carries an `edit_check` envelope; `file_requested` and `file_released`
  carry the blob of the request that queued the member.

Keys come from a passphrase that never reaches the relay or the repository.
The key schedule and the blob format are specified in
[`hivelane/crypto.py`](../hivelane/crypto.py), with test vectors in
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
3. **Dispatch on `type`.** Unwrap `hivelane_feed` and handle each event as if it
   had arrived alone. Ignore unknown types and fields.
4. **Correlate edit checks** by a `request_id` you generate. Time out quickly
   (the daemon waits 1.5 s) and treat a timeout, a dropped connection or a
   malformed reply as allow: coordination must never stop the user's work.
5. **Reconnect** with exponential backoff (the daemon starts at 1 s and caps at
   30 s), wait at least `retry_after` after `rate_limited`, and send `join`
   again on every new connection. Nothing survives a reconnect except the room.
6. **Match the reference client's identities** if you share rooms with
   Hivelane daemons: send paths relative to the repository root with forward
   slashes, and compute `repo_id` the same way. `repo_id` is the first 16 hex
   digits of the SHA-256 of the normalized `origin` URL (host lowercased, with
   scheme, credentials, port and a trailing `.git` dropped, so ssh and https
   clones agree), or, without an origin, the first 16 characters of the
   smallest root commit id; see `compute_repo_id` and `normalize_remote_url`
   in [`hivelane/daemon.py`](../hivelane/daemon.py).
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
  const events = message.type === 'hivelane_feed' ? message.events : [message];
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
[`hivelane/daemon.py`](../hivelane/daemon.py), and the dashboard's, in
`frontend/src/`, are complete reference clients for a member and a spectator.
