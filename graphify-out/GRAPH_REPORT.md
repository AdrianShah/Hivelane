# Graph Report - Synergo  (2026-09-28)

## Corpus Check
- 69 files · ~30,578 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 9 file(s) not represented in the graph (top: (none) 6, .css 2, .example 1)

## Summary
- 799 nodes · 1503 edges · 59 communities (40 shown, 19 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 64 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `28f27140`
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
- test_fail_open.py
- hello.py
- main.py
- compilerOptions
- compilerOptions
- Lane.tsx
- test_claude_code_adapter.py
- cli.py
- smoke.sh
- PROJECT_STORY.md
- room.py
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
- VersionTooOld
- readyz
- setup.sh script
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
- DeployModal.tsx
- adapters/__init__.py
- synergo/__init__.py
- synergo
- signal
- repo_relative_path
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
- `_format_changes()` --uses--> `FileChange`  [INFERRED]
  server/gemini.py → server/models.py
- `handshake()` --uses--> `Room`  [INFERRED]
  server/main.py → server/room.py
- `evaluate_and_broadcast_conflicts()` --uses--> `FileOverlap`  [INFERRED]
  server/main.py → server/room.py

## Import Cycles
- None detected.

## Communities (59 total, 19 thin omitted)

### Community 0 - "Sidebar.tsx"
Cohesion: 0.24
Nodes (11): AppView, formatElapsed(), NAV, Sidebar(), SidebarProps, frontend_src_index, Home(), normalizeRoomCode() (+3 more)

### Community 1 - "daemon.py"
Cohesion: 0.12
Nodes (28): difflib, The Synergo daemon: one asyncio process that is both a relay room member and a…, unified_diff(), ActivityMessage, EditCheckRequest, EditCheckResponse, FileChange, IntentMessage (+20 more)

### Community 3 - "Synergo"
Cohesion: 0.07
Nodes (26): 1. Relay server, 2. Dashboard, 3. Synergo CLI (on each contributor machine), Architecture, Container, Dashboard, Dashboard (`frontend/`), Demo tips (+18 more)

### Community 4 - "package.json"
Cohesion: 0.07
Nodes (29): dependencies, react, react-dom, react-router-dom, devDependencies, oxlint, @types/node, @types/react (+21 more)

### Community 5 - "Daemon"
Cohesion: 0.15
Nodes (6): Daemon, events(), status(), Any, Next payload to send: any retained in-flight one, then priority, then bulk. The…, Dispatch one relay frame. A bad frame must not kill the daemon: every hook…

### Community 6 - "gemini.py"
Cohesion: 0.13
Nodes (25): Client, google, google_genai, classify_conflict(), _format_changes(), generate_caption(), _get_client(), heuristic_conflict_alert() (+17 more)

### Community 7 - "test_fail_open.py"
Cohesion: 0.15
Nodes (22): io, pathlib, socket, synergo, main(), CaptureFixture, MonkeyPatch, Path (+14 more)

### Community 8 - "hello.py"
Cohesion: 0.17
Nodes (9): datetime, fib(), glass_theme(), lane_status(), Any, Synergo demo project — edit these files to light up agent lanes., Conflict radar sketch — agents × overlapping files., Obvious numeric diff bait for the board. (+1 more)

### Community 9 - "main.py"
Cohesion: 0.14
Nodes (30): fastapi_middleware_cors, handle_edit_check(), handle_intent(), handshake(), WebSocket, Relay server: accepts watcher and spectator WebSocket connections, keeps room…, Run the join handshake. Returns (user_id, agent) for a named member, or None…, Answer "may I edit this file?" on the agent's blocking hook. Deliberately not… (+22 more)

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
Cohesion: 0.15
Nodes (30): conflict_message(), _file_path(), hook_settings(), normalize(), Any, Claude Code hook adapter. Schema per https://code.claude.com/docs/en/hooks…, Human sentence naming who is in this file and what to do about it. The relay's…, Stdout for Claude Code. Empty string means "no output, proceed". (+22 more)

### Community 14 - "cli.py"
Cohesion: 0.11
Nodes (37): argparse, ArgumentParser, httpx, Namespace, synergo_adapters, build_parser(), cmd_hook_claude_code(), cmd_hook_install() (+29 more)

### Community 15 - "smoke.sh"
Cohesion: 0.57
Nodes (5): fail(), hook_event(), smoke.sh script, start_daemon(), wait_for()

### Community 16 - "PROJECT_STORY.md"
Cohesion: 0.25
Nodes (7): Accomplishments that we're proud of, Challenges we ran into, How we built it, Inspiration, What it does, What's next for Synergo, What we learned

### Community 17 - "room.py"
Cohesion: 0.11
Nodes (19): asyncio, FastAPI, logging, LogRecord, os, pydantic, secrets, configure() (+11 more)

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
Cohesion: 0.08
Nodes (38): collections, dataclasses, math, post, Request, check_room_creation(), check_ws_attempt(), client_ip() (+30 more)

### Community 24 - "React + TypeScript + Vite"
Cohesion: 0.50
Nodes (3): Expanding the Oxlint configuration, React Compiler, React + TypeScript + Vite

### Community 27 - "sanitize.py"
Cohesion: 0.33
Nodes (6): re, is_probably_valid(), Display-name sanitization. The server is authoritative; clients may pre-…, Return a safe display name or raise ValueError with a client-facing reason., Non-raising variant for client-side pre-checks., sanitize_name()

### Community 28 - "VersionTooOld"
Cohesion: 0.67
Nodes (3): Exception, Raised by the handshake so the caller closes with the right code., VersionTooOld

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
Cohesion: 0.18
Nodes (24): json, relative_path(), NormalizedEvent, BaseModel, drain(), make_daemon(), Path, Daemon translation: post_edit debouncing, diffing and the pre_edit cache. (+16 more)

### Community 38 - "graphify"
Cohesion: 0.50
Nodes (3): uvx, graphify, graphify-mcp

### Community 40 - "App.tsx"
Cohesion: 0.23
Nodes (14): App(), BLANK_LANE, ConnStatus, isActionableConflict(), isExpired(), pruneExpired(), pruneLaneDecisions(), relativeTime() (+6 more)

### Community 41 - "types.ts"
Cohesion: 0.12
Nodes (15): Activity, ActivityUpdate, AgentKind, EditDecision, FeedEvent, IntentUpdate, JoinMessage, NameRejected (+7 more)

### Community 42 - "AddWatcher.tsx"
Cohesion: 0.47
Nodes (5): AddWatcher(), AddWatcherProps, Step, steps(), relayWsBase()

### Community 44 - "test_edit_check_roundtrip.py"
Cohesion: 0.12
Nodes (20): fastapi_testclient, main, _cleanup_later(), _join(), _next_feed_event(), _next_frame(), Any, edit_check over a real WebSocket: the reply must arrive immediately, ahead of… (+12 more)

### Community 45 - "ConflictBanner.tsx"
Cohesion: 0.53
Nodes (5): ConflictBanner(), ConflictBannerProps, formatUsers(), secondsAgo(), ConflictAlert

### Community 47 - "DeployModal.tsx"
Cohesion: 0.67
Nodes (3): DeployModal(), DeployModalProps, watcherCommand()

### Community 55 - "repo_relative_path"
Cohesion: 0.29
Nodes (11): Path, Nearest ancestor containing .git, or None outside a repo. Walked on the…, Path key shared by every agent in the room. Keyed on the repo root, not the…, repo_relative_path(), repo_root(), Path, Path keys must be identical for every agent in the room, whatever their cwd., _repo() (+3 more)

### Community 56 - "Room"
Cohesion: 0.06
Nodes (37): Debounced room broadcast: one WebSocket frame every ~500ms carrying all queued…, SynergoFeed, BaseModel, Decision, WebSocket, True if user_id is still connected. Intentionally the same check as is_taken: a…, True once no members and no spectators remain — safe to evict from the…, Return True if this user sent this kind of message too recently. On acceptance,… (+29 more)

### Community 58 - "test_room_store.py"
Cohesion: 0.05
Nodes (37): Protocol, _generate_code(), Valid room codes, including those with no live Room yet., Live Room instances plus the durable set of valid room codes. Room contents…, Generate a fresh room code and register an empty Room under it., Look up a room by code, reviving a known code whose Room is gone. Still strict…, Rooms live in this process (what /health reports)., RoomManager (+29 more)

## Knowledge Gaps
- **131 isolated node(s):** `uvx`, `graphify-mcp`, `$schema`, `plugins`, `react/rules-of-hooks` (+126 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 344 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **19 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Room` connect `Room` to `main.py`, `test_room_store.py`, `gemini.py`, `room.py`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Why does `Daemon` connect `Daemon` to `daemon.py`, `NormalizedEvent`, `test_claude_code_adapter.py`, `cli.py`, `test_frame_handling.py`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Why does `main()` connect `test_fail_open.py` to `Synergo`, `test_claude_code_adapter.py`, `cli.py`?**
  _High betweenness centrality (0.039) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `Room` (e.g. with `evaluate_and_broadcast_conflicts()` and `handle_activity()`) actually correct?**
  _`Room` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `Daemon` (e.g. with `EventAck` and `NormalizedEvent`) actually correct?**
  _`Daemon` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `NormalizedEvent` (e.g. with `render_response()` and `post_event()`) actually correct?**
  _`NormalizedEvent` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `RoomManager` (e.g. with `Room` and `RoomStore`) actually correct?**
  _`RoomManager` has 3 INFERRED edges - model-reasoned connections that need verification._