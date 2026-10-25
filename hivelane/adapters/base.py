"""The contract every agent hook adapter implements, plus shared install logic.

An adapter is a plain module (registered in hivelane.adapters.REGISTRY) that
exposes:

    ID: str                       agent id, as in `hivelane hook <id>`
    LABEL: str                    human name
    EXECUTABLES / CONFIG_DIRS     how hivelane.detect spots the agent
    CAPABILITIES: dict            {"can_block", "has_intent", "has_post_edit"}

    normalize(payload: dict) -> list[dict]
        Native hook stdin payload -> NormalizedEvent-shaped dicts (every
        EVENT_FIELDS key present). [] when hivelane does not care or the
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
        scope_root None means the user's global config. Idempotent; an entry
        is hivelane's when its command parses as `hivelane hook <id>`
        (parse_hook_command), and a user-customised command is kept on
        reinstall. An unreadable or unparseable config file raises
        SettingsError and is never overwritten.

The hook path imports only this module's top half (stdlib, no json package,
no pathlib); the install helpers import json/pathlib lazily.
"""

from __future__ import annotations

import os

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Optional

# Display/back-compat only: ownership is decided by parse_hook_command, never
# by this substring.
HOOK_MARKER = "hivelane hook"

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
        if not isinstance(new, str):
            new = ""
        # A too-long side drops the hint: "" for `new` would read as a deletion.
        if isinstance(old, str) and old and len(old) <= MAX_HINT_CHARS and len(new) <= MAX_HINT_CHARS:
            hints.append({"old": old, "new": new})
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
        if "://" in token:
            continue  # a URL, not a repo file
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


# Files larger than this, or binary (a NUL in the first BINARY_SNIFF_BYTES),
# are sent as "content unknown" (None): a 30 MB log would blow the hook's time
# budget before the request even left. Must match hivelane.mcp_server.MAX_CONTENT
# (asserted in the tests) so the hook, MCP and watch paths agree.
MAX_CONTENT_BYTES = 256 * 1024
BINARY_SNIFF_BYTES = 8192


def read_text(path: str) -> Optional[str]:
    """A file's text, or None when it is missing, binary, oversized or not
    UTF-8. Never reads more than MAX_CONTENT_BYTES + 1 bytes.

    open() rather than pathlib: pathlib is a measurable slice of the hook's
    cold start. Newlines are normalised as text-mode reading would.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONTENT_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > MAX_CONTENT_BYTES or b"\0" in raw[:BINARY_SNIFF_BYTES]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def absolute(path: str, cwd: str) -> str:
    """`path` made absolute against the agent's cwd, normalised."""
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


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
# What a block says when its own message could not be built: a block is never
# rendered as an allow, even from a malformed ack.
GENERIC_BLOCK = "a teammate's agent is already working in this file, " + CALL_TO_ACTION["block"]


def _queue_position(ack: dict[str, Any]) -> int:
    position = ack.get("queue_position")
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        return 0
    return position


# Text from the room (a teammate's prompt, the relay's reason) reaches the
# local model's context. Room members are semi-trusted at best, so it is capped,
# flattened to one printable line and, for the free-form prompt, quoted as the
# teammate's words rather than anything Hivelane says.
MAX_PROMPT_CHARS = 200
MAX_REASON_CHARS = 400
# Characters a model-suggested shell command may carry unquoted.
_SAFE_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_./\\:@+=,%~"
)
_UNQUOTABLE_PATH_CHARS = frozenset('"`$!')


def clean_text(value: Any, limit: int) -> str:
    """One printable line of at most `limit` characters: control, format and
    line-break characters become spaces, backticks and double quotes become
    single quotes (so the text cannot open a code span or close our quotes),
    runs of whitespace collapse."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text[: limit * 4]
    chars = []
    for ch in text:
        if ch in "`\"":
            ch = "'"
        elif not ch.isprintable():
            ch = " "
        chars.append(ch)
    text = " ".join("".join(chars).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def shell_path(path: str) -> Optional[str]:
    """`path` as one shell word for a suggested command, or None when it
    cannot be quoted safely for both sh and cmd."""
    if not path or any(not ch.isprintable() for ch in path):
        return None
    if all(ch in _SAFE_PATH_CHARS for ch in path):
        return path
    if any(ch in _UNQUOTABLE_PATH_CHARS for ch in path):
        return None
    return f'"{path}"'


def ack_message(ack: dict[str, Any], decision: str, fallback_path: Optional[str]) -> str:
    """Human sentence naming who is in this file and what to do about it.

    The relay's reason is authoritative for the who-did-what clause because it
    distinguishes a peer who actually edited the file from one who has only
    declared intent, composing that here would misreport the second case.
    A block then says where the agent stands in the file's wait queue, what
    the relay suggests doing meanwhile (or the generic advice), and how to
    wait for its turn from a shell; a warn adds the relay's suggestion.
    Everything taken from the ack is coerced to text and sanitised.
    """
    others = ack.get("others") or []
    if not isinstance(others, list):
        others = []
    named_raw = ack.get("file_path") or fallback_path
    named = clean_text(named_raw, MAX_REASON_CHARS) if named_raw else ""
    path = named or "this file"
    detail = clean_text(ack.get("reason"), MAX_REASON_CHARS) or f"a teammate's agent is already working in {path}"
    if len(others) > 1:
        extra = len(others) - 1
        detail += f" (and {extra} other{'s' if extra > 1 else ''})"

    suggestion = clean_text(ack.get("suggestion"), MAX_REASON_CHARS) if isinstance(ack.get("suggestion"), str) else ""
    if decision == "block":
        steps = []
        position = _queue_position(ack)
        if position:
            steps.append(f"you're #{position} in line")
        steps.append(suggestion or CALL_TO_ACTION["block"])
        word = shell_path(named_raw) if isinstance(named_raw, str) else None
        if word:
            steps.append(f"run `hivelane wait {word}` to wait your turn")
    else:
        steps = [CALL_TO_ACTION.get(decision, CALL_TO_ACTION["warn"])]
        if suggestion:
            steps.append(suggestion)
    message = f"{detail}, {'; '.join(steps)}"
    first = others[0] if others else None
    if isinstance(first, dict) and first.get("prompt"):
        prompt = clean_text(first["prompt"], MAX_PROMPT_CHARS)
        if prompt:
            message += f" (their task, in the teammate's own words: \"{prompt}\")"
    return message


# --- shared install logic for Claude-Code-shaped JSON hook configs -------------
# Claude Code's settings.json and Codex's hooks.json share the same
# {"hooks": {Event: [{matcher?, hooks: [{type, command, timeout}]}]}} shape.


class SettingsError(Exception):
    """A config file exists but cannot be understood: never overwrite it."""


class SettingsFormat:
    """How a settings file was written, so a rewrite keeps it that way: its
    encoding and byte-order mark, its line ending, and whether it carried
    comments (which a rewrite would drop, so such a file is never rewritten)."""

    __slots__ = ("encoding", "bom", "newline", "commented")

    def __init__(self, encoding: str = "utf-8", bom: bytes = b"", newline: str = "\n", commented: bool = False) -> None:
        self.encoding = encoding
        self.bom = bom
        self.newline = newline
        self.commented = commented


_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def strip_json_comments(text: str) -> str:
    """`text` with // and /* */ comments outside strings blanked out (the
    JSONC that Gemini CLI, Cursor and VS Code accept). Line structure is kept
    so a JSON error still points at the right line."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            chunk = text[i:] if end < 0 else text[i : end + 2]
            out.append("".join(c if c == "\n" else " " for c in chunk))
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def read_settings(path: Path) -> tuple[dict[str, Any], SettingsFormat]:
    """Parse an existing JSON settings file and how it was written, or raise
    SettingsError with a message fit for the user (never a traceback).

    Accepts UTF-8 (with or without a BOM), UTF-16 with a BOM (what Windows
    PowerShell 5.1's `>` writes) and comments. Never returns {} for content it
    failed to understand: the caller writes this file back, so a silent empty
    dict would replace the user's own settings.
    """
    import json

    fmt = SettingsFormat()
    try:
        if not path.exists():
            return {}, fmt
        raw = path.read_bytes()
    except OSError as exc:
        raise SettingsError(f"cannot read {path} ({exc.strerror or exc}), fix it first") from None
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            fmt.bom, fmt.encoding = bom, encoding
            raw = raw[len(bom) :]
            break
    try:
        text = raw.decode(fmt.encoding)
    except UnicodeDecodeError:
        raise SettingsError(
            f"{path} is not UTF-8 or UTF-16 text, re-save it as UTF-8 first (hivelane never rewrites a file it cannot read)"
        ) from None
    if "\r\n" in text:
        fmt.newline = "\r\n"
    if not text.strip():
        return {}, fmt
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        stripped = strip_json_comments(text)
        if stripped == text:
            raise SettingsError(f"{path} is not valid JSON ({exc}), fix it first") from None
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc2:
            raise SettingsError(f"{path} is not valid JSON ({exc2}), fix it first") from None
        fmt.commented = True
    if not isinstance(data, dict):
        raise SettingsError(f"{path} does not contain a JSON object, refusing to overwrite")
    return data, fmt


def load_settings(path: Path) -> dict[str, Any]:
    """Parse an existing JSON settings file, or raise SettingsError."""
    return read_settings(path)[0]


def save_settings(path: Path, settings: dict[str, Any], fmt: Optional[SettingsFormat] = None) -> None:
    """Write `settings` back atomically (temp file + os.replace, so a crash or a
    concurrent writer never sees a torn file), keeping the file's encoding,
    BOM and line ending, and non-ASCII text as written."""
    import json

    fmt = fmt or SettingsFormat()
    if fmt.commented:
        raise SettingsError(f"{path} has comments, which rewriting it would drop, refusing to overwrite")
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    if fmt.newline != "\n":
        text = text.replace("\n", fmt.newline)
    data = fmt.bom + text.encode(fmt.encoding)
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --- recognising hivelane's own hook entries --------------------------------------
# Structured, not a substring: the command is split into words and must invoke
# hivelane itself (`hivelane`, an absolute path to it, `python -m hivelane[.cli]`,
# or any of those behind a wrapper such as `uv run`) followed by `hook <id>`
# for a registered agent. `./lint-hivelane hooks.sh` is not ours.

HIVELANE_EXECUTABLES: tuple[str, ...] = ("hivelane", "hivelane.exe", "hivelane.cmd", "hivelane.bat")
HIVELANE_MODULES: tuple[str, ...] = ("hivelane", "hivelane.cli")


def split_command(command: str) -> list[str]:
    """Whitespace-separated words, honouring '...' and "..." quoting (enough
    for both sh and cmd command lines; backslashes are kept literally, as
    Windows paths need)."""
    words: list[str] = []
    current: list[str] = []
    quote = ""
    started = False
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
            else:
                current.append(ch)
        elif ch in "'\"":
            quote = ch
            started = True
        elif ch.isspace():
            if started or current:
                words.append("".join(current))
                current, started = [], False
        else:
            current.append(ch)
    if started or current:
        words.append("".join(current))
    return words


def parse_hook_command(command: Any) -> Optional[tuple[str, list[str], list[str]]]:
    """(agent id, words before the hivelane invocation, words after `hook
    <id>`) when `command` runs `hivelane hook <registered id>`, else None."""
    if not isinstance(command, str) or "hook" not in command:
        return None
    from hivelane.adapters import REGISTRY

    words = split_command(command)
    for index, word in enumerate(words):
        name = _basename(word)
        if name in HIVELANE_EXECUTABLES:
            prefix, rest = words[:index], index + 1
        elif (
            word == "-m"
            and index >= 1
            and _basename(words[index - 1]).startswith(PYTHON_PREFIXES)
            and index + 1 < len(words)
            and words[index + 1] in HIVELANE_MODULES
        ):
            prefix, rest = words[: index - 1], index + 2
        else:
            continue
        # Only launchers may come first (`uv run`, `env FOO=1`, ...): in
        # `echo hivelane hook x` hivelane is an argument, not the command.
        if not all(_launcher_word(w) for w in prefix):
            return None
        if rest + 1 < len(words) and words[rest] == "hook" and words[rest + 1] in REGISTRY:
            return words[rest + 1], prefix, words[rest + 2 :]
        return None
    return None


PYTHON_PREFIXES: tuple[str, ...] = ("python", "py")
LAUNCHERS: tuple[str, ...] = (
    "uv", "uvx", "pipx", "npx", "poetry", "pdm", "rye", "hatch", "run", "exec", "env", "--", "&", "call",
)


def _basename(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _launcher_word(word: str) -> bool:
    name = _basename(word)
    if name.endswith(".exe"):
        name = name[:-4]
    return name in LAUNCHERS or word.startswith("-") or ("=" in word and not word.startswith("="))


def _is_hivelane(hook: Any) -> bool:
    return isinstance(hook, dict) and parse_hook_command(hook.get("command")) is not None


def is_canonical_command(command: str, agent_id: str) -> bool:
    """`command` is exactly what hivelane writes by default (bare `hivelane hook
    <id>`), as opposed to a form the user customised: extra flags such as
    --port, a wrapper, an absolute path."""
    return split_command(command) == ["hivelane", "hook", agent_id]


def _entries(group: Any) -> Optional[list[Any]]:
    """A group's hook list, or None when the group is not a well-formed one."""
    if not isinstance(group, dict):
        return None
    entries = group.get("hooks")
    return entries if isinstance(entries, list) else None


def has_hivelane(groups: list[Any]) -> bool:
    return any(any(_is_hivelane(h) for h in _entries(g) or []) for g in groups)


def has_hivelane_hooks(settings: dict[str, Any], events: Any) -> bool:
    """Every one of `events` carries a hivelane hook entry."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    return all(isinstance(hooks.get(e), list) and has_hivelane(hooks[e]) for e in events)


def _existing_command(entries: list[Any]) -> Optional[str]:
    """The first hivelane hook command among flat hook entries."""
    for entry in entries:
        if _is_hivelane(entry):
            return str(entry["command"])
    return None


def keep_user_command(existing: Optional[str], desired: str) -> str:
    """The command a reinstall writes: the user's own when they customised
    hivelane's entry for the same agent (a --port, a wrapper, an absolute
    path), otherwise hivelane's current default."""
    if existing is None:
        return desired
    mine, theirs = parse_hook_command(desired), parse_hook_command(existing)
    if mine is None or theirs is None or mine[0] != theirs[0]:
        return desired
    return desired if is_canonical_command(existing, theirs[0]) else existing


def _with_command(entries: list[Any], command: str) -> list[Any]:
    return [{**e, "command": command} if _is_hivelane(e) else e for e in entries]


def merge_hooks(settings: dict[str, Any], desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Add hivelane's hook entries without disturbing anything already there.

    An older hivelane entry (a narrower matcher, another timeout) is upgraded
    in place of being duplicated; a command the user customised is kept.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        if hooks is not None:
            raise SettingsError("the existing `hooks` entry is not an object, refusing to overwrite")
        hooks = settings["hooks"] = {}
    for event in desired:
        # Checked up front, so a refusal leaves `settings` untouched.
        if hooks.get(event) is not None and not isinstance(hooks[event], list):
            raise SettingsError(f"hooks.{event} is not a list, refusing to overwrite")
    changes: list[str] = []
    for event, groups in desired.items():
        existing = hooks.get(event)
        if existing is None:
            existing = hooks[event] = []
        current = None
        for group in existing:
            current = current or _existing_command(_entries(group) or [])
        if current is not None:
            wanted = [
                {**g, "hooks": _with_command(g["hooks"], keep_user_command(current, _existing_command(g["hooks"]) or current))}
                for g in groups
            ]
            if all(group in existing for group in wanted):
                continue
            existing[:] = _without_hivelane(existing)
            groups = wanted
        existing.extend(groups)
        changes.append(event)
    return changes


def _without_hivelane(groups: list[Any]) -> list[Any]:
    """`groups` minus hivelane's entries; a group hivelane emptied goes, any
    other group (malformed, or empty to begin with) stays exactly as it was."""
    kept_groups: list[Any] = []
    for group in groups:
        entries = _entries(group)
        if entries is None:
            kept_groups.append(group)
            continue
        kept = [hook for hook in entries if not _is_hivelane(hook)]
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
        kept_groups = _without_hivelane(groups)
        if kept_groups == groups:
            continue
        changes.append(event)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if changes and not hooks:
        settings.pop("hooks", None)
    return changes


def _refuse_commented(path: Path, desired: Optional[dict[str, Any]] = None) -> None:
    import json

    message = f"{path} has comments, which rewriting it would drop, refusing to overwrite."
    if desired is not None:
        message += f" Add hivelane's entries by hand under \"hooks\":\n{json.dumps(desired, indent=2)}"
    raise SettingsError(message)


def install_json_hooks(path: Path, desired: dict[str, list[dict[str, Any]]]) -> list[str]:
    settings, fmt = read_settings(path)
    added = merge_hooks(settings, desired)
    if added:
        if fmt.commented:
            _refuse_commented(path, desired)
        save_settings(path, settings, fmt)
    return added


def uninstall_json_hooks(path: Path) -> list[str]:
    if not path.exists():
        return []
    settings, fmt = read_settings(path)
    removed = remove_hooks(settings)
    if removed:
        if fmt.commented:
            _refuse_commented(path)
        save_settings(path, settings, fmt)
    return removed


def json_hooks_installed(path: Path, events: Any) -> bool:
    try:
        return has_hivelane_hooks(load_settings(path), events)
    except (SettingsError, OSError, UnicodeDecodeError):
        return False


# --- the command an installed hook runs ------------------------------------------


def hivelane_on_path() -> bool:
    """Whether a bare `hivelane` resolves on this PATH (a seam for tests). The
    agent's own PATH may differ; this is the best evidence install has."""
    import shutil

    return shutil.which("hivelane") is not None


def _quoted(path: str) -> str:
    # Double quotes work in sh and cmd alike, and keep a Windows path's
    # backslashes literal in sh.
    return f'"{path}"'


def absolute_hivelane() -> str:
    """This install's hivelane as an absolute command: the console script next
    to the running interpreter, else `<python> -m hivelane.cli`."""
    import sys

    scripts = os.path.dirname(os.path.abspath(sys.executable))
    for name in ("hivelane.exe", "hivelane"):
        candidate = os.path.join(scripts, name)
        if os.path.isfile(candidate):
            return _quoted(candidate)
    return f"{_quoted(sys.executable)} -m hivelane.cli"


def hook_command_for(agent_id: str) -> str:
    """`hivelane hook <id>` when hivelane is on PATH, else the same through an
    absolute path, so the agent can run it from wherever it starts hooks."""
    if hivelane_on_path():
        return f"hivelane hook {agent_id}"
    return f"{absolute_hivelane()} hook {agent_id}"


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
        """The command install writes (see hook_command_for)."""
        return hook_command_for(self.id)

    def detect(self) -> Any:
        """The hivelane.detect.DetectedAgent for this agent, or None."""
        from hivelane import detect

        return detect.detect(detect.get_agent(self.id))

    def normalize(self, payload: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def render(self, event: dict[str, Any], ack: Any) -> HookOutput:
        raise NotImplementedError

    def render_notice(self, event: dict[str, Any], message: str) -> HookOutput:
        return PROCEED

    def passthrough(self, payload: Any) -> str:
        """What to print when nothing else was rendered ("" = nothing). For
        agents (Cursor) whose permission hooks need some well-formed answer."""
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

        if scope == USER_SCOPE:
            from hivelane import detect

            base = detect.home()
        elif root is None:
            raise ValueError("a project-scope settings path needs the project root")
        else:
            base = Path(root)
        return base.joinpath(*self.config_subpath)

    def install(self, scope: str, root: Any = None) -> list[str]:
        return install_json_hooks(self.settings_path(scope, root), self.hook_settings())

    def uninstall(self, scope: str, root: Any = None) -> list[str]:
        return uninstall_json_hooks(self.settings_path(scope, root))

    def is_installed(self, scope: str, root: Any = None) -> bool:
        return json_hooks_installed(self.settings_path(scope, root), tuple(self.hook_settings()))
