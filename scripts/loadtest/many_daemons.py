"""Many real `Daemon` instances in one process against one relay.

Starts a relay subprocess (limits off), creates one room per --per-room
daemons, starts N `synergo.daemon.Daemon` objects in this process (no HTTP
side: events go straight to Daemon.handle_event, which is what POST /events
calls) and has every daemon run edit cycles: pre_edit (symbol extraction and
an edit_check round trip to the relay) then post_edit (diff, redaction,
batched activity). Reports per-event latency, errors, and with --profile a
cProfile of the daemon side.

    python scripts/loadtest/many_daemons.py --daemons 24 --per-room 6 --seconds 20
    python scripts/loadtest/many_daemons.py --daemons 1 --seconds 10 --profile
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import cProfile
import io
import json
import pstats
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from relay_load import REPO, create_room, free_port, pct, start_relay  # noqa: E402

SOURCE = "".join(
    f"def handler_{i}(request, value):\n    result = value * {i}\n    return result + len(request)\n\n\n"
    for i in range(40)
)  # ~4 KB, 40 functions


def edited(source: str, index: int, stamp: int) -> str:
    old = f"    result = value * {index}\n"
    return source.replace(old, f"    result = value * {index} + {stamp}  # token=sk-test-{stamp:08d}\n", 1)


async def run(args) -> dict:
    from synergo.daemon import Daemon
    from synergo.events import NormalizedEvent

    work = Path(tempfile.mkdtemp(prefix="synergo-many-"))
    port = free_port()
    relay = start_relay(port, work / "relay.log", args.python)
    rooms = [create_room(port) for _ in range((args.daemons + args.per_room - 1) // args.per_room)]
    daemons = []
    lat = {"pre_edit": [], "post_edit": []}
    decisions: dict[str, int] = {}
    errors = 0
    try:
        for i in range(args.daemons):
            root = work / f"repo{i}"
            (root / "src").mkdir(parents=True)
            for f in range(10):
                (root / "src" / f"mod{f}.py").write_text(SOURCE, encoding="utf-8")
            d = Daemon(rooms[i // args.per_room], f"dev{i}", server=f"ws://127.0.0.1:{port}/ws",
                       port=0, root=str(root), token="x" * 43)
            daemons.append((d, root))
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            for d, _ in daemons:
                await d.start()
        deadline = time.time() + 30
        while time.time() < deadline and not all(d._default.connection.connected for d, _ in daemons):
            await asyncio.sleep(0.05)
        connect_s = time.perf_counter() - started
        connected = sum(d._default.connection.connected for d, _ in daemons)

        profiler = cProfile.Profile() if args.profile else None
        stop_at = time.perf_counter() + args.seconds

        async def agent(i: int, d, root: Path) -> None:
            nonlocal errors
            rnd = random.Random(i)
            n = 0
            while time.perf_counter() < stop_at:
                n += 1
                path = root / "src" / f"mod{rnd.randrange(10)}.py"
                fn = rnd.randrange(40)
                old = path.read_text(encoding="utf-8")
                new = edited(old, fn, n)
                common = dict(agent="claude-code", session_id=f"s{i}", cwd=str(root), file_path=str(path), tool="Edit")
                try:
                    t0 = time.perf_counter()
                    ack = await d.handle_event(NormalizedEvent(kind="pre_edit", old_content=old, new_content=new,
                                                               edit_hints=[{"old": f"value * {fn}\n", "new": f"value * {fn} + {n}\n"}],
                                                               **common))
                    lat["pre_edit"].append((time.perf_counter() - t0) * 1000)
                    decisions[ack.decision] = decisions.get(ack.decision, 0) + 1
                    path.write_text(new, encoding="utf-8")
                    t0 = time.perf_counter()
                    await d.handle_event(NormalizedEvent(kind="post_edit", old_content=old, new_content=new, **common))
                    lat["post_edit"].append((time.perf_counter() - t0) * 1000)
                except Exception:
                    errors += 1
                await asyncio.sleep(1.0 / args.rate)

        if profiler:
            profiler.enable()
        await asyncio.gather(*(agent(i, d, root) for i, (d, root) in enumerate(daemons)))
        if profiler:
            profiler.disable()
        still = sum(d._default.connection.connected for d, _ in daemons)
        with contextlib.redirect_stdout(io.StringIO()):
            await asyncio.gather(*(d.aclose() for d, _ in daemons), return_exceptions=True)
    finally:
        relay.kill()
        relay.wait()
    result = {
        "daemons": args.daemons, "per_room": args.per_room, "rooms": len(rooms), "rate_cycles_per_s": args.rate,
        "seconds": args.seconds, "connect_all_s": round(connect_s, 2), "connected": connected,
        "still_connected": still, "errors": errors, "decisions": decisions,
    }
    for kind, values in lat.items():
        result[kind] = {"n": len(values), "p50_ms": round(pct(values, 50), 2), "p95_ms": round(pct(values, 95), 2),
                        "p99_ms": round(pct(values, 99), 2),
                        "mean_ms": round(statistics.fmean(values), 2) if values else None}
    if profiler:
        out = io.StringIO()
        pstats.Stats(profiler, stream=out).sort_stats("cumulative").print_stats(35)
        result["profile_top"] = out.getvalue()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--daemons", type=int, default=24)
    ap.add_argument("--per-room", type=int, default=6)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--rate", type=float, default=1.0, help="edit cycles per second per daemon")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", default=str(REPO / "benchmarks" / "load" / "many_daemons.json"))
    args = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    result = asyncio.run(run(args))
    profile = result.pop("profile_top", None)
    print(json.dumps(result, indent=2))
    if profile:
        print(profile)
        Path(args.out).with_suffix(".profile.txt").write_text(profile)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
