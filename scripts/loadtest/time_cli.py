"""Wall time of the user-facing CLI commands, each a fresh process.

Runs in a throwaway git repo with SYNERGO_HOME (and HOME/USERPROFILE) pointed
at a temp dir, against a relay started on a free port, so nothing touches the
real home, hooks or daemon. `synergo up` runs with --no-hooks -y and starts a
background daemon on a free port, which is shut down afterwards.

    python scripts/loadtest/time_cli.py [-n 5]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay_load import REPO, free_port, start_relay  # noqa: E402


def timed(cmd: list[str], cwd: Path, env: dict, runs: int) -> dict:
    samples, code, tail = [], 0, ""
    for _ in range(runs):
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
        samples.append((time.perf_counter() - t0) * 1000)
        code = proc.returncode
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    return {"cmd": " ".join(cmd[1:]) if cmd[0].endswith(("synergo", "synergo.exe")) else " ".join(cmd),
            "runs": runs, "median_ms": round(statistics.median(samples), 0), "min_ms": round(min(samples), 0),
            "max_ms": round(max(samples), 0), "exit": code, "last_line": tail[0][:120]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", "--runs", type=int, default=5)
    ap.add_argument("--synergo", default=shutil.which("synergo") or str(Path(sys.executable).parent / "synergo"))
    ap.add_argument("--out", default=str(REPO / "benchmarks" / "profile" / "cli_timings.json"))
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="synergo-cli-"))
    repo = work / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    home = work / "home"
    home.mkdir()
    relay_port, daemon_port = free_port(), free_port()
    relay = start_relay(relay_port, work / "relay.log", sys.executable)
    env = dict(os.environ, SYNERGO_HOME=str(home / ".synergo"), HOME=str(home), USERPROFILE=str(home),
               SYNERGO_PORT=str(daemon_port), SYNERGO_RELAY=f"ws://127.0.0.1:{relay_port}/ws",
               SYNERGO_NAME="bench", SYNERGO_DASHBOARD="http://127.0.0.1:5173")
    s = args.synergo
    rows = []
    try:
        rows.append(timed([sys.executable, "-c", "pass"], repo, env, args.runs))
        rows.append(timed([sys.executable, "-X", "importtime", "-c", "import synergo.cli"], repo, env, 1))
        rows.append(timed([s, "--help"], repo, env, args.runs))
        rows.append(timed([s, "up", "--no-hooks", "-y"], repo, env, 1))  # first: creates the room, starts the daemon
        rows[-1]["cmd"] = "up --no-hooks -y (first run: create room, spawn daemon)"
        rows.append(timed([s, "up", "--no-hooks", "-y"], repo, env, args.runs))
        rows[-1]["cmd"] = "up --no-hooks -y (re-run, daemon already up)"
        rows.append(timed([s, "status"], repo, env, args.runs))
        rows.append(timed([s, "doctor"], repo, env, args.runs))
    finally:
        subprocess.run([s, "down"], cwd=repo, env=env, capture_output=True, timeout=60)
        relay.kill()
        relay.wait()
    rows.pop(1)  # importtime run is only there to warm caches
    for r in rows:
        print(f"{r['cmd']:<58} median {r['median_ms']:>6.0f} ms  (min {r['min_ms']:.0f}, max {r['max_ms']:.0f}, exit {r['exit']})")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
