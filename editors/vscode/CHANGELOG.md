# Changelog

## 0.2.0

First Marketplace and Open VSX release (preview).

- No longer starts on every editor launch: the extension stays off until you run a Hivelane command (for example **Hivelane: Menu**), then runs for that window.
- If the `hivelane` CLI is not on the editor's PATH, the extension says so once (with "Don't show again"), the status bar tooltip and menu link the install steps, and "Start daemon" explains instead of running a missing command.
- Marketplace listing: a rewritten, user-facing README (build and publishing notes moved to DEVELOPMENT.md), icon, preview flag, gallery banner, SCM Providers category, commands grouped under the "Hivelane" category. Virtual workspaces are declared unsupported.
- Publisher and repository confirmed as `adrianshah` and AdrianShah/Hivelane.

- Relay feed no longer reconnects in a storm: the backoff resets only after a successful join, `room_rejected`/`version_rejected` and close codes 4403/4404/4426 stop the feed, `rate_limited` `retry_after` is honoured, and retries are capped (then "Hivelane: Reconnect peer badges").
- Approval-required rooms: the extension no longer queues itself as a spectator join request; badges stay off unless `hivelane.joinApprovalRooms` is on, and a declined request is never retried.
- End-to-end encrypted rooms and repos with a fatal daemon error get no feed; the tooltip says why.
- Peer badges are scoped by `repo_id`; path case is folded only on Windows/macOS.
- Dashboard links follow the CLI (`HIVELANE_DASHBOARD`, else localhost for a local relay, else https://adrianshah.github.io/Hivelane) and carry `?relay=`; room codes are normalized (uppercase, spaces/dashes stripped).
- Reloads from disk (agent writes, git checkout) are no longer treated as human edits; the baseline is read asynchronously and BOM-stripped.
- One status poll at a time; "token mismatch" in the status bar; port precedence fixed; tokens validated like the daemon does; heartbeat detects half-open sockets.
- License is now Apache-2.0 (the project's license);

## 0.1.0

- Status bar with room and online count, quick-pick menu.
- File decorations for files peers edited in the last 60 s.
- Pre-edit warnings for human edits; saved edits shared with the room.
- Commands: Check this file, Who's here.
