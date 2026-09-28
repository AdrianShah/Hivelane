# Graph Report - Synergo  (2026-09-28)

## Corpus Check
- 68 files · ~31,091 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 7 file(s) not represented in the graph (top: (none) 4, .css 2, .example 1)

## Summary
- 795 nodes · 1485 edges · 62 communities (44 shown, 18 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 62 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `d58d9da0`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Sidebar.tsx
- watcher.py
- companion/main.py
- Synergo
- package.json
- WatcherRecord
- gemini.py
- Room
- prompt_capture.py
- BaseModel
- compilerOptions
- compilerOptions
- Lane.tsx
- test_claude_code_adapter.py
- cli.py
- models.py
- PROJECT_STORY.md
- .check_edit
- demo_proj/README.md
- utils.py
- .oxlintrc.json
- vercel.json
- test_frame_handling.py
- .start
- React + TypeScript + Vite
- vite-env.d.ts
- tsconfig.json
- sanitize.py
- companion/__init__.py
- start_mac.sh
- stop_mac.sh
- setup.sh script
- detect.py
- Daemon
- graphify
- pre-commit
- App.tsx
- types.ts
- AddWatcher.tsx
- Companion Service
- test_edit_check_roundtrip.py
- ConflictBanner.tsx
- _install_signal_handlers
- DeployModal.tsx
- adapters/__init__.py
- synergo/__init__.py
- synergo
- ActivityMessage
- server/main.py
- daemon.py
- test_check_edit.py
- handle_edit_check
- RoomManager
- create_watcher
- test_intent_does_not_consume_the_activity_rate_limit_budget
- test_stale_intent_no_longer_escalates

## God Nodes (most connected - your core abstractions)
1. `Room` - 55 edges
2. `Daemon` - 38 edges
3. `NormalizedEvent` - 28 edges
4. `EventAck` - 21 edges
5. `compilerOptions` - 18 edges
6. `compilerOptions` - 15 edges
7. `_join()` - 15 edges
8. `WatcherRecord` - 14 edges
9. `ActivityMessage` - 14 edges
10. `WatcherManager` - 13 edges

## Surprising Connections (you probably didn't know these)
- `WebSocket` --references--> `events()`  [INFERRED]
  README.md → synergo/daemon.py
- `_to_info()` --uses--> `WatcherRecord`  [INFERRED]
  companion/main.py → companion/process_manager.py
- `create_watcher()` --uses--> `WatcherAlreadyRunning`  [INFERRED]
  companion/main.py → companion/process_manager.py
- `_format_changes()` --uses--> `FileChange`  [INFERRED]
  server/gemini.py → server/models.py
- `generate_caption()` --uses--> `ActivityMessage`  [INFERRED]
  server/gemini.py → server/models.py

## Import Cycles
- None detected.

## Communities (62 total, 18 thin omitted)

### Community 0 - "Sidebar.tsx"
Cohesion: 0.24
Nodes (11): AppView, formatElapsed(), NAV, Sidebar(), SidebarProps, frontend_src_index, Home(), normalizeRoomCode() (+3 more)

### Community 1 - "watcher.py"
Cohesion: 0.06
Nodes (40): AbstractEventLoop, argparse, difflib, FileSystemEvent, FileSystemEventHandler, Queue, RuntimeError, watchdog_events (+32 more)

### Community 2 - "companion/main.py"
Cohesion: 0.20
Nodes (13): delete_watcher(), detect(), health(), list_watchers(), get, Local companion API: lets the dashboard start/stop watchers on this machine.…, Installed / running state for each known AI coding tool., _to_info() (+5 more)

### Community 3 - "Synergo"
Cohesion: 0.09
Nodes (21): 1. Relay server, 2. Dashboard, 3. Synergo CLI (on each contributor machine), Architecture, Dashboard (e.g. Vercel), Dashboard (`frontend/`), Demo tips, Deployment (+13 more)

### Community 4 - "package.json"
Cohesion: 0.07
Nodes (29): dependencies, react, react-dom, react-router-dom, devDependencies, oxlint, @types/node, @types/react (+21 more)

### Community 5 - "WatcherRecord"
Cohesion: 0.13
Nodes (6): Tracks watcher subprocesses keyed by generated watcher_id., Drain merged stdout/stderr into the record's ring buffer., SIGTERM, wait briefly, then SIGKILL. Returns None if unknown id., Terminate every supervised watcher. Safe to call more than once., WatcherManager, WatcherRecord

### Community 6 - "gemini.py"
Cohesion: 0.16
Nodes (20): asyncio, Client, google, google_genai, classify_conflict(), _format_changes(), generate_caption(), _get_client() (+12 more)

### Community 7 - "Room"
Cohesion: 0.09
Nodes (7): BaseModel, WebSocket, True once no members and no spectators remain — safe to evict from the…, Return True if this user sent this kind of message too recently. On acceptance,…, Drop touches that can no longer affect a comparison. Measured against the…, Queue a room-wide event and flush as a Synergo feed batch (~500ms). Point-to-…, Room

### Community 8 - "prompt_capture.py"
Cohesion: 0.10
Nodes (27): Connection, datetime, fib(), glass_theme(), lane_status(), Any, Synergo demo project — edit these files to light up agent lanes., Conflict radar sketch — agents × overlapping files. (+19 more)

### Community 9 - "BaseModel"
Cohesion: 0.21
Nodes (13): handle_intent(), handshake(), WebSocket, Run the join handshake. Returns (user_id, agent) for a named member, or None…, websocket_endpoint(), IntentMessage, JoinMessage, NameRejected (+5 more)

### Community 10 - "compilerOptions"
Cohesion: 0.10
Nodes (19): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+11 more)

### Community 11 - "compilerOptions"
Cohesion: 0.12
Nodes (16): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+8 more)

### Community 12 - "Lane.tsx"
Cohesion: 0.19
Nodes (13): ArchitectureMap(), AVATAR_COLORS, avatarGradient(), decisionNote(), initials(), Lane(), LaneProps, relativeTime() (+5 more)

### Community 13 - "test_claude_code_adapter.py"
Cohesion: 0.08
Nodes (47): fixture, io, json, pytest, _no_gemini(), MonkeyPatch, Shared fixtures. Keeps the suite offline: no Gemini key, no network calls., socket (+39 more)

### Community 14 - "cli.py"
Cohesion: 0.09
Nodes (46): ArgumentParser, httpx, synergo_adapters, build_parser(), cmd_hook_claude_code(), cmd_hook_install(), cmd_hook_uninstall(), cmd_join() (+38 more)

### Community 15 - "models.py"
Cohesion: 0.31
Nodes (8): pydantic, ActivityUpdate, IntentUpdate, Wire format for every WebSocket message. Single source of truth shared by the…, Debounced room broadcast: one WebSocket frame every ~500ms carrying all queued…, SynergoFeed, In-memory state for one workspace ("room"). A process may hold many Room…, time

### Community 16 - "PROJECT_STORY.md"
Cohesion: 0.25
Nodes (7): Accomplishments that we're proud of, Challenges we ran into, How we built it, Inspiration, What it does, What's next for Synergo, What we learned

### Community 17 - ".check_edit"
Cohesion: 0.25
Nodes (5): Decision, True if user_id is still connected. Intentionally the same check as is_taken: a…, Other users' recent touches of the same file within the conflict window., Active peers whose recent declared intent names this file, newest first.…, Decide whether user_id may edit file_path right now. Returns the decision plus…

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
Cohesion: 0.33
Nodes (9): _daemon(), Relay frames are a network boundary: a bad one must not kill the daemon., Join catch-up arrives unbatched, not inside a synergo_feed frame., test_bare_user_joined_frame_records_the_peer(), test_malformed_known_frame_is_ignored(), test_non_object_frame_is_ignored(), test_own_name_is_never_recorded_as_a_peer(), test_unparseable_frame_is_ignored() (+1 more)

### Community 23 - ".start"
Cohesion: 0.22
Nodes (7): _build_command(), _normalize_path(), Exception, Spawn a watcher for one (path, agent) pair. Raises ValueError if the path is…, Forget cleanly exited watchers so their (path, agent) frees up. Caller must…, Raised when a (path, agent) pair is already supervised and alive., WatcherAlreadyRunning

### Community 24 - "React + TypeScript + Vite"
Cohesion: 0.50
Nodes (3): Expanding the Oxlint configuration, React Compiler, React + TypeScript + Vite

### Community 27 - "sanitize.py"
Cohesion: 0.33
Nodes (6): re, is_probably_valid(), Display-name sanitization. The server is authoritative; clients may pre-…, Return a safe display name or raise ValueError with a client-facing reason., Non-raising variant for client-side pre-checks., sanitize_name()

### Community 36 - "detect.py"
Cohesion: 0.22
Nodes (12): detect_tools(), _find_install_path(), _is_running(), _platform_key(), Best-effort detection of locally installed / running AI coding tools. Every…, Lowercased names of running processes, or None if psutil is unusable., Report install + running state for each known tool. Shape: {"platform": str,…, _running_process_names() (+4 more)

### Community 37 - "Daemon"
Cohesion: 0.08
Nodes (31): WebSocket, Daemon, events(), status(), Any, FastAPI, Next payload to send: any retained in-flight one, then priority, then bulk. The…, Dispatch one relay frame. A bad frame must not kill the daemon: every hook… (+23 more)

### Community 38 - "graphify"
Cohesion: 0.50
Nodes (3): uvx, graphify, graphify-mcp

### Community 40 - "App.tsx"
Cohesion: 0.21
Nodes (15): App(), BLANK_LANE, ConnStatus, isActionableConflict(), isExpired(), pruneExpired(), pruneLaneDecisions(), relativeTime() (+7 more)

### Community 41 - "types.ts"
Cohesion: 0.13
Nodes (14): Activity, ActivityUpdate, AgentKind, IntentUpdate, JoinMessage, NameRejected, RoomRejected, ServerMessage (+6 more)

### Community 42 - "AddWatcher.tsx"
Cohesion: 0.21
Nodes (13): AddWatcher(), AddWatcherProps, CompanionState, errorDetail(), fetchWithTimeout(), manualCommand(), statusColor(), relayWsBase() (+5 more)

### Community 43 - "Companion Service"
Cohesion: 0.15
Nodes (12): API, Companion Service, `DELETE /watchers/{watcher_id}`, Future work: run as a managed service, `GET /detect`, `GET /health`, `GET /watchers`, Lifecycle guarantees (+4 more)

### Community 44 - "test_edit_check_roundtrip.py"
Cohesion: 0.26
Nodes (12): fastapi_testclient, main, _join(), _next_feed_event(), _next_frame(), Any, edit_check over a real WebSocket: the reply must arrive immediately, ahead of…, A daemon that connects second must still learn its peers; without this catch-up… (+4 more)

### Community 45 - "ConflictBanner.tsx"
Cohesion: 0.53
Nodes (5): ConflictBanner(), ConflictBannerProps, formatUsers(), secondsAgo(), ConflictAlert

### Community 46 - "_install_signal_handlers"
Cohesion: 0.40
Nodes (4): _install_signal_handlers(), lifespan(), FastAPI, Reap watchers on Ctrl+C / SIGTERM even if uvicorn exits abruptly.

### Community 47 - "DeployModal.tsx"
Cohesion: 0.67
Nodes (3): DeployModal(), DeployModalProps, watcherCommand()

### Community 53 - "ActivityMessage"
Cohesion: 0.24
Nodes (9): evaluate_and_broadcast_conflicts(), handle_activity(), _log_conflict_task_error(), Classify each overlapping file and broadcast conflict_alert when warranted.…, ActivityMessage, FileOverlap, Return per-file overlaps with the most recent active peer (if any). Call this…, One file that the current user and another active user both touched recently. (+1 more)

### Community 54 - "server/main.py"
Cohesion: 0.14
Nodes (12): dotenv, fastapi, logging, secrets, create_room(), health(), get, post (+4 more)

### Community 55 - "daemon.py"
Cohesion: 0.08
Nodes (45): collections, Supervises locally spawned `watcher/watcher.py` subprocesses. State is in-…, dataclasses, pathlib, subprocess, Path, The Synergo daemon: one asyncio process that is both a relay room member and a…, Nearest ancestor containing .git, or None outside a repo. Walked on the… (+37 more)

### Community 56 - "test_check_edit.py"
Cohesion: 0.21
Nodes (19): FileChange, FakeSocket, _join(), Room.check_edit policy: the pre-edit decision an agent's hook depends on., A long-lived room must not keep an entry for every file ever touched., Stand-in member socket: Room only stores it and calls send_json on broadcast,…, The sweep must not reach a touch a comparison can still see., test_allows_past_the_conflict_window() (+11 more)

### Community 57 - "handle_edit_check"
Cohesion: 0.17
Nodes (12): _edit_check_reason(), handle_edit_check(), Decision, One human-readable sentence about the peer that drove the decision., Answer "may I edit this file?" on the agent's blocking hook. Deliberately not…, EditCheckRequest, EditCheckResponse, EditDecision (+4 more)

### Community 58 - "RoomManager"
Cohesion: 0.22
Nodes (4): _generate_code(), Generate a fresh room code and register an empty Room under it., Strict lookup: rooms exist only after POST /rooms. Returns None if the code is…, RoomManager

### Community 59 - "create_watcher"
Cohesion: 0.50
Nodes (5): create_watcher(), CreateWatcherRequest, CreateWatcherResponse, BaseModel, post

## Knowledge Gaps
- **139 isolated node(s):** `uvx`, `graphify-mcp`, `start_mac.sh script`, `stop_mac.sh script`, `$schema` (+134 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 344 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **18 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Room` connect `Room` to `BaseModel`, `models.py`, `.check_edit`, `ActivityMessage`, `server/main.py`, `test_check_edit.py`, `handle_edit_check`, `RoomManager`, `test_intent_does_not_consume_the_activity_rate_limit_budget`, `test_stale_intent_no_longer_escalates`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Why does `Daemon` connect `Daemon` to `test_frame_handling.py`, `test_claude_code_adapter.py`, `cli.py`, `daemon.py`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `Room` (e.g. with `evaluate_and_broadcast_conflicts()` and `handle_activity()`) actually correct?**
  _`Room` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `Daemon` (e.g. with `EventAck` and `NormalizedEvent`) actually correct?**
  _`Daemon` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `NormalizedEvent` (e.g. with `render_response()` and `post_event()`) actually correct?**
  _`NormalizedEvent` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `EventAck` (e.g. with `conflict_message()` and `render_response()`) actually correct?**
  _`EventAck` has 6 INFERRED edges - model-reasoned connections that need verification._
- **What connects `uvx`, `graphify-mcp`, `start_mac.sh script` to the rest of the system?**
  _139 weakly-connected nodes found - possible documentation gaps or missing edges._