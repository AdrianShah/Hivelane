"""Replay recorded two-agent sessions against Room's edit-decision logic.

Usage:
    python scripts/benchmark_decisions.py              # run the benchmark
    python scripts/benchmark_decisions.py --generate   # (re)write synthetic sessions
    python scripts/benchmark_decisions.py -v           # per-check detail

Each session in benchmarks/sessions/*.json is
    {"name", "description", "events": [
        {"t": seconds, "user": "alice", "kind": "intent"|"plan"|"pre_edit"|"post_edit",
         "file": "...", "symbols": [...], "line_ranges": [[a, b], ...],
         "branch": "...", "prompt": "...", "files": [...], "label": "conflict"|"no_conflict"}]}
Only pre_edit events carry a label; each one is a Room.check_edit call.

Two engines run on the same events, in-process, the way server/main.py drives
Room: a file-level baseline (symbols, line ranges, branch and plans stripped)
and the full engine (everything forwarded). Room.check_edit / record_plan /
record_intent get keyword args for the new fields only when their signatures
accept them, so on an older Room the harness degrades to file-level.

Prediction rules, reported separately:
  * block  - the check interrupts the agent (decision == "block")
  * alert  - any non-allow decision (warn or block)
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.models import ActivityMessage  # noqa: E402
from server.room import Room  # noqa: E402

try:  # Phase 3 wire model; absent on an older relay
    from server.models import PlanMessage  # noqa: E402
except ImportError:  # pragma: no cover
    PlanMessage = None  # type: ignore[assignment,misc]

SESSIONS = ROOT / "benchmarks" / "sessions"
BASE_TIME = 1_700_000_000.0


class _FakeSocket:
    async def send_json(self, _payload: Any) -> None:  # never triggered
        pass

    async def send_text(self, _payload: Any) -> None:
        pass


def _accepts(fn: Any) -> set[str]:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return set()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {"*"}
    return set(params)


def _filter(kwargs: dict, accepted: set[str]) -> dict:
    if "*" in accepted:
        return kwargs
    return {k: v for k, v in kwargs.items() if k in accepted}


def _decision_of(result: Any) -> tuple[str, str]:
    """Normalize check_edit's return value -> (decision, scope)."""
    if isinstance(result, tuple):
        decision = result[0]
        scope = ""
        for extra in result[2:]:
            if isinstance(extra, str):
                scope = extra
            elif hasattr(extra, "scope"):
                scope = getattr(extra, "scope") or ""
        if hasattr(decision, "decision"):
            scope = getattr(decision, "scope", scope) or scope
            decision = decision.decision
        return str(decision), scope
    if hasattr(result, "decision"):
        return str(result.decision), str(getattr(result, "scope", "") or "")
    return str(result), ""


def replay(session: dict, *, full: bool) -> list[dict]:
    room = Room()
    users = sorted({e["user"] for e in session["events"]})
    for user in users:
        room.add_member(user, _FakeSocket(), "claude-code")  # type: ignore[arg-type]
    # evaluate_edit also reports the scope; check_edit is the older API.
    check_fn = getattr(room, "evaluate_edit", None) or room.check_edit
    check_params = _accepts(check_fn)
    plan_fn = getattr(room, "record_plan", None)
    plan_params = _accepts(plan_fn) if plan_fn else set()
    intent_params = _accepts(room.record_intent)
    results = []
    for event in sorted(session["events"], key=lambda e: e["t"]):
        now = BASE_TIME + event["t"]
        user = event["user"]
        extra = {
            "symbols": event.get("symbols") or [],
            "line_ranges": event.get("line_ranges") or [],
            "branch": event.get("branch") or "",
        }
        if not full:
            extra = {"symbols": [], "line_ranges": [], "branch": ""}
        kind = event["kind"]
        if kind == "intent":
            room.record_intent(
                user,
                event["prompt"],
                now,
                "claude-code",
                **_filter({"repo_id": "", "branch": extra["branch"]}, intent_params),
            )
        elif kind == "plan":
            if plan_fn is None or not full:
                continue  # the baseline has no plan awareness
            fields = {
                "user_id": user,
                "summary": event.get("prompt", ""),
                "files": event.get("files", []),
                "symbols": event.get("symbols", []),
                "repo_id": "",
                "branch": extra["branch"],
            }
            if "message" in plan_params and PlanMessage is not None:
                plan_fn(PlanMessage.model_validate(fields), now, "claude-code")
            else:  # older/alternative signature: keyword args only
                plan_fn(**_filter({**fields, "agent": "claude-code", "timestamp": now,
                                   "now": now}, plan_params))
        elif kind == "post_edit":
            change: dict[str, Any] = {
                "file_path": event["file"],
                "diff": event.get("diff") or "+edit",
            }
            if full:
                change.update(symbols=extra["symbols"], line_ranges=extra["line_ranges"])
            payload: dict[str, Any] = {
                "user_id": user,
                "timestamp": now,
                "prompt": None,
                "changes": [change],
                "agent": "claude-code",
                "repo_id": "",
            }
            if full:
                payload["branch"] = extra["branch"]
            room.record_activity(ActivityMessage.model_validate(payload))
        elif kind == "pre_edit":
            kwargs = _filter({"repo_id": "", **extra}, check_params)
            decision, scope = _decision_of(
                check_fn(user, event["file"], now, **kwargs)
            )
            results.append(
                {
                    "label": event["label"],
                    "decision": decision,
                    "scope": scope,
                    "file": event["file"],
                    "user": user,
                }
            )
    return results


def metrics(rows: list[dict], rule: str) -> dict:
    def predicted(row: dict) -> bool:
        if rule == "block":
            return row["decision"] == "block"
        return row["decision"] != "allow"

    tp = sum(1 for r in rows if predicted(r) and r["label"] == "conflict")
    fp = sum(1 for r in rows if predicted(r) and r["label"] != "conflict")
    fn = sum(1 for r in rows if not predicted(r) and r["label"] == "conflict")
    tn = sum(1 for r in rows if not predicted(r) and r["label"] != "conflict")
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "fpr": fp / (fp + tn) if fp + tn else 0.0,
    }


def load_sessions() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(SESSIONS.glob("*.json"))]


def run(verbose: bool) -> dict:
    sessions = load_sessions()
    rows: dict[str, list[dict]] = {"baseline": [], "full": []}
    for session in sessions:
        for engine in rows:
            out = replay(session, full=engine == "full")
            for r in out:
                r["session"] = session["name"]
            rows[engine].extend(out)
    print(f"{len(sessions)} sessions, {len(rows['full'])} labelled checks\n")
    summary = {}
    for rule in ("block", "alert"):
        print(f"rule={rule}")
        print(f"  {'engine':9} {'prec':>6} {'recall':>6} {'FPR':>6} {'FP':>4} {'TP':>4}")
        for engine, data in rows.items():
            m = metrics(data, rule)
            summary[(rule, engine)] = m
            print(
                f"  {engine:9} {m['precision']:6.2f} {m['recall']:6.2f} "
                f"{m['fpr']:6.2f} {m['fp']:4} {m['tp']:4}"
            )
        b, f = summary[(rule, "baseline")]["fp"], summary[(rule, "full")]["fp"]
        drop = (1 - f / b) * 100 if b else 0.0
        print(f"  false positives: {b} -> {f} ({drop:.0f}% reduction)\n")
    if verbose:
        for b, f in zip(rows["baseline"], rows["full"]):
            print(
                f"{f['session']:22} {f['file']:34} {f['label']:12} "
                f"base={b['decision']:5} full={f['decision']:5} {f['scope']}"
            )
    return summary


# --- synthetic session generator ---------------------------------------------


def _ev(t: float, user: str, kind: str, file: str = "", **kw: Any) -> dict:
    event: dict[str, Any] = {"t": t, "user": user, "kind": kind}
    if file:
        event["file"] = file
    event.update(kw)
    return event


def generate() -> None:
    out: list[dict] = []

    def add(name: str, desc: str, events: list[dict]) -> None:
        out.append({"name": name, "description": desc, "events": events})

    files = [
        ("server/room.py", "Room.check_edit", (456, 492), "Room.broadcast", (520, 540)),
        ("server/main.py", "handle_edit_check", (496, 560), "handle_join", (300, 360)),
        ("synergo/daemon.py", "Daemon.reconnect", (120, 170), "Daemon.send_activity", (400, 450)),
        ("frontend/src/components/Lane.tsx", "Lane", (40, 200), "laneColor", (300, 320)),
        ("server/store.py", "Store.load", (20, 60), "Store.save", (90, 130)),
    ]
    for i, (f, s1, r1, s2, r2) in enumerate(files):
        add(
            f"same_function_{i}",
            "both agents edit the same function",
            [
                _ev(0, "alice", "post_edit", f, symbols=[s1], line_ranges=[list(r1)], branch="main"),
                _ev(6, "bob", "pre_edit", f, symbols=[s1],
                    line_ranges=[[r1[0] + 5, r1[0] + 15]], branch="main", label="conflict"),
                _ev(40, "bob", "pre_edit", f, symbols=[s1], line_ranges=[list(r1)],
                    branch="main", label="conflict"),
            ],
        )
        add(
            f"different_function_{i}",
            "same file, disjoint functions far apart",
            [
                _ev(0, "alice", "post_edit", f, symbols=[s1], line_ranges=[list(r1)], branch="main"),
                _ev(5, "bob", "pre_edit", f, symbols=[s2], line_ranges=[list(r2)],
                    branch="main", label="no_conflict"),
                _ev(30, "bob", "pre_edit", f, symbols=[s2], line_ranges=[list(r2)],
                    branch="main", label="no_conflict"),
            ],
        )
        add(
            f"other_branch_{i}",
            "same function but on different branches (merge conflict later, not now)",
            [
                _ev(0, "alice", "post_edit", f, symbols=[s1], line_ranges=[list(r1)], branch="main"),
                _ev(4, "bob", "pre_edit", f, symbols=[s1], line_ranges=[list(r1)],
                    branch=f"feature/x{i}", label="no_conflict"),
            ],
        )
    for i, (f, s1, r1) in enumerate(
        [("server/room.py", "Room.check_edit", (456, 470)),
         ("synergo/daemon.py", "Daemon.reconnect", (120, 130))]
    ):
        add(
            f"nearby_lines_{i}",
            "different symbols but adjacent line ranges",
            [
                _ev(0, "alice", "post_edit", f, symbols=[s1], line_ranges=[list(r1)], branch="main"),
                _ev(5, "bob", "pre_edit", f, symbols=[f"helper_{i}"],
                    line_ranges=[[r1[1] + 3, r1[1] + 12]], branch="main", label="conflict"),
            ],
        )
    for i, (fa, fb) in enumerate(
        [("server/room.py", "server/main.py"),
         ("frontend/src/App.tsx", "frontend/src/types.ts"),
         ("synergo/daemon.py", "synergo/protocol.py")]
    ):
        add(
            f"different_files_{i}",
            "different files entirely",
            [
                _ev(0, "alice", "post_edit", fa, symbols=["x"], line_ranges=[[1, 20]], branch="main"),
                _ev(3, "bob", "pre_edit", fb, symbols=["y"], line_ranges=[[1, 20]],
                    branch="main", label="no_conflict"),
            ],
        )
    for i, f in enumerate(["server/room.py", "synergo/daemon.py", "frontend/src/App.tsx"]):
        add(
            f"import_block_{i}",
            "both agents add imports at the top of the file",
            [
                _ev(0, "alice", "post_edit", f, symbols=["<imports>"], line_ranges=[[1, 12]], branch="main"),
                _ev(5, "bob", "pre_edit", f, symbols=["<imports>"], line_ranges=[[3, 8]],
                    branch="main", label="conflict"),
            ],
        )
    for i, f in enumerate(["pyproject.toml", "frontend/package.json", ".synergo.toml"]):
        add(
            f"config_file_{i}",
            "config file edited by both (always risky)",
            [
                _ev(0, "alice", "post_edit", f, symbols=[], line_ranges=[[10, 12]], branch="main"),
                _ev(8, "bob", "pre_edit", f, symbols=[], line_ranges=[[40, 42]],
                    branch="main", label="conflict"),
            ],
        )
    for i, (f, sym) in enumerate(
        [("server/room.py", "Room.check_edit"),
         ("server/models.py", "EditDecision"),
         ("frontend/src/components/Lane.tsx", "Lane")]
    ):
        add(
            f"plan_only_{i}",
            "alice planned the file but has not edited it yet",
            [
                _ev(0, "alice", "plan", prompt=f"Rework {sym}", files=[f], symbols=[sym], branch="main"),
                _ev(10, "bob", "pre_edit", f, symbols=[sym], line_ranges=[[1, 30]],
                    branch="main", label="conflict"),
            ],
        )
    add(
        "intent_mention_0",
        "alice declared an intent naming the module",
        [
            _ev(0, "alice", "intent", prompt="refactor the room broadcast flushing", branch="main"),
            _ev(5, "bob", "pre_edit", "server/room.py", symbols=["Room.broadcast"],
                line_ranges=[[493, 503]], branch="main", label="conflict"),
        ],
    )
    add(
        "stale_touch_0",
        "alice edited long ago (outside the warn window)",
        [
            _ev(0, "alice", "post_edit", "server/room.py", symbols=["Room.check_edit"],
                line_ranges=[[456, 492]], branch="main"),
            _ev(900, "bob", "pre_edit", "server/room.py", symbols=["Room.check_edit"],
                line_ranges=[[456, 492]], branch="main", label="no_conflict"),
        ],
    )
    for path in SESSIONS.glob("*.json"):
        path.unlink()
    for session in out:
        (SESSIONS / f"{session['name']}.json").write_text(json.dumps(session, indent=1) + "\n")
    print(f"wrote {len(out)} sessions to {SESSIONS}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay sessions against Room.check_edit")
    parser.add_argument("--generate", action="store_true", help="rewrite synthetic sessions")
    parser.add_argument("-v", "--verbose", action="store_true", help="per-check detail")
    args = parser.parse_args()
    SESSIONS.mkdir(parents=True, exist_ok=True)
    if args.generate:
        generate()
    run(args.verbose)
