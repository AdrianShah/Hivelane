"""Shared helpers for the Synergo demo tree.

Touch this file from a second agent to trigger an overlap conflict on the board.
"""

from __future__ import annotations


def overlap_score(agents: int, files: int) -> int:
    """Rough conflict radar used in demo captions."""
    return max(0, agents) * max(1, files) * 7


def watch_badge(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    return parts[-1] if parts else path


def format_diff_summary(added: int, removed: int) -> str:
    return f"+{added} / -{removed}"
