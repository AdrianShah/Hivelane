"""Micro-benchmarks of the relay's hot paths, in process (no sockets).

- evaluate_edit vs peer touches on the file, and vs active plans/intents;
- the whole handle_edit_check handler (validation, decision, reply build);
- handle_activity (validation, caps, overlap detection, logging, queueing);
- broadcast fan-out: one feed flush to S sockets, where each send_json
  serializes the payload again (Starlette's WebSocket.send_json);
- serialization: pydantic model_dump + json.dumps (what the relay does),
  model_dump_json, and orjson / msgspec when importable (not dependencies);
- history record overhead with 1 vs 200 live rooms keeping history;
- the webhook event sink on a room with no webhooks.

    python scripts/loadtest/profile_relay.py            # prints a table, writes JSON
    python scripts/loadtest/profile_relay.py --cprofile # adds a cProfile of edit_check
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import json
import logging
import os
import pstats
import sys
import time
import timeit
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("HIVELANE_ENV", "development")
os.environ["ROOM_MESSAGE_LIMIT"] = "0"
os.environ.pop("GEMINI_API_KEY", None)

DIFF = "".join(f"+    line {i}: value = compute(value, {i})  # padding padding\n" for i in range(36))


class FakeSocket:
    """Costs what Starlette's send_json costs before the transport: one
    json.dumps per call."""

    def __init__(self) -> None:
        self.bytes = 0

    async def send_json(self, data, mode: str = "text") -> None:
        self.bytes += len(json.dumps(data, separators=(",", ":"), ensure_ascii=False))


def per_call_us(fn, number: int) -> float:
    return min(timeit.repeat(fn, number=number, repeat=5)) / number * 1e6


def bench() -> dict:
    logging.disable(logging.CRITICAL)  # handlers' log.info measured separately
    from server import history, main, models
    from server.room import Room

    out: dict = {}
    now = time.time()

    # --- evaluate_edit vs touches ------------------------------------------------
    rows = []
    for touches in (0, 1, 6, 25, 100, 500):
        room = Room()
        for u in range(touches + 1):
            room.add_member(f"u{u}", FakeSocket(), "claude-code")
        for u in range(1, touches + 1):
            room.record_activity(models.ActivityMessage(
                user_id=f"u{u}", timestamp=now - 5, changes=[{"file_path": "src/a.py", "diff": DIFF,
                                                             "symbols": [f"fn{u % 8}"], "line_ranges": [[u, u + 20]]}]))
        fn = lambda: room.evaluate_edit("u0", "src/a.py", now, symbols=["fn3"], line_ranges=[(10, 30)])
        rows.append({"touches_on_file": touches, "us": round(per_call_us(fn, 2000 if touches < 100 else 200), 2),
                     "decision": fn().decision})
    out["evaluate_edit_vs_touches"] = rows

    rows = []
    for peers in (0, 6, 25, 100, 500):
        room = Room()
        for u in range(peers + 1):
            room.add_member(f"u{u}", FakeSocket(), "claude-code")
        for u in range(1, peers + 1):
            room.record_plan(models.PlanMessage(user_id=f"u{u}", summary="refactor parser",
                                                files=[f"src/f{u}_{k}.py" for k in range(10)]), now, "claude-code")
            room.record_intent(f"u{u}", f"fix the bug in module{u}.py and tidy up", now, "claude-code")
        fn = lambda: room.evaluate_edit("u0", "src/zzz.py", now)  # misses: worst case scan
        rows.append({"peers_with_plan_and_intent": peers, "us": round(per_call_us(fn, 500), 2)})
    out["evaluate_edit_vs_plans_intents"] = rows

    # --- full handlers -------------------------------------------------------------
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    rows = []
    for members in (2, 6, 25, 100):
        room = Room()
        socks = [FakeSocket() for _ in range(members)]
        for u, s in enumerate(socks):
            room.add_member(f"u{u}", s, "claude-code")
        for u in range(1, members):
            room.record_activity(models.ActivityMessage(user_id=f"u{u}", timestamp=now,
                                                        changes=[{"file_path": f"src/m{u % 20}.py", "diff": DIFF}]))
        raw = {"type": "edit_check", "request_id": "r", "file_path": "src/m3.py", "symbols": ["fn1"], "line_ranges": [[1, 9]]}

        async def many(n=300):
            for _ in range(n):
                await main.handle_edit_check(room, socks[0], "u0", dict(raw))
        t0 = time.perf_counter()
        loop.run_until_complete(many())
        per = (time.perf_counter() - t0) / 300 * 1e6
        if room._flush_task:
            room._flush_task.cancel()
        room._broadcast_queue.clear()
        rows.append({"members": members, "handle_edit_check_us": round(per, 1)})
    out["handle_edit_check"] = rows

    room = Room()
    sock = FakeSocket()
    room.add_member("u0", sock, "claude-code")
    act = {"type": "activity", "user_id": "u0", "prompt": "make the change",
           "changes": [{"file_path": "src/a.py", "diff": DIFF, "symbols": ["fn1"], "line_ranges": [[1, 30]]}]}

    async def acts(n=300):
        for _ in range(n):
            room._last_activity_at.clear()  # bypass the 0.5 s per-member throttle
            await main.handle_activity(room, "u0", dict(act))
    t0 = time.perf_counter()
    loop.run_until_complete(acts())
    out["handle_activity_us"] = round((time.perf_counter() - t0) / 300 * 1e6, 1)
    if room._flush_task:
        room._flush_task.cancel()
    room._broadcast_queue.clear()

    # logging cost of the per-message log.info lines (as configured in dev)
    logging.disable(logging.NOTSET)
    lg = logging.getLogger("relay.bench")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(logging.StreamHandler(io.StringIO()))
    out["log_info_line_us"] = round(per_call_us(lambda: lg.info("activity from %s (%d file(s), prompt=%s)", "u0", 1, "yes"), 5000), 2)
    logging.disable(logging.CRITICAL)

    # --- broadcast fan-out --------------------------------------------------------
    update = models.ActivityUpdate(user_id="u1", timestamp=now, prompt="make the change",
                                   changes=[{"file_path": "src/a.py", "diff": DIFF}], caption=None, agent="claude-code")
    rows = []
    for sockets in (2, 7, 26, 101, 501):
        room = Room()
        for u in range(sockets):
            room.add_spectator(FakeSocket())
        events = [update.model_dump() for _ in range(10)]
        feed = models.HivelaneFeed(events=events).model_dump()

        async def deliver(n=20):
            for _ in range(n):
                await room._deliver(feed)
        t0 = time.perf_counter()
        loop.run_until_complete(deliver())
        per_flush = (time.perf_counter() - t0) / 20 * 1e3
        once = per_call_us(lambda: json.dumps(feed, separators=(",", ":"), ensure_ascii=False), 200) / 1e3
        rows.append({"sockets": sockets, "flush_10_events_ms": round(per_flush, 3),
                     "serialize_once_ms": round(once, 3),
                     "feed_bytes": len(json.dumps(feed, separators=(",", ":")))})
    out["broadcast_flush"] = rows

    # --- serialization -----------------------------------------------------------------
    ser = {}
    ser["model_dump_us"] = per_call_us(update.model_dump, 20000)
    d = update.model_dump()
    ser["json_dumps_us"] = per_call_us(lambda: json.dumps(d, separators=(",", ":"), ensure_ascii=False), 20000)
    ser["model_dump_json_us"] = per_call_us(update.model_dump_json, 20000)
    ser["model_validate_activity_us"] = per_call_us(lambda: models.ActivityMessage.model_validate(act), 20000)
    ser["json_loads_activity_us"] = per_call_us(lambda: json.loads(json.dumps(act)), 20000)
    try:
        import orjson  # type: ignore
        ser["orjson_dumps_us"] = per_call_us(lambda: orjson.dumps(d), 20000)
    except ImportError:
        pass
    try:
        import msgspec  # type: ignore
        enc = msgspec.json.Encoder()
        ser["msgspec_dumps_us"] = per_call_us(lambda: enc.encode(d), 20000)
    except ImportError:
        pass
    out["serialization_activity_update"] = {k: round(v, 2) for k, v in ser.items()}

    # --- history overhead --------------------------------------------------------------
    rows = []
    for live in (1, 200):
        hs = [history.RoomHistory(1.0) for _ in range(live)]
        events = [update.model_dump() for _ in range(5)]
        for h in hs:
            h.record(events)
        first, batch = hs[0], events  # bound now: the lambda must not see a later hs
        rows.append({"rooms_with_history": live, "record_5_events_us": round(per_call_us(lambda: first.record(batch), 300), 1)})
        for h in hs:
            h.clear()
        hs.clear()
    out["history_record"] = rows

    # --- webhook sink, room with none --------------------------------------------
    sink = main.webhook_service.observe
    room = Room()
    room.code = "ABCDEFGH"
    dec = models.EditDecision(user_id="u", agent="claude-code", file_path="a.py", decision="block", others=["v"],
                              reason="x", timestamp=now).model_dump()

    async def sinks(n=2000):
        for _ in range(n):
            sink(room, dec)
    loop.run_until_complete(sinks(1))  # first call loads (empty) cache
    loop.run_until_complete(asyncio.sleep(0.05))
    t0 = time.perf_counter()
    loop.run_until_complete(sinks())
    out["webhook_observe_us"] = round((time.perf_counter() - t0) / 2000 * 1e6, 2)
    out["room_settings_property_us"] = round(per_call_us(lambda: room.settings.e2e, 20000), 2)
    loop.close()
    return out


def cprofile_edit_check() -> str:
    logging.disable(logging.CRITICAL)
    from server import main, models
    from server.room import Room
    room = Room()
    socks = [FakeSocket() for _ in range(6)]
    for u, s in enumerate(socks):
        room.add_member(f"u{u}", s, "claude-code")
    now = time.time()
    for u in range(1, 6):
        room.record_activity(models.ActivityMessage(user_id=f"u{u}", timestamp=now,
                                                    changes=[{"file_path": "src/m3.py", "diff": DIFF, "symbols": ["fn1"]}]))
    raw = {"type": "edit_check", "request_id": "r", "file_path": "src/m3.py", "symbols": ["fn1"], "line_ranges": [[1, 9]]}

    async def many():
        for _ in range(3000):
            await main.handle_edit_check(room, socks[0], "u0", dict(raw))
            room._broadcast_queue.clear()
    prof = cProfile.Profile()
    loop = asyncio.new_event_loop()
    prof.enable()
    loop.run_until_complete(many())
    prof.disable()
    loop.close()
    s = io.StringIO()
    pstats.Stats(prof, stream=s).sort_stats("tottime").print_stats(25)
    return s.getvalue()


def main_() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "benchmarks" / "profile" / "relay_micro.json"))
    ap.add_argument("--cprofile", action="store_true")
    args = ap.parse_args()
    result = bench()
    print(json.dumps(result, indent=2))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    if args.cprofile:
        text = cprofile_edit_check()
        print(text)
        Path(args.out).with_name("relay_edit_check.cprofile.txt").write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main_())
