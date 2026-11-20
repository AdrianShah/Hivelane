"""`hivelane task ...`: the room's shared task list from a shell.

People and agents create tasks, claim them under a lease, post notes and
complete them. A claimed task's files are reserved: teammates' agents are
blocked from editing them until the task is done, released, or its lease
lapses (server/tasks.py). Every command talks to the local daemon (POST
/tasks), which sends it to the room of the repo the current directory is in.

Imported only when a `task` command runs, so other commands do not pay for it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from typing import Any

# How often `claim --wait` asks again while nothing is ready.
CLAIM_RETRY_SECONDS = 5.0
# The daemon waits up to 5 s for the relay's reply.
REQUEST_TIMEOUT = 10.0

_REASONS = {
    "not_found": "there is no task {id} in this room",
    "not_ready": "{id} is waiting on tasks it depends on",
    "claimed": "{id} is already claimed by someone else",
    "not_claimer": "only the agent that claimed {id} can do that",
    "not_owner": "only whoever created or claimed {id} can do that",
    "closed": "{id} is already done or cancelled",
    "none_ready": "no task is ready to claim in this repo",
    "limit": "the room's task list is full; finish or cancel some tasks first",
    "rate_limited": "too many task changes at once; try again in a moment",
    "invalid": "the relay refused that request (is the title empty?)",
    "offline": "the daemon is not connected to the room right now",
    "uncoordinated": "this directory is not in a repo hivelane coordinates (run `hivelane up`)",
    "timeout": "the relay did not answer in time; try again",
    "not_sent": "the request could not be sent (an e2e room without its key?)",
}

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(raw: str) -> float:
    """'90', '90s', '30m', '1.5h' -> seconds."""
    match = _DURATION.match(raw or "")
    if match is None:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a duration like 90s, 30m or 2h")
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def register(sub: Any, port_help: str) -> None:
    """Add `hivelane task` and its subcommands to the CLI's parser."""
    task = sub.add_parser(
        "task",
        help="the room's shared task list: add, claim, finish (claimed files are reserved)",
        description="A shared task list for the repo's room. Claiming a task reserves its "
        "files: teammates' agents are blocked from editing them until the task is done, "
        "released, or its lease runs out. Your own agents (any chat or subagent) are not.",
    )
    task.add_argument("--port", type=int, default=None, help=port_help)
    # --port after the subcommand too (`hivelane task list --port 4601`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=argparse.SUPPRESS, help=port_help)
    actions = task.add_subparsers(dest="task_command", metavar="COMMAND", required=True)

    add = actions.add_parser("add", parents=[common], help="create a task")
    add.add_argument("title", help="what needs doing, in one line")
    add.add_argument("--body", default="", help="more detail")
    add.add_argument("--files", type=_csv, default=[], metavar="A,B", help="files the task covers (reserved while claimed)")
    add.add_argument("--after", type=_csv, default=[], metavar="T1,T2", help="tasks that must be done first")

    listing = actions.add_parser("list", parents=[common], help="list open and claimed tasks")
    listing.add_argument("--all", action="store_true", help="include done and cancelled tasks")
    listing.add_argument("--mine", action="store_true", help="only tasks you claimed")
    listing.add_argument("--json", action="store_true", help="print JSON")

    show = actions.add_parser("show", parents=[common], help="show one task with its notes")
    show.add_argument("task_id", metavar="ID")
    show.add_argument("--json", action="store_true", help="print JSON")

    claim = actions.add_parser("claim", parents=[common], help="claim a task (or --next: the oldest ready one)")
    claim.add_argument("task_id", nargs="?", default="", metavar="ID")
    claim.add_argument("--next", action="store_true", help="claim the oldest task that is ready")
    claim.add_argument("--wait", type=duration, default=0.0, metavar="DURATION", help="keep trying this long while nothing is ready (e.g. 10m)")
    claim.add_argument("--lease", type=duration, default=1800.0, metavar="DURATION", help="how long the claim lasts unless renewed (default 30m; your edits to its files renew it)")

    renew = actions.add_parser("renew", parents=[common], help="extend your claim's lease")
    renew.add_argument("task_id", metavar="ID")
    renew.add_argument("--lease", type=duration, default=1800.0, metavar="DURATION", help="new lease from now (default 30m)")

    note = actions.add_parser("note", parents=[common], help="post a progress note on a task")
    note.add_argument("task_id", metavar="ID")
    note.add_argument("text")

    done = actions.add_parser("done", parents=[common], help="mark your claimed task done (frees its files)")
    done.add_argument("task_id", metavar="ID")
    done.add_argument("result", nargs="?", default="", help="what came of it, e.g. a PR link")

    release = actions.add_parser("release", parents=[common], help="give up your claim (the task is open again)")
    release.add_argument("task_id", metavar="ID")

    cancel = actions.add_parser("cancel", parents=[common], help="cancel a task you created or claimed")
    cancel.add_argument("task_id", metavar="ID")
    cancel.add_argument("reason", nargs="?", default="")

    task.set_defaults(func=cmd_task)


def _call(port: int, body: dict[str, Any]) -> dict[str, Any]:
    import urllib.error

    from hivelane.cli import CommandFailed, load_service, service_errors

    svc = load_service()
    if not svc.is_running(port):
        raise CommandFailed(f"no daemon running on port {port}; run `hivelane up` first")
    with service_errors(svc, "the daemon refused"):
        try:
            reply = svc._request(port, "POST", "/tasks", body, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise CommandFailed(svc._error_detail(exc)) from None
        except OSError as exc:
            raise CommandFailed(f"could not reach the daemon on port {port} ({exc})") from None
    return reply if isinstance(reply, dict) else {}


def _refusal(reply: dict[str, Any], task_id: str) -> str:
    reason = str(reply.get("reason") or "error")
    text = _REASONS.get(reason, reason).format(id=task_id.upper() or "that task")
    return f"hivelane: {text}"


def _left(task: dict[str, Any]) -> str:
    lease = task.get("lease_until")
    if not lease:
        return ""
    minutes = max(0, int((float(lease) - time.time()) // 60))
    return f"{minutes} min left" if minutes < 120 else f"{minutes // 60} h left"


def _line(task: dict[str, Any]) -> str:
    status = task.get("status", "?")
    who = ""
    if status == "claimed":
        who = f" [{task.get('claimer')}, {_left(task)}]"
    elif status == "open" and task.get("depends_on"):
        who = f" [after {', '.join(task['depends_on'])}]"
    files = task.get("files") or []
    shown = f"  ({', '.join(files[:3])}{', …' if len(files) > 3 else ''})" if files else ""
    return f"{task.get('task_id', '?'):>4}  {status:<9} {task.get('title') or '(encrypted)'}{who}{shown}"


def _details(task: dict[str, Any]) -> list[str]:
    lines = [_line(task)]
    if task.get("body"):
        lines.append(f"      {task['body']}")
    lines.append(f"      created by {task.get('created_by')}")
    for note in task.get("notes") or []:
        label = {"done": "result", "cancel": "cancelled"}.get(note.get("kind"), "note")
        lines.append(f"      {label} from {note.get('user_id')}: {note.get('text')}")
    return lines


def cmd_task(args: argparse.Namespace) -> int:
    from hivelane.cli import resolve_port, say

    port = resolve_port(args.port)
    action = args.task_command
    cwd = os.getcwd()
    task_id = getattr(args, "task_id", "") or ""

    if action in ("list", "show"):
        reply = _call(port, {"cwd": cwd, "op": "list", "include_closed": action == "show" or args.all})
        if not reply.get("ok"):
            say(_refusal(reply, task_id), err=True)
            return 1
        tasks = list(reply.get("tasks") or [])
        if action == "show":
            wanted = task_id.strip().upper()
            wanted = wanted if wanted.startswith("T") else f"T{wanted}"
            tasks = [t for t in tasks if t.get("task_id") == wanted]
            if not tasks:
                say(_refusal({"reason": "not_found"}, wanted), err=True)
                return 1
        if getattr(args, "mine", False):
            tasks = [t for t in tasks if t.get("claimer") == reply.get("me")]
        if args.json:
            print(json.dumps(tasks[0] if action == "show" else tasks, indent=2))
            return 0
        if action == "show":
            say("\n".join(_details(tasks[0])))
            return 0
        if not tasks:
            say("hivelane: no tasks" + ("" if args.all else " open (--all shows finished ones)"))
            return 0
        say("\n".join(_line(t) for t in tasks))
        return 0

    body: dict[str, Any] = {"cwd": cwd, "op": action, "task_id": task_id}
    if action == "add":
        body.update(op="create", title=args.title, body=args.body, files=args.files, depends_on=args.after)
    elif action == "claim":
        if not task_id and not args.next:
            say("hivelane: name a task to claim, or use --next", err=True)
            return 2
        body.update(next=bool(args.next and not task_id), lease_seconds=args.lease)
    elif action == "renew":
        body.update(lease_seconds=args.lease)
    elif action == "note":
        body.update(text=args.text)
    elif action == "done":
        body.update(text=args.result)
    elif action == "cancel":
        body.update(text=args.reason)

    deadline = time.monotonic() + (args.wait if action == "claim" else 0.0)
    announced = False
    while True:
        reply = _call(port, body)
        retry = action == "claim" and reply.get("reason") in ("none_ready", "not_ready", "claimed")
        if reply.get("ok") or not retry or time.monotonic() >= deadline:
            break
        if not announced:
            say("hivelane: nothing ready yet, waiting…")
            announced = True
        time.sleep(min(CLAIM_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))

    if not reply.get("ok"):
        say(_refusal(reply, task_id), err=True)
        return 1
    task = reply.get("task") or {}
    tid = task.get("task_id", task_id.upper())
    if action == "add":
        say(f"hivelane: created {tid}: {task.get('title') or args.title}")
        if reply.get("ignored_files"):
            say(f"hivelane: not reserving ignored files: {', '.join(reply['ignored_files'])}", err=True)
    elif action == "claim":
        files = task.get("files") or []
        reserved = f"; reserved for you: {', '.join(files)}" if files else ""
        say(f"hivelane: claimed {tid}: {task.get('title') or ''} ({_left(task)}{reserved})")
    elif action == "renew":
        say(f"hivelane: renewed {tid} ({_left(task)})")
    elif action == "note":
        say(f"hivelane: noted on {tid}")
    elif action == "done":
        say(f"hivelane: {tid} is done; its files are free")
    elif action == "release":
        say(f"hivelane: released {tid}; it is open again")
    elif action == "cancel":
        say(f"hivelane: cancelled {tid}")
    return 0
