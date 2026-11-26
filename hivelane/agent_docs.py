"""The Hivelane section `hivelane up` keeps in a repo's agent instruction
files (AGENTS.md, CLAUDE.md, GEMINI.md, Copilot's and Cursor's rules).

Why: a teammate who clones the repo and opens any coding agent should not
need to know a command. The committed section tells their agent to check
`hivelane status` and run `hivelane up -y` itself, so joining takes no
command from the human, whatever agent they use.

The section sits between BEGIN/END markers and is rewritten in place on
every run, so re-running `up` updates it and never duplicates it; whatever
else the file says is left byte for byte as it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hivelane.links import INSTALL_COMMAND

BEGIN = "<!-- hivelane:begin (managed by `hivelane up`; edits here are replaced) -->"
END = "<!-- hivelane:end -->"
# Matches the begin marker whatever its trailing note says, so a future
# wording change still finds (and replaces) an old block.
_BEGIN_PREFIX = "<!-- hivelane:begin"

# Instruction files agents read, updated when they already exist. AGENTS.md
# is read by Codex, Cursor, Copilot, Gemini CLI (configurable) and others;
# the rest are each one agent's own file.
MARKDOWN_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md")
# Created when none of MARKDOWN_FILES exists.
DEFAULT_FILE = "AGENTS.md"
# Cursor reads rules from .cursor/rules/*.mdc; ours is a file of its own,
# written only in a repo that already has a .cursor directory.
CURSOR_RULE = ".cursor/rules/hivelane.mdc"
CURSOR_FRONTMATTER = (
    "---\n"
    "description: This repo coordinates parallel AI agents through Hivelane\n"
    "alwaysApply: true\n"
    "---\n\n"
)


def block() -> str:
    """The managed section, markers included."""
    return (
        f"{BEGIN}\n"
        "## Hivelane (agent coordination)\n"
        "\n"
        "This repo coordinates parallel AI coding agents through Hivelane, so\n"
        "teammates' agents do not edit the same code at the same time.\n"
        "\n"
        "- At the start of a session, run `hivelane status`. If it fails, or does\n"
        "  not list this repo as connected, run `hivelane up -y` in the repo root\n"
        "  (if a teammate gave you a `hivelane up --room ...` line, run that one,\n"
        "  with `-y`). If `hivelane` is not installed, install it first:\n"
        f"  `{INSTALL_COMMAND}`.\n"
        "- With only MCP tools, call the hivelane `room_status` tool; if the repo\n"
        "  is not connected, call `join_room` (with the room code or link a\n"
        "  teammate gave you, if any).\n"
        "- If an edit is blocked or warned because a teammate is in the file,\n"
        "  tell the user and coordinate instead of working around it.\n"
        "- Use the room's task tools (`hivelane task list`, `task claim`,\n"
        "  `task done`, or the `task_*` MCP tools) to pick up and finish work.\n"
        f"{END}\n"
    )


@dataclass(frozen=True)
class DocChange:
    path: Path
    action: str  # "created", "added", "updated", "unchanged"


def targets(root: Path) -> list[tuple[Path, bool]]:
    """(path, is_cursor_rule) for every file `up` keeps the section in."""
    found = [(root / rel, False) for rel in MARKDOWN_FILES if (root / rel).is_file()]
    if not found:
        found.append((root / DEFAULT_FILE, False))
    # Claude Code reads CLAUDE.md, not AGENTS.md: where it is set up for this
    # repo (`up` wrote its hooks to .claude/), it gets its own copy.
    claude = root / "CLAUDE.md"
    if (root / ".claude").is_dir() and all(path != claude for path, _ in found):
        found.append((claude, False))
    if (root / ".cursor").is_dir():
        found.append((root / CURSOR_RULE, True))
    return found


def _replace_block(text: str, new_block: str) -> Optional[str]:
    """`text` with its managed section replaced, or None when it has none.
    An unterminated begin marker is treated as no section: rewriting up to
    the end of the file would delete whatever the person wrote after it."""
    start = text.find(_BEGIN_PREFIX)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        return None
    end += len(END)
    if text[end : end + 2] == "\r\n":
        end += 2
    elif text[end : end + 1] == "\n":
        end += 1
    return text[:start] + new_block + text[end:]


def render(existing: Optional[str], *, cursor_rule: bool = False) -> str:
    """The file's new contents: the section replaced in place, else appended
    after a blank line, else (no file yet) the section alone."""
    section = block()
    if existing is None:
        return (CURSOR_FRONTMATTER if cursor_rule else "") + section
    # Keep the file's own line endings.
    if "\r\n" in existing:
        section = section.replace("\n", "\r\n")
    replaced = _replace_block(existing, section)
    if replaced is not None:
        return replaced
    newline = "\r\n" if "\r\n" in existing else "\n"
    if not existing.strip():
        return section
    body = existing.rstrip("\r\n")
    return body + newline + newline + section


def apply(root: Path) -> list[DocChange]:
    """Write the section into every target. Raises OSError (the caller says
    which file and carries on with `up`)."""
    changes: list[DocChange] = []
    for path, cursor_rule in targets(root):
        existing: Optional[str]
        if path.is_file():
            # newline="" keeps \r\n as written, so the comparison below is exact.
            with path.open(encoding="utf-8-sig", newline="") as handle:
                existing = handle.read()
        else:
            existing = None
        new = render(existing, cursor_rule=cursor_rule)
        if existing is not None and new == existing:
            changes.append(DocChange(path, "unchanged"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(new)
        if existing is None:
            action = "created"
        elif _replace_block(existing, "") is not None:
            action = "updated"
        else:
            action = "added"
        changes.append(DocChange(path, action))
    return changes


def pending(root: Path) -> list[Path]:
    """Targets whose section is missing or out of date (what `up` asks about)."""
    out: list[Path] = []
    for path, cursor_rule in targets(root):
        if not path.is_file():
            out.append(path)
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                existing = handle.read()
        except (OSError, UnicodeDecodeError):
            out.append(path)
            continue
        if render(existing, cursor_rule=cursor_rule) != existing:
            out.append(path)
    return out
