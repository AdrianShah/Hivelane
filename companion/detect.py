"""Best-effort detection of locally installed / running AI coding tools.

Every check degrades gracefully: an unknown install path reports
installed=False, and a psutil failure reports running=None ("unknown")
rather than raising.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, TypedDict

log = logging.getLogger("companion.detect")


class ToolInfo(TypedDict):
    id: str
    label: str
    installed: bool
    install_path: Optional[str]
    running: Optional[bool]


# agent id -> display label. ids match watcher --agent choices where they
# exist; antigravity is detection-only and reports as "unknown" to the relay.
TOOL_LABELS: dict[str, str] = {
    "cursor": "Cursor",
    "claude-code": "Claude Code",
    "codex": "Codex",
    "antigravity": "Antigravity",
}

# Per-OS install locations, checked in order. ~ is expanded.
INSTALL_PATHS: dict[str, dict[str, list[str]]] = {
    "darwin": {
        "cursor": [
            "/Applications/Cursor.app",
            "~/Applications/Cursor.app",
        ],
        "claude-code": [
            "~/.claude",
            "~/.local/bin/claude",
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
        ],
        "codex": [
            "~/.codex",
            "~/.local/bin/codex",
            "/usr/local/bin/codex",
            "/opt/homebrew/bin/codex",
        ],
        "antigravity": [
            "/Applications/Antigravity.app",
            "~/Applications/Antigravity.app",
        ],
    },
    "win32": {
        "cursor": [
            "~/AppData/Local/Programs/cursor/Cursor.exe",
            "C:/Program Files/Cursor/Cursor.exe",
        ],
        "claude-code": [
            "~/.claude",
            "~/AppData/Roaming/npm/claude.cmd",
        ],
        "codex": [
            "~/.codex",
            "~/AppData/Roaming/npm/codex.cmd",
        ],
        "antigravity": [
            "~/AppData/Local/Programs/Antigravity/Antigravity.exe",
            "C:/Program Files/Antigravity/Antigravity.exe",
        ],
    },
    "linux": {
        "cursor": [
            "~/.local/share/cursor",
            "/usr/share/cursor",
            "/opt/cursor",
        ],
        "claude-code": [
            "~/.claude",
            "~/.local/bin/claude",
            "/usr/local/bin/claude",
        ],
        "codex": [
            "~/.codex",
            "~/.local/bin/codex",
            "/usr/local/bin/codex",
        ],
        "antigravity": [
            "~/.local/share/antigravity",
            "/opt/antigravity",
        ],
    },
}

# Executables to look for on PATH as a secondary install signal.
PATH_EXECUTABLES: dict[str, list[str]] = {
    "cursor": ["cursor"],
    "claude-code": ["claude"],
    "codex": ["codex"],
    "antigravity": ["antigravity"],
}

# Lowercased substrings matched against running process names.
PROCESS_HINTS: dict[str, list[str]] = {
    "cursor": ["cursor"],
    "claude-code": ["claude"],
    "codex": ["codex"],
    "antigravity": ["antigravity"],
}


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _find_install_path(tool_id: str, platform_key: str) -> Optional[str]:
    for candidate in INSTALL_PATHS.get(platform_key, {}).get(tool_id, []):
        expanded = Path(os.path.expanduser(candidate))
        try:
            if expanded.exists():
                return str(expanded)
        except OSError:
            continue
    for executable in PATH_EXECUTABLES.get(tool_id, []):
        found = shutil.which(executable)
        if found:
            return found
    return None


def _running_process_names() -> Optional[list[str]]:
    """Lowercased names of running processes, or None if psutil is unusable."""
    try:
        import psutil  # imported lazily so detection works without psutil
    except Exception as exc:
        log.warning("psutil unavailable; process detection disabled: %s", exc)
        return None

    names: list[str] = []
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
            except Exception:
                continue
            if name:
                names.append(str(name).lower())
    except Exception as exc:
        log.warning("psutil process_iter failed: %s", exc)
        return None
    return names


def _is_running(tool_id: str, names: Optional[list[str]]) -> Optional[bool]:
    if names is None:
        return None
    hints = PROCESS_HINTS.get(tool_id, [])
    return any(hint in name for name in names for hint in hints)


def detect_tools() -> dict[str, object]:
    """Report install + running state for each known tool.

    Shape: {"platform": str, "tools": [ToolInfo, ...]}.
    """
    platform_key = _platform_key()
    names = _running_process_names()

    tools: list[ToolInfo] = []
    for tool_id, label in TOOL_LABELS.items():
        install_path = _find_install_path(tool_id, platform_key)
        running = _is_running(tool_id, names)
        tools.append(
            ToolInfo(
                id=tool_id,
                label=label,
                # A running process proves an install we failed to locate
                # (custom install directories are common).
                installed=install_path is not None or running is True,
                install_path=install_path,
                running=running,
            )
        )
    return {"platform": platform_key, "tools": tools}
