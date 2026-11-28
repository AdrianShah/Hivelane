"""`hivelane blame` and `hivelane notes`: the CLI side of prompt blame
(hivelane/provenance.py has the mechanics).

Imported by cli.build_parser at parse time, so it has no heavy imports of its
own; provenance is loaded inside the commands. It never imports hivelane.cli:
cli hands it what it needs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Optional


def register(sub: Any, root_help: str, project_root: Callable[[Optional[str]], Path],
             failed: type[Exception]) -> None:
    """Add `hivelane blame` and `hivelane notes` to the CLI's parser.
    project_root (cli.project_root) resolves --root; `failed` is the CLI's
    CommandFailed, raised for a reported failure."""

    def run_blame(args: argparse.Namespace) -> int:
        return cmd_blame(args, project_root, failed)

    def run_notes(args: argparse.Namespace) -> int:
        return cmd_notes(args, project_root, failed)

    blame = sub.add_parser(
        "blame",
        help="which agent, prompt and task wrote these lines, and why (prompt blame)",
        description="Map lines through `git blame` to their commits, then to the provenance "
        "notes Hivelane attached to those commits (refs/notes/hivelane): the agent, the prompt "
        "excerpt, the task, when, and the person. Lines not committed yet are matched against "
        "the local journal. Needs `hivelane githook install` for commits to get notes.",
    )
    blame.add_argument("target", metavar="FILE[:LINE[-LINE]]", help="e.g. server/room.py:120-160")
    blame.add_argument("--json", action="store_true", help="print JSON instead of text")
    blame.add_argument("--root", default=None, metavar="PATH", help=root_help)
    blame.set_defaults(func=run_blame)

    notes = sub.add_parser(
        "notes",
        help="prompt-blame notes: status, or share them with a remote (explicit opt-in)",
        description="Prompt-blame notes live in refs/notes/hivelane and stay on this machine "
        "until you push them. `push` shares them (prompt excerpts included, already redacted) "
        "with everyone who can read the remote; `fetch` merges a remote's notes into yours.",
    )
    actions = notes.add_subparsers(dest="notes_command", required=True, metavar="ACTION")
    for action, text in (
        ("status", "what is recorded and attached, and the settings in force"),
        ("push", "push refs/notes/hivelane to a remote (shares prompt excerpts)"),
        ("fetch", "fetch a remote's refs/notes/hivelane and merge it into yours"),
    ):
        p = actions.add_parser(action, help=text)
        if action != "status":
            p.add_argument("remote", nargs="?", default="origin", help="the remote (default origin)")
        else:
            p.add_argument("--json", action="store_true", help="print JSON")
        p.add_argument("--root", default=None, metavar="PATH", help=root_help)
        p.set_defaults(func=run_notes)


def cmd_blame(args: argparse.Namespace, project_root: Callable[[Optional[str]], Path],
              failed: type[Exception]) -> int:
    import json

    from hivelane import provenance

    root = project_root(args.root)
    try:
        path, first, last = provenance.parse_target(args.target)
        groups = provenance.blame(root, path, first, last, cwd=Path.cwd())
    except (ValueError, provenance.GitError, OSError) as exc:
        raise failed(f"blame: {exc}") from None
    if args.json:
        print(json.dumps(groups, indent=2, ensure_ascii=False))
        return 0
    print(provenance.format_blame(groups) or f"{path}: no lines")
    if not any(g.get("provenance") for g in groups):
        hint = _install_hint(root)
        if hint:
            print(hint, file=sys.stderr)
    return 0


def _install_hint(root: Path) -> Optional[str]:
    from hivelane import githook

    try:
        if githook.installed(root).get(githook.POST_COMMIT):
            return None
    except Exception:
        return None
    return "hint: run `hivelane githook install` so your commits get prompt-blame notes"


def cmd_notes(args: argparse.Namespace, project_root: Callable[[Optional[str]], Path],
              failed: type[Exception]) -> int:
    import json

    from hivelane import provenance

    root = project_root(args.root)
    try:
        if args.notes_command == "status":
            info = provenance.status(root)
            try:
                from hivelane import githook

                info["hooks"] = githook.installed(root)
            except Exception:
                info["hooks"] = {}
            if args.json:
                print(json.dumps(info, indent=2))
                return 0
            state = "on" if info["enabled"] else "off (blame = false)"
            prompts = f"excerpts up to {info['excerpt_chars']} chars, redacted" if info["prompts"] else "none kept"
            hooks = info.get("hooks") or {}
            missing = [name for name, ok in hooks.items() if not ok]
            print(f"prompt blame: {state}; prompt text: {prompts}")
            print(f"journal: {info['journal']} ({info['pending']} edit(s) not yet in a commit)")
            print(f"notes: {info['notes']} commit(s) in {info['ref']} (local only unless you `hivelane notes push`)")
            if missing:
                print(f"hooks missing: {', '.join(missing)}; run `hivelane githook install`")
            return 0
        if args.notes_command == "push":
            print(provenance.push(root, args.remote))
            return 0
        print(provenance.fetch(root, args.remote))
        return 0
    except provenance.GitError as exc:
        raise failed(f"notes {args.notes_command}: {exc}") from None


def run_githook(action: str, argv: list[str], root: Optional[str] = None) -> int:
    """`hivelane githook post-commit|post-rewrite`, run by the hooks. Always 0:
    a commit is never failed or held by prompt blame."""
    try:
        from hivelane import provenance

        start = Path(root) if root else Path.cwd()
        if action == "post-commit":
            return provenance.post_commit(start)
        if action == "post-rewrite":
            kind = argv[0] if argv else ""
            if kind not in ("amend", "rebase"):
                return 0
            return provenance.post_rewrite(start, sys.stdin.read())
    except Exception:
        # Deliberately silent: this runs inside git's commit/rewrite hooks,
        # and a provenance failure must never fail or hold the user's commit.
        # The cost is a commit without a note, which blame reports as such.
        pass
    return 0
