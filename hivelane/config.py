"""Project and personal configuration: `.hivelane.toml` plus `~/.hivelane/config.toml`.

The project file is committed, so a teammate who pulls the repo joins the same
room with the same conflict windows. The personal file holds what differs per
person (display name, a private relay) and the secrets that must never be
committed: e2e room keys (`room_keys`) and room owner tokens (`owner_tokens`),
both tables keyed by room code, the file kept owner-only (0600). Precedence,
highest first: explicit overrides (CLI flags) > environment > personal file >
project file > defaults, except `room`, the windows, `e2e` and
`history_hours`, which the project file owns so one person's personal config
cannot silently split them from the team.

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

PROJECT_FILE = ".hivelane.toml"
DEFAULT_RELAY = "ws://localhost:8000/ws"
DEFAULT_BLOCK_WINDOW = 20.0
DEFAULT_WARN_WINDOW = 60.0
# Hours of room events the relay keeps for the dashboard's replay view
# (RoomSettings.history_hours has the same bound). 0 keeps none.
MAX_HISTORY_HOURS = 24.0

RELAY_ENV = "HIVELANE_RELAY"
NAME_ENV = "HIVELANE_NAME"
HOME_ENV = "HIVELANE_HOME"
# The e2e room passphrase for whichever repo the process runs in; overrides
# the personal config's room_keys. The long-lived daemon ignores it (it
# serves many rooms); HIVELANE_ROOM_KEY_<ROOM> names one room and applies
# everywhere, the daemon included.
ROOM_KEY_ENV = "HIVELANE_ROOM_KEY"


def room_key_env_name(room: str) -> str:
    """HIVELANE_ROOM_KEY_<ROOM>: the room code upper-cased, - as _."""
    return f"{ROOM_KEY_ENV}_{re.sub(r'[^A-Z0-9]', '_', room.strip().upper())}"

# Keys the writer knows how to emit, in the order they are written.
_PROJECT_KEYS = (
    "room",
    "relay",
    "block_window_seconds",
    "warn_window_seconds",
    "history_hours",
    "ignore",
    "share_diffs",
    "e2e",
    "redact",
    "redact_patterns",
    "insecure",
)
# What write_personal_config emits: the per-person keys.
_PERSONAL_KEYS = ("name", "relay", "agent", "insecure", "room_keys", "owner_tokens")
# Secrets: personal file only. One found in the committed project file has
# already leaked to everyone with the repo, so it is an error, not ignored.
_SECRET_TABLES = ("room_keys", "owner_tokens")

# A room code ends up in the relay's WebSocket path, so keep it path-safe.
_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RELAY_SCHEMES = ("ws://", "wss://")


class ConfigFileError(Exception):
    """A config file exists but cannot be used. Reported, never guessed around."""


@dataclass(frozen=True)
class HivelaneConfig:
    room: Optional[str] = None
    relay: str = DEFAULT_RELAY
    name: Optional[str] = None
    agent: str = "claude-code"
    block_window_seconds: float = DEFAULT_BLOCK_WINDOW
    warn_window_seconds: float = DEFAULT_WARN_WINDOW
    # Room history for replay (project file only). None: the file does not
    # say, so joins leave the room's setting alone; 0 turns history off.
    history_hours: Optional[float] = None
    ignore: tuple[str, ...] = ()
    share_diffs: bool = True
    # End-to-end encryption (the team's choice: project file only).
    e2e: bool = False
    # Secret redaction before anything leaves the machine (hivelane.redact).
    redact: bool = True
    redact_patterns: tuple[str, ...] = ()
    # Allow a plaintext ws:// relay on a non-loopback host.
    insecure: bool = False
    # Personal secrets by room code: ((ROOM, value), ...).
    room_keys: tuple[tuple[str, str], ...] = ()
    owner_tokens: tuple[tuple[str, str], ...] = ()
    # Directory holding the project file, or None when there is none.
    root: Optional[Path] = None
    # Which files contributed, for `hivelane doctor`.
    sources: tuple[Path, ...] = field(default_factory=tuple)

    def room_settings(self) -> dict[str, Any]:
        """The JoinMessage.settings payload (RoomSettings on the wire).
        history_hours is included only when the project file sets it."""
        settings: dict[str, Any] = {
            "block_window_seconds": self.block_window_seconds,
            "warn_window_seconds": self.warn_window_seconds,
            "e2e": self.e2e,
        }
        if self.history_hours is not None:
            settings["history_hours"] = self.history_hours
        return settings

    def room_key(self, room: Optional[str] = None, *, generic_env: bool = True) -> Optional[str]:
        """The e2e passphrase for room (default: this config's room):
        HIVELANE_ROOM_KEY_<ROOM>, else HIVELANE_ROOM_KEY (unless generic_env is
        False, as in the daemon, which serves rooms that variable was never
        meant for), else the personal file's room_keys entry."""
        found = self._room_key_found(room, generic_env)
        return found[0] if found else None

    def room_key_source(self, room: Optional[str] = None, *, generic_env: bool = True) -> Optional[str]:
        found = self._room_key_found(room, generic_env)
        return found[1] if found else None

    def _room_key_found(self, room: Optional[str], generic_env: bool) -> Optional[tuple[str, str]]:
        code = room or self.room
        if code:
            scoped_name = room_key_env_name(code)
            scoped = os.environ.get(scoped_name, "").strip()
            if scoped:
                return scoped, scoped_name
        if generic_env:
            env = os.environ.get(ROOM_KEY_ENV, "").strip()
            if env:
                return env, ROOM_KEY_ENV
        value = _lookup(self.room_keys, code)
        if value is not None:
            return value, f"{personal_config_path()} (room_keys)"
        return None

    def owner_token(self, room: Optional[str] = None) -> Optional[str]:
        """The owner token `hivelane up` stored when it created room."""
        return _lookup(self.owner_tokens, room or self.room)


# Mirrors hivelane.crypto.normalize_room (the relay's rule): whitespace and
# "-" removed, upper case. Not imported: this module must stay light.
_ROOM_SEPARATORS = re.compile(r"[\s\-]+")


def canonical_room(room: str) -> str:
    """`k7q2-xm9p`, ` K7Q2 XM9P ` and `K7Q2XM9P` are one relay room, so
    they must find one `room_keys` / `owner_tokens` entry."""
    return _ROOM_SEPARATORS.sub("", room or "").upper()


def _lookup(pairs: tuple[tuple[str, str], ...], room: Optional[str]) -> Optional[str]:
    if not room:
        return None
    wanted = canonical_room(room)
    for code, value in pairs:
        if canonical_room(code) == wanted:
            return value
    return None


def hivelane_home() -> Path:
    """Per-user state directory (logs, pid files, personal config)."""
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else Path.home() / ".hivelane"


def personal_config_path() -> Path:
    return hivelane_home() / "config.toml"


def find_project_config(start: Path | str | None = None) -> Optional[Path]:
    """Nearest `.hivelane.toml` at or above `start` (default: cwd)."""
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


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    return (
        host in ("localhost", "::1", "0.0.0.0")
        or host.endswith(".localhost")
        or host.startswith("127.")
    )


def transport_problem(relay: str, insecure: bool = False) -> Optional[str]:
    """Why relay may not be used, or None. A ws:// relay on anything but this
    machine sends every path, diff and prompt in cleartext over the network
    (and the room code with it), so it needs an explicit opt-in."""
    from urllib.parse import urlsplit

    text = (relay or "").strip()
    if not text.lower().startswith("ws://") or insecure:
        return None
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        host = ""
    if is_loopback_host(host):
        return None
    return (
        f"refusing the plaintext relay {text}: ws:// to {host or 'a remote host'} sends your "
        "paths, diffs and prompts unencrypted over the network. Use wss:// (TLS), or pass "
        "--insecure (or set `insecure = true` in .hivelane.toml) if this network is trusted"
    )


def _check_relay(value: str, source: str) -> str:
    if not value.startswith(_RELAY_SCHEMES):
        raise ConfigFileError(
            f"{source}: relay must be a ws:// or wss:// URL (got {value!r})"
        )
    return value


def _bool(data: dict[str, Any], key: str, path: Path) -> Optional[bool]:
    if key not in data:
        return None
    if not isinstance(data[key], bool):
        raise ConfigFileError(f"{path}: {key} must be true or false")
    return data[key]


def _secret_table(data: dict[str, Any], key: str, path: Path) -> tuple[tuple[str, str], ...]:
    table = data[key]
    if not isinstance(table, dict):
        raise ConfigFileError(f'{path}: {key} must be a table of ROOM = "value"')
    pairs: list[tuple[str, str]] = []
    for room, value in table.items():
        if not isinstance(room, str) or not _ROOM_RE.match(room.strip()):
            raise ConfigFileError(f"{path}: {key} has a key that is not a room code ({room!r})")
        if not isinstance(value, str) or not value.strip():
            raise ConfigFileError(f"{path}: {key}.{room} must be a non-empty string")
        pairs.append((room.strip().upper(), value.strip()))
    return tuple(pairs)


def _apply(base: dict[str, Any], data: dict[str, Any], path: Path, *, project: bool) -> None:
    if project:
        leaked = [key for key in _SECRET_TABLES if key in data]
        if leaked:
            raise ConfigFileError(
                f"{path}: {', '.join(leaked)} must never be in the committed project file "
                f"(anyone with the repo can read it). Remove it, rotate the secret, and keep it "
                f"in {personal_config_path()} or the {ROOM_KEY_ENV} environment variable instead"
            )
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
    hours = _float(data, "history_hours", path)
    if hours is not None:
        if hours > MAX_HISTORY_HOURS:
            raise ConfigFileError(
                f"{path}: history_hours must be between 0 and {MAX_HISTORY_HOURS:g}"
            )
        if project:
            base["history_hours"] = hours  # the team's, like the windows
    if "ignore" in data:
        ignore = data["ignore"]
        if not isinstance(ignore, list) or not all(isinstance(g, str) for g in ignore):
            raise ConfigFileError(f"{path}: ignore must be a list of glob strings")
        base["ignore"] = tuple(base.get("ignore", ())) + tuple(ignore)
    if "share_diffs" in data:
        if not isinstance(data["share_diffs"], bool):
            raise ConfigFileError(f"{path}: share_diffs must be true or false")
        base["share_diffs"] = data["share_diffs"]
    e2e = _bool(data, "e2e", path)
    if e2e is not None and project:
        base["e2e"] = e2e  # the team's decision, like the room
    for key in ("redact", "insecure"):
        value = _bool(data, key, path)
        if value is not None:
            base[key] = value
    if "redact_patterns" in data:
        patterns = data["redact_patterns"]
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            raise ConfigFileError(f"{path}: redact_patterns must be a list of regular expressions")
        from hivelane.redact import validate_patterns

        problem = validate_patterns(patterns)
        if problem:
            raise ConfigFileError(f"{path}: redact_patterns: {problem}")
        base["redact_patterns"] = tuple(base.get("redact_patterns", ())) + tuple(patterns)
    if not project:
        for key in _SECRET_TABLES:
            if key in data:
                base[key] = _secret_table(data, key, path)


def load_config(
    start: Path | str | None = None, **overrides: Any
) -> HivelaneConfig:
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

    return HivelaneConfig(root=root, sources=tuple(sources), **values)


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


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        # An inline table: room_keys = { K7Q2XM = "..." }.
        items = [
            f"{key if _BARE_KEY.match(str(key)) else _toml_string(str(key))} = {_toml_value(v)}"
            for key, v in value.items()
        ]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    return _toml_string(str(value))


_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")
_TABLE_LINE = re.compile(r"^\s*\[")
_HEADER = "# Hivelane project settings, commit this file so teammates join the same room."
_PERSONAL_HEADER = "# Personal Hivelane settings for this machine (never committed)."


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


def _write_lines(path: Path, lines: list[str], newline: str, *, private: bool = False) -> None:
    text = newline.join(lines) + newline
    # Validate before touching the file: a broken write would otherwise
    # surface on a teammate's machine, far from the change that caused it.
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(
            f"refusing to write {path}: the result would not be valid TOML ({exc})"
        ) from None
    if private:
        from hivelane.auth import write_private

        write_private(path, text)
        return
    path.write_text(text, encoding="utf-8", newline="")


def write_project_config(root: Path, **values: Any) -> Path:
    """Create or update `<root>/.hivelane.toml`, preserving unknown keys and comments.

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
    conflict windows belong to the project file. The file may hold room keys
    and owner tokens, so it is kept owner-only (0600) where the OS has modes."""
    unknown = set(values) - set(_PERSONAL_KEYS)
    if unknown:
        raise ValueError(f"unsupported personal keys: {sorted(unknown)}")
    path = personal_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _update_file(path, values, _PERSONAL_KEYS, _PERSONAL_HEADER, private=True)
    return path


def personal_values() -> dict[str, Any]:
    """The raw personal file ({} when there is none)."""
    path = personal_config_path()
    if not path.is_file():
        return {}
    return _read_toml(path)


def set_personal_secret(table: str, room: str, value: Optional[str]) -> Path:
    """Set (or with None remove) `table`.ROOM in the personal file, keeping
    the table's other rooms. table is "room_keys" or "owner_tokens"."""
    if table not in _SECRET_TABLES:
        raise ValueError(f"not a secret table: {table!r}")
    if not _ROOM_RE.match(room.strip()):
        raise ConfigFileError(f"{room!r} is not a room code")
    code = canonical_room(room)
    current = personal_values().get(table) or {}
    if not isinstance(current, dict):
        raise ConfigFileError(f"{personal_config_path()}: {table} must be a table")
    merged = {canonical_room(str(k)): v for k, v in current.items()}
    if value is None:
        merged.pop(code, None)
    else:
        merged[code] = value
    return write_personal_config(**{table: merged})


def _update_file(
    path: Path, values: dict[str, Any], keys: tuple[str, ...], header: str, *, private: bool = False
) -> Path:
    """Set top-level `values` (None skipped) in the TOML file at `path`,
    creating it with `header` if missing (owner-only when private)."""
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

    _write_lines(path, lines, newline, private=private)
    return path


def remove_project_keys(root: Path, *keys: str) -> list[str]:
    """Delete top-level `keys` from `<root>/.hivelane.toml`, leaving every other
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


def with_overrides(config: HivelaneConfig, **overrides: Any) -> HivelaneConfig:
    return replace(config, **{k: v for k, v in overrides.items() if v is not None})
