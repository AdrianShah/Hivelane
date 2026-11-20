"""`hivelane mcp`: a stdio MCP server for agents without native hooks.

Implements the MCP JSON-RPC handshake directly with the stdlib (newline-
delimited JSON over stdin/stdout). Tools talk to the local daemon over HTTP and
never crash the server: a daemon that is down becomes a tool error text.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from hivelane import __version__ as _VERSION  # type: ignore[attr-defined]

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
HTTP_TIMEOUT = 5.0
TASK_TIMEOUT = 10.0
MAX_CONTENT = 256 * 1024

AGENTS_MD = """\
## Hivelane (team coordination)

Teammates' AI agents may be editing this repo at the same time. Hivelane's MCP
tools tell you who is where:

- At the start of every task, call `declare_intent` with a one-line summary of
  what you are about to do (and the files you expect to touch).
- Before editing any file, call `check_file` with its path. If it reports a
  teammate is working there, tell the user and ask before editing.
- After editing a file, call `report_edit` with its path.
- Call `who_is_here` to see who else is in the room.
- The room has a shared task list. `task_list` shows it; `task_claim` (with
  `next: true` for the oldest ready task) claims one and reserves its files
  for you; post progress with `task_note` and finish with `task_done`. If an
  edit is blocked because a teammate's task reserves the file, pick other work.
"""


def config_snippet(agent: str, port: Optional[int]) -> str:
    args = ["mcp", "--agent", agent]
    if port is not None:
        args += ["--port", str(port)]
    return json.dumps({"mcpServers": {"hivelane": {"command": "hivelane", "args": args}}}, indent=2)


class DaemonDown(Exception):
    pass


# Loopback only: HTTP(S)_PROXY must never route a call to the daemon (and its
# token) through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DaemonClient:
    """Talks to the daemon's loopback HTTP API with the install's token
    (HIVELANE_HOME/token, read per request; hivelane.auth)."""

    def __init__(self, port: int, timeout: float = HTTP_TIMEOUT, token: Optional[str] = None) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.timeout = timeout
        # An explicit token (tests) is used as given; otherwise the file is read.
        self.token = token

    def _headers(self) -> dict[str, str]:
        from hivelane import auth

        headers = {"Content-Type": "application/json"}
        headers.update(auth.auth_headers(self.token))
        return headers

    def _request(self, path: str, body: Optional[dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        from hivelane import auth

        # File contents and the token go only to a daemon that proves it holds
        # the token, never to another process that took the port.
        try:
            proven = auth.verify_daemon(_OPENER, self.base, self.timeout, self.token)
        except OSError as exc:
            raise DaemonDown(f"hivelane daemon not reachable at {self.base} ({exc})") from None
        if not proven:
            raise DaemonDown(
                f"the process at {self.base} did not prove it is your hivelane daemon; "
                "nothing was sent. Restart it with `hivelane up` or run `hivelane doctor`"
            )
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST" if body is not None else "GET",
            headers=self._headers(),
        )
        try:
            with _OPENER.open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                from hivelane import auth

                raise DaemonDown(
                    f"the hivelane daemon at {self.base} refused this client's token "
                    f"({auth.token_path()}); run `hivelane doctor`"
                ) from None
            raise DaemonDown(f"daemon answered HTTP {exc.code} for {path}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DaemonDown(f"hivelane daemon not reachable at {self.base} ({exc})") from None

    def post_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return self._request("/events", event) or {}

    def status(self) -> Any:
        return self._request("/status")

    def tasks(self, body: dict[str, Any]) -> dict[str, Any]:
        # The daemon waits up to 5 s for the relay's reply.
        return self._request("/tasks", body, timeout=TASK_TIMEOUT) or {}


def read_text(path: Path, cap: int = MAX_CONTENT) -> Optional[str]:
    """File text, or None for missing / binary / oversized files."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > cap or b"\0" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def format_ack(ack: dict[str, Any], path: str) -> str:
    decision = ack.get("decision", "allow")
    lines = []
    others = ack.get("others") or []
    if decision == "allow" and not others:
        lines.append(f"OK: no teammate is working in {path}. Go ahead.")
    else:
        label = {"block": "BLOCK", "warn": "WARNING"}.get(decision, "NOTE")
        lines.append(f"{label}: {ack.get('reason') or 'a teammate is active in ' + path}")
        for o in others:
            ago = o.get("seconds_ago")
            when = f" {int(ago)}s ago" if isinstance(ago, (int, float)) else ""
            prompt = f' ("{o["prompt"]}")' if o.get("prompt") else ""
            lines.append(f"- {o.get('user_id', '?')} via {o.get('agent', '?')}{when}{prompt}")
        if decision == "block":
            lines.append("Do not edit this file; tell the user and coordinate first.")
        else:
            lines.append("Tell the user before editing; coordinate if the change overlaps.")
    return "\n".join(lines)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_file",
        "description": "Call BEFORE editing a file: reports whether a teammate's agent is working in it.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "file path (relative to the repo or absolute)"}},
            "required": ["path"],
        },
    },
    {
        "name": "declare_intent",
        "description": "Call at the START of a task: tells teammates what you are about to do.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "one-line summary of the task"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "files you expect to touch"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "who_is_here",
        "description": "Shows the room, the connection state and the teammates present.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_edit",
        "description": "Call AFTER editing a file so teammates see the change.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "task_list",
        "description": "Lists the room's shared tasks (open and claimed; all=true adds finished ones).",
        "inputSchema": {"type": "object", "properties": {"all": {"type": "boolean"}}},
    },
    {
        "name": "task_add",
        "description": "Adds a task to the room's shared list. Files listed are reserved for whoever claims it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "what needs doing, in one line"},
                "body": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "after": {"type": "array", "items": {"type": "string"}, "description": "task ids that must be done first"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "task_claim",
        "description": "Claims a task (task_id, or next=true for the oldest ready one). Its files are reserved for you until you finish or the lease (default 30 min, renewed by your edits) runs out.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "next": {"type": "boolean"},
                "lease_minutes": {"type": "number"},
            },
        },
    },
    {
        "name": "task_note",
        "description": "Posts a short progress note on a task you created or claimed.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["task_id", "text"],
        },
    },
    {
        "name": "task_done",
        "description": "Marks your claimed task done (with a result, e.g. a PR link) and frees its files.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "result": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "task_release",
        "description": "Gives up your claim on a task without finishing it (it is open again).",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


class McpServer:
    def __init__(self, client: DaemonClient, agent: str = "unknown", cwd: Optional[str] = None) -> None:
        from hivelane.constants import AGENT_KINDS

        self.client = client
        self.agent = agent if agent in AGENT_KINDS or agent == "unknown" else "unknown"
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.session_id = f"mcp-{uuid.uuid4().hex[:12]}"
        self.handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "check_file": self.check_file,
            "declare_intent": self.declare_intent,
            "who_is_here": self.who_is_here,
            "report_edit": self.report_edit,
            "task_list": self.task_list,
            "task_add": self.task_add,
            "task_claim": self.task_claim,
            "task_note": self.task_note,
            "task_done": self.task_done,
            "task_release": self.task_release,
        }

    # --- tools ---------------------------------------------------------------

    def _event(self, kind: str, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "agent": self.agent, "session_id": self.session_id, "cwd": self.cwd, "tool": "mcp", **extra}

    def _abs(self, path: str) -> str:
        p = Path(path)
        return str(p if p.is_absolute() else Path(self.cwd) / p)

    def check_file(self, args: dict[str, Any]) -> str:
        path = str(args["path"])
        full = self._abs(path)
        # The current content, as hook adapters send it: the daemon keeps it
        # as the "before" of this session's report_edit, which can then share
        # a real diff and the symbols changed instead of the whole file.
        old = read_text(Path(full))
        extra: dict[str, Any] = {"old_content": old} if old is not None else {}
        ack = self.client.post_event(self._event("pre_edit", file_path=full, **extra))
        return format_ack(ack, path)

    def declare_intent(self, args: dict[str, Any]) -> str:
        text = str(args["text"])
        files = [str(f) for f in (args.get("files") or [])]
        if files:
            text = f"{text}\nFiles: {', '.join(files)}"
        ack = self.client.post_event(self._event("intent", prompt=text))
        if files:
            # Also a plan: the relay checks the listed files against teammates'
            # edits and plans, and the overlaps come back on this ack.
            plan_ack = self.client.post_event(
                self._event("plan", summary=str(args["text"]), files=files, symbols=[])
            )
            if isinstance(plan_ack, dict) and plan_ack.get("decision", "allow") != "allow":
                ack = plan_ack
        if ack.get("decision", "allow") == "allow" and not ack.get("others"):
            return "Intent shared with the room. No overlapping work reported."
        return format_ack(ack, ", ".join(files) or "this task")

    def report_edit(self, args: dict[str, Any]) -> str:
        path = str(args["path"])
        full = Path(self._abs(path))
        content = read_text(full)
        if content is None and full.exists():
            return f"Not reported: {path} is binary or larger than {MAX_CONTENT // 1024} KB."
        ack = self.client.post_event(self._event("post_edit", file_path=str(full), new_content=content or ""))
        if ack.get("decision", "allow") == "allow" and not ack.get("others"):
            return f"Edit to {path} reported."
        return format_ack(ack, path)

    def who_is_here(self, args: dict[str, Any]) -> str:
        status = self.client.status() or {}
        repos = status.get("repos") if isinstance(status, dict) else None
        if not repos:
            repos = [status] if isinstance(status, dict) and status.get("room") else []
        if not repos:
            return "The hivelane daemon is running but no repo is connected. Run `hivelane up` in the repo."
        lines = []
        for r in repos:
            state = "connected" if r.get("connected") else "not connected"
            lines.append(f"Room {r.get('room', '?')} ({state}) for {r.get('root') or r.get('repo_id', '?')}")
            peers = r.get("peers") or []
            if peers:
                for p in peers:
                    lines.append(f"- {p.get('user_id', p) if isinstance(p, dict) else p}")
            else:
                lines.append("- no teammates present")
        return "\n".join(lines)

    # --- room tasks ------------------------------------------------------------

    def _task(self, op: str, **fields: Any) -> dict[str, Any]:
        return self.client.tasks({"cwd": self.cwd, "op": op, **fields})

    @staticmethod
    def _task_refusal(reply: dict[str, Any], task_id: str = "") -> str:
        from hivelane import tasks_cli

        text = tasks_cli.refusal(reply, task_id).removeprefix("hivelane: ")
        return text[:1].upper() + text[1:] + "."

    def task_list(self, args: dict[str, Any]) -> str:
        from hivelane import tasks_cli

        reply = self._task("list", include_closed=bool(args.get("all")))
        if not reply.get("ok"):
            return self._task_refusal(reply)
        tasks = reply.get("tasks") or []
        if not tasks:
            return "No tasks in this room's list."
        return "\n".join(tasks_cli.task_line(t).strip() for t in tasks)

    def task_add(self, args: dict[str, Any]) -> str:
        reply = self._task(
            "create",
            title=str(args.get("title") or ""),
            body=str(args.get("body") or ""),
            files=[str(f) for f in (args.get("files") or [])],
            depends_on=[str(t) for t in (args.get("after") or [])],
        )
        if not reply.get("ok"):
            return self._task_refusal(reply)
        return f"Created {reply['task']['task_id']}."

    def task_claim(self, args: dict[str, Any]) -> str:
        from hivelane import tasks_cli

        task_id = str(args.get("task_id") or "")
        minutes = args.get("lease_minutes")
        lease = 1800.0 if not isinstance(minutes, (int, float)) else min(28800.0, max(60.0, float(minutes) * 60))
        reply = self._task("claim", task_id=task_id, next=bool(args.get("next")) and not task_id, lease_seconds=lease)
        if not reply.get("ok"):
            return self._task_refusal(reply, task_id)
        task = reply["task"]
        files = task.get("files") or []
        reserved = f" Reserved for you: {', '.join(files)}." if files else ""
        return f"Claimed {tasks_cli.task_line(task).strip()}.{reserved} Finish with task_done."

    def task_note(self, args: dict[str, Any]) -> str:
        task_id = str(args["task_id"])
        reply = self._task("note", task_id=task_id, text=str(args.get("text") or ""))
        return f"Noted on {task_id.upper()}." if reply.get("ok") else self._task_refusal(reply, task_id)

    def task_done(self, args: dict[str, Any]) -> str:
        task_id = str(args["task_id"])
        reply = self._task("done", task_id=task_id, text=str(args.get("result") or ""))
        return f"{task_id.upper()} is done; its files are free." if reply.get("ok") else self._task_refusal(reply, task_id)

    def task_release(self, args: dict[str, Any]) -> str:
        task_id = str(args["task_id"])
        reply = self._task("release", task_id=task_id)
        return f"Released {task_id.upper()}." if reply.get("ok") else self._task_refusal(reply, task_id)

    # --- JSON-RPC ------------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        method = msg.get("method")
        msg_id = msg.get("id")
        is_request = "id" in msg
        if method is None:
            return None  # a response to us; we send no requests
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            result = self._dispatch(method, params)
        except _RpcError as exc:
            if not is_request:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": exc.message}}
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hivelane", "version": _VERSION},
                "instructions": AGENTS_MD,
            }
        if method == "ping":
            return {}
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            handler = self.handlers.get(name or "")
            if handler is None:
                raise _RpcError(-32602, f"unknown tool: {name}")
            try:
                arguments = params.get("arguments")
                text = handler(arguments if isinstance(arguments, dict) else {})
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except KeyError as exc:
                return {"content": [{"type": "text", "text": f"missing argument: {exc}"}], "isError": True}
            except DaemonDown as exc:
                return {
                    "content": [{"type": "text", "text": f"{exc}. Start it with `hivelane up`; until then, proceed without coordination."}],
                    "isError": True,
                }
            except Exception as exc:  # never crash the server
                return {"content": [{"type": "text", "text": f"hivelane error: {exc}"}], "isError": True}
        raise _RpcError(-32601, f"method not found: {method}")

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            batch = msg if isinstance(msg, list) else [msg]
            for item in batch:
                if not isinstance(item, dict):
                    continue
                reply = self.handle(item)
                if reply is not None:
                    self._write(stdout, reply)

    @staticmethod
    def _write(stdout: TextIO, obj: dict[str, Any]) -> None:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def run(port: int, agent: str) -> int:
    server = McpServer(DaemonClient(port), agent=agent)
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    try:
        server.serve(stdin, stdout)
    except KeyboardInterrupt:
        pass
    return 0
