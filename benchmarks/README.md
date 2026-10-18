# Benchmarks

Scripts live in `scripts/` and `scripts/loadtest/`; results land here.
The full write-up, with numbers and the prioritised list of speedups, is
[docs/performance.md](../docs/performance.md).

None of the scripts needs a dependency the relay or daemon does not already
have. `psutil` is optional (relay CPU/RSS sampling; without it the load test
falls back to PowerShell `Get-Process` on Windows or `/proc` on Linux). For
profiling extras, use a scratch venv rather than the project one:

```bash
python -m venv /tmp/bench-venv
/tmp/bench-venv/bin/pip install -e ".[relay]" psutil pytest-xdist orjson msgspec
```

| What | Command | Output | Time |
|------|---------|--------|------|
| Relay capacity sweep (rooms, room size, total members) | `python scripts/loadtest/relay_load.py --sweep full` | `benchmarks/load/relay_load_full.{csv,json}` | ~7 min |
| One load step | `python scripts/loadtest/relay_load.py --rooms 30 --members 6 --spectators 1 --rate 2` | `benchmarks/load/relay_load_custom.*` | ~25 s |
| Load-test self-check | `python scripts/loadtest/relay_load.py --sweep quick` | `relay_load_quick.*` | ~15 s |
| Many real daemons, one process, one relay | `python scripts/loadtest/many_daemons.py --daemons 24 --per-room 6` | `benchmarks/load/many_daemons.json` | ~25 s |
| One daemon, cProfile of pre/post_edit | `python scripts/loadtest/many_daemons.py --daemons 1 --rate 4 --profile --out benchmarks/load/single_daemon.json` | `single_daemon.json`, `.profile.txt` | ~20 s |
| Relay hot-path micro-benchmarks | `python scripts/loadtest/profile_relay.py --cprofile` | `benchmarks/profile/relay_micro.json`, `relay_edit_check.cprofile.txt` | ~40 s |
| Hook cold start and round trips | `python scripts/bench_hook.py -n 15 --relay` | stdout (`benchmarks/profile/bench_hook.txt`) | ~30 s |
| CLI command timings | `python scripts/loadtest/time_cli.py -n 5` | `benchmarks/profile/cli_timings.json` | ~40 s |
| Dashboard frame handling (Node >= 22.18) | `node scripts/loadtest/dashboard_frames.mjs 1000 10000` | `benchmarks/profile/dashboard_frames.json` | ~5 s |
| Import-time breakdown | `python -X importtime -c "import server.main" 2> imports.txt` | stderr | 3 s |
| Test-suite durations | `python -m pytest -q --durations=40` | `benchmarks/profile/pytest_durations.txt` | ~2 min |
| Decision quality (not speed) | `python scripts/benchmark_decisions.py` | `benchmarks/sessions/` | |

## How the load test works

`relay_load.py` starts `uvicorn server.main:app` on a free port with every
limit off (`SYNERGO_MAX_ROOMS=0`, `SYNERGO_MAX_MEMBERS=0`, `ROOM_CREATE_LIMIT=0`,
`WS_ATTEMPT_LIMIT=0`, `ROOM_MESSAGE_LIMIT=0`), creates the rooms over HTTP and
spreads them over `--workers` load-generator processes (a room's sockets always
share one process, so latencies use one clock). Every member sends `--rate`
messages a second: `edit_check` and `activity` (a ~2 KB diff) alternating, an
`intent` every 10th message and a `plan` every 20th, over 20 files per room so
warns, blocks and their broadcasts happen. After a ramp, it measures for
`--window` seconds:

- `rtt_*`: edit_check sent -> edit_check_response received;
- `fanout_*`: activity sent -> its activity_update received by a spectator.
  The relay batches broadcasts every 0.5 s, so ~250 ms mean / ~500 ms p95 is
  the floor, not a cost;
- relay CPU (percent of one core) and peak RSS;
- connect errors, disconnects, unanswered edit_checks (5 s).

A step is `ok` when edit_check p95 < 50 ms with no errors. A sweep skips the
rest of a family after two failing steps in a row.

Load generation shares the machine with the relay, so the knees are
conservative: run the load generator on a second machine for a cleaner number.

## Results on file

- `load/relay_load_full.*`: the full sweep (18 steps).
- `load/relay_load_refine.*`: repeats around the knees (180 and 240 members,
  35 members in one room, 150 rooms of 3).
- `load/many_daemons.json`, `load/single_daemon.*`: real `Daemon` objects.
- `load/summary.csv`: the summary table (every step, full sweep and repeats).
- `profile/`: micro-benchmarks, CLI and startup timings, hook timings, test
  durations, dashboard frame costs.
