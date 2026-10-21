"""Project and personal configuration: `.synergo.toml` plus `~/.synergo/config.toml`.

The project file is committed, so a teammate who pulls the repo joins the same
room with the same conflict windows. The personal file holds what differs per
person (display name, a private relay). Precedence, highest first: explicit
overrides (CLI flags) > environment > personal file > project file > defaults —
except `room` and the windows, which the project file owns so one person's
personal config cannot silently split them from the team.

Stdlib only (tomllib for reading, a tiny writer for the handful of keys we
emit), because the hook path may import this on its lazy-start branch.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

PROJECT_FILE = ".synergo.toml"
DEFAULT_RELAY = "ws://localhost:8000/ws"
DEFAULT_BLOCK_WINDOW = 20.0
DEFAULT_WARN_WINDOW = 60.0

RELAY_ENV = "SYNERGO_RELAY"
NAME_ENV = "SYNERGO_NAME"
HOME_ENV = "SYNERGO_HOME"

# Keys the writer knows how to emit, in the order they are written.
_PROJECT_KEYS = (
    "room",
    "relay",
    "block_window_seconds",
    "warn_window_seconds",
    "ignore",
    "share_diffs",
)
# What write_personal_config emits: the per-person keys.
_PERSONAL_KEYS = ("name", "relay", "agent")

# A room code ends up in the relay's WebSocket path, so keep it path-safe.
_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RELAY_SCHEMES = ("ws://", "wss://")


class ConfigFileError(Exception):
    """A config file exists but cannot be used. Reported, never guessed around."""


@dataclass(frozen=True)
class SynergoConfig:
    room: Optional[str] = None
    relay: str = DEFAULT_RELAY
    name: Optional[str] = None
    agent: str = "claude-code"
    block_window_seconds: float = DEFAULT_BLOCK_WINDOW
    warn_window_seconds: float = DEFAULT_WARN_WINDOW
    ignore: tuple[str, ...] = ()
    share_diffs: bool = True
    # Directory holding the project file, or None when there is none.
    root: Optional[Path] = None
    # Which files contributed, for `synergo doctor`.
    sources: tuple[Path, ...] = field(default_factory=tuple)

    def room_settings(self) -> dict[str, float]:
        """The JoinMessage.settings payload (RoomSettings on the wire)."""
        return {
            "block_window_seconds": self.block_window_seconds,
            "warn_window_seconds": self.warn_window_seconds,
        }


def synergo_home() -> Path:
    """Per-user state directory (logs, pid files, personal config)."""
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else Path.home() / ".synergo"


def personal_config_path() -> Path:
    return synergo_home() / "config.toml"


def find_project_config(start: Path | str | None = None) -> Optional[Path]:
    """Nearest `.synergo.toml` at or above `start` (default: cwd)."""
    try:
        current = Path(start).resolve() if start else Path.cwd()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        path = candidate / PROJECT_FILE
        if path.is_file():
            return path
    return None


def _read_text(path: Path) -> str:
    try:
        # utf-8-sig: Windows editors sometimes save a BOM, which tomllib rejects.
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigFileError(f"{path} is not UTF-8 ({exc})") from None
    except OSError as exc:
        raise ConfigFileError(f"cannot read {path} ({exc})") from None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_text(path))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(f"{path} is not valid TOML ({exc})") from None


def _float(data: dict[str, Any], key: str, path: Path) -> Optional[float]:
    if key not in data:
        return None
    value = data[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ConfigFileError(f"{path}: {key} must be a non-negative number")
    return float(value)


def _check_relay(value: str, source: str) -> str:
    if not value.startswith(_RELAY_SCHEMES):
        raise ConfigFileError(
            f"{source}: relay must be a ws:// or wss:// URL (got {value!r})"
        )
    return value


def _apply(base: dict[str, Any], data: dict[str, Any], path: Path, *, project: bool) -> None:
    for key in ("room", "relay", "name", "agent"):
        if key in data:
            if not isinstance(data[key], str) or not data[key].strip():
                raise ConfigFileError(f"{path}: {key} must be a non-empty string")
            value = data[key].strip()
            if key == "room" and not _ROOM_RE.match(value):
                raise ConfigFileError(
                    f"{path}: room must be letters, digits, '-' or '_' (got {value!r})"
                )
            if key == "relay":
                _check_relay(value, str(path))
            if key == "room" and not project:
                continue  # the room is the team's, not one person's
            base[key] = value
    for key in ("block_window_seconds", "warn_window_seconds"):
        value = _float(data, key, path)
        if value is not None and project:
            base[key] = value
    if "ignore" in data:
        ignore = data["ignore"]
        if not isinstance(ignore, list) or not all(isinstance(g, str) for g in ignore):
            raise ConfigFileError(f"{path}: ignore must be a list of glob strings")
        base["ignore"] = tuple(base.get("ignore", ())) + tuple(ignore)
    if "share_diffs" in data:
        if not isinstance(data["share_diffs"], bool):
            raise ConfigFileError(f"{path}: share_diffs must be true or false")
        base["share_diffs"] = data["share_diffs"]


def load_config(
    start: Path | str | None = None, **overrides: Any
) -> SynergoConfig:
    """Resolve the effective config for the repo containing `start`.

    `overrides` are explicit values (CLI flags); None values are ignored.
    """
    values: dict[str, Any] = {}
    sources: list[Path] = []
    root: Optional[Path] = None

    project = find_project_config(start)
    if project is not None:
        _apply(values, _read_toml(project), project, project=True)
        sources.append(project)
        root = project.parent

    personal = personal_config_path()
    if personal.is_file():
        _apply(values, _read_toml(personal), personal, project=False)
        sources.append(personal)

    if os.environ.get(RELAY_ENV, "").strip():
        values["relay"] = _check_relay(os.environ[RELAY_ENV].strip(), RELAY_ENV)
    if os.environ.get(NAME_ENV, "").strip():
        values["name"] = os.environ[NAME_ENV].strip()

    for key, value in overrides.items():
        if value is None:
            continue
        if key == "root":
            # Not a config value: an explicit project root replaces discovery.
            root = Path(value)
            continue
        if key == "sources":
            continue
        if key == "relay":
            _check_relay(str(value), "relay")
        values[key] = value

    if values.get("block_window_seconds", DEFAULT_BLOCK_WINDOW) > values.get(
        "warn_window_seconds", DEFAULT_WARN_WINDOW
    ):
        raise ConfigFileError("block_window_seconds cannot exceed warn_window_seconds")

    return SynergoConfig(root=root, sources=tuple(sources), **values)


_BASIC_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_string(text: str) -> str:
    out = []
    for char in text:
        if char in _BASIC_ESCAPES:
            out.append(_BASIC_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_string(str(value))


_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
_TABLE_LINE = re.compile(r"^\s*\[")
_HEADER = "# Synergo project settings — commit this file so teammates join the same room."
_PERSONAL_HEADER = "# Personal Synergo settings for this machine (never committed)."


def _parses(text: str) -> bool:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return True


def _value_end(lines: list[str], start: int) -> int:
    """One past the last line of the key/value pair starting at lines[start].

    A multi-line array spans several lines; rewriting only its first line
    would leave the rest dangling and break the file.
    """
    for end in range(start + 1, len(lines) + 1):
        if _parses("\n".join(lines[start:end])):
            return end
    return start + 1  # unparseable as-is: treat it as a single line


def _trailing_comment(line: str) -> str:
    """`  # note` from `key = "value"  # note`, or "" when there is none."""
    for pos, char in enumerate(line):
        if char == "#" and _parses(line[:pos]):
            head = line[:pos].rstrip()
            return line[len(head):]
    return ""


def _top_level_entries(lines: list[str]) -> tuple[dict[str, tuple[int, int]], int]:
    """Top-level keys -> (first line, one past last line), plus the index of
    the first table header (len(lines) when there is none). Keys under a
    [table] are not ours: load_config only reads top-level keys."""
    entries: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if _TABLE_LINE.match(line):
            return entries, index
        match = _KEY_LINE.match(line)
        if match:
            end = _value_end(lines, index)
            entries.setdefault(match.group(1), (index, end))
            index = end
        else:
            index += 1
    return entries, len(lines)


def _read_lines(path: Path, header: str = _HEADER) -> tuple[list[str], str]:
    """The file's lines and its newline style, so a CRLF file stays CRLF."""
    if not path.exists():
        return [header], "\n"
    text = _read_text(path)
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), newline


def _write_lines(path: Path, lines: list[str], newline: str) -> None:
    text = newline.join(lines) + newline
    # Validate before touching the file: a broken write would otherwise
    # surface on a teammate's machine, far from the change that caused it.
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(
            f"refusing to write {path}: the result would not be valid TOML ({exc})"
        ) from None
    path.write_text(text, encoding="utf-8", newline="")


def write_project_config(root: Path, **values: Any) -> Path:
    """Create or update `<root>/.synergo.toml`, preserving unknown keys and comments.

    Known keys present in the file are rewritten in place (keeping a trailing
    comment); new ones go after the last top-level key, never under a [table]
    header. Lines we do not understand are left exactly as they were. None
    values are ignored.
    """
    unknown = set(values) - set(_PROJECT_KEYS)
    if unknown:
        raise ValueError(f"unsupported project keys: {sorted(unknown)}")
    return _update_file(Path(root) / PROJECT_FILE, values, _PROJECT_KEYS, _HEADER)


def write_personal_config(**values: Any) -> Path:
    """Create or update the personal `config.toml`, with the same preservation
    rules as write_project_config. Per-person keys only: the room and the
    conflict windows belong to the project file."""
    unknown = set(values) - set(_PERSONAL_KEYS)
    if unknown:
        raise ValueError(f"unsupported personal keys: {sorted(unknown)}")
    path = personal_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return _update_file(path, values, _PERSONAL_KEYS, _PERSONAL_HEADER)


def _update_file(path: Path, values: dict[str, Any], keys: tuple[str, ...], header: str) -> Path:
    """Set top-level `values` (None skipped) in the TOML file at `path`,
    creating it with `header` if missing."""
    pending = {k: v for k, v in values.items() if v is not None}
    lines, newline = _read_lines(path, header)
    entries, _ = _top_level_entries(lines)

    # Rewrite existing keys bottom-up so earlier indices stay valid.
    for key, (start, end) in sorted(entries.items(), key=lambda item: -item[1][0]):
        if key not in pending:
            continue
        comment = _trailing_comment(lines[start]) if end == start + 1 else ""
        lines[start:end] = [f"{key} = {_toml_value(pending.pop(key))}{comment}"]

    added = [f"{key} = {_toml_value(pending[key])}" for key in keys if key in pending]
    if added:
        entries, first_table = _top_level_entries(lines)
        if first_table >= len(lines):
            insert_at = len(lines)
        else:
            # Before the first table: after the last top-level key, or else
            # above any comment block attached to the table header.
            insert_at = max((end for _, end in entries.values()), default=0)
            if insert_at == 0:
                insert_at = first_table
                while insert_at > 0 and lines[insert_at - 1].lstrip().startswith("#"):
                    insert_at -= 1
            if lines[insert_at].strip():
                added.append("")
        lines[insert_at:insert_at] = added

    _write_lines(path, lines, newline)
    return path


def remove_project_keys(root: Path, *keys: str) -> list[str]:
    """Delete top-level `keys` from `<root>/.synergo.toml`, leaving every other
    line as it was. Returns the keys actually removed."""
    path = Path(root) / PROJECT_FILE
    if not path.exists():
        return []
    lines, newline = _read_lines(path)
    entries, _ = _top_level_entries(lines)
    removed = [key for key in keys if key in entries]
    for key in sorted(removed, key=lambda k: -entries[k][0]):
        start, end = entries[key]
        del lines[start:end]
    if removed:
        _write_lines(path, lines, newline)
    return removed


def with_overrides(config: SynergoConfig, **overrides: Any) -> SynergoConfig:
    return replace(config, **{k: v for k, v in overrides.items() if v is not None})
