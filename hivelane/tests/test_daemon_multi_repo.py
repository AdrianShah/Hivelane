"""One daemon, many repos: routing by cwd, adoption from .hivelane.toml, ignore
globs, share_diffs, join settings, the admin endpoints and per-repo fatality.

A stub relay (a bare websockets server) stands in for the real one so each
test can see exactly what every room received; tests/e2e covers the real relay.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve

from hivelane import auth
from hivelane import daemon as daemon_module
from hivelane.daemon import Daemon, IgnoreRules, default_name, sanitize_name
from hivelane.events import NormalizedEvent

TIMEOUT = 5.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class StubRelay:
    """Records joins and frames per room; answers edit_check with
    `decision[room]` (default allow) and joins with `reject[room]` if set."""

    def __init__(self) -> None:
        self.joins: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.frames: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.decision: dict[str, str] = {}
        self.reject: dict[str, dict[str, Any]] = {}
        self.closed: dict[str, int] = defaultdict(int)
        self.port = 0
        self._server: Any = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def of_type(self, room: str, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames[room] if frame.get("type") == kind]

    async def _handler(self, ws: ServerConnection) -> None:
        room = (ws.request.path if ws.request else "").rsplit("/", 1)[-1]
        try:
            async for raw in ws:
                data = json.loads(raw)
                if data.get("type") == "join":
                    self.joins[room].append(data)
                    if room in self.reject:
                        await ws.send(json.dumps(self.reject[room]))
                    else:
                        # Like the real relay: the joiner hears its own
                        # user_joined, which is what admits it.
                        await ws.send(json.dumps({"type": "user_joined", "user_id": str(data.get("name") or "").lower(), "agent": data.get("agent") or "unknown"}))
                    continue
                self.frames[room].append(data)
                if data.get("type") == "edit_check":
                    decision = self.decision.get(room, "allow")
                    others = (
                        [{"user_id": "sam", "agent": "cursor", "seconds_ago": 3.0}]
                        if decision != "allow"
                        else []
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "edit_check_response",
                                "request_id": data["request_id"],
                                "decision": decision,
                                "file_path": data["file_path"],
                                "others": others,
                                "reason": f"sam is in {data['file_path']}" if others else "",
                            }
                        )
                    )
        finally:
            self.closed[room] += 1

    async def __aenter__(self) -> StubRelay:
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


def event(kind: str, cwd: Path, file_path: Path | None = None, **fields: Any) -> NormalizedEvent:
    return NormalizedEvent(
        kind=kind,  # type: ignore[arg-type]
        agent="claude-code",
        session_id="s1",
        cwd=str(cwd),
        file_path=str(file_path) if file_path else None,
        **fields,
    )


def make_repo(path: Path, project: str | None = None) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / "src").mkdir()
    if project is not None:
        (path / ".hivelane.toml").write_text(project, encoding="utf-8")
    return path.resolve()


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No personal config, no environment overrides, no git subprocesses."""
    monkeypatch.setenv("HIVELANE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HIVELANE_NAME", raising=False)
    monkeypatch.delenv("HIVELANE_RELAY", raising=False)
    monkeypatch.setattr(daemon_module, "_repo_ids", {})
    monkeypatch.setattr(daemon_module, "_repo_roots", {})
    monkeypatch.setattr(daemon_module, "compute_repo_id", lambda root: f"id-{root.name}")
    monkeypatch.setattr(daemon_module, "default_name", lambda root=None: "ada")
    yield


# --- routing ---------------------------------------------------------------------


def test_events_route_to_their_own_repos_room(tmp_path: Path) -> None:
    one = make_repo(tmp_path / "one")
    two = make_repo(tmp_path / "two")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    async def scenario() -> None:
        async with StubRelay() as relay:
            relay.decision["ROOMA"] = "block"
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(one, "ROOMA", relay=relay.url, name="ada")
            await daemon.register(two, "ROOMB", relay=relay.url, name="ada")
            await wait_for(
                lambda: all(r["connected"] for r in daemon.status()["repos"]), "both rooms"
            )

            # From a subdirectory of repo one: keyed on its root, checked in its room.
            blocked = await daemon.handle_event(
                event("pre_edit", one / "src", one / "src" / "app.py")
            )
            assert blocked.decision == "block"
            assert blocked.file_path == "src/app.py"
            allowed = await daemon.handle_event(
                event("pre_edit", two, two / "src" / "app.py")
            )
            assert allowed.decision == "allow"
            await daemon.handle_event(event("intent", two, prompt="tidy repo two"))
            await wait_for(lambda: relay.of_type("ROOMB", "intent"), "repo two's intent")

            assert [f["file_path"] for f in relay.of_type("ROOMA", "edit_check")] == ["src/app.py"]
            assert [f["file_path"] for f in relay.of_type("ROOMB", "edit_check")] == ["src/app.py"]
            assert [f["repo_id"] for f in relay.of_type("ROOMA", "edit_check")] == ["id-one"]
            assert relay.of_type("ROOMB", "intent")[0]["repo_id"] == "id-two"
            assert relay.of_type("ROOMA", "intent") == []

            # A repo nobody registered and with no config fails open, silently.
            ack = await daemon.handle_event(
                event("pre_edit", elsewhere, elsewhere / "x.py")
            )
            assert ack.decision == "allow"
            assert sum(len(frames) for frames in relay.frames.values()) == 3

            status = daemon.status()
            assert status["mode"] == "multi-repo"
            assert {(r["root"], r["room"]) for r in status["repos"]} == {
                (str(one), "ROOMA"),
                (str(two), "ROOMB"),
            }
            # The single-room shape only appears when there is exactly one repo.
            assert "connected" not in status
            await daemon.aclose()

    asyncio.run(scenario())


def test_repos_in_one_room_share_one_connection(tmp_path: Path) -> None:
    """The relay admits a name once per room: two repos joining the same room
    as the same person must share a socket, not collide on the name."""
    one = make_repo(tmp_path / "one")
    two = make_repo(tmp_path / "two")

    async def scenario() -> None:
        async with StubRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(one, "TEAM", relay=relay.url, name="ada")
            await daemon.register(two, "TEAM", relay=relay.url, name="ada")
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the room")
            for repo in (one, two):
                await daemon.handle_event(
                    event("post_edit", repo, repo / "src" / "x.py", old_content="a\n", new_content="b\n")
                )
            await wait_for(lambda: len(relay.of_type("TEAM", "activity")) == 2, "two activities")
            assert len(relay.joins["TEAM"]) == 1
            assert sorted(a["repo_id"] for a in relay.of_type("TEAM", "activity")) == ["id-one", "id-two"]

            # Dropping one repo keeps the shared connection up for the other.
            assert await daemon.unregister(one) is True
            assert [r["root"] for r in daemon.status()["repos"]] == [str(two)]
            assert daemon.status()["repos"][0]["connected"]
            # The relay only notices a close asynchronously, so prove the
            # socket is still up by using it: a later edit in the other repo
            # still arrives, over the same single join.
            await daemon.handle_event(
                event("post_edit", two, two / "src" / "y.py", old_content="a\n", new_content="b\n")
            )
            await wait_for(lambda: len(relay.of_type("TEAM", "activity")) == 3, "a third activity")
            assert len(relay.joins["TEAM"]) == 1
            assert relay.closed["TEAM"] == 0
            await daemon.aclose()
            await wait_for(lambda: relay.closed["TEAM"] == 1, "the socket to close")

    asyncio.run(scenario())


def test_registered_repo_wins_over_single_room_default(tmp_path: Path) -> None:
    """`hivelane join` mode still takes every event, except those from a repo
    registered explicitly, which go to that repo's own room."""
    repo = make_repo(tmp_path / "repo")
    other = make_repo(tmp_path / "other")

    async def scenario() -> None:
        async with StubRelay() as relay:
            daemon = Daemon("MAIN", "ada", server=relay.url)
            await daemon.start()
            await daemon.register(repo, "SIDE", relay=relay.url, name="ada")
            await wait_for(
                lambda: all(r["connected"] for r in daemon.status()["repos"]), "both rooms"
            )
            await daemon.handle_event(event("intent", repo, prompt="side work"))
            await daemon.handle_event(event("intent", other, prompt="main work"))
            await wait_for(
                lambda: relay.of_type("SIDE", "intent") and relay.of_type("MAIN", "intent"),
                "both intents",
            )
            assert relay.of_type("SIDE", "intent")[0]["prompt"] == "side work"
            assert relay.of_type("MAIN", "intent")[0]["prompt"] == "main work"
            assert daemon.status()["mode"] == "single-room"
            await daemon.aclose()

    asyncio.run(scenario())


# --- adoption from .hivelane.toml ----------------------------------------------------


def test_repo_is_adopted_from_its_project_config(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with StubRelay() as relay:
            repo = make_repo(
                tmp_path / "repo",
                f'room = "TEAMX"\nrelay = "{relay.url}"\n'
                "block_window_seconds = 5\nwarn_window_seconds = 30\n"
                'ignore = ["*.lock", "generated/**"]\nshare_diffs = false\n',
            )
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()

            # The first event registers the repo; queued until the socket is up.
            ack = await daemon.handle_event(event("intent", repo / "src", prompt="ship it"))
            assert ack.decision == "allow"
            await wait_for(lambda: relay.of_type("TEAMX", "intent"), "the intent")
            # The queued intent can reach the relay before the daemon has read
            # its user_joined, and an unadmitted daemon allows every edit.
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "admission")
            join = relay.joins["TEAMX"][0]
            assert join["name"] == "ada"
            assert join["repo_id"] == "id-repo"
            assert join["settings"] == {"block_window_seconds": 5.0, "warn_window_seconds": 30.0}

            # Ignored files are neither checked nor reported.
            relay.decision["TEAMX"] = "block"
            for path in (repo / "yarn.lock", repo / "generated" / "api" / "client.py"):
                ack = await daemon.handle_event(event("pre_edit", repo, path))
                assert ack.decision == "allow"
                await daemon.handle_event(event("post_edit", repo, path, old_content="", new_content="x\n"))
            assert relay.of_type("TEAMX", "edit_check") == []

            # share_diffs = false: the path goes out (overlaps still work), the diff does not.
            ack = await daemon.handle_event(event("pre_edit", repo, repo / "src" / "a.py", old_content="a\n"))
            assert ack.decision == "block"
            await daemon.handle_event(event("post_edit", repo, repo / "src" / "a.py", new_content="b\n"))
            await wait_for(lambda: relay.of_type("TEAMX", "activity"), "the activity")
            (activity,) = relay.of_type("TEAMX", "activity")
            # Symbol names and line numbers still go out (they drive overlap
            # detection); the content of the change does not.
            assert activity["changes"] == [
                {"file_path": "src/a.py", "diff": "", "symbols": ["<module>"], "line_ranges": [[1, 1]]}
            ]

            entry = daemon.status()["repos"][0]
            assert entry["auto"] is True
            assert entry["root"] == str(repo)
            assert entry["share_diffs"] is False
            assert entry["ignore"] == ["*.lock", "generated/**"]
            await daemon.aclose()

    asyncio.run(scenario())


def test_unconfigured_directories_are_cached_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = daemon_module.discover_repo

    def counting(cwd: str) -> Any:
        calls.append(cwd)
        return real(cwd)

    monkeypatch.setattr(daemon_module, "discover_repo", counting)
    plain = make_repo(tmp_path / "plain")

    # An injected clock for the daemon module only (asyncio keeps the real
    # one), so the cache window is crossed by advancing it, not by sleeping.
    real_time = daemon_module.time

    class Clock:
        now = real_time.monotonic()

        def monotonic(self) -> float:
            return self.now

        def __getattr__(self, name: str) -> Any:
            return getattr(real_time, name)

    clock = Clock()
    monkeypatch.setattr(daemon_module, "time", clock)

    async def scenario() -> None:
        daemon = Daemon()
        await daemon.start()
        for _ in range(3):
            await daemon.handle_event(event("pre_edit", plain, plain / "a.py"))
        assert len(calls) == 1
        clock.now += daemon_module.NEGATIVE_CACHE_SECONDS - 1
        await daemon.handle_event(event("pre_edit", plain, plain / "a.py"))
        assert len(calls) == 1
        clock.now += 2
        await daemon.handle_event(event("pre_edit", plain, plain / "a.py"))
        assert len(calls) == 2
        await daemon.aclose()

    asyncio.run(scenario())


def test_single_room_mode_does_not_adopt_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cwd: str) -> Any:
        raise AssertionError("single-room mode must not look for project configs")

    monkeypatch.setattr(daemon_module, "discover_repo", boom)
    repo = make_repo(tmp_path / "repo", 'room = "OTHER"\n')

    async def scenario() -> list[dict[str, Any]]:
        daemon = Daemon("MAIN", "ada")
        await daemon.handle_event(event("intent", repo, prompt="go"))
        return [json.loads(daemon._outbox.get_nowait()) for _ in range(daemon._outbox.qsize())]

    assert [m["type"] for m in asyncio.run(scenario())] == ["intent"]


# --- fatal rejections ------------------------------------------------------------------


def test_a_rejected_repo_does_not_take_the_others_down(tmp_path: Path) -> None:
    good = make_repo(tmp_path / "good")
    bad = make_repo(tmp_path / "bad")

    async def scenario() -> None:
        async with StubRelay() as relay:
            relay.reject["BAD"] = {"type": "room_rejected", "reason": "Room not found"}
            daemon = Daemon()
            await daemon.start()
            await daemon.register(good, "GOOD", relay=relay.url, name="ada")
            await daemon.register(bad, "BAD", relay=relay.url, name="ada")

            def entry(root: Path) -> dict[str, Any]:
                return next(r for r in daemon.status()["repos"] if r["root"] == str(root))

            await wait_for(lambda: entry(bad)["fatal"] and entry(good)["connected"], "the verdicts")
            assert "Room not found" in entry(bad)["fatal"]
            assert entry(bad)["connected"] is False
            ack = await daemon.handle_event(event("pre_edit", bad, bad / "x.py"))
            assert ack.decision == "allow"
            await daemon.handle_event(event("intent", good, prompt="still here"))
            await wait_for(lambda: relay.of_type("GOOD", "intent"), "the good room's intent")

            # Registering it again retries with a fresh connection.
            relay.reject.clear()
            await daemon.register(bad, "BAD", relay=relay.url, name="ada")
            await wait_for(lambda: entry(bad)["connected"], "the retried room")
            assert entry(bad)["fatal"] is None
            await daemon.aclose()

    asyncio.run(scenario())


def test_single_room_mode_still_exits_on_a_fatal_rejection(capsys: pytest.CaptureFixture[str]) -> None:
    async def scenario() -> int:
        async with StubRelay() as relay:
            relay.reject["MAIN"] = {"type": "name_rejected", "reason": "Name taken"}
            daemon = Daemon("MAIN", "ada", server=relay.url, port=free_port())
            return await asyncio.wait_for(daemon.run(), timeout=TIMEOUT * 2)

    assert asyncio.run(scenario()) == 1
    assert "Name taken" in capsys.readouterr().err


def test_close_flushes_debounced_edits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    async def scenario() -> None:
        async with StubRelay() as relay:
            daemon = Daemon(batch_seconds=30)
            await daemon.start()
            await daemon.register(repo, "TEAM", relay=relay.url, name="ada")
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the room")
            await daemon.handle_event(
                event("post_edit", repo, repo / "src" / "a.py", old_content="a\n", new_content="b\n")
            )
            await daemon.aclose()
            await wait_for(lambda: relay.closed["TEAM"] == 1, "the socket to close")
            assert [c["file_path"] for c in relay.of_type("TEAM", "activity")[0]["changes"]] == ["src/a.py"]

    asyncio.run(scenario())


# --- HTTP admin API ---------------------------------------------------------------------


def test_admin_endpoints_register_remove_and_shut_down(tmp_path: Path) -> None:
    port = free_port()

    async def scenario() -> None:
        async with StubRelay() as relay:
            repo = make_repo(tmp_path / "repo", f'room = "TEAM"\nrelay = "{relay.url}"\n')
            daemon = Daemon(port=port)
            run = asyncio.create_task(daemon.run())
            base = f"http://127.0.0.1:{port}"
            headers = {auth.TOKEN_HEADER: daemon.ensure_token()}
            async with httpx.AsyncClient(
                base_url=base, timeout=5.0, trust_env=False, headers=headers
            ) as http:
                await wait_for_http(http)
                body = {
                    "root": str(repo),
                    "room": "TEAM",
                    "relay": relay.url,
                    "name": "ada",
                    "agent": "codex",
                    "settings": {"block_window_seconds": 10, "warn_window_seconds": 40},
                    "ignore": ["dist/"],
                    "share_diffs": True,
                }
                response = await http.post("/repos", json=body)
                assert response.status_code == 200, response.text
                entry = response.json()
                assert (entry["root"], entry["room"], entry["name"], entry["agent"]) == (
                    str(repo), "TEAM", "ada", "codex",
                )
                await wait_for(lambda: relay.joins["TEAM"], "the join")
                assert relay.joins["TEAM"][0]["settings"] == {
                    "block_window_seconds": 10.0, "warn_window_seconds": 40.0,
                }

                # Re-registering unchanged does not bounce the connection.
                assert (await http.post("/repos", json=body)).status_code == 200
                status = (await http.get("/status")).json()
                assert status["version"] and status["pid"] and status["port"] == port
                assert [r["root"] for r in status["repos"]] == [str(repo)]
                assert status["room"] == "TEAM"  # exactly one repo: the old shape too
                assert len(relay.joins["TEAM"]) == 1

                # Out-of-bounds settings and cross-site requests are refused.
                bad = dict(body, settings={"block_window_seconds": 9999})
                assert (await http.post("/repos", json=bad)).status_code == 422
                forged = await http.post("/shutdown", headers={"Origin": "https://evil.example"})
                assert forged.status_code == 403

                removed = await http.post("/repos/remove", json={"root": str(repo)})
                assert removed.json() == {"removed": True}
                assert (await http.post("/repos/remove", json={"root": str(repo)})).json() == {
                    "removed": False
                }
                # Disconnected stays disconnected, .hivelane.toml notwithstanding.
                ack = await http.post(
                    "/events",
                    json={"kind": "intent", "session_id": "s", "cwd": str(repo), "prompt": "x"},
                )
                assert ack.json()["decision"] == "allow"
                assert (await http.get("/status")).json()["repos"] == []

                assert (await http.post("/shutdown")).json()["stopping"] is True
            assert await asyncio.wait_for(run, timeout=TIMEOUT * 2) == 0
            assert relay.closed["TEAM"] == 1

    asyncio.run(scenario())


async def wait_for_http(http: httpx.AsyncClient) -> None:
    # A head start: Windows retries a refused loopback connect for seconds.
    await asyncio.sleep(0.2)
    deadline = asyncio.get_running_loop().time() + TIMEOUT * 2
    while True:
        try:
            await http.get("/status")
            return
        except httpx.TransportError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.05)


def test_stop_is_safe_from_another_thread(tmp_path: Path) -> None:
    import threading

    port = free_port()
    daemon = Daemon(port=port)

    async def scenario() -> int:
        run = asyncio.create_task(daemon.run())
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            await wait_for_http(http)
        threading.Thread(target=daemon.stop).start()
        return await asyncio.wait_for(run, timeout=TIMEOUT * 2)

    assert asyncio.run(scenario()) == 0


# --- names and globs ------------------------------------------------------------------


def test_derived_names_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    # default_name here is the real one, imported before the fixture's stub.
    assert sanitize_name("  Ada   Lovelace <ada@x.org> ") == "Ada Lovelace adaxorg"
    assert sanitize_name("Zoë") == "Zo"
    assert len(sanitize_name("x" * 100)) == daemon_module.NAME_MAX_LENGTH

    monkeypatch.setattr(daemon_module, "_git", lambda root, *args: "Grace  Hopper!")
    assert default_name(Path(".")) == "Grace Hopper"
    monkeypatch.setattr(daemon_module, "_git", lambda root, *args: "")
    monkeypatch.setattr(daemon_module.getpass, "getuser", lambda: "ghopper$")
    assert default_name(Path(".")) == "ghopper"


@pytest.mark.parametrize(
    ("patterns", "path", "ignored"),
    [
        (["*.lock"], "yarn.lock", True),
        (["*.lock"], "web/yarn.lock", True),
        (["*.lock"], "lockfile.py", False),
        (["build/**"], "build/out/app.js", True),
        (["build/**"], "src/build/app.js", False),
        (["build/"], "src/build/app.js", True),
        (["**/*.min.js"], "app.min.js", True),
        (["**/*.min.js"], "static/js/app.min.js", True),
        (["generated/*.py"], "generated/a.py", True),
        (["generated/*.py"], "generated/sub/a.py", False),
        (["vendor"], "vendor/lib/x.go", True),
        (["/docs"], "docs/a.md", True),
        (["/docs"], "src/docs/a.md", False),
        (["secret?.txt"], "secret1.txt", True),
        (["[abc].py"], "b.py", True),
        (["[!abc].py"], "b.py", False),
        (["src\\gen\\*"], "src/gen/x.ts", True),
        ([], "a.py", False),
        (["", "  ", "# a comment"], "a.py", False),
    ],
)
def test_ignore_globs(patterns: list[str], path: str, ignored: bool) -> None:
    assert IgnoreRules(patterns).matches(path) is ignored
