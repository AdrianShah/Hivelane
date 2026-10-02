# Graph Report - Synergo  (2026-10-01)

## Corpus Check
- 85 files · ~10,664,286 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 953 nodes · 2092 edges · 71 communities (53 shown, 18 thin omitted)
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 409 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `47319ac6`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Sidebar.tsx|Sidebar.tsx]]
- [[_COMMUNITY_daemon.py|daemon.py]]
- [[_COMMUNITY_contextlib|contextlib]]
- [[_COMMUNITY_Synergo|Synergo]]
- [[_COMMUNITY_package.json|package.json]]
- [[_COMMUNITY_Daemon|Daemon]]
- [[_COMMUNITY_gemini.py|gemini.py]]
- [[_COMMUNITY_test_fail_open.py|test_fail_open.py]]
- [[_COMMUNITY_hello.py|hello.py]]
- [[_COMMUNITY_main.py|main.py]]
- [[_COMMUNITY_compilerOptions|compilerOptions]]
- [[_COMMUNITY_compilerOptions|compilerOptions]]
- [[_COMMUNITY_Lane.tsx|Lane.tsx]]
- [[_COMMUNITY_test_claude_code_adapter.py|test_claude_code_adapter.py]]
- [[_COMMUNITY_cli.py|cli.py]]
- [[_COMMUNITY_smoke.sh|smoke.sh]]
- [[_COMMUNITY_PROJECT_STORY|PROJECT_STORY.md]]
- [[_COMMUNITY_room.py|room.py]]
- [[_COMMUNITY_demo_projREADME|demo_proj/README.md]]
- [[_COMMUNITY_utils.py|utils.py]]
- [[_COMMUNITY_.oxlintrc.json|.oxlintrc.json]]
- [[_COMMUNITY_vercel.json|vercel.json]]
- [[_COMMUNITY_test_frame_handling.py|test_frame_handling.py]]
- [[_COMMUNITY_test_rate_limits.py|test_rate_limits.py]]
- [[_COMMUNITY_React + TypeScript + Vite|React + TypeScript + Vite]]
- [[_COMMUNITY_vite-env.d.ts|vite-env.d.ts]]
- [[_COMMUNITY_tsconfig.json|tsconfig.json]]
- [[_COMMUNITY_sanitize.py|sanitize.py]]
- [[_COMMUNITY_VersionTooOld|VersionTooOld]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_readyz|readyz]]
- [[_COMMUNITY_setup.sh script|setup.sh script]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY__edit_check_reason|_edit_check_reason]]
- [[_COMMUNITY_deploy_hook.sh|deploy_hook.sh]]
- [[_COMMUNITY_test_config.py|test_config.py]]
- [[_COMMUNITY_NormalizedEvent|NormalizedEvent]]
- [[_COMMUNITY_graphify|graphify]]
- [[_COMMUNITY_pre-commit|pre-commit]]
- [[_COMMUNITY_App.tsx|App.tsx]]
- [[_COMMUNITY_types.ts|types.ts]]
- [[_COMMUNITY_AddWatcher.tsx|AddWatcher.tsx]]
- [[_COMMUNITY_post_deploy_check.sh|post_deploy_check.sh]]
- [[_COMMUNITY_test_edit_check_roundtrip.py|test_edit_check_roundtrip.py]]
- [[_COMMUNITY_ConflictBanner.tsx|ConflictBanner.tsx]]
- [[_COMMUNITY_shutil|shutil]]
- [[_COMMUNITY_DeployModal.tsx|DeployModal.tsx]]
- [[_COMMUNITY_adapters__init__.py|adapters/__init__.py]]
- [[_COMMUNITY_synergo__init__.py|synergo/__init__.py]]
- [[_COMMUNITY_synergo|synergo]]
- [[_COMMUNITY_servertests__init__.py|server/tests/__init__.py]]
- [[_COMMUNITY_synergotests__init__.py|synergo/tests/__init__.py]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_signal|signal]]
- [[_COMMUNITY_repo_relative_path|repo_relative_path]]
- [[_COMMUNITY_Room|Room]]
- [[_COMMUNITY_threading|threading]]
- [[_COMMUNITY_test_room_store.py|test_room_store.py]]
- [[_COMMUNITY_urllib_parse|urllib_parse]]
- [[_COMMUNITY_watchdog_events|watchdog_events]]
- [[_COMMUNITY_watchdog_observers|watchdog_observers]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]

## God Nodes (most connected - your core abstractions)
1. `Room` - 67 edges
2. `Daemon` - 47 edges
3. `NormalizedEvent` - 46 edges
4. `$()` - 43 edges
5. `EventAck` - 40 edges
6. `ActivityMessage` - 39 edges
7. `RoomManager` - 32 edges
8. `BaseModel` - 31 edges
9. `FileTouch` - 28 edges
10. `ActivityUpdate` - 25 edges

## Surprising Connections (you probably didn't know these)
- `Environments` --references--> `main()`  [INFERRED]
  README.md → synergo/cli.py
- `NameRejected` --inherits--> `BaseModel`  [EXTRACTED]
  synergo/protocol.py → server/room.py
- `RoomRejected` --inherits--> `BaseModel`  [EXTRACTED]
  synergo/protocol.py → server/room.py
- `SynergoFeed` --inherits--> `BaseModel`  [EXTRACTED]
  synergo/protocol.py → server/room.py
- `EventAck` --inherits--> `BaseModel`  [EXTRACTED]
  synergo/events.py → server/room.py

## Import Cycles
- 1-file cycle: `server/main.py -> server/main.py`
- 1-file cycle: `synergo/daemon.py -> synergo/daemon.py`
- 1-file cycle: `server/room_manager.py -> server/room_manager.py`
- 1-file cycle: `server/tests/test_check_edit.py -> server/tests/test_check_edit.py`

## Communities (71 total, 18 thin omitted)

### Community 1 - "daemon.py"
Cohesion: 0.11
Nodes (15): asyncio, difflib, The Synergo daemon: one asyncio process that is both a relay room member and a, unified_diff(), NameRejected, Synergo relay wire format, as seen from a client.  Deliberately duplicated fro, RoomRejected, SynergoFeed (+7 more)

### Community 3 - "Synergo"
Cohesion: 0.07
Nodes (26): 1. Relay server, 2. Dashboard, 3. Synergo CLI (on each contributor machine), Architecture, Container, Dashboard, Dashboard (`frontend/`), Demo tips (+18 more)

### Community 4 - "package.json"
Cohesion: 0.09
Nodes (21): dependencies, react, react-dom, react-router-dom, devDependencies, oxlint, @types/node, @types/react (+13 more)

### Community 5 - "Daemon"
Cohesion: 0.17
Nodes (5): ArgumentParser, Daemon, Any, Next payload to send: any retained in-flight one, then priority, then bulk., Dispatch one relay frame.          A bad frame must not kill the daemon: every

### Community 6 - "gemini.py"
Cohesion: 0.09
Nodes (24): AddWatcher(), AddWatcherProps, Step, steps(), AppView, formatElapsed(), NAV, Sidebar() (+16 more)

### Community 7 - "test_fail_open.py"
Cohesion: 0.11
Nodes (39): io, pathlib, socket, synergo, synergo_adapters, PeerTouch, EventAck, MonkeyPatch (+31 more)

### Community 8 - "hello.py"
Cohesion: 0.17
Nodes (9): datetime, Any, fib(), glass_theme(), lane_status(), Synergo demo project — edit these files to light up agent lanes., Conflict radar sketch — agents × overlapping files., Obvious numeric diff bait for the board. (+1 more)

### Community 9 - "main.py"
Cohesion: 0.07
Nodes (92): ActivityUpdate, Client, ConflictAlert, fastapi_middleware_cors, FileChange, FileOverlap, google, google_genai (+84 more)

### Community 10 - "compilerOptions"
Cohesion: 0.10
Nodes (19): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+11 more)

### Community 11 - "compilerOptions"
Cohesion: 0.12
Nodes (16): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+8 more)

### Community 12 - "Lane.tsx"
Cohesion: 0.07
Nodes (29): dependencies, react, react-dom, devDependencies, @babel/core, babel-plugin-react-compiler, eslint, @eslint/js (+21 more)

### Community 13 - "test_claude_code_adapter.py"
Cohesion: 0.38
Nodes (17): EditCheckRequest, FastAPI, IntentMessage, ActivityMessage, AgentKind, EventAck, NormalizedEvent, Path (+9 more)

### Community 14 - "cli.py"
Cohesion: 0.12
Nodes (35): argparse, Exception, httpx, Namespace, build_parser(), cmd_hook_claude_code(), cmd_hook_install(), cmd_hook_uninstall() (+27 more)

### Community 15 - "smoke.sh"
Cohesion: 0.57
Nodes (5): smoke.sh script, fail(), hook_event(), start_daemon(), wait_for()

### Community 16 - "PROJECT_STORY.md"
Cohesion: 0.25
Nodes (7): Accomplishments that we're proud of, Challenges we ran into, How we built it, Inspiration, What it does, What's next for Synergo, What we learned

### Community 17 - "room.py"
Cohesion: 0.25
Nodes (6): LogRecord, configure(), JsonFormatter, Log formatting. Human-readable locally, JSON everywhere else.  No log shipper, Install the right formatter on the root logger. Safe to call once., sys

### Community 18 - "demo_proj/README.md"
Cohesion: 0.33
Nodes (5): Edit src/hello.py and src/utils.py (ideally from two agents) to show lanes + conflicts., Point the watcher here to stream live edits into the dashboard:, python watcher/watcher.py --path demo_proj --name hossein --room <ROOM> \, --server wss://hackday-hackthevalley.onrender.com/ws --agent cursor, Synergo demo project

### Community 19 - "utils.py"
Cohesion: 0.18
Nodes (29): $(), ACTIVITY, backOut(), camera(), caption(), center(), clamp(), CursorPath (+21 more)

### Community 20 - ".oxlintrc.json"
Cohesion: 0.33
Nodes (5): plugins, rules, react/only-export-components, react/rules-of-hooks, $schema

### Community 21 - "vercel.json"
Cohesion: 0.33
Nodes (5): buildCommand, framework, headers, outputDirectory, rewrites

### Community 22 - "test_frame_handling.py"
Cohesion: 0.27
Nodes (25): bass_note(), bell(), blip(), boom(), clap(), crash(), crowd(), env() (+17 more)

### Community 23 - "test_rate_limits.py"
Cohesion: 0.21
Nodes (14): collections, math, check_room_creation(), check_ws_attempt(), client_ip(), HTTPConnection, RateLimitResult, Dependency-free per-IP rate limiting for the unauthenticated relay surface.  S (+6 more)

### Community 24 - "React + TypeScript + Vite"
Cohesion: 0.50
Nodes (3): Expanding the Oxlint configuration, React Compiler, React + TypeScript + Vite

### Community 25 - "vite-env.d.ts"
Cohesion: 0.13
Nodes (21): FixedWindowLimiter, Fixed-window counter per key, with a bounded key set.      The key set is capp, HTTPConnection, MonkeyPatch, starlette_websockets, _connection(), Per-IP rate limiting on the unauthenticated surface: room creation, WebSocket c, An unknown room must not be distinguishable from a known one once the     calle (+13 more)

### Community 27 - "sanitize.py"
Cohesion: 0.10
Nodes (19): compilerOptions, allowArbitraryExtensions, allowImportingTsExtensions, erasableSyntaxOnly, jsx, lib, module, moduleDetection (+11 more)

### Community 28 - "VersionTooOld"
Cohesion: 0.12
Nodes (16): compilerOptions, allowImportingTsExtensions, erasableSyntaxOnly, lib, module, moduleDetection, noEmit, noFallthroughCasesInSwitch (+8 more)

### Community 29 - "Community 29"
Cohesion: 0.18
Nodes (15): conflict_message(), _file_path(), hook_settings(), normalize(), Claude Code hook adapter. Schema per https://code.claude.com/docs/en/hooks (fetc, Human sentence naming who is in this file and what to do about it.      The re, Stdout for Claude Code. Empty string means "no output, proceed"., The hooks block synergo wants merged into a Claude Code settings.json.      Us (+7 more)

### Community 30 - "readyz"
Cohesion: 0.12
Nodes (15): Activity, ActivityUpdate, AgentKind, FeedEvent, FileChange, IntentUpdate, JoinMessage, NameRejected (+7 more)

### Community 32 - "Community 32"
Cohesion: 0.19
Nodes (10): AVATAR_COLORS, avatarGradient(), initials(), Lane(), LaneProps, relativeTime(), shortHash(), watchBadge() (+2 more)

### Community 33 - "_edit_check_reason"
Cohesion: 0.13
Nodes (5): Path, subprocess, Configuration loading: development defaults, and refusing to boot when a stagin, Importing the module — not just calling load_settings — must abort, so a     mi, test_import_fails_fast_when_production_has_no_cors_origins()

### Community 36 - "test_config.py"
Cohesion: 0.23
Nodes (10): dataclasses, dotenv, _bool(), ConfigError, _csv(), load_settings(), _positive_int(), Relay configuration, read from the environment once at import.  A misconfigure (+2 more)

### Community 37 - "NormalizedEvent"
Cohesion: 0.17
Nodes (20): Daemon, post_event(), EventAck, NormalizedEvent, relative_path(), NormalizedEvent, Adapter-agnostic event schema spoken over the daemon's local HTTP port.  Every, Path (+12 more)

### Community 38 - "graphify"
Cohesion: 0.50
Nodes (3): uvx, graphify, graphify-mcp

### Community 39 - "pre-commit"
Cohesion: 0.15
Nodes (12): author, dependencies, playwright-core, description, keywords, license, main, name (+4 more)

### Community 40 - "App.tsx"
Cohesion: 0.17
Nodes (11): collections_abc, os, pytest, server, MonkeyPatch, _isolate_environment(), _no_gemini(), Shared fixtures. Keeps the suite offline: no Gemini key, no network calls. (+3 more)

### Community 41 - "types.ts"
Cohesion: 0.27
Nodes (11): _daemon(), Relay frames are a network boundary: a bad one must not kill the daemon., Join catch-up arrives unbatched, not inside a synergo_feed frame., Throttling is the relay asking us to wait, not refusing us: the daemon     must, test_bare_user_joined_frame_records_the_peer(), test_malformed_known_frame_is_ignored(), test_non_object_frame_is_ignored(), test_own_name_is_never_recorded_as_a_peer() (+3 more)

### Community 42 - "AddWatcher.tsx"
Cohesion: 0.53
Nodes (5): ConflictBanner(), ConflictBannerProps, formatUsers(), secondsAgo(), ConflictAlert

### Community 44 - "test_edit_check_roundtrip.py"
Cohesion: 0.13
Nodes (17): fastapi_testclient, main, Any, _join(), _next_feed_event(), _next_frame(), edit_check over a real WebSocket: the reply must arrive immediately, ahead of (, A daemon that connects second must still learn its peers; without this     catc (+9 more)

### Community 45 - "ConflictBanner.tsx"
Cohesion: 0.33
Nodes (3): overlap_score(), Shared helpers for the Synergo demo tree.  Touch this file from a second agent, Rough conflict radar used in demo captions.

### Community 47 - "DeployModal.tsx"
Cohesion: 0.40
Nodes (4): /brag plan: Synergo, Honesty notes, Identity (from source), Storyboard (23.5s, 1920×1080, 30fps, 120 BPM, A minor)

### Community 48 - "adapters/__init__.py"
Cohesion: 0.40
Nodes (4): FRONTEND, [mode, ...rest], T, WORK

### Community 51 - "server/tests/__init__.py"
Cohesion: 0.67
Nodes (3): DeployModal(), DeployModalProps, watcherCommand()

### Community 52 - "synergo/tests/__init__.py"
Cohesion: 0.50
Nodes (3): Expanding the ESLint configuration, React Compiler, React + TypeScript + Vite

### Community 55 - "repo_relative_path"
Cohesion: 0.33
Nodes (10): Nearest ancestor containing .git, or None outside a repo.      Walked on the f, Path key shared by every agent in the room.      Keyed on the repo root, not t, repo_relative_path(), repo_root(), Path, Path keys must be identical for every agent in the room, whatever their cwd., _repo(), test_outside_a_repo_falls_back_to_cwd_relative() (+2 more)

### Community 56 - "Room"
Cohesion: 0.09
Nodes (31): AgentKind, WebSocket, True once no members and no spectators remain — safe to evict from         the, Return True if this user sent this kind of message too recently.          On a, Drop touches that can no longer affect a comparison.          Measured against, Queue a room-wide event and flush as a Synergo feed batch (~500ms).          P, Room, AgentKind (+23 more)

### Community 58 - "test_room_store.py"
Cohesion: 0.05
Nodes (47): logging, Protocol, RoomManager, RoomStore, RuntimeError, secrets, _generate_code(), Room (+39 more)

## Knowledge Gaps
- **218 isolated node(s):** `uvx`, `graphify-mcp`, `ACTIVITY`, `Ev`, `EVENTS` (+213 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **18 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `$()` connect `utils.py` to `Community 32`, `gemini.py`, `main.py`, `AddWatcher.tsx`, `readyz`, `Community 63`?**
  _High betweenness centrality (0.141) - this node is a cross-community bridge._
- **Why does `Client` connect `main.py` to `utils.py`, `gemini.py`?**
  _High betweenness centrality (0.079) - this node is a cross-community bridge._
- **Why does `Room` connect `Room` to `main.py`, `test_room_store.py`?**
  _High betweenness centrality (0.055) - this node is a cross-community bridge._
- **Are the 24 inferred relationships involving `Room` (e.g. with `FileOverlap` and `Request`) actually correct?**
  _`Room` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 18 inferred relationships involving `Daemon` (e.g. with `ArgumentParser` and `Daemon`) actually correct?**
  _`Daemon` has 18 INFERRED edges - model-reasoned connections that need verification._
- **Are the 27 inferred relationships involving `NormalizedEvent` (e.g. with `ArgumentParser` and `Daemon`) actually correct?**
  _`NormalizedEvent` has 27 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `EventAck` (e.g. with `ArgumentParser` and `EditCheckRequest`) actually correct?**
  _`EventAck` has 25 INFERRED edges - model-reasoned connections that need verification._