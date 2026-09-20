# Graph Report - Synergo  (2026-09-20)

## Corpus Check
- 45 files · ~22,624 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 3, .css 2, .example 1)

## Summary
- 515 nodes · 801 edges · 36 communities (26 shown, 10 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 26 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ffb8f847`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- App.tsx
- watcher.py
- companion/main.py
- Synergo
- package.json
- WatcherRecord
- gemini.py
- Room
- prompt_capture.py
- server/main.py
- compilerOptions
- compilerOptions
- Lane.tsx
- hello.py
- models.py
- room_manager.py
- PROJECT_STORY.md
- FileOverlap
- demo_proj/README.md
- utils.py
- .oxlintrc.json
- vercel.json
- sanitize.py
- handle_activity
- React + TypeScript + Vite
- vite-env.d.ts
- tsconfig.json
- create_room
- companion/__init__.py
- start_mac.sh
- stop_mac.sh
- setup.sh script

## God Nodes (most connected - your core abstractions)
1. `Room` - 31 edges
2. `compilerOptions` - 18 edges
3. `compilerOptions` - 15 edges
4. `WatcherRecord` - 14 edges
5. `WatcherManager` - 13 edges
6. `ActivityMessage` - 12 edges
7. `Batcher` - 11 edges
8. `Synergo` - 11 edges
9. `classify_conflict()` - 10 edges
10. `capture_latest_prompt()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `_to_info()` --uses--> `WatcherRecord`  [INFERRED]
  companion/main.py → companion/process_manager.py
- `handshake()` --uses--> `Room`  [INFERRED]
  server/main.py → server/room.py
- `evaluate_and_broadcast_conflicts()` --uses--> `ActivityMessage`  [INFERRED]
  server/main.py → server/models.py
- `evaluate_and_broadcast_conflicts()` --uses--> `FileOverlap`  [INFERRED]
  server/main.py → server/room.py
- `evaluate_and_broadcast_conflicts()` --uses--> `Room`  [INFERRED]
  server/main.py → server/room.py

## Import Cycles
- None detected.

## Communities (36 total, 10 thin omitted)

### Community 0 - "App.tsx"
Cohesion: 0.06
Nodes (59): App(), ConnStatus, isActionableConflict(), isExpired(), pruneExpired(), relativeTime(), SPECTATOR_JOIN, TOP_NAV (+51 more)

### Community 1 - "watcher.py"
Cohesion: 0.06
Nodes (40): AbstractEventLoop, argparse, difflib, FileSystemEvent, FileSystemEventHandler, Namespace, Queue, RuntimeError (+32 more)

### Community 2 - "companion/main.py"
Cohesion: 0.06
Nodes (44): collections, detect_tools(), _find_install_path(), _is_running(), _platform_key(), Best-effort detection of locally installed / running AI coding tools. Every…, Lowercased names of running processes, or None if psutil is unusable., Report install + running state for each known tool. Shape: {"platform": str,… (+36 more)

### Community 3 - "Synergo"
Cohesion: 0.05
Nodes (36): API, Companion Service, `DELETE /watchers/{watcher_id}`, Future work: run as a managed service, `GET /detect`, `GET /health`, `GET /watchers`, Lifecycle guarantees (+28 more)

### Community 4 - "package.json"
Cohesion: 0.07
Nodes (28): dependencies, react, react-dom, react-router-dom, devDependencies, oxlint, @types/node, @types/react (+20 more)

### Community 5 - "WatcherRecord"
Cohesion: 0.10
Nodes (10): _build_command(), _normalize_path(), Tracks watcher subprocesses keyed by generated watcher_id., Spawn a watcher for one (path, agent) pair. Raises ValueError if the path is…, Drain merged stdout/stderr into the record's ring buffer., SIGTERM, wait briefly, then SIGKILL. Returns None if unknown id., Terminate every supervised watcher. Safe to call more than once., Forget cleanly exited watchers so their (path, agent) frees up. Caller must… (+2 more)

### Community 6 - "gemini.py"
Cohesion: 0.15
Nodes (21): Client, google, google_genai, classify_conflict(), _format_changes(), generate_caption(), _get_client(), heuristic_conflict_alert() (+13 more)

### Community 7 - "Room"
Cohesion: 0.12
Nodes (7): ActivityUpdate, BaseModel, WebSocket, Return True if this user sent activity too recently (drop the update). On…, Queue a room-wide event and flush as a Synergo feed batch (~500ms). Point-to-…, True once no members and no spectators remain — safe to evict from the…, Room

### Community 8 - "prompt_capture.py"
Cohesion: 0.17
Nodes (19): Connection, json, sqlite3, urllib_parse, capture_latest_prompt(), _composer_ids_for_workspace(), _cursor_user_dir(), _folder_from_uri() (+11 more)

### Community 9 - "server/main.py"
Cohesion: 0.17
Nodes (17): dotenv, handshake(), health(), get, WebSocket, Relay server: accepts watcher and spectator WebSocket connections, keeps room…, Run the join handshake. Returns (user_id, agent) for a named member, or None…, _schedule_empty_room_cleanup() (+9 more)

### Community 10 - "compilerOptions"
Cohesion: 0.10
Nodes (19): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+11 more)

### Community 11 - "compilerOptions"
Cohesion: 0.12
Nodes (16): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+8 more)

### Community 12 - "Lane.tsx"
Cohesion: 0.22
Nodes (11): ArchitectureMap(), AVATAR_COLORS, avatarGradient(), initials(), Lane(), LaneProps, relativeTime(), shortHash() (+3 more)

### Community 13 - "hello.py"
Cohesion: 0.17
Nodes (9): Any, datetime, fib(), glass_theme(), lane_status(), Synergo demo project — edit these files to light up agent lanes., Conflict radar sketch — agents × overlapping files., Obvious numeric diff bait for the board. (+1 more)

### Community 14 - "models.py"
Cohesion: 0.23
Nodes (10): asyncio, FastAPI, logging, pydantic, Wire format for every WebSocket message. Single source of truth shared by the…, Debounced room broadcast: one WebSocket frame every ~500ms carrying all queued…, SynergoFeed, In-memory state for one workspace ("room"). A process may hold many Room… (+2 more)

### Community 15 - "room_manager.py"
Cohesion: 0.18
Nodes (6): secrets, _generate_code(), Registry of live Room instances, keyed by short shareable room codes. The room…, Generate a fresh room code and register an empty Room under it., Strict lookup: rooms exist only after POST /rooms. Returns None if the code is…, RoomManager

### Community 16 - "PROJECT_STORY.md"
Cohesion: 0.25
Nodes (7): Accomplishments that we're proud of, Challenges we ran into, How we built it, Inspiration, What it does, What's next for Synergo, What we learned

### Community 17 - "FileOverlap"
Cohesion: 0.25
Nodes (5): FileOverlap, Other users' recent touches of the same file within the conflict window., Return per-file overlaps with the most recent active peer (if any). Call this…, One file that the current user and another active user both touched recently., True if user_id is still connected. Intentionally the same check as is_taken: a…

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

### Community 22 - "sanitize.py"
Cohesion: 0.40
Nodes (4): re, is_probably_valid(), Display-name sanitization. The server is authoritative; clients may pre-…, Non-raising variant for client-side pre-checks.

### Community 23 - "handle_activity"
Cohesion: 0.40
Nodes (5): evaluate_and_broadcast_conflicts(), handle_activity(), _log_conflict_task_error(), Classify each overlapping file and broadcast conflict_alert when warranted.…, Task

### Community 24 - "React + TypeScript + Vite"
Cohesion: 0.50
Nodes (3): Expanding the Oxlint configuration, React Compiler, React + TypeScript + Vite

### Community 27 - "create_room"
Cohesion: 0.67
Nodes (3): create_room(), post, Generate a fresh room code. The room code is the shared secret — whoever has it…

## Knowledge Gaps
- **137 isolated node(s):** `start_mac.sh script`, `stop_mac.sh script`, `$schema`, `plugins`, `react/rules-of-hooks` (+132 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 270 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **10 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Room` connect `Room` to `gemini.py`, `server/main.py`, `models.py`, `room_manager.py`, `FileOverlap`, `handle_activity`?**
  _High betweenness centrality (0.050) - this node is a cross-community bridge._
- **Why does `WatcherManager` connect `WatcherRecord` to `companion/main.py`?**
  _High betweenness centrality (0.022) - this node is a cross-community bridge._
- **Why does `WatcherRecord` connect `WatcherRecord` to `companion/main.py`?**
  _High betweenness centrality (0.019) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `Room` (e.g. with `evaluate_and_broadcast_conflicts()` and `handle_activity()`) actually correct?**
  _`Room` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `start_mac.sh script`, `stop_mac.sh script`, `$schema` to the rest of the system?**
  _137 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `App.tsx` be split into smaller, more focused modules?**
  _Cohesion score 0.05575065847234416 - nodes in this community are weakly interconnected._
- **Should `watcher.py` be split into smaller, more focused modules?**
  _Cohesion score 0.06060606060606061 - nodes in this community are weakly interconnected._