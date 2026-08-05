"""Synergo demo project — edit these files to light up agent lanes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


ROOM_CODE = "udaeuh"
PROJECT = "synergo"


def greet(name: str = "hacker") -> str:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return f"[{PROJECT}] hey {name} — live @ {stamp}Z"


def lane_status(agent: str = "hossein", files: int = 1) -> dict[str, Any]:
    """Conflict radar sketch — agents × overlapping files."""
    score = abs(len(agent)) * max(1, files) * 7
    return {
        "project": PROJECT,
        "agent": agent,
        "room": ROOM_CODE,
        "overlap_files": files,
        "conflict_score": score,
        "alert": "merge conflict inbound" if score >= 20 else "all clear",
        "ui": "dark-glassmorphism",
        "ok": True,
    }


def fib(n: int) -> list[int]:
    """Obvious numeric diff bait for the board."""
    n = max(2, min(n, 40))
    seq = [0, 1]
    while len(seq) < n:
        seq.append(seq[-1] + seq[-2])
    return seq


def glass_theme() -> dict[str, str]:
    """Palette tokens mirrored from the Synergo dashboard."""
    return {
        "bg": "#070b14",
        "panel": "rgba(18, 26, 40, 0.55)",
        "accent": "#5eead4",
        "lavender": "#a5b4fc",
        "conflict": "#fb7185",
    }


def shout(msg: str) -> str:
    return f"⚡ {msg.upper()} ⚡"


if __name__ == "__main__":
    print(greet("hackday"))
    print(lane_status("hossein", files=3))
    print("fib(12) =", fib(12))
    print("theme =", glass_theme())
    print(shout("synergo lanes online"))
