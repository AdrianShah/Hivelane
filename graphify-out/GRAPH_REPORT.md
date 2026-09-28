# Graph Report - Synergo  (2026-09-28)

## Corpus Check
- 67 files · ~29,613 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 7 file(s) not represented in the graph (top: (none) 4, .css 2, .example 1)

## Summary
- 775 nodes · 1462 edges · 62 communities (42 shown, 20 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 63 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e99cf862`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Sidebar.tsx
- daemon.py
- contextlib
- Synergo
- package.json
- Daemon
- gemini.py
- test_outbox.py
- hello.py
- models.py
- compilerOptions
- compilerOptions
- Lane.tsx
- test_claude_code_adapter.py
- cli.py
- smoke.sh
- PROJECT_STORY.md
- room_manager.py
- demo_proj/README.md
- utils.py
- .oxlintrc.json
- vercel.json
- test_frame_handling.py
- test_rate_limits.py
- React + TypeScript + Vite
- vite-env.d.ts
- tsconfig.json
- sanitize.py
- devDependencies
- scripts
- readyz
- setup.sh script
- .recent_other_touches
- _edit_check_reason
- deploy_hook.sh
- test_config.py
- NormalizedEvent
- graphify
- pre-commit
- App.tsx
- types.ts
- AddWatcher.tsx
- post_deploy_check.sh
- test_edit_check_roundtrip.py
- ConflictBanner.tsx
- shutil
- react
- adapters/__init__.py
- synergo/__init__.py
- synergo
- main.py
- signal
- pathlib
- Room
- threading
- test_room_store.py
- urllib_parse
- watchdog_events
- watchdog_observers

## God Nodes (most connected - your core abstractions)
1. `Room` - 55 edges
2. `Daemon` - 38 edges
3. `NormalizedEvent` - 28 edges
4. `RoomManager` - 21 edges
5. `EventAck` - 21 edges
6. `compilerOptions` - 18 edges
7. `SqliteRoomStore` - 18 edges
8. `compilerOptions` - 15 edges
9. `load_settings()` - 15 edges
10. `_join()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `Environments` --references--> `main()`  [INFERRED]
  README.md → synergo/cli.py
- `WebSocket` --references--> `events()`  [INFERRED]
  README.md → synergo/daemon.py
- `handshake()` --uses--> `Room`  [INFERRED]
  server/main.py → server/room.py
- `evaluate_and_broadcast_conflicts()` --uses--> `ActivityMessage`  [INFERRED]
  server/main.py → server/models.py
- `evaluate_and_broadcast_conflicts()` --uses--> `Room`  [INFERRED]
  server/main.py → server/room.py

## Import Cycles
- None detected.

## Communities (62 total, 20 thin omitted)

### Community 0 - "Sidebar.tsx"
Cohesion: 0.23
Nodes (11): AppView, formatElapsed(), NAV, Sidebar(), SidebarProps, frontend_src_index, Home(), normalizeRoomCode() (+3 more)

### Community 1 - "daemon.py"
Cohesion: 0.18
Nodes (16): difflib, The Synergo daemon: one asyncio process that is both a relay room member and a…, unified_diff(), EditCheckResponse, FileChange, IntentMessage, JoinMessage, NameRejected (+8 more)

### Community 3 - "Synergo"
Cohesion: 0.08
Nodes (24): 1. Relay server, 2. Dashboard, 3. Synergo CLI (on each contributor machine), Architecture, Dashboard, Dashboard (`frontend/`), Demo tips, Deployment (+16 more)

### Community 4 - "package.json"
Cohesion: 0.12
Nodes (15): dependencies, react, react-dom, react-router-dom, name, private, type, version (+7 more)

### Community 5 - "Daemon"
Cohesion: 0.15
Nodes (5): Daemon, status(), Any, Next payload to send: any retained in-flight one, then priority, then bulk. The…, Dispatch one relay frame. A bad frame must not kill the daemon: every hook…

### Community 6 - "gemini.py"
Cohesion: 0.15
Nodes (21): Client, google, google_genai, classify_conflict(), _format_changes(), generate_caption(), _get_client(), heuristic_conflict_alert() (+13 more)

### Community 7 - "test_outbox.py"
Cohesion: 0.27
Nodes (12): ActivityMessage, EditCheckRequest, _activity(), _daemon(), Outbox ordering and retention: edit_check is on a blocking hook, and a payload…, test_edit_check_overtakes_a_backlog_of_activity(), scenario(), test_inflight_payload_is_resent_after_a_drop() (+4 more)

### Community 8 - "hello.py"
Cohesion: 0.17
Nodes (9): datetime, fib(), glass_theme(), lane_status(), Any, Synergo demo project — edit these files to light up agent lanes., Conflict radar sketch — agents × overlapping files., Obvious numeric diff bait for the board. (+1 more)

### Community 9 - "models.py"
Cohesion: 0.14
Nodes (24): handle_edit_check(), handshake(), WebSocket, Run the join handshake. Returns (user_id, agent) for a named member, or None…, Answer "may I edit this file?" on the agent's blocking hook. Deliberately not…, websocket_endpoint(), EditCheckRequest, EditCheckResponse (+16 more)

### Community 10 - "compilerOptions"
Cohesion: 0.10
Nodes (19): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+11 more)

### Community 11 - "compilerOptions"
Cohesion: 0.12
Nodes (16): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+8 more)

### Community 12 - "Lane.tsx"
Cohesion: 0.21
Nodes (12): ArchitectureMap(), AVATAR_COLORS, avatarGradient(), decisionNote(), initials(), Lane(), LaneProps, relativeTime() (+4 more)

### Community 13 - "test_claude_code_adapter.py"
Cohesion: 0.08
Nodes (52): io, json, socket, synergo, conflict_message(), _file_path(), hook_settings(), normalize() (+44 more)

### Community 14 - "cli.py"
Cohesion: 0.11
Nodes (37): argparse, ArgumentParser, Exception, httpx, Namespace, synergo_adapters, build_parser(), cmd_hook_claude_code() (+29 more)

### Community 15 - "smoke.sh"
Cohesion: 0.57
Nodes (5): fail(), hook_event(), smoke.sh script, start_daemon(), wait_for()

### Community 16 - "PROJECT_STORY.md"
Cohesion: 0.25
Nodes (7): Accomplishments that we're proud of, Challenges we ran into, How we built it, Inspiration, What it does, What's next for Synergo, What we learned

### Community 17 - "room_manager.py"
Cohesion: 0.27
Nodes (8): logging, os, secrets, Registry of live Room instances, keyed by short shareable room codes. The room…, Durable registry of which room codes exist. Only the codes are persisted, never…, sqlite3, time, typing

### Community 18 - "demo_proj/README.md"
Cohesion: 0.33
Nodes (5): Edit src/hello.py and src/utils.py (ideally from two agents) to show lanes + conflicts., Point the watcher here to stream live edits into the dashboard:, python watcher/watcher.py --path demo_proj --name hossein --room <ROOM> \, --server wss://hackday-hackthevalley.onrender.com/ws --agent cursor, Synergo demo project

### Community 19 - "utils.py"
Cohesion: 0.33
Nodes (3): overlap_score(), Shared helpers for the Synergo demo tree. Touch this file from a second agent…, Rough conflict radar used in demo captions.

### Community 20 - ".oxlintrc.json"
Cohesion: 0.33
Nodes (5): plugins, rules, react/only-export-components, react/rules-of-hooks, $schema

### Community 21 - "vercel.json"
Cohesion: 0.33
Nodes (5): buildCommand, framework, headers, outputDirectory, rewrites

### Community 22 - "test_frame_handling.py"
Cohesion: 0.27
Nodes (11): _daemon(), Relay frames are a network boundary: a bad one must not kill the daemon., Join catch-up arrives unbatched, not inside a synergo_feed frame., Throttling is the relay asking us to wait, not refusing us: the daemon must…, test_bare_user_joined_frame_records_the_peer(), test_malformed_known_frame_is_ignored(), test_non_object_frame_is_ignored(), test_own_name_is_never_recorded_as_a_peer() (+3 more)

### Community 23 - "test_rate_limits.py"
Cohesion: 0.07
Nodes (38): collections, dataclasses, math, post, Request, check_room_creation(), check_ws_attempt(), client_ip() (+30 more)

### Community 24 - "React + TypeScript + Vite"
Cohesion: 0.50
Nodes (3): Expanding the Oxlint configuration, React Compiler, React + TypeScript + Vite

### Community 27 - "sanitize.py"
Cohesion: 0.33
Nodes (6): re, is_probably_valid(), Display-name sanitization. The server is authoritative; clients may pre-…, Return a safe display name or raise ValueError with a client-facing reason., Non-raising variant for client-side pre-checks., sanitize_name()

### Community 28 - "devDependencies"
Cohesion: 0.25
Nodes (8): devDependencies, oxlint, @types/node, @types/react, @types/react-dom, typescript, vite, @vitejs/plugin-react

### Community 29 - "scripts"
Cohesion: 0.40
Nodes (5): scripts, build, dev, lint, preview

### Community 30 - "readyz"
Cohesion: 0.50
Nodes (4): get, health(), Readiness: configuration this deploy needs before it can serve a dashboard.…, readyz()

### Community 33 - "_edit_check_reason"
Cohesion: 0.67
Nodes (3): _edit_check_reason(), Decision, One human-readable sentence about the peer that drove the decision.

### Community 36 - "test_config.py"
Cohesion: 0.07
Nodes (37): collections_abc, dotenv, parametrize, pytest, RuntimeError, server, _bool(), ConfigError (+29 more)

### Community 37 - "NormalizedEvent"
Cohesion: 0.20
Nodes (23): relative_path(), NormalizedEvent, BaseModel, drain(), make_daemon(), Path, Daemon translation: post_edit debouncing, diffing and the pre_edit cache., Priority first, matching the order the real drain sends in. (+15 more)

### Community 38 - "graphify"
Cohesion: 0.50
Nodes (3): uvx, graphify, graphify-mcp

### Community 40 - "App.tsx"
Cohesion: 0.23
Nodes (14): App(), BLANK_LANE, ConnStatus, isActionableConflict(), isExpired(), pruneExpired(), pruneLaneDecisions(), relativeTime() (+6 more)

### Community 41 - "types.ts"
Cohesion: 0.12
Nodes (15): Activity, ActivityUpdate, AgentKind, FeedEvent, FileChange, IntentUpdate, JoinMessage, NameRejected (+7 more)

### Community 42 - "AddWatcher.tsx"
Cohesion: 0.47
Nodes (5): AddWatcher(), AddWatcherProps, Step, steps(), relayWsBase()

### Community 44 - "test_edit_check_roundtrip.py"
Cohesion: 0.26
Nodes (12): fastapi_testclient, main, _join(), _next_feed_event(), _next_frame(), Any, edit_check over a real WebSocket: the reply must arrive immediately, ahead of…, A daemon that connects second must still learn its peers; without this catch-up… (+4 more)

### Community 45 - "ConflictBanner.tsx"
Cohesion: 0.53
Nodes (5): ConflictBanner(), ConflictBannerProps, formatUsers(), secondsAgo(), ConflictAlert

### Community 47 - "react"
Cohesion: 0.50
Nodes (4): DeployModal(), DeployModalProps, watcherCommand(), react

### Community 53 - "main.py"
Cohesion: 0.16
Nodes (17): asyncio, FastAPI, fastapi_middleware_cors, pydantic, evaluate_and_broadcast_conflicts(), handle_activity(), handle_intent(), _log_conflict_task_error() (+9 more)

### Community 55 - "pathlib"
Cohesion: 0.26
Nodes (12): pathlib, Path, Nearest ancestor containing .git, or None outside a repo. Walked on the…, Path key shared by every agent in the room. Keyed on the repo root, not the…, repo_relative_path(), repo_root(), Path, Path keys must be identical for every agent in the room, whatever their cwd. (+4 more)

### Community 56 - "Room"
Cohesion: 0.06
Nodes (33): BaseModel, Decision, WebSocket, True if user_id is still connected. Intentionally the same check as is_taken: a…, True once no members and no spectators remain — safe to evict from the…, Return True if this user sent this kind of message too recently. On acceptance,…, Drop touches that can no longer affect a comparison. Measured against the…, Active peers whose recent declared intent names this file, newest first.… (+25 more)

### Community 58 - "test_room_store.py"
Cohesion: 0.05
Nodes (37): Protocol, _generate_code(), Valid room codes, including those with no live Room yet., Live Room instances plus the durable set of valid room codes. Room contents…, Generate a fresh room code and register an empty Room under it., Look up a room by code, reviving a known code whose Room is gone. Still strict…, Rooms live in this process (what /health reports)., RoomManager (+29 more)

## Knowledge Gaps
- **128 isolated node(s):** `uvx`, `graphify-mcp`, `$schema`, `plugins`, `react/rules-of-hooks` (+123 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 331 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **20 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Room` connect `Room` to `.recent_other_touches`, `gemini.py`, `models.py`, `room_manager.py`, `main.py`, `test_room_store.py`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Why does `Daemon` connect `Daemon` to `daemon.py`, `NormalizedEvent`, `test_outbox.py`, `test_claude_code_adapter.py`, `cli.py`, `test_frame_handling.py`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Why does `main()` connect `test_claude_code_adapter.py` to `Synergo`, `cli.py`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `Room` (e.g. with `evaluate_and_broadcast_conflicts()` and `handle_activity()`) actually correct?**
  _`Room` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `Daemon` (e.g. with `EventAck` and `NormalizedEvent`) actually correct?**
  _`Daemon` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `NormalizedEvent` (e.g. with `render_response()` and `post_event()`) actually correct?**
  _`NormalizedEvent` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `RoomManager` (e.g. with `Room` and `RoomStore`) actually correct?**
  _`RoomManager` has 3 INFERRED edges - model-reasoned connections that need verification._