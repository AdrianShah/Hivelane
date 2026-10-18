# Performance and capacity

How much one relay process handles, how fast every Synergo process is, and
what to speed up first. Every number here comes from the scripts listed in
[benchmarks/README.md](../benchmarks/README.md); raw results are in
`benchmarks/load/` and `benchmarks/profile/`.

## Machine

| | |
|-|-|
| CPU | AMD Ryzen 5 2600, 6 cores / 12 threads, 3.4 GHz |
| RAM | 32 GB |
| OS | Windows 10 Home 19045 |
| Python | 3.13.14 (CPython, asyncio selector loop; no uvloop on Windows) |
| Node | 24.13.0 |
| Relay | one `uvicorn server.main:app` process, `SYNERGO_ENV=development` (text logs at info), no Redis, memory room store |

The load generator ran on the same machine, and Windows asyncio is slower
than Linux with uvloop, so treat the capacity numbers as a conservative floor
for a Linux VM of similar speed (the Docker image runs uvicorn with uvloop).

## Capacity (load test)

Each member sent 2 messages/s: edit_check and activity (with a ~2 KB diff)
alternating, an intent every 10th message, a plan every 20th, over 20 files per
room (so warns, blocks and their broadcasts happen). A step passes when the
edit_check round trip p95 stays under 50 ms with no errors. Broadcast fan-out
includes the relay's 0.5 s batching, so ~500 ms p95 is its floor.

| Sweep | Last passing step | p95 there | First failing step | p95 there | Relay CPU at knee |
|-------|-------------------|----------:|--------------------|----------:|------:|
| Total members, rooms of 6 (+1 dashboard) | 120 members (180 passes 2 runs of 3) | 21.5 ms (27-43 ms) | 240 members | 55-58 ms | 40-50 % |
| Total members, rooms of 3 (+1 dashboard) | 300 members (100 rooms) | 39.2 ms | 450 members (150 rooms) | 72.7 ms | 48 % |
| Members in one room (+2 dashboards) | 25 | 27.9 ms | 35 (marginal) / 50 | 52.1 / 158 ms | 15-25 % |
| Rooms (3 members each) | 100 | 39.2 ms | 150 | 72.7 ms | 48 % |

- **Knee for one process: about 180-200 concurrently busy members (~400
  inbound messages/s), 25 members in one room, ~100-150 rooms at that rate.**
  Past it the relay queues rather than fails: 1,500 members took p95 4.6 s but
  no socket was dropped and every edit_check was answered.
- Memory: 52 MB idle; about 0.2 MB per connected socket (414 MB with 2,000
  sockets).
- Latency climbs at 40-50 % of one core, long before the core is saturated:
  the relay writes to sockets one at a time under the room lock (bottleneck 1).
- Real agents send far less than 2 messages/s (an edit cycle every 10-30 s is
  0.07-0.2 messages/s). Scaling by message rate gives an estimate of 2,000-3,500
  real members per process; memory and connection churn then matter more. This
  is an extrapolation, not a measurement.
- Many daemons: 24 real `Daemon` objects in one process against one relay
  (4 rooms of 6, one edit cycle/s each): all connected in 0.06 s, none dropped,
  0 errors; pre_edit p50 34.7 ms / p95 90.8 ms (24 daemons sharing one event
  loop and real blocks), post_edit p50 3.3 / p95 5.0 ms.
- `scripts/smoke.sh`: PASS, 17.5 s.

Full table: `benchmarks/load/summary.csv`. Recommended limits for a public
relay, derived from these knees: [self-hosting.md](self-hosting.md#capacity-limits).

## Speed by process

### 1. Hook (`synergo hook claude-code`, a fresh process per edit)

| Scenario | p50 | p95 |
|----------|----:|----:|
| Floor: `python -c pass` | 74 ms | 89 ms |
| Stub daemon (allow ack) | 112 ms | 129 ms |
| Real daemon, token auth, no room | 119 ms | 126 ms |
| Real daemon + real relay (edit_check round trip) | 114 ms | 116 ms |
| No daemon (fail-open) | 362 ms | 378 ms |

- The relay round trip adds nothing visible: the hook's cost is interpreter
  start (74 ms) plus ~40 ms of console-script launcher, imports
  (`synergo.hook_fast` 17 ms cumulative) and the loopback POST.
- Fail-open on Windows is 250 ms slower: Windows retries a refused loopback
  connect, so `hook_fast.CONNECT_TIMEOUT` (0.25 s) is paid on every edit while
  the daemon is down.

### 2. Daemon

| Measure | Value |
|---------|------:|
| `synergo serve` start to listening | 1.33-1.56 s |
| Import `synergo.daemon` | 0.90-1.00 s (fastapi 0.45-0.49 s, asyncio 0.11-0.15 s, uvicorn 0.11 s) |
| RSS, idle | 52.6 MB |
| pre_edit (symbol extraction + edit_check to relay), one daemon | p50 1.75 ms, p95 3.1 ms |
| post_edit (diff, redaction, signatures, batching), one daemon | p50 4.6 ms, p95 6.4 ms |

cProfile of post_edit: `ast.parse` runs about 4 times per edit (old and new
content, once for `depgraph` signatures and once for the symbol outline),
~45 % of daemon CPU; `difflib.unified_diff` ~15 %; path resolution
(`Path.resolve`/`realpath`) ~15 %.

### 3. Relay

| Measure | Value |
|---------|------:|
| Import `server.main` | 2.6 s, of which `google.genai` 1.65 s (imported even with no `GEMINI_API_KEY`) |
| Start to `/health` (google-genai not installed) | 1.2 s |
| RSS, idle | 52 MB |
| `handle_edit_check`, whole handler | 80 µs (2 members) to 210 µs (100 members) |
| `evaluate_edit` | 11.5 µs + ~1.9 µs per peer touch on the file (964 µs at 500) |
| `evaluate_edit`, plan/intent scan | +1.2 µs per peer with a plan and an intent |
| `handle_activity` (validate, cap, overlaps, queue broadcast) | 52 µs |
| One `log.info` line (dev text format) | 21 µs; 2-4 lines per inbound message |
| Feed flush, 10 events (24 KB), to 2 / 7 / 26 / 101 / 501 sockets | 0.28 / 0.95 / 3.6 / 13.5 / 67.5 ms |
| Serializing that feed once | 0.13 ms |
| History `record`, 5 events, 1 vs 200 rooms keeping history | 93 vs 129 µs |
| Webhook sink, room without webhooks | 0.6 µs |
| `room.settings` property (a `model_copy`) | 2.3 µs per read |

Serialization of one `activity_update` (2 KB diff):

| | µs |
|-|---:|
| `model_dump()` | 3.4 |
| `json.dumps` of that dict (what `send_json` does, once per socket) | 16.7 |
| `model_dump_json()` | 5.7 |
| `orjson.dumps` (not a dependency) | 1.5 |
| `msgspec` JSON encode (not a dependency) | 1.6 |
| `model_validate` of an inbound activity | 4.6 |
| `json.loads` + `json.dumps` of an inbound activity | 19.7 |

cProfile of 3,000 edit_checks: pydantic validation 9 %, `json` encoding of the
reply 7 %, `to_python` (model_dump) 5 %, `_suggestion_inputs`/`test_names`
(fnmatch, regex) 9 %, `evaluate_edit` 18 % cumulative.

### 4. CLI

| Command | Median |
|---------|-------:|
| `python -c pass` (floor) | 76 ms |
| `synergo --help` | 1,068 ms |
| `synergo status` | 1,152 ms |
| `synergo doctor` | 1,577 ms |
| `synergo up --no-hooks -y`, first run (creates the room, spawns the daemon) | 4,565 ms |
| `synergo up --no-hooks -y`, re-run | 1,335 ms |

About 0.9 s of every command is `build_parser` importing `synergo.daemon`
(fastapi, uvicorn, websockets) for one string constant, `DEFAULT_SERVER`.
`synergo.cli` itself imports in 18 ms.

### 5. Dashboard

Measured on the **committed** frontend (a temporary `git worktree` of HEAD),
not the in-progress redesign in the working tree.

| Measure | Value |
|---------|------:|
| JS bundle (one chunk) | 383.6 KB, 117.6 KB gzip |
| CSS | 33.8 KB, 7.4 KB gzip |
| `index.html` | 4.6 KB, 1.7 KB gzip |
| Largest sources (pre-minification) | react-dom 533 KB, react-router 363 KB, components 135 KB, dashboard logic 67 KB, App.tsx 62 KB |
| `tsc -b` / `vite build` | 6.8 s / 4.3 s |
| Session-log reducer, 1k events | 9.5 ms (9.5 µs/event) |
| Session-log reducer, 10k / 50k events (at the 3,000-touch cap) | 253 ms / 1.22 s (25 µs/event) |
| One feed frame on a full log (reducer + prevention + stats + heat-map tree) | 8 ms |

There is no code splitting: every view ships in the first load. First render
and in-browser frame cost were not measured (the browser automation tools
were unavailable in this run); the Node numbers cover the pure logic React
re-runs per frame.

### 6. Tests and CI

| Measure | Value |
|---------|------:|
| Full suite, serial (1,299 passed, 1 skipped) | 105 s in pytest, 109 s wall |
| `pytest-xdist -n 6` / `-n 12` (scratch venv) | 30.5 s / 26.2 s (3.6-4.2x) |
| Slowest 40 tests | 50 s together; the top five are subprocess and daemon lifecycle tests (2.4-3.8 s each) |
| Pattern | a few dozen relay tests take 0.5 s or 1.0 s: they wait out `BROADCAST_FLUSH_SECONDS` (0.5 s) once or twice |
| CI, relay + daemon job (last full matrix, phase-5) | ubuntu 1.4-1.5 min, windows 2.6-3.1 min, macOS ~10.5 min (both failed) |
| CI, dashboard / relay image / cross-cwd jobs | 21-29 s / 24-50 s / 23-24 s |

### 7. Docker and docs

Docker was not running on this machine; image build time and size were not
measured. Docs site (`docs-site`, Astro + Starlight): `npm run build` 7.8 s
(19 pages in 3.2 s, search index 0.24 s).

## Top bottlenecks

1. **Broadcast fan-out serializes per socket, in series, under the room lock.**
   `Room._deliver` calls `send_json` for every member and dashboard, and each
   call runs `json.dumps` again: a 10-event feed costs 13.5 ms for 101 sockets
   where one serialization is 0.13 ms. Every send is awaited one after another
   while holding the lock that `broadcast()` also takes, so one slow socket
   delays the whole room. This is why one room tops out at 25-35 members and
   why latency rises at half a core.
2. **Per-message info logging.** `activity from`, `plan from`, `edit_check
   block`, `file_requested` are logged at info for every message: 21 µs per
   line, several lines per message, 9 MB of log in a 7-minute sweep.
3. **CLI imports the daemon stack for a constant.** ~0.9 s of every
   `synergo --help`/`status`/`doctor`/`up` (1.1-1.6 s against a 76 ms floor).
4. **Relay imports google-genai unconditionally.** 1.65 s of a 2.6 s import,
   paid on every relay start and test-suite run, used only with
   `GEMINI_API_KEY`.
5. **Serial test suite.** 105 s locally, 1.5-3 min per CI job; a few dozen
   relay tests sleep through the 0.5 s broadcast batching.

Smaller: the hook's Windows fail-open path (+250 ms per edit while the daemon
is down), the daemon parsing each file 4 times per post_edit, `room.settings`
copying the model on every read, history re-serializing every kept event, and
the dashboard's single 118 KB-gzip chunk.

## Recommended speedups, in priority order

| # | Change | Expected gain | Effort | Risk | New dependency |
|---|--------|---------------|--------|------|----------------|
| 1 | In `Room._deliver`, serialize the feed once (`json.dumps` to text) and `send_text` it to every socket; send concurrently (`asyncio.gather` with a per-socket timeout) and outside the room lock; drop a socket that times out | Flush 13.5 ms -> ~0.3 ms at 100 sockets (measured parts); estimated one-room knee 25 -> 100+ members and total knee +30-50 % | S | Low-medium (ordering per socket stays FIFO; timeouts need a test) | No |
| 2 | `build_parser`: stop importing `synergo.daemon` for `DEFAULT_SERVER` (move the constant to a light module such as `synergo/config.py`) | -0.9 s on every CLI command (`--help` 1.07 s -> ~0.15 s, estimated) | XS | Low | No |
| 3 | `server/gemini.py`: import `google.genai` only when `GEMINI_API_KEY` is set | Relay start -1.6 s; test-suite collection about -1.6 s | XS | Low | No |
| 4 | Log per-message lines (`activity from`, `plan from`, `edit_check ...`, `file_requested`) at debug; keep counters in `/metrics` | ~5-10 % relay CPU at load (estimate: 40-80 µs of ~1 ms per message) and far smaller logs | XS | Low (less detail in default logs) | No |
| 5 | Run tests with `pytest-xdist` (`-n auto`) locally and in CI; let relay tests set `BROADCAST_FLUSH_SECONDS` to ~0.05 s through a fixture | Local 105 s -> 26-30 s (measured); CI jobs roughly halve; a further ~15-20 s from shorter flush waits (estimate) | S | Medium (tests that share ports or global state; one flaky-run check needed) | Dev only: `pytest-xdist` |
| 6 | Use `orjson` (or `msgspec`) for the relay's outbound frames, with `json` as fallback | `json.dumps` 16.7 -> 1.5 µs per update; on top of #1, roughly -10 % relay CPU per message (estimate) | S | Low | Yes, optional (`orjson`, binary wheels for every CI platform) |
| 7 | Nothing to do for the image: `server/requirements.txt` already pins `uvicorn[standard]`, so Linux relays run on uvloop + httptools. and so does the `relay` extra (`synergo relay`). Listed so nobody re-measures it | Typically 1.5-2x websocket throughput over the plain asyncio loop measured here (published uvicorn figures, not measured: Windows has no uvloop) | XS | Low | Already a relay dependency |
| 8 | Daemon post_edit: parse each content once and share the tree between `depgraph.changed_signatures` and `symbols.outline_for` (cache by content hash) | post_edit 4.6 -> ~3 ms (estimate from the profile: ~45 % is `ast.parse`) | S | Low | No |
| 9 | Hook on Windows: remember "daemon down" for a few seconds (a timestamp file next to the spawn lock) and skip the connect | Fail-open edit 362 -> ~115 ms while the daemon is down | S | Low-medium (must not hide a daemon that just came up; keep the window short) | No |
| 10 | Read `room._settings` directly on hot paths (`handle_activity`, webhook sink) instead of the copying `settings` property | 2.3 µs per read, a few per message | XS | Low | No |
| 11 | History: reuse the bytes already serialized for the feed (#1) instead of `json.dumps` per kept event; keep a running process-wide byte total instead of summing every room | `record` 93-129 µs -> ~10 µs per flush (estimate) | S | Low | No |
| 12 | Dashboard: lazy-load views (`React.lazy` for replay, heat map, docs, settings); in `sessionLog`, replace the linear replay check (`touches.some`) with a keyed set | First load below ~80 KB gzip (estimate); reducer 25 -> ~10 µs per event at the cap | S-M | Low | No |

Not implemented here: this pass only made the capacity limits configurable
(part A of the task). No speedup above was applied.
