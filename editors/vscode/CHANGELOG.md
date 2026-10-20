# Changelog

## Unreleased

- Relay feed no longer reconnects in a storm: the backoff resets only after a successful join, `room_rejected`/`version_rejected` and close codes 4403/4404/4426 stop the feed, `rate_limited` `retry_after` is honoured, and retries are capped (then "Hivelane: Reconnect peer badges").
- Approval-required rooms: the extension no longer queues itself as a spectator join request; badges stay off unless `hivelane.joinApprovalRooms` is on, and a declined request is never retried.
- End-to-end encrypted rooms and repos with a fatal daemon error get no feed; the tooltip says why.
- Peer badges are scoped by `repo_id`; path case is folded only on Windows/macOS.
- Dashboard links follow the CLI (`HIVELANE_DASHBOARD`, else localhost for a local relay, else https://hivelane.dev) and carry `?relay=`; room codes are normalized (uppercase, spaces/dashes stripped).
- Reloads from disk (agent writes, git checkout) are no longer treated as human edits; the baseline is read asynchronously and BOM-stripped.
- One status poll at a time; "token mismatch" in the status bar; port precedence fixed; tokens validated like the daemon does; heartbeat detects half-open sockets.
- License is now Apache-2.0 (the project's license); publisher/repository point at AdrianShah/Hivelane (owner to confirm).

## 0.1.0

- Status bar with room and online count, quick-pick menu.
- File decorations for files peers edited in the last 60 s.
- Pre-edit warnings for human edits; saved edits shared with the room.
- Commands: Check this file, Who's here.
