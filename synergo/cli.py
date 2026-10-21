"""Synergo: live coordination for parallel AI coding agents.

`synergo up` in a git repo installs agent hooks, creates or joins the repo's
room, and connects it through a background daemon. `synergo doctor` explains
what is wrong when something is.
"""

from __future__ import annotations

# Module-level imports here are paid by every `synergo hook claude-code`, which
# Claude Code spawns as a fresh process around every edit. Keep them to what the
# hook itself needs: the daemon stack (fastapi, uvicorn, websockets, pydantic,
# httpx) and even argparse/json/pathlib/typing are imported inside the functions
# that need them. scripts/bench_hook.py measures the result.
import os
import sys

from synergo import hook_fast
from synergo.hook_fast import DEFAULT_PORT

# Type checkers treat any name TYPE_CHECKING as True.
TYPE_CHECKING = False
if TYPE_CHECKING:
    import argparse
    from pathlib import Path
    from types import ModuleType
    from typing import Any, Optional, Sequence

    from synergo.config import SynergoConfig

STATUS_TIMEOUT = 2.0
# Relay HTTP calls (room creation, doctor's probes).
HTTP_TIMEOUT = 10.0
DOCTOR_HTTP_TIMEOUT = 5.0
# How long `synergo up` waits for the repo to report connected (or fatal).
CONNECT_WAIT = 5.0
POLL_INTERVAL = 0.2
# Foreground mode: how long to wait for the in-process daemon to answer.
FOREGROUND_ATTACH_WAIT = 15.0
MAX_CLOCK_SKEW = 5.0

DEFAULT_DASHBOARD_LOCAL = "http://localhost:5173"
DEFAULT_DASHBOARD = "https://synergo.dev"
DASHBOARD_ENV = "SYNERGO_DASHBOARD"
INSTALL_COMMAND = "pipx install synergo"

# Mirrors server/sanitize.py, which is authoritative: a display name the relay
# keeps is lowercase [a-z0-9_-], 1..MAX_LENGTH long, with no blocked substring.
NAME_MAX_LENGTH = 20
NAME_BLOCKLIST = (
    "admin",
    "root",
    "system",
    "moderator",
    "fuck",
    "shit",
    "bitch",
    "cunt",
    "nigg",
    "rape",
)


class ConfigError(Exception):
    """Bad local configuration: report it, never guess around it."""


class CommandFailed(Exception):
    """A command ran but could not do its job (exit 1, not a usage error)."""


# --- console output ----------------------------------------------------------

MARKS = {"ok": "✔", "fail": "✖", "skip": "-", "info": "•"}
ASCII_MARKS = {"ok": "[ok]", "fail": "[x]", "skip": "[-]", "info": "*"}
# Applied only when the console cannot encode the original.
ASCII_FALLBACKS = {
    "—": "-",
    "–": "-",
    "…": "...",
    "→": "->",
    "✔": "[ok]",
    "✖": "[x]",
    "•": "*",
}


def _stream(err: bool) -> Any:
    return sys.stderr if err else sys.stdout


def can_encode(text: str, stream: Any = None) -> bool:
    encoding = getattr(stream if stream is not None else sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeError, LookupError):
        return False
    return True


def mark(kind: str, *, err: bool = False) -> str:
    glyph = MARKS[kind]
    return glyph if can_encode(glyph, _stream(err)) else ASCII_MARKS[kind]


def say(text: str = "", *, err: bool = False) -> None:
    """print() that never dies on a console that cannot encode the text
    (cp1252 Windows consoles, pipes with an ASCII locale)."""
    stream = _stream(err)
    if stream is None:
        return
    try:
        print(text, file=stream)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(stream, "encoding", None) or "ascii"
    for char, ascii_text in ASCII_FALLBACKS.items():
        text = text.replace(char, ascii_text)
    try:
        print(text.encode(encoding, "replace").decode(encoding, "replace"), file=stream)
    except (UnicodeError, LookupError):
        print(text.encode("ascii", "replace").decode("ascii"), file=stream)


def confirm(question: str, *, assume_yes: bool, default: bool = True) -> bool:
    """Ask on a terminal; anywhere else (CI, pipes) take the default."""
    if assume_yes:
        return True
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


# --- small resolvers ---------------------------------------------------------


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


def find_git_root(start: Optional[Path] = None) -> Optional[Path]:
    """Nearest ancestor holding `.git` (a file in worktrees, so test existence)."""
    from pathlib import Path

    try:
        current = Path(start).resolve() if start is not None else Path.cwd().resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def project_root(raw: Optional[str]) -> Path:
    """`--root`, else the git repo around the cwd. Outside git there is no
    root every teammate agrees on, so refuse rather than guess."""
    from pathlib import Path

    if raw is not None:
        return Path(os.path.expanduser(str(resolve_root(raw)))).resolve()
    root = find_git_root()
    if root is None:
        raise ConfigError(
            "not inside a git repository. Run this from your project's checkout, "
            "or pass --root PATH to use a plain directory (then every teammate "
            "must point --root at the same project)."
        )
    return root


def daemon_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def normalize_room(raw: str) -> str:
    import re

    code = raw.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,64}", code):
        raise ConfigError(f"{raw!r} is not a room code (letters and digits, e.g. K7Q2XM)")
    return code


def normalize_relay(raw: str) -> str:
    """A relay WebSocket base from what a person is likely to paste: ws(s)://
    or http(s)://, with or without the /ws path."""
    from urllib.parse import urlsplit, urlunsplit

    text = raw.strip()
    parts = urlsplit(text)
    scheme = {"ws": "ws", "wss": "wss", "http": "ws", "https": "wss"}.get(parts.scheme.lower())
    if "://" not in text or scheme is None or not parts.netloc:
        raise ConfigError(
            f"--relay {raw!r} is not a relay URL; use one like wss://relay.example.com/ws"
        )
    path = parts.path.rstrip("/") or "/ws"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def http_origin(relay: str) -> str:
    """`ws://host:8000/ws` -> `http://host:8000`: where the relay's HTTP API lives."""
    base = relay.strip().rstrip("/")
    if base.endswith("/ws"):
        base = base[: -len("/ws")]
    if base.startswith("wss://"):
        return "https://" + base[len("wss://") :]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://") :]
    return base


def relay_is_local(relay: str) -> bool:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(relay.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def dashboard_base(relay: str) -> str:
    override = os.environ.get(DASHBOARD_ENV, "").strip()
    if override:
        return override.rstrip("/")
    return DEFAULT_DASHBOARD_LOCAL if relay_is_local(relay) else DEFAULT_DASHBOARD


def room_url(relay: str, room: str) -> str:
    return f"{dashboard_base(relay)}/r/{room}"


def share_command(room: str, relay: str) -> str:
    from synergo.config import DEFAULT_RELAY

    command = f"{INSTALL_COMMAND} && synergo up --room {room}"
    if relay != DEFAULT_RELAY:
        command += f" --relay {relay}"
    return command


def same_path(a: Any, b: Any) -> bool:
    def norm(value: Any) -> str:
        try:
            return os.path.normcase(os.path.realpath(str(value)))
        except (OSError, ValueError):
            return os.path.normcase(str(value))

    return norm(a) == norm(b)


def display_path(path: Path, root: Path) -> str:
    try:
        relative = os.path.relpath(path, root)
    except ValueError:  # different drives on Windows
        return str(path)
    return str(path) if relative.startswith("..") else relative


# --- display names -------------------------------------------------------------


def normalize_name(raw: str) -> str:
    """`raw` rewritten into the form the relay keeps: ASCII-folded, lowercase,
    whitespace runs as `-`, anything outside [a-z0-9_-] dropped."""
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", "-", text.strip().lower())
    text = re.sub(r"[^a-z0-9_-]", "", text)
    return re.sub(r"-{2,}", "-", text).strip("-_")


def name_problem(name: str) -> Optional[str]:
    if not name:
        return "it has no letters or digits"
    if len(name) > NAME_MAX_LENGTH:
        return f"it is longer than {NAME_MAX_LENGTH} characters"
    if any(blocked in name for blocked in NAME_BLOCKLIST):
        return "the relay does not allow it"
    return None


def git_user_name(root: Path) -> Optional[str]:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def os_user_name() -> Optional[str]:
    import getpass

    try:
        return getpass.getuser() or None
    except Exception:  # getpass raises OSError, KeyError or ImportError by platform
        return None


def resolve_name(explicit: Optional[str], configured: Optional[str], root: Path) -> tuple[str, str]:
    """(display name, where it came from): --name > configured name (personal
    file, SYNERGO_NAME, project file) > git user.name > OS user name."""
    for raw, source in ((explicit, "--name"), (configured, "configured name")):
        if raw is None or not raw.strip():
            continue
        name = normalize_name(raw)
        problem = name_problem(name)
        if problem:
            raise ConfigError(
                f"{source} {raw!r} can't be used as a display name: {problem}. "
                f"Use 1-{NAME_MAX_LENGTH} letters, digits, '-' or '_'."
            )
        return name, source
    for raw, source in ((git_user_name(root), "git user.name"), (os_user_name(), "OS user name")):
        if not raw:
            continue
        name = normalize_name(raw)[:NAME_MAX_LENGTH].strip("-_")
        if name_problem(name) is None:
            return name, source
    raise ConfigError(
        f"could not work out a display name; pass --name NAME (1-{NAME_MAX_LENGTH} "
        "letters, digits, '-' or '_')"
    )


# --- seams: the background service and the relay's HTTP API --------------------


def load_service() -> ModuleType:
    import importlib

    return importlib.import_module("synergo.service")


class service_errors:
    """`with service_errors(svc, "doing X"):` turns svc.ServiceError into a
    CommandFailed with context, so every command reports it the same way."""

    def __init__(self, svc: Any, doing: str) -> None:
        self.svc = svc
        self.doing = doing

    def __enter__(self) -> service_errors:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        error_type = getattr(self.svc, "ServiceError", None)
        if exc is not None and error_type is not None and isinstance(exc, error_type):
            raise CommandFailed(f"{self.doing}: {exc}") from exc
        return False


def http_client(timeout: float = HTTP_TIMEOUT) -> Any:
    import httpx

    return httpx.Client(timeout=timeout, follow_redirects=True)


def _retry_hint(raw: Optional[str]) -> str:
    try:
        seconds = int(raw or "")
    except ValueError:
        return "Try again later"
    if seconds >= 120:
        return f"Try again in about {round(seconds / 60)} minutes"
    return f"Try again in {seconds} seconds"


def create_room(relay: str) -> str:
    """Ask the relay for a fresh room code (POST /rooms)."""
    import httpx

    origin = http_origin(relay)
    url = f"{origin}/rooms"
    try:
        with http_client() as client:
            response = client.post(url)
    except httpx.TimeoutException:
        raise ConfigError(
            f"the relay at {origin} did not answer within {HTTP_TIMEOUT:.0f}s. "
            "Check your network, or point at another relay with --relay URL."
        ) from None
    except httpx.HTTPError as exc:
        raise ConfigError(
            f"cannot reach the relay at {origin} ({exc.__class__.__name__}: {exc}). "
            "Is it running? Point at another one with --relay URL, or join an "
            "existing room with --room CODE."
        ) from None
    if response.status_code == 429:
        raise ConfigError(
            "the relay is rate-limiting room creation from your address. "
            f"{_retry_hint(response.headers.get('Retry-After'))}, or join a "
            "teammate's room with `synergo up --room CODE`."
        )
    if response.status_code == 503:
        raise ConfigError(
            f"the relay at {origin} is full and cannot create rooms right now. "
            "Try again later, join an existing room with --room CODE, or run "
            "your own relay and pass --relay URL."
        )
    if response.status_code >= 400:
        raise ConfigError(f"the relay at {origin} refused to create a room (HTTP {response.status_code})")
    try:
        room = response.json()["room_id"]
    except (ValueError, KeyError, TypeError):
        raise ConfigError(f"unexpected answer from {url}: no room_id in the response") from None
    if not isinstance(room, str):
        raise ConfigError(f"unexpected answer from {url}: room_id is not a string")
    return normalize_room(room)


# --- daemon status helpers -------------------------------------------------------


def repo_entries(status: Any) -> Optional[list[dict[str, Any]]]:
    """The multi-repo daemon's `repos`, or None for any other shape."""
    if not isinstance(status, dict):
        return None
    repos = status.get("repos")
    if not isinstance(repos, list) or not all(isinstance(r, dict) for r in repos):
        return None
    return repos


def is_single_room(status: Any) -> bool:
    """A `synergo join` daemon: today's says so; an older one has no `repos`."""
    if not isinstance(status, dict):
        return False
    if status.get("mode") == "single-room":
        return True
    return "repos" not in status and "room" in status and "connected" in status


def find_repo_entry(status: Any, root: Path) -> Optional[dict[str, Any]]:
    for entry in repo_entries(status) or []:
        if entry.get("root") and same_path(entry["root"], root):
            return entry
    return None


def wait_for_repo(svc: Any, port: int, root: Path, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Poll the daemon until this repo reports connected or fatal, or time runs
    out. Returns the last entry seen (None if it never appeared)."""
    import time

    deadline = time.monotonic() + (CONNECT_WAIT if timeout is None else timeout)
    entry: Optional[dict[str, Any]] = None
    while True:
        found = find_repo_entry(svc.status(port), root)
        if found is not None:
            entry = found
            if entry.get("connected") or entry.get("fatal"):
                return entry
        if time.monotonic() >= deadline:
            return entry
        time.sleep(POLL_INTERVAL)


def _peers_text(peers: Any) -> str:
    if not isinstance(peers, list) or not peers:
        return "no teammates online yet"
    names = ", ".join(str(p) for p in peers)
    return f"{len(peers)} teammate{'s' if len(peers) != 1 else ''} online: {names}"


def report_connection(entry: Optional[dict[str, Any]], name: str, log_hint: str) -> bool:
    """Print how the repo's relay connection looks. False means fatal."""
    if entry is None:
        say(f"  {mark('info')} the daemon has not reported this repo yet")
        say(f"      check `synergo status`; logs: {log_hint}")
        return True
    if entry.get("fatal"):
        say(f"  {mark('fail')} the relay refused this repo: {entry['fatal']}")
        say(f"      logs: {log_hint}")
        return False
    if entry.get("connected"):
        say(f"  {mark('ok')} connected as {entry.get('name') or name} ({_peers_text(entry.get('peers'))})")
        return True
    say(f"  {mark('info')} still connecting to the relay; the daemon keeps retrying in the background")
    say("      check progress with `synergo status`")
    return True


# --- join / daemon (single room, foreground) ---------------------------------


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


def run_hook(port_arg: Optional[int], agent: str = "claude-code") -> int:
    """Fails open unconditionally: any problem means exit 0 and no decision, so
    a dead or slow daemon can never stall or break the user's agent. The work
    itself lives in synergo.hook_fast, which is stdlib-only."""
    try:
        port = resolve_port(port_arg)
    except ConfigError as exc:
        print(f"synergo: {exc} — proceeding without coordination", file=sys.stderr)
        return 0
    return hook_fast.run_agent(agent, port)


def cmd_hook_agent(args: argparse.Namespace) -> int:
    return run_hook(args.port, args.hook_command)


cmd_hook_claude_code = cmd_hook_agent


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


def run_hook_fast_path(argv: Sequence[str], agent: str = "claude-code") -> int:
    """`synergo hook <agent> [--port N]` without building the full parser,
    whose choices and defaults come from the pydantic-backed modules.

    A bad flag must still fail open: argparse would exit 2, and Claude Code
    (and Codex) read exit 2 from a PreToolUse hook as "block this edit", so a
    typo in a hooks config would stall every edit.
    """
    try:
        port_arg = parse_hook_args(argv)
    except ValueError as exc:
        print(f"synergo: {exc} — proceeding without coordination", file=sys.stderr)
        return 0
    return run_hook(port_arg, agent)


# --- hook install / uninstall ------------------------------------------------
# The JSON merge logic lives in synergo.adapters.base, shared by every adapter
# whose hooks config is Claude-Code-shaped; these wrappers keep cli's
# ConfigError contract.


def settings_path(global_scope: bool, agent: str = "claude-code") -> Path:
    from pathlib import Path

    from synergo import adapters

    adapter = adapters.load(agent)
    if global_scope:
        return adapter.settings_path("user", None)
    return adapter.settings_path("project", Path.cwd())


def load_settings(path: Path) -> dict[str, Any]:
    """Parse an existing settings file, or raise ConfigError (never {} for
    content it failed to understand: the caller writes this file back)."""
    from synergo.adapters import base

    try:
        return base.load_settings(path)
    except base.SettingsError as exc:
        raise ConfigError(str(exc)) from None


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    from synergo.adapters import base

    base.save_settings(path, settings)


def has_synergo_hooks(settings: dict[str, Any], events: Sequence[str]) -> bool:
    """Every one of `events` carries a synergo hook entry."""
    from synergo.adapters import base

    return base.has_synergo_hooks(settings, events)


def merge_hooks(settings: dict[str, Any], desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Add synergo's hook entries without disturbing anything already there."""
    from synergo.adapters import base

    try:
        return base.merge_hooks(settings, desired)
    except base.SettingsError as exc:
        raise ConfigError(str(exc)) from None


def remove_hooks(settings: dict[str, Any]) -> list[str]:
    from synergo.adapters import base

    return base.remove_hooks(settings)


def cmd_hook_install(args: argparse.Namespace) -> int:
    from synergo import adapters

    adapter = adapters.load(args.target)
    from pathlib import Path

    from synergo.adapters import base

    path = settings_path(args.global_scope, args.target)
    scope, root = ("user", None) if args.global_scope else ("project", Path.cwd())
    try:
        # Through the adapter: not every agent's file has Claude Code's shape.
        changes = adapter.install(scope, root)
    except base.SettingsError as exc:
        raise ConfigError(str(exc)) from None
    if not changes:
        print(f"synergo: hooks already installed in {path} — nothing to do")
        return 0
    print(f"synergo: added hooks to {path}")
    for event in changes:
        print(f"  + {event} -> {adapter.hook_command}")
    if args.target == "codex":
        print("  Codex asks you to review and trust new hooks (`/hooks`) before they run.")
    return 0


def cmd_hook_uninstall(args: argparse.Namespace) -> int:
    from pathlib import Path

    from synergo import adapters
    from synergo.adapters import base

    path = settings_path(args.global_scope, args.target)
    scope, root = ("user", None) if args.global_scope else ("project", Path.cwd())
    try:
        changes = adapters.load(args.target).uninstall(scope, root)
    except base.SettingsError as exc:
        raise ConfigError(str(exc)) from None
    if not changes:
        print(f"synergo: no synergo hooks found in {path}")
        return 0
    print(f"synergo: removed hooks from {path}")
    for event in changes:
        print(f"  - {event}")
    return 0


# --- up ----------------------------------------------------------------------


def setup_hooks(root: Path, *, skip: bool, assume_yes: bool) -> None:
    """Install hooks for every detected agent Synergo has an adapter for, plus
    Claude Code (the primary adapter) even when it was not detected."""
    from synergo import detect

    found = detect.detect_agents()
    targets: list[tuple[Any, str]] = [(a.spec, a.evidence) for a in found if a.has_adapter]
    if not any(spec.id == detect.PRIMARY_AGENT for spec, _ in targets):
        primary = detect.get_agent(detect.PRIMARY_AGENT)
        if skip:
            targets.append((primary, "not detected"))  # reported as skipped below
        elif confirm(
            f"{primary.label} was not found on this machine. Install its hooks anyway?",
            assume_yes=assume_yes,
        ):
            targets.append((primary, "not detected"))
        else:
            say(f"  {mark('skip')} {primary.label}: hooks not installed")

    scope = detect.HookScope.project(root)
    for spec, evidence in targets:
        if skip:
            say(f"  {mark('skip')} {spec.label} ({evidence}): hooks not installed (--no-hooks)")
            continue
        result = spec.install(scope)
        where = display_path(result.path, root)
        if result.changed:
            say(f"  {mark('ok')} {spec.label} ({evidence}): added hooks to {where} ({', '.join(result.added)})")
        else:
            say(f"  {mark('ok')} {spec.label} ({evidence}): hooks already in {where}")
    for agent in found:
        if not agent.has_adapter:
            hint = agent.spec.note or "adapter coming soon"
            say(f"  {mark('info')} {agent.label} ({agent.evidence}): detected, {hint}")


def project_file_values(root: Path) -> dict[str, Any]:
    """The raw contents of `<root>/.synergo.toml` ({} when absent)."""
    import tomllib

    from synergo.config import PROJECT_FILE

    path = root / PROJECT_FILE
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML ({exc})") from None


def cmd_up(args: argparse.Namespace) -> int:
    from synergo import config as cfg

    root = project_root(args.root)
    port = resolve_port(args.port)
    relay_flag = normalize_relay(args.relay) if args.relay else None
    room_flag = normalize_room(args.room) if args.room else None
    try:
        config = cfg.load_config(root, relay=relay_flag)
    except cfg.ConfigFileError as exc:
        raise ConfigError(str(exc)) from None
    name, name_source = resolve_name(args.name, config.name, root)
    svc = load_service()

    say(f"synergo: setting up {root}")

    # 1. agent hooks
    setup_hooks(root, skip=args.no_hooks, assume_yes=args.yes)

    # 2. the room: --room > .synergo.toml > a new one from the relay
    existing = config.room.upper() if config.room else None
    if room_flag is not None:
        room = room_flag
        if existing and existing != room and not confirm(
            f"{cfg.PROJECT_FILE} says room {existing}. Switch this repo to {room}?",
            assume_yes=args.yes,
        ):
            raise CommandFailed(f"kept room {existing}; nothing else changed")
        say(f"  {mark('ok')} joining room {room}" + (f" (was {existing})" if existing and existing != room else ""))
    elif existing:
        room = existing
        say(f"  {mark('ok')} joining room {room} from {cfg.PROJECT_FILE}")
    else:
        room = create_room(config.relay)
        say(f"  {mark('ok')} created room {room} on {config.relay}")

    on_file = project_file_values(root)
    updates: dict[str, Any] = {}
    if str(on_file.get("room", "")).upper() != room:
        updates["room"] = room
    # The relay is recorded when asked for, or when a new room lives on a
    # non-default relay: teammates can only join it there.
    if relay_flag is not None or ("room" in updates and config.relay != cfg.DEFAULT_RELAY):
        if on_file.get("relay") != config.relay:
            updates["relay"] = config.relay
    if updates:
        try:
            path = cfg.write_project_config(root, **updates)
        except cfg.ConfigFileError as exc:
            raise ConfigError(str(exc)) from None
        say(
            f"  {mark('ok')} wrote {', '.join(updates)} to {display_path(path, root)} "
            "(commit it so teammates join the same room)"
        )

    # An explicit --name is remembered, or the next plain `synergo up` would
    # quietly rename you back to your git identity.
    if args.name is not None and normalize_name(config.name or "") != name:
        try:
            path = cfg.write_personal_config(name=name)
        except cfg.ConfigFileError as exc:
            raise ConfigError(str(exc)) from None
        say(f"  {mark('ok')} saved name {name} to {path} (used in every repo)")

    try:
        config = cfg.load_config(root, relay=relay_flag, room=room, name=name)
    except cfg.ConfigFileError as exc:
        raise ConfigError(str(exc)) from None

    # 3. the daemon
    if args.foreground:
        print_summary(config, name_source)
        return run_foreground(svc, port, root, config)

    if is_single_room(svc.status(port)):
        raise CommandFailed(
            f"port {port} is held by a single-room `synergo join` daemon, which would "
            "shadow this repo; stop it (Ctrl+C in its terminal) and run `synergo up` again"
        )
    with service_errors(svc, "could not start the background daemon"):
        pid = svc.start_background(port)
    say(f"  {mark('ok')} daemon running on port {port} (pid {pid})")
    with service_errors(svc, f"could not connect {root} to the daemon"):
        svc.connect_repo(port, root, config)
    entry = wait_for_repo(svc, port, root)
    ok = report_connection(entry, name, str(svc.log_path()))
    if not ok:
        return 1
    print_summary(config, name_source)
    return 0


def print_summary(config: SynergoConfig, name_source: str) -> None:
    from synergo.config import DEFAULT_RELAY

    room = config.room or ""
    say()
    say(f"  room       {room}")
    say(f"  relay      {config.relay}" + ("" if config.relay != DEFAULT_RELAY else " (default)"))
    say(f"  name       {config.name}  (from {name_source})")
    say(f"  dashboard  {room_url(config.relay, room)}")
    say()
    say("Share with teammates:")
    say(f"  {share_command(room, config.relay)}")


def run_foreground(svc: Any, port: int, root: Path, config: SynergoConfig) -> int:
    """Serve in this process; register the repo from a helper thread once the
    daemon answers, since serve() only returns on shutdown."""
    import threading
    import time

    if svc.is_running(port):
        raise ConfigError(
            f"a Synergo daemon is already running on port {port}; stop it with "
            "`synergo down`, or drop --foreground"
        )

    def attach() -> None:
        deadline = time.monotonic() + FOREGROUND_ATTACH_WAIT
        while not svc.is_running(port):
            if time.monotonic() >= deadline:
                say(f"synergo: the daemon did not come up on port {port}", err=True)
                return
            time.sleep(POLL_INTERVAL)
        try:
            svc.connect_repo(port, root, config)
        except Exception as exc:  # reported, never fatal to the serving thread
            say(f"synergo: could not connect {root}: {exc}", err=True)
            return
        report_connection(wait_for_repo(svc, port, root), config.name or "", str(svc.log_path()))

    say()
    say(f"synergo: running the daemon in the foreground on port {port} (Ctrl+C to stop)")
    threading.Thread(target=attach, name="synergo-attach", daemon=True).start()
    try:
        with service_errors(svc, "the daemon failed"):
            return int(svc.serve(port) or 0)
    except KeyboardInterrupt:
        say("\nsynergo: stopped")
        return 0


# --- down / leave / open / serve / service ---------------------------------------


def cmd_down(args: argparse.Namespace) -> int:
    port = resolve_port(args.port)
    svc = load_service()
    was_running = svc.is_running(port)
    # Called even when nothing answers: stop() also clears a hung daemon that
    # still holds the port.
    with service_errors(svc, "could not stop the daemon"):
        stopped = svc.stop(port)
    if not stopped:
        raise CommandFailed(f"the daemon on port {port} did not stop in time")
    if was_running:
        say("synergo: daemon stopped. Hooks stay installed; agents run uncoordinated until `synergo up`.")
    else:
        say(f"synergo: no daemon running on port {port}")
    return 0


def cmd_leave(args: argparse.Namespace) -> int:
    from synergo import config as cfg

    root = project_root(args.root)
    port = resolve_port(args.port)
    svc = load_service()
    if svc.is_running(port):
        with service_errors(svc, "could not disconnect this repo"):
            removed = svc.disconnect_repo(port, root)
        if removed:
            say(f"synergo: disconnected {root} from its room")
        else:
            say(f"synergo: {root} was not connected")
    else:
        say(f"synergo: no daemon running on port {port}; nothing to disconnect")

    if args.forget:
        project = cfg.find_project_config(root)
        if project is None:
            say(f"synergo: no {cfg.PROJECT_FILE} to forget")
            return 0
        try:
            forgotten = cfg.remove_project_keys(project.parent, "room")
        except cfg.ConfigFileError as exc:
            raise ConfigError(str(exc)) from None
        if forgotten:
            say(
                f"synergo: removed the room from {project}; the next `synergo up` "
                "creates a new one (commit the change to move the whole team)"
            )
        else:
            say(f"synergo: {project} has no room set")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    import webbrowser

    from synergo import config as cfg

    try:
        start = project_root(args.root)
    except ConfigError:
        if args.root is not None:
            raise
        start = None
    try:
        config = cfg.load_config(start)
    except cfg.ConfigFileError as exc:
        raise ConfigError(str(exc)) from None
    if not config.room:
        raise ConfigError(f"no room configured here (no {cfg.PROJECT_FILE} with a room); run `synergo up` first")
    url = room_url(config.relay, config.room)
    say(f"synergo: opening {url}")
    if not webbrowser.open(url):
        say("synergo: could not open a browser; visit the URL above")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    port = resolve_port(args.port)
    svc = load_service()
    try:
        with service_errors(svc, "the daemon failed"):
            return int(svc.serve(port) or 0)
    except KeyboardInterrupt:
        say("\nsynergo: stopped")
        return 0


def cmd_service_install(args: argparse.Namespace) -> int:
    port = resolve_port(args.port)
    svc = load_service()
    with service_errors(svc, "could not install autostart"):
        message = svc.install_autostart(port)
    say(f"synergo: {message}")
    return 0


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    svc = load_service()
    with service_errors(svc, "could not remove autostart"):
        message = svc.uninstall_autostart()
    say(f"synergo: {message}")
    return 0


# --- status ------------------------------------------------------------------


def _ago(timestamp: Any) -> Optional[str]:
    import time

    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        return None
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m ago"


def format_status(status: Any, port: int) -> Optional[list[str]]:
    """Human lines for a daemon status payload, or None for unknown shapes."""
    repos = repo_entries(status)
    if repos is not None:
        version = status.get("version")
        header = "Synergo daemon" + (f" {version}" if version else "")
        mode = ", single room (`synergo join`)" if status.get("mode") == "single-room" else ""
        header += f" (pid {status.get('pid', '?')}, port {status.get('port', port)}{mode})"
        lines = [header]
        if not repos:
            lines.append("  no repos connected; run `synergo up` in a repo")
        for repo in repos:
            lines.extend(_format_repo(repo))
        return lines
    if isinstance(status, dict) and "room" in status and "connected" in status:
        # The single-room `synergo join` daemon.
        lines = [f"Synergo daemon (single room, port {status.get('port', port)})"]
        lines.extend(_format_repo({**status, "relay": status.get("server")}))
        return lines
    return None


def _format_repo(repo: dict[str, Any]) -> list[str]:
    if repo.get("fatal"):
        state, kind = f"stopped: {repo['fatal']}", "fail"
    elif repo.get("connected"):
        since = _ago(repo.get("connected_since"))
        state, kind = "connected" + (f" since {since}" if since else ""), "ok"
    else:
        state, kind = "connecting to the relay", "info"
    lines = [f"  {mark(kind)} {repo.get('root') or '(no root)'}"]
    lines.append(
        f"      room {repo.get('room', '?')} on {repo.get('relay', '?')} as "
        f"{repo.get('name', '?')} ({repo.get('agent', '?')})"
    )
    details = [state, _peers_text(repo.get("peers"))]
    queued = repo.get("queued_events")
    if isinstance(queued, int) and queued:
        details.append(f"{queued} queued event{'s' if queued != 1 else ''}")
    contact = _ago(repo.get("last_relay_contact"))
    if contact:
        details.append(f"last relay contact {contact}")
    lines.append("      " + "; ".join(details))
    return lines


def cmd_status(args: argparse.Namespace) -> int:
    import json

    port = resolve_port(args.port)
    svc = load_service()
    status = svc.status(port, timeout=STATUS_TIMEOUT)
    if status is None:
        say(f"synergo: no daemon on 127.0.0.1:{port}; start one with `synergo up`")
        return 1
    lines = format_status(status, port)
    if lines is None:
        say(json.dumps(status, indent=2))
    else:
        for line in lines:
            say(line)
    return 0


# --- doctor ------------------------------------------------------------------


class Check:
    def __init__(self, label: str, status: str, detail: str = "", fix: str = "") -> None:
        self.label = label
        self.status = status  # "ok" | "fail" | "skip"
        self.detail = detail
        self.fix = fix

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _relay_checks(relay: str) -> list[Check]:
    import time
    from email.utils import parsedate_to_datetime

    import httpx

    from synergo.protocol import PROTOCOL_VERSION

    origin = http_origin(relay)
    checks: list[Check] = []
    relay_fix = "start the relay, or fix the relay URL (--relay, .synergo.toml, SYNERGO_RELAY)"
    try:
        with http_client(DOCTOR_HTTP_TIMEOUT) as client:
            sent = time.time()
            health = client.get(f"{origin}/health")
            received = time.time()
            readyz = client.get(f"{origin}/readyz") if health.status_code == 200 else None
    except httpx.HTTPError as exc:
        checks.append(Check("Relay reachable", "fail", f"{origin}: {exc.__class__.__name__}: {exc}", relay_fix))
        checks.append(Check("Relay protocol compatible", "skip", "relay unreachable"))
        checks.append(Check("Clock in sync with the relay", "skip", "relay unreachable"))
        return checks

    if health.status_code != 200:
        checks.append(Check("Relay reachable", "fail", f"{origin}/health answered HTTP {health.status_code}", relay_fix))
        checks.append(Check("Relay protocol compatible", "skip", "relay unhealthy"))
    else:
        checks.append(Check("Relay reachable", "ok", origin))
        checks.append(_protocol_check(readyz, PROTOCOL_VERSION))

    date = health.headers.get("date")
    try:
        relay_time = parsedate_to_datetime(date).timestamp() if date else None
    except (TypeError, ValueError):
        relay_time = None
    if relay_time is None:
        checks.append(Check("Clock in sync with the relay", "skip", "relay sent no Date header"))
    else:
        # The Date header has one-second resolution; compare with the midpoint.
        skew = (sent + received) / 2 - relay_time
        if abs(skew) > MAX_CLOCK_SKEW:
            ahead = "ahead of" if skew > 0 else "behind"
            checks.append(
                Check(
                    "Clock in sync with the relay",
                    "fail",
                    f"your clock is {abs(skew):.0f}s {ahead} the relay's; conflict windows use timestamps",
                    "sync your system clock (Windows: Settings > Time & language > Sync now; "
                    "macOS/Linux: enable NTP)",
                )
            )
        else:
            checks.append(Check("Clock in sync with the relay", "ok", f"within {MAX_CLOCK_SKEW:.0f}s ({skew:+.1f}s)"))
    return checks


def _protocol_check(readyz: Any, ours: int) -> Check:
    label = "Relay protocol compatible"
    upgrade = "upgrade synergo: `pipx upgrade synergo` (or `pip install -U synergo`)"
    if readyz is None or readyz.status_code != 200:
        code = "no answer" if readyz is None else f"HTTP {readyz.status_code}"
        return Check(label, "fail", f"/readyz: {code}", "the relay may predate protocol versioning; upgrade it")
    try:
        data = readyz.json()
        theirs = data["protocol_version"]
        minimum = data["min_supported_protocol"]
    except (ValueError, KeyError, TypeError):
        return Check(label, "fail", "relay did not report a protocol version", "upgrade the relay")
    if not isinstance(theirs, int) or not isinstance(minimum, int):
        return Check(label, "fail", "relay reported a malformed protocol version", "upgrade the relay")
    if ours < minimum:
        return Check(label, "fail", f"relay needs protocol >= {minimum}; this synergo speaks {ours}", upgrade)
    detail = f"protocol {ours} (relay speaks {theirs}, accepts >= {minimum})"
    if theirs > ours:
        detail += "; a newer synergo is available"
    return Check(label, "ok", detail)


def run_doctor_checks(root_arg: Optional[str], port: int) -> list[Check]:
    import platform

    from synergo import config as cfg
    from synergo import detect

    checks: list[Check] = []
    version_ok = sys.version_info >= (3, 12)
    checks.append(
        Check(
            "Python >= 3.12",
            "ok" if version_ok else "fail",
            f"Python {platform.python_version()}",
            "install Python 3.12+ and reinstall synergo (`pipx install --python python3.12 synergo`)",
        )
    )

    root: Optional[Path] = None
    try:
        root = project_root(root_arg)
        checks.append(Check("Git repository", "ok", str(root)))
    except ConfigError as exc:
        checks.append(Check("Git repository", "fail", str(exc), "cd into your project's git checkout, or pass --root PATH"))

    config: Optional[SynergoConfig] = None
    project = cfg.find_project_config(root) if root is not None else cfg.find_project_config()
    if project is None:
        checks.append(
            Check(f"{cfg.PROJECT_FILE} found", "fail", "no project config", "run `synergo up` (or pull it from your team's repo)")
        )
    else:
        try:
            config = cfg.load_config(project.parent)
        except cfg.ConfigFileError as exc:
            checks.append(Check(f"{cfg.PROJECT_FILE} valid", "fail", str(exc), f"fix {project}, then run `synergo up`"))
        else:
            if config.room:
                checks.append(Check(f"{cfg.PROJECT_FILE} valid", "ok", f"{project} (room {config.room})"))
            else:
                checks.append(Check(f"{cfg.PROJECT_FILE} valid", "fail", f"{project} sets no room", "run `synergo up` to create one"))

    claude = detect.get_agent(detect.PRIMARY_AGENT)
    scopes = ([detect.HookScope.project(root)] if root is not None else []) + [detect.HookScope.user()]
    where = [claude.adapter.settings_path(s) for s in scopes if claude.is_installed(s)] if claude.adapter else []
    if where:
        checks.append(Check("Agent hooks installed", "ok", ", ".join(str(p) for p in where)))
    else:
        checks.append(
            Check(
                "Agent hooks installed",
                "fail",
                f"no synergo hooks in the project's or your global {claude.label} settings",
                "run `synergo up` (or `synergo hook install claude-code`)",
            )
        )

    svc = load_service()
    status = svc.status(port)
    if status is None:
        checks.append(
            Check("Daemon running", "fail", f"nothing answering on 127.0.0.1:{port}", f"run `synergo up` (logs: {svc.log_path()})")
        )
        checks.append(Check("Repo connected", "skip", "daemon not running"))
    else:
        checks.append(Check("Daemon running", "ok", f"pid {status.get('pid', '?')}, port {port}"))
        checks.append(_repo_check(status, root, port))

    relay = config.relay if config is not None else (os.environ.get(cfg.RELAY_ENV) or cfg.DEFAULT_RELAY)
    checks.extend(_relay_checks(relay))
    return checks


def _repo_check(status: dict[str, Any], root: Optional[Path], port: int) -> Check:
    label = "Repo connected"
    if root is None:
        return Check(label, "skip", "no repository")
    entry = find_repo_entry(status, root)
    if entry is None and is_single_room(status):
        # `synergo join`: one room for every repo on the machine.
        single = (repo_entries(status) or [status])[0]
        if single.get("fatal") or not single.get("connected"):
            return Check(
                label,
                "fail",
                f"single-room `synergo join` daemon on port {port}: "
                + str(single.get("fatal") or "not connected to the relay"),
                "stop it (Ctrl+C in its terminal), then run `synergo up`",
            )
        return Check(label, "ok", f"single-room `synergo join` daemon in room {single.get('room')}")
    if entry is None:
        return Check(label, "fail", "this repo is not registered with the daemon", "run `synergo up`")
    if entry.get("fatal"):
        return Check(label, "fail", str(entry["fatal"]), "fix the cause above, then run `synergo up` again")
    if not entry.get("connected"):
        return Check(label, "fail", "registered but not connected to the relay", "see the relay checks below; `synergo status` shows retries")
    return Check(label, "ok", f"room {entry.get('room')} as {entry.get('name')} ({_peers_text(entry.get('peers'))})")


def cmd_doctor(args: argparse.Namespace) -> int:
    port = resolve_port(args.port)
    checks = run_doctor_checks(args.root, port)
    say("Synergo doctor")
    width = max(len(c.label) for c in checks)
    for check in checks:
        line = f"  {mark(check.status)} {check.label.ljust(width)}"
        if check.detail:
            line += f"  {check.detail}"
        say(line.rstrip())
        if check.failed and check.fix:
            say(f"      fix: {check.fix}")
    failed = sum(1 for c in checks if c.failed)
    say()
    if failed:
        say(f"{failed} check{'s' if failed != 1 else ''} failed.")
        return 1
    say("All checks passed.")
    return 0


# --- parser ------------------------------------------------------------------


# --- fallbacks for agents without native hooks (mcp / watch / githook) ------


def cmd_mcp(args: argparse.Namespace) -> int:
    from synergo import mcp_server

    if args.print_config:
        print(mcp_server.config_snippet(args.agent, args.port))
        return 0
    if args.agents_md:
        print(mcp_server.AGENTS_MD, end="")
        return 0
    return mcp_server.run(resolve_port(args.port), args.agent)


def cmd_watch(args: argparse.Namespace) -> int:
    from synergo import watch
    from synergo.config import load_config

    root = project_root(args.root)
    ignore = tuple(getattr(load_config(root), "ignore", ()) or ())
    port = resolve_port(args.port)
    watcher = watch.Watcher(root, watch.daemon_poster(port, say), agent=args.agent, ignore=ignore)
    say(f"synergo watch: watching {root} for {args.agent} (observe only: warns after the fact). Ctrl+C to stop.")
    try:
        watcher.run(args.interval)
    except KeyboardInterrupt:
        say("synergo watch: stopped")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from synergo import watch
    from synergo.config import load_config

    argv = list(args.cmd)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ConfigError("synergo run: no command given, e.g. `synergo run aider` or `synergo run --agent unknown -- mytool`")
    from pathlib import Path

    agent = args.agent or Path(argv[0]).stem.lower()
    root = project_root(args.root)
    ignore = tuple(getattr(load_config(root), "ignore", ()) or ())
    port = resolve_port(args.port)
    watcher = watch.Watcher(root, watch.daemon_poster(port, say), agent=agent, ignore=ignore)
    say(f"synergo run: watching {root} as {watcher.agent} while `{argv[0]}` runs (observe only: warns after the fact)")
    code = watch.run_wrapped(argv, watcher, interval=args.interval, say=say)
    say(f"synergo run: {argv[0]} exited ({code}); watcher stopped")
    return code


def cmd_githook(args: argparse.Namespace) -> int:
    import subprocess

    from synergo import githook

    root = project_root(args.root)
    if args.githook_command == "run":
        return githook.run(root, resolve_port(args.port), strict=args.strict, say=lambda t: say(t, err=True))
    try:
        if args.githook_command == "install":
            say(githook.install(root))
        else:
            say(githook.uninstall(root))
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        raise CommandFailed(f"githook {args.githook_command}: {exc}") from None
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Deferred: both modules import pydantic, which only the hook path (routed
    # around this parser in main) has to avoid.
    import argparse

    from synergo import __version__
    from synergo.daemon import DEFAULT_SERVER
    from synergo.protocol import AGENT_KINDS

    parser = argparse.ArgumentParser(
        prog="synergo",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"synergo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    port_help = f"daemon loopback port (default {DEFAULT_PORT}, or SYNERGO_PORT)"
    root_help = "project directory (default: the git repo around the current directory)"

    up = sub.add_parser(
        "up",
        help="set up this repo: hooks, room, background daemon (start here)",
        description="Install agent hooks, create or join this repo's room, and connect "
        "it through the background daemon. Safe to re-run.",
    )
    up.add_argument("--room", default=None, metavar="CODE", help="join this room (default: .synergo.toml, else create one)")
    up.add_argument("--relay", default=None, metavar="URL", help="relay URL, e.g. wss://relay.example.com/ws")
    up.add_argument("--name", default=None, help="display name (default: config, git user.name, OS user)")
    up.add_argument("--root", default=None, metavar="PATH", help=root_help)
    up.add_argument("--port", type=int, default=None, help=port_help)
    up.add_argument("-y", "--yes", action="store_true", help="answer yes to every question")
    up.add_argument("--no-hooks", action="store_true", help="do not install agent hooks")
    up.add_argument("--foreground", action="store_true", help="run the daemon in this terminal (debugging, CI)")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="stop the background daemon (hooks stay installed)")
    down.add_argument("--port", type=int, default=None, help=port_help)
    down.set_defaults(func=cmd_down)

    leave = sub.add_parser("leave", help="disconnect this repo from its room")
    leave.add_argument("--forget", action="store_true", help="also remove the room from .synergo.toml")
    leave.add_argument("--root", default=None, metavar="PATH", help=root_help)
    leave.add_argument("--port", type=int, default=None, help=port_help)
    leave.set_defaults(func=cmd_leave)

    status = sub.add_parser("status", help="show the daemon and its connected repos")
    status.add_argument("--port", type=int, default=None, help=port_help)
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check the setup and say how to fix what is wrong")
    doctor.add_argument("--root", default=None, metavar="PATH", help=root_help)
    doctor.add_argument("--port", type=int, default=None, help=port_help)
    doctor.set_defaults(func=cmd_doctor)

    open_cmd = sub.add_parser("open", help="open this repo's room in the dashboard")
    open_cmd.add_argument("--root", default=None, metavar="PATH", help=root_help)
    open_cmd.set_defaults(func=cmd_open)

    serve = sub.add_parser("serve", help="run the multi-repo daemon in the foreground")
    serve.add_argument("--port", type=int, default=None, help=port_help)
    serve.set_defaults(func=cmd_serve)

    service = sub.add_parser("service", help="start the daemon automatically at login")
    service_sub = service.add_subparsers(dest="service_command", required=True, metavar="ACTION")
    service_install = service_sub.add_parser("install", help="register the daemon to start at login")
    service_install.add_argument("--port", type=int, default=None, help=port_help)
    service_install.set_defaults(func=cmd_service_install)
    service_uninstall = service_sub.add_parser("uninstall", help="remove the login item")
    service_uninstall.set_defaults(func=cmd_service_uninstall)

    for verb in ("join", "daemon"):
        run = sub.add_parser(verb, help="run a single-room daemon in the foreground (advanced)")
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
    from synergo import adapters

    for agent_id in adapters.ids():
        label = adapters.load(agent_id).label
        hook_cmd = hook_sub.add_parser(agent_id, help=f"handle one {label} hook invocation")
        hook_cmd.add_argument("--port", type=int, default=None)
        hook_cmd.set_defaults(func=cmd_hook_agent)

    install = hook_sub.add_parser("install", help="install synergo hooks into an agent's hooks config")
    install.add_argument("target", choices=list(adapters.ids()))
    install.add_argument(
        "--global", dest="global_scope", action="store_true", help="use the user-level config (~/.claude, ~/.codex)"
    )
    install.set_defaults(func=cmd_hook_install)

    uninstall = hook_sub.add_parser("uninstall", help="remove synergo hooks from an agent's hooks config")
    uninstall.add_argument("target", choices=list(adapters.ids()))
    uninstall.add_argument("--global", dest="global_scope", action="store_true")
    uninstall.set_defaults(func=cmd_hook_uninstall)

    mcp = sub.add_parser("mcp", help="MCP server (stdio) for agents without native hooks")
    mcp.add_argument("--agent", default="unknown", help="agent id reported to the room (default: unknown)")
    mcp.add_argument("--port", type=int, default=None, help=port_help)
    mcp.add_argument("--print-config", action="store_true", help="print a JSON config snippet for an MCP client")
    mcp.add_argument("--agents-md", action="store_true", help="print an instruction block for AGENTS.md / CLAUDE.md")
    mcp.set_defaults(func=cmd_mcp)

    watch_cmd = sub.add_parser("watch", help="observe-only file watcher for agents without hooks (e.g. aider)")
    watch_cmd.add_argument("--agent", default="unknown", help="agent id, e.g. aider")
    watch_cmd.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default 1)")
    watch_cmd.add_argument("--root", default=None, metavar="PATH", help=root_help)
    watch_cmd.add_argument("--port", type=int, default=None, help=port_help)
    watch_cmd.set_defaults(func=cmd_watch)

    run_cmd = sub.add_parser(
        "run",
        help="run a hookless agent (e.g. aider) with an observe-only watcher",
        description="Run COMMAND in this terminal while `synergo watch` shares its edits. "
        "An `-m/--message` argument is posted as the agent's intent first. "
        "Exits with COMMAND's exit code.",
    )
    run_cmd.add_argument("--agent", default=None, help="agent id reported to the room (default: the command name, e.g. aider; unknown ids read as unknown)")
    run_cmd.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default 1)")
    run_cmd.add_argument("--root", default=None, metavar="PATH", help=root_help)
    run_cmd.add_argument("--port", type=int, default=None, help=port_help)
    run_cmd.add_argument("cmd", nargs=argparse.REMAINDER, metavar="COMMAND [ARGS...]", help="command to run, e.g. aider -m 'fix the login bug'")
    run_cmd.set_defaults(func=cmd_run)

    githook_cmd = sub.add_parser("githook", help="git pre-commit check against teammates' recent edits")
    githook_sub = githook_cmd.add_subparsers(dest="githook_command", required=True, metavar="ACTION")
    for action, text in (("install", "install (chaining any existing hook)"),
                         ("uninstall", "remove and restore any previous hook"),
                         ("run", "check staged files (called by the hook)")):
        p = githook_sub.add_parser(action, help=text)
        p.add_argument("--root", default=None, metavar="PATH", help=root_help)
        if action == "run":
            p.add_argument("--port", type=int, default=None, help=port_help)
            p.add_argument("--strict", action="store_true", help="fail the commit when a teammate touched a staged file")
        p.set_defaults(func=cmd_githook)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "hook" and not any(a in HOOK_HELP_FLAGS for a in argv[2:]):
        from synergo.adapters import REGISTRY

        if argv[1] in REGISTRY:
            return run_hook_fast_path(argv[2:], argv[1])
    args = build_parser().parse_args(argv)

    from synergo.config import ConfigFileError

    try:
        return args.func(args)
    except (ConfigError, ConfigFileError) as exc:
        say(f"synergo: {exc}", err=True)
        return 2
    except CommandFailed as exc:
        say(f"synergo: {exc}", err=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
