"""The daemon's Phase 4 client security, against a scripted stub relay that
records every frame: redaction before anything is sent, end-to-end encrypted
rooms (no cleartext path, diff or prompt on the wire; blobs a teammate holding
the key can open; relay answers turned back into real names), and room
ownership (owner token on join, join approvals, the approval wait)."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from synergo import auth, crypto
from synergo import daemon as daemon_module
from synergo.daemon import Daemon
from synergo.events import NormalizedEvent

TIMEOUT = 5.0
PASSPHRASE = "correct horse battery staple"
ROOM = "SECRET1"
E2E = {"block_window_seconds": 20, "warn_window_seconds": 60, "e2e": True}


class ScriptedRelay:
    """Records joins and frames; answers edit_check through `on_check` and
    plans with `plan_reply`; push() sends any frame to a room's sockets."""

    def __init__(self) -> None:
        self.joins: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.frames: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.raw: list[str] = []
        self.sockets: dict[str, list[ServerConnection]] = defaultdict(list)
        self.on_check: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
        self.plan_reply: dict[str, Any] = {"type": "plan_conflicts", "conflicts": []}
        self.on_join: Optional[dict[str, Any]] = None
        self.port = 0
        self._server: Any = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def of_type(self, room: str, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.frames[room] if f.get("type") == kind]

    async def push(self, room: str, frame: dict[str, Any]) -> None:
        for ws in list(self.sockets[room]):
            await ws.send(json.dumps(frame))

    async def _handler(self, ws: ServerConnection) -> None:
        room = (ws.request.path if ws.request else "").rsplit("/", 1)[-1]
        self.sockets[room].append(ws)
        try:
            async for raw in ws:
                self.raw.append(str(raw))
                data = json.loads(raw)
                if data.get("type") == "join":
                    self.joins[room].append(data)
                    if self.on_join is not None:
                        await ws.send(json.dumps(self.on_join))
                    continue
                self.frames[room].append(data)
                if data.get("type") == "edit_check":
                    reply = {
                        "type": "edit_check_response",
                        "request_id": data["request_id"],
                        "decision": "allow",
                        "file_path": data["file_path"],
                        "others": [],
                        "reason": "",
                    }
                    if self.on_check is not None:
                        reply.update(self.on_check(data))
                    await ws.send(json.dumps(reply))
                elif data.get("type") == "plan":
                    await ws.send(json.dumps(self.plan_reply))
        finally:
            self.sockets[room].remove(ws)

    async def __aenter__(self) -> ScriptedRelay:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()


async def wait_for(predicate: Callable[[], Any], what: str, timeout: float = TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def event(kind: str, cwd: Path, file_path: Optional[Path] = None, **fields: Any) -> NormalizedEvent:
    return NormalizedEvent(
        kind=kind,  # type: ignore[arg-type]
        agent="claude-code",
        session_id="s1",
        cwd=str(cwd),
        file_path=str(file_path) if file_path else None,
        **fields,
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("SYNERGO_HOME", str(tmp_path / "home"))
    for var in ("SYNERGO_NAME", "SYNERGO_RELAY", "SYNERGO_ROOM_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(daemon_module, "_repo_ids", {})
    monkeypatch.setattr(daemon_module, "_repo_roots", {})
    monkeypatch.setattr(daemon_module, "compute_repo_id", lambda root: f"id-{root.name}")
    monkeypatch.setattr(daemon_module, "default_name", lambda root=None: "ada")
    yield


def make_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / "server").mkdir()
    return path.resolve()


SECRET_DIFF_LINE = "token = 'ghp_" + "Q" * 36 + "'"


# --- redaction on the wire ---------------------------------------------------------


def test_nothing_secret_reaches_the_relay(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(
                repo, "TEAM", relay=relay.url, name="ada", redact_patterns=[r"ACME-\d{4}"]
            )
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the join")
            await daemon.handle_event(
                event("intent", repo, prompt="use key sk-ant-api03-abcdefghijklmnop1234567890 for ACME-1234")
            )
            await daemon.handle_event(
                event("post_edit", repo, repo / "server" / "app.py", old_content="x = 1\n", new_content=f"x = 1\n{SECRET_DIFF_LINE}\n")
            )
            await daemon.handle_event(
                event("post_edit", repo, repo / ".env", old_content="A=1\n", new_content="A=2\n")
            )
            await wait_for(lambda: relay.of_type("TEAM", "activity"), "the activity")
            await daemon.aclose()

        wire = "\n".join(relay.raw)
        assert "ghp_" not in wire and "sk-ant-" not in wire and "ACME-1234" not in wire
        intent = relay.of_type("TEAM", "intent")[0]
        assert intent["prompt"] == "use key [REDACTED:anthropic-key] for [REDACTED:custom]"
        changes = {c["file_path"]: c["diff"] for c in relay.of_type("TEAM", "activity")[0]["changes"]}
        assert "[REDACTED:github-token]" in changes["server/app.py"]
        assert "--- a/server/app.py" in changes["server/app.py"]
        # The path still goes (overlaps are still detected); the content never.
        assert changes[".env"] == "[withheld: sensitive file]"

    asyncio.run(scenario())


# --- end-to-end encrypted rooms ---------------------------------------------------------


def peer_blob(kind: str, envelope: dict[str, Any]) -> str:
    return crypto.derive(PASSPHRASE, ROOM).seal(kind, envelope)


def test_an_e2e_room_sees_hashes_and_ciphertext_only(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    key = crypto.derive(PASSPHRASE, ROOM)
    rel = "server/room.py"

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            # The teammate the relay reports: edited the same file, with a task.
            sam_blob = peer_blob(
                "activity",
                {"v": 1, "prompt": "rework the join handshake", "branch": "main",
                 "changes": [{"file_path": rel, "diff": "-a\n+b", "symbols": ["Room.join"]}]},
            )
            relay.on_check = lambda data: {
                "decision": "block",
                "others": [{"user_id": "sam", "agent": "cursor", "seconds_ago": 3.0,
                            "symbols": [key.hash("Room.join")], "branch": key.hash("main"),
                            "kind": "edited", "e2e": sam_blob}],
                "reason": f"sam's cursor edited {data['file_path']} 3s ago",
                "scope": "file",
            }
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(
                repo, ROOM, relay=relay.url, name="ada", settings=E2E, room_key=PASSPHRASE
            )
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the join")
            assert relay.joins[ROOM][0]["settings"]["e2e"] is True

            await daemon.handle_event(event("intent", repo, prompt="fix the login bug"))
            ack = await daemon.handle_event(
                event("pre_edit", repo, repo / "server" / "room.py",
                      old_content="class Room:\n    def join(self):\n        pass\n",
                      edit_hints=[{"old": "pass", "new": "return 1"}])
            )
            await daemon.handle_event(
                event("post_edit", repo, repo / "server" / "room.py",
                      old_content="class Room:\n    def join(self):\n        pass\n",
                      new_content="class Room:\n    def join(self):\n        return 1\n")
            )
            await daemon.handle_event(
                event("post_edit", repo, repo / "package.json", old_content="{}\n", new_content='{"a": 1}\n')
            )
            await wait_for(lambda: relay.of_type(ROOM, "activity"), "the activity")
            await daemon.aclose()

        # The agent reads real names: path, teammate's task, symbols.
        assert ack.decision == "block"
        assert ack.file_path == rel
        assert ack.reason == f"sam's cursor edited {rel} 3s ago"
        assert ack.others[0].prompt == "rework the join handshake"
        assert ack.others[0].symbols == ["Room.join"]
        assert ack.others[0].branch == "main"
        assert ack.others[0].e2e == ""  # never handed on to the agent's hook

        # The relay saw none of it.
        wire = "\n".join(raw for raw in relay.raw if '"type":"join"' not in raw.replace(" ", ""))
        for secret in (rel, "room.py", "fix the login bug", "Room.join", "return 1", "package.json"):
            assert secret not in wire, secret
        check = relay.of_type(ROOM, "edit_check")[0]
        assert check["file_path"] == key.hash(rel) and re.fullmatch(r"h:[0-9a-f]{32}", check["file_path"])
        assert check["symbols"] == [key.hash("Room.join")]
        intent = relay.of_type(ROOM, "intent")[0]
        assert intent["prompt"] == ""
        assert key.open(intent["e2e"]) == ("intent", {"v": 1, "prompt": "fix the login bug"})
        activity = relay.of_type(ROOM, "activity")[0]
        assert activity["prompt"] is None
        by_hash = {c["file_path"]: c for c in activity["changes"]}
        assert all(c["diff"] == "" for c in activity["changes"])
        assert by_hash[key.hash(rel)]["symbols"] == [key.hash("Room.join")]
        # Always-warn files: the relay cannot tell, so nothing finer than the file.
        assert by_hash[key.hash("package.json")]["symbols"] == []
        assert by_hash[key.hash("package.json")]["line_ranges"] == []
        kind, envelope = key.open(activity["e2e"])
        assert kind == "activity" and envelope["prompt"] == "fix the login bug"
        real = {c["file_path"]: c for c in envelope["changes"]}
        assert "+        return 1" in real[rel]["diff"] and real[rel]["symbols"] == ["Room.join"]

    asyncio.run(scenario())


def test_plan_conflicts_and_feed_blobs_are_opened(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    key = crypto.derive(PASSPHRASE, ROOM)

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            relay.plan_reply = {
                "type": "plan_conflicts",
                "conflicts": [{
                    "file_path": key.hash("server/room.py"), "user_id": "sam", "agent": "cursor",
                    "kind": "planned", "summary": "", "seconds_ago": 4.0,
                    "e2e": peer_blob("plan", {"v": 1, "summary": "split Room", "files": ["server/room.py"],
                                              "symbols": [], "branch": "main"}),
                }],
            }
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(repo, ROOM, relay=relay.url, name="ada", settings=E2E, room_key=PASSPHRASE)
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the join")
            ack = await daemon.handle_event(
                event("plan", repo, summary="refactor room", files=["server/room.py"], symbols=[])
            )
            assert ack.decision == "warn"
            assert 'sam plans to edit server/room.py for "split Room"' in ack.reason

            # A teammate's activity in the feed: depgraph gets the real diff.
            conn = daemon._bindings[repo].connection
            await relay.push(ROOM, {"type": "synergo_feed", "events": [{
                "type": "activity_update", "user_id": "sam", "timestamp": 1.0, "prompt": None,
                "changes": [{"file_path": key.hash("server/util.py"), "diff": "", "symbols": []}],
                "e2e": peer_blob("activity", {"v": 1, "prompt": "add helper", "branch": "",
                                              "changes": [{"file_path": "server/util.py",
                                                           "diff": "-def f(a):\n+def f(a, b):", "symbols": ["f"]}]}),
            }]})
            await wait_for(lambda: conn._peer_tasks.get("sam") == "add helper", "the decrypted feed")
            assert conn.reveal(f"see {key.hash('server/util.py')}") == "see server/util.py"
            plan = relay.of_type(ROOM, "plan")[0]
            assert plan["summary"] == "" and plan["files"] == [key.hash("server/room.py")]
            await daemon.aclose()

    asyncio.run(scenario())


def test_a_wrong_key_reads_nothing_and_breaks_nothing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            relay.on_check = lambda data: {
                "decision": "warn",
                "others": [{"user_id": "sam", "seconds_ago": 3.0, "e2e": peer_blob("activity", {"v": 1, "prompt": "secret task", "changes": []})}],
                "reason": f"sam touched {data['file_path']}",
            }
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(repo, ROOM, relay=relay.url, name="ada", settings=E2E, room_key="not the key")
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the join")
            ack = await daemon.handle_event(event("pre_edit", repo, repo / "server" / "a.py"))
            await daemon.aclose()
        assert ack.decision == "warn"
        assert ack.others[0].prompt is None  # not garbage, not the other key's text
        assert ack.file_path == "server/a.py"
        assert ack.reason == "sam touched server/a.py"  # our own path we can always name

    asyncio.run(scenario())


def test_an_e2e_room_without_a_key_refuses_to_join(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(repo, ROOM, relay=relay.url, name="ada", settings=E2E)
            await wait_for(lambda: daemon.status()["repos"][0]["fatal"], "the refusal")
            entry = daemon.status()["repos"][0]
            assert "no room key" in entry["fatal"] and f"synergo key set {ROOM}" in entry["fatal"]
            assert (entry["e2e"], entry["e2e_key"]) == (True, False)
            ack = await daemon.handle_event(event("intent", repo, prompt="cleartext?"))
            assert ack.decision == "allow"
            await daemon.aclose()
        assert relay.joins[ROOM] == [] and relay.raw == []

    asyncio.run(scenario())


def test_a_room_turned_e2e_by_a_teammate_stops_cleartext(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(repo, "TEAM", relay=relay.url, name="ada",
                                  settings={"block_window_seconds": 20, "warn_window_seconds": 60})
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the join")
            # Never switches the room's flag off: the join leaves e2e out.
            assert "e2e" not in relay.joins["TEAM"][0]["settings"]
            await relay.push("TEAM", {"type": "room_settings", "block_window_seconds": 20,
                                      "warn_window_seconds": 60, "e2e": True, "set_by": "sam"})
            await wait_for(lambda: daemon.status()["repos"][0]["fatal"], "the stop")
            assert "e2e = true" in daemon.status()["repos"][0]["fatal"]
            await daemon.aclose()

    asyncio.run(scenario())


# --- room ownership ---------------------------------------------------------------------


def test_owner_token_join_requests_and_policy(tmp_path: Path) -> None:
    from synergo.daemon import LOOPBACK  # noqa: F401  (documented loopback-only)

    repo = make_repo(tmp_path / "repo")
    port = _free_port()

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            daemon = Daemon(port=port, batch_seconds=0.05)
            run = asyncio.create_task(daemon.run())
            headers = {auth.TOKEN_HEADER: daemon.ensure_token()}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers,
                                         timeout=5.0, trust_env=False) as http:
                await _wait_http(http)
                body = {"root": str(repo), "room": "OWNED", "relay": relay.url, "name": "ada",
                        "owner_token": "owner-secret-123"}
                assert (await http.post("/repos", json=body)).status_code == 200
                await wait_for(lambda: relay.joins["OWNED"], "the join")
                assert relay.joins["OWNED"][0]["owner_token"] == "owner-secret-123"

                await relay.push("OWNED", {"type": "join_request", "request_id": "req1",
                                           "name": "eve", "agent": "cursor", "timestamp": 1.0})
                await wait_for(lambda: (_entry(daemon)["join_requests"] or [None])[0], "the request")
                entry = _entry(daemon)
                assert entry["owner"] is True
                assert entry["join_requests"] == [{"request_id": "req1", "name": "eve", "agent": "cursor", "timestamp": 1.0}]
                assert "owner-secret-123" not in json.dumps((await http.get("/status")).json())

                reply = await http.post("/rooms/decide", json={"request_id": "req1", "approve": True})
                assert reply.json() == {"sent": True, "room": "OWNED", "approve": True}
                await wait_for(lambda: relay.of_type("OWNED", "join_decision"), "the decision")
                assert relay.of_type("OWNED", "join_decision")[0] == {
                    "type": "join_decision", "request_id": "req1", "approve": True}
                assert (await http.post("/rooms/decide", json={"request_id": "req1", "approve": False})).status_code == 404

                reply = await http.post("/rooms/policy", json={"root": str(repo), "require_approval": True})
                assert reply.status_code == 200
                await wait_for(lambda: relay.of_type("OWNED", "room_policy"), "the policy")
                assert relay.of_type("OWNED", "room_policy")[0]["require_approval"] is True
                await relay.push("OWNED", {"type": "room_policy_update", "require_approval": True})
                await wait_for(lambda: _entry(daemon)["require_approval"] is True, "the policy echo")
                assert (await http.post("/shutdown")).status_code == 200
            assert await asyncio.wait_for(run, timeout=TIMEOUT * 2) == 0

    asyncio.run(scenario())


def test_a_non_owner_cannot_set_policy_and_a_held_join_waits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            relay.on_join = {"type": "join_pending", "request_id": "r9", "reason": "the owner approves joins"}
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(repo, "HELD", relay=relay.url, name="ada")
            await wait_for(lambda: _entry(daemon)["awaiting_approval"], "the hold")
            entry = _entry(daemon)
            assert entry["connected"] is False  # admitted only once approved
            assert entry["awaiting_approval"]["reason"] == "the owner approves joins"
            assert "owner_token" not in relay.joins["HELD"][0]
            conn = daemon._bindings[repo].connection
            assert conn.set_policy(True) is False
            ack = await daemon.handle_event(event("pre_edit", repo, repo / "server" / "a.py"))
            assert ack.decision == "allow" and relay.of_type("HELD", "edit_check") == []

            await relay.push("HELD", {"type": "user_joined", "user_id": "sam", "agent": "cursor"})
            await wait_for(lambda: _entry(daemon)["connected"], "the approval")
            assert _entry(daemon)["awaiting_approval"] is None
            await daemon.aclose()

    asyncio.run(scenario())


def _entry(daemon: Daemon) -> dict[str, Any]:
    return daemon.status()["repos"][0]


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_http(http: httpx.AsyncClient) -> None:
    await asyncio.sleep(0.2)
    deadline = asyncio.get_running_loop().time() + TIMEOUT * 2
    while True:
        try:
            await http.get("/health")
            return
        except httpx.TransportError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.05)
