"""`hivelane githook`: git hooks for Hivelane.

- `pre-commit` warns when a teammate touched a staged file recently.
- `post-commit` attaches prompt-blame provenance (which agent, prompt and
  task wrote the commit's lines) as a git note (hivelane/provenance.py).
- `post-rewrite` carries those notes across `commit --amend` and `rebase`.

Install chains to any existing hook of each kind: the old hook is moved, byte
for byte, to `<hook>.hivelane-prev` and our script runs it afterwards (with
the same arguments, and for post-rewrite the same stdin). Uninstall moves it
back. The hooks directory honours core.hooksPath. The two post hooks can
never fail or hold a commit: their output is discarded, their exit status
ignored, and every git call they make is time-boxed.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from hivelane.mcp_server import DaemonClient, DaemonDown

MARKER = "# hivelane-githook v1"
PRE_COMMIT = "pre-commit"
POST_COMMIT = "post-commit"
POST_REWRITE = "post-rewrite"
HOOKS = (PRE_COMMIT, POST_COMMIT, POST_REWRITE)
PREV_SUFFIX = ".hivelane-prev"
PREV_NAME = PRE_COMMIT + PREV_SUFFIX


def prev_name(hook: str) -> str:
    return hook + PREV_SUFFIX


def hook_script(hook: str = PRE_COMMIT) -> str:
    import shlex

    # Single-quoted for sh: a path with $, ` or " in it stays a path.
    python = shlex.quote(sys.executable.replace("\\", "/"))
    header = f"""#!/bin/sh
{MARKER}: installed by `hivelane githook install`; remove with `hivelane githook uninstall`.
"""
    prev = f'prev="$(dirname "$0")/{prev_name(hook)}"\n'
    if hook == PRE_COMMIT:
        return header + f"""if command -v hivelane >/dev/null 2>&1; then
  hivelane githook run || exit $?
elif [ -x {python} ]; then
  {python} -c "import sys; from hivelane.cli import main; sys.exit(main(['githook', 'run']))" || exit $?
fi
{prev}if [ -f "$prev" ]; then
  exec "$prev" "$@"
fi
exit 0
"""
    if hook == POST_COMMIT:
        return header + f"""# Prompt blame: attach which agent wrote what as a git note. Fails open.
if command -v hivelane >/dev/null 2>&1; then
  hivelane githook post-commit >/dev/null 2>&1
elif [ -x {python} ]; then
  {python} -c "import sys; from hivelane.cli import main; sys.exit(main(['githook', 'post-commit']))" >/dev/null 2>&1
fi
{prev}if [ -f "$prev" ]; then
  exec "$prev" "$@"
fi
exit 0
"""
    if hook == POST_REWRITE:
        # stdin (the old -> new commit list) is read once and handed to both.
        return header + f"""# Prompt blame: carry notes across amend and rebase. Fails open.
input="$(cat)"
if command -v hivelane >/dev/null 2>&1; then
  printf '%s\\n' "$input" | hivelane githook post-rewrite "$1" >/dev/null 2>&1
elif [ -x {python} ]; then
  printf '%s\\n' "$input" | {python} -c "import sys; from hivelane.cli import main; sys.exit(main(['githook', 'post-rewrite'] + sys.argv[1:2]))" "$1" >/dev/null 2>&1
fi
{prev}if [ -f "$prev" ]; then
  if [ -n "$input" ]; then
    printf '%s\\n' "$input" | "$prev" "$@"
  else
    "$prev" "$@" </dev/null
  fi
  exit $?
fi
exit 0
"""
    raise ValueError(f"unknown hook {hook!r}")


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


def _install_one(d: Path, name: str) -> str:
    hook = d / name
    prev = d / prev_name(name)
    if hook.exists() and is_ours(hook):
        return f"already installed: {hook}"
    chained = ""
    if hook.exists():
        if prev.exists():
            raise RuntimeError(f"{prev} already exists; refusing to overwrite it")
        os.replace(hook, prev)
        chained = f" (chains to the previous hook, saved as {prev.name})"
    hook.write_bytes(hook_script(name).encode())
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"installed {hook}{chained}"


def install(root: Path, hooks: tuple[str, ...] = HOOKS) -> str:
    d = hooks_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    for name in hooks:
        # Checked up front so a refusal leaves nothing half-installed.
        hook, prev = d / name, d / prev_name(name)
        if hook.exists() and not is_ours(hook) and prev.exists():
            raise RuntimeError(f"{prev} already exists; refusing to overwrite it")
    return "\n".join(_install_one(d, name) for name in hooks)


def _uninstall_one(d: Path, name: str) -> Optional[str]:
    hook = d / name
    prev = d / prev_name(name)
    if not hook.exists() or not is_ours(hook):
        return None
    hook.unlink()
    if prev.exists():
        os.replace(prev, hook)
        return f"removed; restored the previous hook at {hook}"
    return f"removed {hook}"


def uninstall(root: Path) -> str:
    d = hooks_dir(root)
    done = [line for line in (_uninstall_one(d, name) for name in HOOKS) if line]
    return "\n".join(done) if done else "not installed"


def installed(root: Path) -> dict[str, bool]:
    """Which of Hivelane's hooks are in place."""
    try:
        d = hooks_dir(root)
    except (subprocess.CalledProcessError, OSError):
        return {name: False for name in HOOKS}
    return {name: is_ours(d / name) for name in HOOKS}


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
        say(f"hivelane: {rel}: {ack.get('reason') or 'a teammate touched it recently'}" + (f" [{who}]" if who else ""))
    if flagged and strict:
        say(f"hivelane: commit stopped (--strict): {flagged} file(s) touched by teammates")
        return 1
    return 0
