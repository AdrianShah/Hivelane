"""The `synergo` command line: run the daemon, serve agent hooks, manage hook
installation."""

from __future__ import annotations

# Module-level imports here are paid by every `synergo hook claude-code`, which
# Claude Code spawns as a fresh process around every edit. Keep them to what the
# hook itself needs: the daemon stack (fastapi, uvicorn, websockets, pydantic,
# httpx) and even argparse/json/pathlib/typing are imported inside the functions
# that need them. scripts/bench_hook.py measures the result.
import os
import sys

from synergo import hook_fast
from synergo.adapters import claude_code
from synergo.hook_fast import DEFAULT_PORT

# Type checkers treat any name TYPE_CHECKING as True.
TYPE_CHECKING = False
if TYPE_CHECKING:
    import argparse
    from pathlib import Path
    from typing import Any, Optional, Sequence

STATUS_TIMEOUT = 2.0


class ConfigError(Exception):
    """Bad local configuration: report it, never guess around it."""


def resolve_port(explicit: Optional[int] = None) -> int:
    if explicit is not None:
        port = explicit
    else:
        raw = os.environ.get("SYNERGO_PORT")
        if not raw:
            return DEFAULT_PORT
        try:
            port = int(raw)
        except ValueError:
            raise ConfigError(f"SYNERGO_PORT is not a number: {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"port out of range: {port}")
    return port


def resolve_root(raw: Optional[str]) -> Optional[str]:
    """Check --root up front: a typo would otherwise key every path against a
    directory that does not exist, and no overlap would ever be detected."""
    if raw is None:
        return None
    if not raw.strip():
        raise ConfigError("--root is empty")
    expanded = os.path.expanduser(raw)
    if not os.path.exists(expanded):
        raise ConfigError(f"--root {raw!r} does not exist")
    if not os.path.isdir(expanded):
        raise ConfigError(f"--root {raw!r} is not a directory")
    return raw


def daemon_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


# --- join / daemon -----------------------------------------------------------


def cmd_join(args: argparse.Namespace) -> int:
    import asyncio

    from synergo.daemon import Daemon

    daemon = Daemon(
        room=args.room,
        name=args.name,
        agent=args.agent,
        server=args.server,
        port=resolve_port(args.port),
        root=resolve_root(args.root),
    )
    try:
        return asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\nsynergo: stopped")
        return 0


# --- hook entrypoint ---------------------------------------------------------


HOOK_HELP_FLAGS = ("-h", "--help")


def run_hook(port_arg: Optional[int]) -> int:
    """Fails open unconditionally: any problem means exit 0 and no decision, so
    a dead or slow daemon can never stall or break the user's agent. The work
    itself lives in synergo.hook_fast, which is stdlib-only."""
    try:
        port = resolve_port(port_arg)
    except ConfigError as exc:
        print(f"synergo: {exc} — proceeding without coordination", file=sys.stderr)
        return 0
    return hook_fast.run_claude_code(port)


def cmd_hook_claude_code(args: argparse.Namespace) -> int:
    return run_hook(args.port)


def parse_hook_args(argv: Sequence[str]) -> Optional[int]:
    """The hook's only flag, `--port N` / `--port=N`, parsed by hand.

    Raises ValueError on anything else. Not argparse: building a parser costs
    the hook ~20 ms of imports (argparse sizes its help formatter through
    shutil) on every edit, for one optional integer.
    """
    port: Optional[int] = None
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--port" and rest:
            port = int(rest.pop(0))
        elif arg.startswith("--port="):
            port = int(arg.partition("=")[2])
        else:
            raise ValueError(f"unexpected argument {arg!r}")
    return port


def run_hook_fast_path(argv: Sequence[str]) -> int:
    """`synergo hook claude-code [--port N]` without building the full parser,
    whose choices and defaults come from the pydantic-backed modules.

    A bad flag must still fail open: argparse would exit 2, and Claude Code
    reads exit 2 from a PreToolUse hook as "block this edit", so a typo in
    settings.json would stall every edit.
    """
    try:
        port_arg = parse_hook_args(argv)
    except ValueError as exc:
        print(f"synergo: {exc} — proceeding without coordination", file=sys.stderr)
        return 0
    return run_hook(port_arg)


# --- hook install / uninstall ------------------------------------------------


def settings_path(global_scope: bool) -> Path:
    from pathlib import Path

    if global_scope:
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def load_settings(path: Path) -> dict[str, Any]:
    """Parse an existing settings file, or raise.

    Never returns {} for content it failed to understand: the caller writes this
    file back, so a silent empty dict would replace the user's own settings.
    """
    import json

    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}) — fix it first") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} does not contain a JSON object — refusing to overwrite")
    return data


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _has_synergo(groups: list[Any]) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and claude_code.HOOK_MARKER in str(hook.get("command", "")):
                return True
    return False


def merge_hooks(settings: dict[str, Any], desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Add synergo's hook entries without disturbing anything already there."""
    hooks = settings.setdefault("hooks", {})
    changes: list[str] = []
    for event, groups in desired.items():
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = [] if existing is None else [existing]
            hooks[event] = existing
        if _has_synergo(existing):
            continue
        existing.extend(groups)
        changes.append(event)
    return changes


def remove_hooks(settings: dict[str, Any]) -> list[str]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    changes: list[str] = []
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups: list[Any] = []
        touched = False
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            entries = group.get("hooks") or []
            kept = [
                hook
                for hook in entries
                if not (
                    isinstance(hook, dict)
                    and claude_code.HOOK_MARKER in str(hook.get("command", ""))
                )
            ]
            if len(kept) != len(entries):
                touched = True
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if touched:
            changes.append(event)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return changes


def cmd_hook_install(args: argparse.Namespace) -> int:
    path = settings_path(args.global_scope)
    settings = load_settings(path)
    changes = merge_hooks(settings, claude_code.hook_settings())
    if not changes:
        print(f"synergo: hooks already installed in {path} — nothing to do")
        return 0
    save_settings(path, settings)
    print(f"synergo: added hooks to {path}")
    for event in changes:
        print(f"  + {event} -> {claude_code.HOOK_COMMAND}")
    return 0


def cmd_hook_uninstall(args: argparse.Namespace) -> int:
    path = settings_path(args.global_scope)
    settings = load_settings(path)
    changes = remove_hooks(settings)
    if not changes:
        print(f"synergo: no synergo hooks found in {path}")
        return 0
    save_settings(path, settings)
    print(f"synergo: removed hooks from {path}")
    for event in changes:
        print(f"  - {event}")
    return 0


# --- status ------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    import json

    import httpx

    port = resolve_port(args.port)
    try:
        response = httpx.get(daemon_url(port, "/status"), timeout=STATUS_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError:
        print(f"synergo: no daemon on 127.0.0.1:{port} — start one with `synergo join <ROOM> --name <NAME>`")
        return 1
    print(json.dumps(response.json(), indent=2))
    return 0


# --- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Deferred: both modules import pydantic, which only the hook path (routed
    # around this parser in main) has to avoid.
    import argparse

    from synergo.daemon import DEFAULT_SERVER
    from synergo.protocol import AGENT_KINDS

    parser = argparse.ArgumentParser(prog="synergo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for verb in ("join", "daemon"):
        run = sub.add_parser(verb, help="run the Synergo daemon in the foreground")
        run.add_argument("room", help="room code, e.g. TEAM42")
        run.add_argument("--name", required=True, help="your display name in the room")
        run.add_argument("--server", default=DEFAULT_SERVER, help=f"relay ws base (default {DEFAULT_SERVER})")
        run.add_argument("--agent", default="claude-code", choices=AGENT_KINDS, help="agent you are driving")
        run.add_argument("--port", type=int, default=None, help=f"loopback port (default {DEFAULT_PORT})")
        run.add_argument(
            "--root",
            default=None,
            metavar="PATH",
            help="project directory file paths are keyed against (default: the git repo "
            "around each agent's cwd); outside git, every teammate must pass one",
        )
        run.set_defaults(func=cmd_join)

    hook = sub.add_parser("hook", help="agent hook entrypoint and installation")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)

    # Normally reached only for --help: main() routes the hook around this parser.
    hook_cmd = hook_sub.add_parser("claude-code", help="handle one Claude Code hook invocation")
    hook_cmd.add_argument("--port", type=int, default=None)
    hook_cmd.set_defaults(func=cmd_hook_claude_code)

    install = hook_sub.add_parser("install", help="install hooks into a Claude Code settings.json")
    install.add_argument("target", choices=["claude-code"])
    install.add_argument("--global", dest="global_scope", action="store_true", help="use ~/.claude/settings.json")
    install.set_defaults(func=cmd_hook_install)

    uninstall = hook_sub.add_parser("uninstall", help="remove synergo hooks from a settings.json")
    uninstall.add_argument("target", choices=["claude-code"])
    uninstall.add_argument("--global", dest="global_scope", action="store_true")
    uninstall.set_defaults(func=cmd_hook_uninstall)

    status = sub.add_parser("status", help="show the running daemon's status")
    status.add_argument("--port", type=int, default=None)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["hook", "claude-code"] and not any(a in HOOK_HELP_FLAGS for a in argv[2:]):
        return run_hook_fast_path(argv[2:])
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"synergo: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
