"""Best-effort desktop notifications from the daemon.

Used for things a person should see even when the agent is idle: a room
owner pinging this agent from the dashboard. Never raises and never blocks:
the notifier runs as a detached child process, and a machine without one
(a headless box, Windows without a toast tool) simply gets nothing beyond
the agent-chat note and the daemon log line.

HIVELANE_DESKTOP_NOTIFY=0 turns it off.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, Optional

ENV_SWITCH = "HIVELANE_DESKTOP_NOTIFY"
_OFF = {"0", "false", "no", "off"}
# Notification bodies are short on every platform; longer text is cut.
MAX_BODY_CHARS = 240

# The text is handed to AppleScript as an argument, never spliced into the
# script source, so nothing in it can be read as AppleScript.
_APPLESCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)

Spawner = Callable[[list[str]], None]


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "1").strip().lower() not in _OFF


def command_for(title: str, body: str, platform: Optional[str] = None) -> Optional[list[str]]:
    """The argv that shows (title, body) on this platform, or None."""
    platform = platform or sys.platform
    title = " ".join(title.split())[:80] or "Hivelane"
    body = " ".join(body.split())[:MAX_BODY_CHARS]
    if platform == "darwin":
        if shutil.which("osascript") is None:
            return None
        argv = ["osascript"]
        for line in _APPLESCRIPT:
            argv.extend(["-e", line])
        return [*argv, title, body]
    if platform.startswith("linux") or "bsd" in platform:
        if shutil.which("notify-send") is None:
            return None
        return ["notify-send", "--app-name=Hivelane", title, body]
    return None


def _spawn(argv: list[str]) -> None:
    subprocess.Popen(  # noqa: S603 - fixed argv, text passed as arguments
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def notify(title: str, body: str, *, spawn: Spawner = _spawn) -> bool:
    """Show a desktop notification; True when one was handed to the OS."""
    if not enabled():
        return False
    argv = command_for(title, body)
    if argv is None:
        return False
    try:
        spawn(argv)
    except (OSError, ValueError):
        return False
    return True
