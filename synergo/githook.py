"""`synergo githook`: a pre-commit check that warns when a teammate touched a
staged file recently.

Install chains to any existing pre-commit hook: the old hook is moved, byte for
byte, to `pre-commit.synergo-prev` and our script runs it after the check.
Uninstall moves it back. The hooks directory honours core.hooksPath.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from synergo.mcp_server import DaemonClient, DaemonDown

MARKER = "# synergo-githook v1"
PREV_NAME = "pre-commit.synergo-prev"


def hook_script() -> str:
    python = sys.executable.replace("\\", "/")
    return f"""#!/bin/sh
{MARKER}: installed by `synergo githook install`; remove with `synergo githook uninstall`.
if command -v synergo >/dev/null 2>&1; then
  synergo githook run || exit $?
elif [ -x "{python}" ]; then
  "{python}" -c "import sys; from synergo.cli import main; sys.exit(main(['githook', 'run']))" || exit $?
fi
prev="$(dirname "$0")/{PREV_NAME}"
if [ -f "$prev" ]; then
  exec "$prev" "$@"
fi
exit 0
"""


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def hooks_dir(root: Path) -> Path:
    path = Path(_git(root, "rev-parse", "--git-path", "hooks"))
    return path if path.is_absolute() else (Path(root) / path)


def is_ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def install(root: Path) -> str:
    d = hooks_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    hook = d / "pre-commit"
    prev = d / PREV_NAME
    if hook.exists() and is_ours(hook):
        return f"already installed: {hook}"
    chained = ""
    if hook.exists():
        if prev.exists():
            raise RuntimeError(f"{prev} already exists; refusing to overwrite it")
        os.replace(hook, prev)
        chained = f" (chains to the previous hook, saved as {prev.name})"
    hook.write_bytes(hook_script().encode())
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"installed {hook}{chained}"


def uninstall(root: Path) -> str:
    d = hooks_dir(root)
    hook = d / "pre-commit"
    prev = d / PREV_NAME
    if not hook.exists() or not is_ours(hook):
        return "not installed"
    hook.unlink()
    if prev.exists():
        os.replace(prev, hook)
        return f"removed; restored the previous hook at {hook}"
    return f"removed {hook}"


def staged_files(root: Path) -> list[str]:
    out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRD")
    return [line for line in out.splitlines() if line]


def _same(a: object, b: Path) -> bool:
    try:
        return a is not None and Path(str(a)).resolve() == b.resolve()
    except OSError:
        return False


def run(root: Path, port: int, *, strict: bool = False, say: Callable[[str], None] = print,
        client: Optional[DaemonClient] = None) -> int:
    client = client or DaemonClient(port, timeout=2.0)
    try:
        status = client.status() or {}
    except DaemonDown:
        return 0
    repos = status.get("repos") if isinstance(status, dict) else None
    repos = repos or []
    entry = next((r for r in repos if _same(r.get("root"), root)), None)
    if entry is None and len(repos) == 1 and repos[0].get("root") is None:
        entry = repos[0]
    if entry is None or not entry.get("peers"):
        return 0
    try:
        files = staged_files(root)
    except (subprocess.CalledProcessError, OSError):
        return 0
    flagged = 0
    for rel in files:
        event = {
            "kind": "pre_edit",
            "agent": "unknown",
            "session_id": "githook",
            "cwd": str(root),
            "file_path": str(Path(root) / rel),
            "tool": "githook",
        }
        try:
            ack = client.post_event(event)
        except DaemonDown:
            return 0
        others = ack.get("others") or []
        if ack.get("decision", "allow") == "allow" and not others:
            continue
        flagged += 1
        who = ", ".join(f"{o.get('user_id', '?')} ({o.get('agent', '?')})" for o in others)
        say(f"synergo: {rel}: {ack.get('reason') or 'a teammate touched it recently'}" + (f" [{who}]" if who else ""))
    if flagged and strict:
        say(f"synergo: commit stopped (--strict): {flagged} file(s) touched by teammates")
        return 1
    return 0
