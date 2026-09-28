"""The `synergo` command line: run the daemon, serve agent hooks, manage hook
installation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import httpx

from synergo.adapters import claude_code
from synergo.daemon import DEFAULT_PORT, DEFAULT_SERVER, Daemon
from synergo.events import EventAck, NormalizedEvent
from synergo.protocol import AGENT_KINDS

HOOK_POST_TIMEOUT = 2.0
STATUS_TIMEOUT = 2.0


def resolve_port(explicit: Optional[int] = None) -> int:
    if explicit is not None:
        return explicit
    raw = os.environ.get("SYNERGO_PORT")
    if raw:
        return int(raw)
    return DEFAULT_PORT


def daemon_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


# --- join / daemon -----------------------------------------------------------


def cmd_join(args: argparse.Namespace) -> int:
    daemon = Daemon(
        room=args.room,
        name=args.name,
        agent=args.agent,
        server=args.server,
        port=resolve_port(args.port),
    )
    try:
        return asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\nsynergo: stopped")
        return 0


# --- hook entrypoint ---------------------------------------------------------


def post_event(event: NormalizedEvent, port: int) -> EventAck:
    response = httpx.post(
        daemon_url(port, "/events"),
        content=event.model_dump_json(),
        headers={"content-type": "application/json"},
        timeout=HOOK_POST_TIMEOUT,
    )
    response.raise_for_status()
    return EventAck.model_validate(response.json())


def cmd_hook_claude_code(args: argparse.Namespace) -> int:
    """Fails open unconditionally: any problem means no stdout and exit 0, so a
    dead or slow daemon can never stall or break the user's agent."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        event = claude_code.normalize(payload)
        if event is None:
            return 0
        ack = post_event(event, resolve_port(args.port))
        output = claude_code.render_response(event, ack)
        if output:
            sys.stdout.write(output)
        return 0
    except BaseException:
        return 0


# --- hook install / uninstall ------------------------------------------------


def settings_path(global_scope: bool) -> Path:
    if global_scope:
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def save_settings(path: Path, settings: dict[str, Any]) -> None:
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
    parser = argparse.ArgumentParser(prog="synergo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for verb in ("join", "daemon"):
        run = sub.add_parser(verb, help="run the Synergo daemon in the foreground")
        run.add_argument("room", help="room code, e.g. TEAM42")
        run.add_argument("--name", required=True, help="your display name in the room")
        run.add_argument("--server", default=DEFAULT_SERVER, help=f"relay ws base (default {DEFAULT_SERVER})")
        run.add_argument("--agent", default="claude-code", choices=AGENT_KINDS, help="agent you are driving")
        run.add_argument("--port", type=int, default=None, help=f"loopback port (default {DEFAULT_PORT})")
        run.set_defaults(func=cmd_join)

    hook = sub.add_parser("hook", help="agent hook entrypoint and installation")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)

    run_hook = hook_sub.add_parser("claude-code", help="handle one Claude Code hook invocation")
    run_hook.add_argument("--port", type=int, default=None)
    run_hook.set_defaults(func=cmd_hook_claude_code)

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
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
