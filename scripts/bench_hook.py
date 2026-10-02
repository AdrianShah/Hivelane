"""Measure the wall time of one `synergo hook claude-code` invocation.

Claude Code spawns the hook as a fresh process before and after every edit, so
its cold start is paid on every file write the agent makes. This runs the hook
N times as a subprocess with a representative PreToolUse payload against

  (a) no daemon listening (connection refused — the fail-open path), and
  (b) a stub HTTP server that answers every event with an allow ack,

and prints p50/p95 wall time for each, next to the cost of starting a bare
interpreter, which no hook can beat.

Usage:
    .venv/Scripts/python scripts/bench_hook.py            # 20 runs per scenario
    .venv/Scripts/python scripts/bench_hook.py -n 50
    .venv/Scripts/python scripts/bench_hook.py --command "synergo hook claude-code"
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

ALLOW_ACK = json.dumps({"decision": "allow", "reason": "", "file_path": None, "others": []}).encode()


class AllowHandler(BaseHTTPRequestHandler):
    """Answers POST /events with an allow ack, like an idle daemon would."""

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(ALLOW_ACK)))
        self.end_headers()
        self.wfile.write(ALLOW_ACK)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def default_command() -> list[str]:
    """The console script Claude Code actually runs, falling back to -m.

    The installed entry point imports synergo.cli the same way the hook does in
    production, so it is the honest thing to time.
    """
    scripts = Path(sys.executable).parent
    for name in ("synergo.exe", "synergo"):
        candidate = scripts / name
        if candidate.exists():
            return [str(candidate), "hook", "claude-code"]
    found = shutil.which("synergo")
    if found:
        return [found, "hook", "claude-code"]
    return [sys.executable, "-m", "synergo.cli", "hook", "claude-code"]


def payload(target: Path) -> bytes:
    return json.dumps(
        {
            "session_id": "bench",
            "transcript_path": str(target.parent / "transcript.jsonl"),
            "cwd": str(target.parent),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_use_id": "toolu_bench",
            "tool_input": {
                "file_path": str(target),
                "old_string": "def room():",
                "new_string": "def room(code: str):",
            },
        }
    ).encode()


def time_runs(command: list[str], stdin: bytes, env: dict[str, str], runs: int, warmup: int) -> list[float]:
    samples: list[float] = []
    for i in range(warmup + runs):
        start = time.perf_counter()
        result = subprocess.run(command, input=stdin, env=env, capture_output=True)
        elapsed = (time.perf_counter() - start) * 1000.0
        if result.returncode != 0:
            raise SystemExit(f"hook exited {result.returncode}: {result.stderr.decode(errors='replace')}")
        if i >= warmup:
            samples.append(elapsed)
    return samples


def percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank on the sorted list; good enough for a 20-sample latency check.
    rank = max(0, min(len(ordered) - 1, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[rank]


def report(label: str, samples: list[float]) -> None:
    print(
        f"{label:<28} n={len(samples):<3} "
        f"p50={percentile(samples, 50):7.1f} ms  "
        f"p95={percentile(samples, 95):7.1f} ms  "
        f"min={min(samples):7.1f} ms  "
        f"mean={statistics.fmean(samples):7.1f} ms"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--runs", type=int, default=20, help="timed runs per scenario (default 20)")
    parser.add_argument("--warmup", type=int, default=2, help="untimed runs first, to settle disk caches")
    parser.add_argument("--command", default=None, help="hook command line (default: the installed synergo script)")
    args = parser.parse_args(argv)

    command = shlex.split(args.command, posix=os.name != "nt") if args.command else default_command()
    print(f"command: {' '.join(command)}")

    with tempfile.TemporaryDirectory(prefix="synergo-bench-") as work:
        work_path = Path(work)
        target = work_path / "room.py"
        target.write_text("def room():\n    pass\n" * 50, encoding="utf-8")
        stdin = payload(target)

        env = dict(os.environ)
        # Keep the offline notice's rate-limit state out of the user's real home.
        env["SYNERGO_HOME"] = str(work_path / "home")

        # What no hook can beat on this machine: starting the interpreter at all.
        report("floor (python -c pass)", time_runs([sys.executable, "-c", "pass"], b"", env, args.runs, args.warmup))

        env["SYNERGO_PORT"] = str(unused_port())
        report("no daemon (refused)", time_runs(command, stdin, env, args.runs, args.warmup))

        server = ThreadingHTTPServer(("127.0.0.1", 0), AllowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env["SYNERGO_PORT"] = str(server.server_address[1])
            report("stub daemon (allow ack)", time_runs(command, stdin, env, args.runs, args.warmup))
        finally:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
