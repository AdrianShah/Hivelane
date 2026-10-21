"""The contract every agent hook adapter implements, plus shared install logic.

An adapter is a plain module (registered in synergo.adapters.REGISTRY) that
exposes:

    ID: str                       agent id, as in `synergo hook <id>`
    LABEL: str                    human name
    EXECUTABLES / CONFIG_DIRS     how synergo.detect spots the agent
    CAPABILITIES: dict            {"can_block", "has_intent", "has_post_edit"}

    normalize(payload: dict) -> list[dict]
        Native hook stdin payload -> NormalizedEvent-shaped dicts (every
        EVENT_FIELDS key present). [] when synergo does not care or the
        payload is malformed. Never raises. A list because one native call
        can touch several files (Codex's apply_patch); every pre_edit in it
        is checked with the daemon and the strictest ack wins.
    render(event: dict, ack: Any) -> HookOutput
        Daemon ack -> what the hook prints and its exit status. Anything that
        is not a well-formed warn/block reads as allow (fail open). A block is
        never rendered as an allow, and a warn never auto-approves. Never
        raises.
    render_notice(event: dict, message: str) -> HookOutput
        A user-visible notice (daemon offline) that decides nothing.
    settings_path(scope_root: Optional[Path]) -> Path
    install(scope_root) -> list[str]      events added ([] = already there)
    uninstall(scope_root) -> list[str]    events removed
    is_installed(scope_root) -> bool
        scope_root None means the user's global config. Idempotent and
        marker-based (HOOK_MARKER in the command); an unparseable config file
        raises SettingsError and is never overwritten.

The hook path imports only this module's top half (stdlib, no json package,
no pathlib); the install helpers import json/pathlib lazily.
"""

from __future__ import annotations

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Optional

HOOK_MARKER = "synergo hook"

# Every NormalizedEvent field, so a dict built here serializes to exactly the
# JSON the pydantic model would (None fields included, as model_dump_json does).
EVENT_FIELDS: tuple[str, ...] = (
    "kind",
    "agent",
    "session_id",
    "cwd",
    "prompt",
    "file_path",
    "old_content",
    "new_content",
    "tool",
    # Optional Phase 3 fields. edit_hints: [{"old": str, "new": str}] for
    # string-replacement edits (lets the daemon predict touched lines); plan
    # events carry summary plus candidate files/symbols pulled from the text.
    "edit_hints",
    "summary",
    "files",
    "symbols",
)

DECISIONS: tuple[str, ...] = ("allow", "warn", "block")
STRICTNESS = {"allow": 0, "warn": 1, "block": 2}


class HookOutput:
    """What one hook invocation prints to stdout and exits with."""

    __slots__ = ("stdout", "exit_code")

    def __init__(self, stdout: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, HookOutput)
            and self.stdout == other.stdout
            and self.exit_code == other.exit_code
        )

    def __repr__(self) -> str:
        return f"HookOutput(stdout={self.stdout!r}, exit_code={self.exit_code})"


PROCEED = HookOutput()


def make_event(agent: str, kind: str, session_id: str, cwd: str, **fields: Any) -> dict[str, Any]:
    event: dict[str, Any] = dict.fromkeys(EVENT_FIELDS)
    event.update(kind=kind, agent=agent, session_id=session_id, cwd=cwd, **fields)
    return event


MAX_HINT_CHARS = 20000
MAX_HINTS = 50


def string_hints(edits: Any, old_key: str = "old_string", new_key: str = "new_string") -> Optional[list[dict[str, str]]]:
    """[{"old", "new"}] from string-replacement edits (dicts carrying old/new
    strings), so the daemon can predict which lines an edit touches. None
    when there is nothing usable."""
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list):
        return None
    hints: list[dict[str, str]] = []
    for edit in edits[:MAX_HINTS]:
        if not isinstance(edit, dict):
            continue
        old, new = edit.get(old_key), edit.get(new_key)
        if isinstance(old, str) and old and len(old) <= MAX_HINT_CHARS:
            hints.append({"old": old, "new": new if isinstance(new, str) and len(new) <= MAX_HINT_CHARS else ""})
    return hints or None


# File extensions a plan's path-like token must end with to be a file candidate.
PLAN_FILE_EXTENSIONS: tuple[str, ...] = (
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".cs", ".rb", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".php", ".scala",
    ".vue", ".svelte", ".css", ".scss", ".html", ".md", ".toml", ".yaml", ".yml", ".json",
    ".sql", ".sh",
)
_TOKEN_STRIP = "`'\"()[]{}<>,;:!?*"
_IDENT_EXTRA = "._$"
MAX_PLAN_TEXT = 8000
MAX_PLAN_ITEMS = 50


def plan_candidates(text: str) -> tuple[list[str], list[str]]:
    """Candidate file paths and symbol names mentioned in plan text.

    Stdlib-only and regex-free (the hook path cannot afford `re`): files are
    whitespace tokens ending in a known extension; symbols are backticked
    identifiers such as `Room.check_edit` or `foo()`. The daemon matches the
    file candidates against the repo's tracked files.
    """
    files: list[str] = []
    found: list[str] = []
    text = text[:MAX_PLAN_TEXT]
    for raw in text.split():
        token = raw.strip(_TOKEN_STRIP).rstrip(".")
        if "." in token and token.lower().endswith(PLAN_FILE_EXTENSIONS) and token not in files:
            files.append(token)
    for index, chunk in enumerate(text.split("`")):
        if index % 2 == 0:
            continue
        name = chunk.strip()
        if name.endswith("()"):
            name = name[:-2]
        if (
            name
            and len(name) <= 120
            and (name[0].isalpha() or name[0] == "_")
            and all(ch.isalnum() or ch in _IDENT_EXTRA for ch in name)
            and not name.lower().endswith(PLAN_FILE_EXTENSIONS)
            and name not in found
        ):
            found.append(name)
    return files[:MAX_PLAN_ITEMS], found[:MAX_PLAN_ITEMS]


def plan_event(agent: str, session_id: str, cwd: str, summary: str, tool: Any = None) -> Optional[dict[str, Any]]:
    summary = summary.strip()
    if not summary:
        return None
    files, symbols = plan_candidates(summary)
    return make_event(
        agent, "plan", session_id, cwd, summary=summary[:MAX_PLAN_TEXT], files=files,
        symbols=symbols, tool=tool,
    )


def read_text(path: str) -> Optional[str]:
    # open() rather than pathlib: pathlib is a measurable slice of the hook's
    # cold start.
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def ack_decision(ack: Any) -> str:
    """The decision an ack really carries; anything malformed is allow."""
    if not isinstance(ack, dict):
        return "allow"
    decision = ack.get("decision", "allow")
    if decision not in DECISIONS:
        return "allow"
    if not isinstance(ack.get("others", []), list):
        return "allow"
    return decision


def strictest(results: list[tuple[dict[str, Any], Any]]) -> Optional[tuple[dict[str, Any], Any]]:
    """Of (event, ack) pairs, the first with the strictest decision."""
    best: Optional[tuple[dict[str, Any], Any]] = None
    best_rank = -1
    for pair in results:
        rank = STRICTNESS[ack_decision(pair[1])]
        if rank > best_rank:
            best, best_rank = pair, rank
    return best


# A block stops the edit; a warn lets it through, so they must not read alike.
CALL_TO_ACTION = {
    "block": "wait, or pick another file",
    "warn": "check with them before you overlap",
}


def ack_message(ack: dict[str, Any], decision: str, fallback_path: Optional[str]) -> str:
    """Human sentence naming who is in this file and what to do about it.

    The relay's reason is authoritative for the who-did-what clause because it
    distinguishes a peer who actually edited the file from one who has only
    declared intent — composing that here would misreport the second case.
    """
    others = ack.get("others") or []
    path = ack.get("file_path") or fallback_path or "this file"
    detail = ack.get("reason") or f"a teammate's agent is already working in {path}"
    if len(others) > 1:
        extra = len(others) - 1
        detail += f" (and {extra} other{'s' if extra > 1 else ''})"

    message = f"{detail} — {CALL_TO_ACTION[decision]}"
    first = others[0] if others else None
    if isinstance(first, dict) and first.get("prompt"):
        message += f' (their task: "{first["prompt"]}")'
    return message



# --- shared install logic for Claude-Code-shaped JSON hook configs -------------
# Claude Code's settings.json and Codex's hooks.json share the same
# {"hooks": {Event: [{matcher?, hooks: [{type, command, timeout}]}]}} shape.


class SettingsError(Exception):
    """A config file exists but cannot be understood: never overwrite it."""


def load_settings(path: Path) -> dict[str, Any]:
    """Parse an existing JSON settings file, or raise SettingsError.

    Never returns {} for content it failed to understand: the caller writes this
    file back, so a silent empty dict would replace the user's own settings.
    """
    import json

    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc}) — fix it first") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path} does not contain a JSON object — refusing to overwrite")
    return data


def save_settings(path: Path, settings: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _is_synergo(hook: Any) -> bool:
    return isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", ""))


def has_synergo(groups: list[Any]) -> bool:
    for group in groups:
        if isinstance(group, dict) and any(_is_synergo(h) for h in group.get("hooks") or []):
            return True
    return False


def has_synergo_hooks(settings: dict[str, Any], events: Any) -> bool:
    """Every one of `events` carries a synergo hook entry."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return all(isinstance(hooks.get(e), list) and has_synergo(hooks[e]) for e in events)


def merge_hooks(settings: dict[str, Any], desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Add synergo's hook entries without disturbing anything already there."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            raise SettingsError("the existing `hooks` entry is not an object — refusing to overwrite")
        hooks = settings["hooks"] = {}
    changes: list[str] = []
    for event, groups in desired.items():
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = [] if existing is None else [existing]
            hooks[event] = existing
        if has_synergo(existing):
            if all(group in existing for group in groups):
                continue
            # An older synergo entry (say, a narrower matcher): replace it.
            existing[:] = _without_synergo(existing)
        existing.extend(groups)
        changes.append(event)
    return changes


def _without_synergo(groups: list[Any]) -> list[Any]:
    kept_groups: list[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        entries = group.get("hooks") or []
        kept = [hook for hook in entries if not _is_synergo(hook)]
        if len(kept) == len(entries):
            kept_groups.append(group)
        elif kept:
            kept_groups.append({**group, "hooks": kept})
    return kept_groups


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
            kept = [hook for hook in entries if not _is_synergo(hook)]
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


def install_json_hooks(path: Path, desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    settings = load_settings(path)
    added = merge_hooks(settings, desired)
    if added:
        save_settings(path, settings)
    return added


def uninstall_json_hooks(path: Path) -> list[str]:
    if not path.exists():
        return []
    settings = load_settings(path)
    removed = remove_hooks(settings)
    if removed:
        save_settings(path, settings)
    return removed


def json_hooks_installed(path: Path, events: Any) -> bool:
    try:
        return has_synergo_hooks(load_settings(path), events)
    except (SettingsError, OSError, UnicodeDecodeError):
        return False


# --- the adapter object ----------------------------------------------------------

USER_SCOPE = "user"
PROJECT_SCOPE = "project"


class Adapter:
    """Base for every adapter; subclasses fill in the class attributes and the
    normalize/render pair. Instantiated once per module as `ADAPTER`."""

    id: str = ""
    label: str = ""
    executables: tuple[str, ...] = ()
    config_dirs: tuple[str, ...] = ()  # relative to the home directory
    capabilities: dict[str, bool] = {"can_block": False, "has_intent": False, "has_post_edit": False}

    @property
    def hook_command(self) -> str:
        return f"synergo hook {self.id}"

    def detect(self) -> Any:
        """The synergo.detect.DetectedAgent for this agent, or None."""
        from synergo import detect

        return detect.detect(detect.get_agent(self.id))

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render(self, event: dict[str, Any], ack: Any) -> HookOutput:
        raise NotImplementedError

    def render_notice(self, event: dict[str, Any], message: str) -> HookOutput:
        return PROCEED

    def passthrough(self, payload: Any) -> str:
        """What to print when nothing else was rendered ("" = nothing). For
        agents (Cursor) whose permission hooks read empty output as a block."""
        return ""

    def settings_path(self, scope: str, root: Any = None) -> Path:
        raise NotImplementedError

    def install(self, scope: str, root: Any = None) -> list[str]:
        raise NotImplementedError

    def uninstall(self, scope: str, root: Any = None) -> list[str]:
        raise NotImplementedError

    def is_installed(self, scope: str, root: Any = None) -> bool:
        raise NotImplementedError


class JsonHooksAdapter(Adapter):
    """An agent whose hooks live in a Claude-Code-shaped JSON file under
    `<root>/<config_subpath>` (project) or `~/<config_subpath>` (user)."""

    config_subpath: tuple[str, ...] = ()

    def hook_settings(self) -> dict[str, list[dict[str, Any]]]:
        raise NotImplementedError

    def settings_path(self, scope: str, root: Any = None) -> Path:
        from pathlib import Path

        if scope == USER_SCOPE or root is None:
            from synergo import detect

            base = detect.home()
        else:
            base = Path(root)
        return base.joinpath(*self.config_subpath)

    def install(self, scope: str, root: Any = None) -> list[str]:
        return install_json_hooks(self.settings_path(scope, root), self.hook_settings())

    def uninstall(self, scope: str, root: Any = None) -> list[str]:
        return uninstall_json_hooks(self.settings_path(scope, root))

    def is_installed(self, scope: str, root: Any = None) -> bool:
        return json_hooks_installed(self.settings_path(scope, root), tuple(self.hook_settings()))
