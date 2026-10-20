"""Daemon robustness: admission-based `connected`, caps on what goes to the
relay, frames that must not kill the link, send pacing, plan reply pairing,
e2e on shared connections, scoped room keys, signals, and the small fixes
around them (watch, mcp, githook, systemd unit, CLI)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from synergo import config as cfg
from synergo import daemon as daemon_module
from synergo.daemon import (
    MAX_DIFF_BYTES,
    MAX_FRAME_BYTES,
    MAX_TEXT_BYTES,
    SEND_SPACING,
    Daemon,
    RoomConnection,
    truncate_utf8,
)
from synergo.events import NormalizedEvent
from synergo.protocol import ActivityMessage, EditCheckRequest, FileChange, PlanConflicts
from synergo.tests.test_daemon_security import (  # noqa: F401  (isolated: fixture)
    E2E,
    ROOM,
    ScriptedRelay,
    event,
    isolated,
    make_repo,
    wait_for,
)

PLAIN = {"block_window_seconds": 20, "warn_window_seconds": 60}


def _conn() -> RoomConnection:
    return RoomConnection("TEAM42", "ada", batch_seconds=30)


def _drain_all(conn: RoomConnection) -> list[dict[str, Any]]:
    out = []
    for queue in (conn._priority_outbox, conn._outbox):
        while not queue.empty():
            out.append(json.loads(queue.get_nowait()))
    return out


# --- connected means admitted (the CI flake) -----------------------------------------


def test_connected_waits_for_the_relays_answer_and_ends_with_fatal() -> None:
    async def scenario() -> None:
        conn = _conn()
        conn._socket = object()  # the join was written; nothing heard yet
        assert not conn.connected and not conn.live
        conn._handle_frame(json.dumps({"type": "join_pending", "request_id": "r"}))
        assert not conn.connected
        conn._handle_frame(json.dumps({"type": "user_joined", "user_id": "ada"}))
        assert conn.connected and conn.live and conn.connected_since is not None
        conn._handle_frame(json.dumps({"type": "room_rejected", "reason": "gone"}))
        assert conn.fatal and conn.connected is False and conn.live is False

    asyncio.run(scenario())


def test_a_refused_join_never_reads_as_connected() -> None:
    conn = _conn()
    conn._socket = object()
    conn._handle_frame(json.dumps({"type": "name_rejected", "reason": "taken"}))
    assert conn.fatal and not conn.connected


def test_a_dropped_link_fails_waiting_hooks_open_at_once(tmp_path: Path) -> None:
    async def scenario() -> float:
        daemon = Daemon("TEAM42", "ada", port=4699, edit_check_timeout=5.0)
        daemon.connected = True
        task = asyncio.create_task(daemon.handle_event(event("pre_edit", tmp_path, tmp_path / "a.py")))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        daemon.connection._fail_open_waiters()
        ack = await task
        assert ack.decision == "allow"
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1.0


# --- size caps and the event loop ------------------------------------------------------


def test_huge_diffs_are_capped_and_huge_inputs_go_path_only(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        daemon = Daemon("TEAM42", "ada", port=4699, batch_seconds=30)
        old = "".join(f"line {i}\n" for i in range(20_000))  # ~200 KB
        new = old.replace("line 1", "LINE 1")
        await daemon.handle_event(
            event("post_edit", tmp_path, tmp_path / "big.txt", old_content=old, new_content=new)
        )
        huge = "x" * (600 * 1024)
        await daemon.handle_event(
            event("post_edit", tmp_path, tmp_path / "huge.txt", old_content="", new_content=huge)
        )
        batch = {key[1]: change for key, change in daemon._batch.items()}
        return batch["big.txt"].diff, batch["huge.txt"].diff

    big, huge = asyncio.run(scenario())
    assert 0 < len(big.encode()) <= MAX_DIFF_BYTES and big.endswith("[... truncated by synergo]")
    assert huge == ""


def test_a_huge_prompt_is_capped_and_activity_frames_fit(tmp_path: Path) -> None:
    async def scenario() -> list[dict[str, Any]]:
        daemon = Daemon("TEAM42", "ada", port=4699, batch_seconds=30)
        await daemon.handle_event(event("intent", tmp_path, prompt="p" * (600 * 1024)))
        assert len(daemon._last_prompt.encode()) <= MAX_TEXT_BYTES
        for n in range(12):
            daemon.connection._batch[("", f"f{n}.py")] = FileChange(
                file_path=f"f{n}.py", diff="d" * MAX_DIFF_BYTES
            )
        daemon.flush_batch()
        frames = []
        for queue in (daemon._priority_outbox, daemon._outbox):
            while not queue.empty():
                raw = queue.get_nowait()
                assert len(raw.encode()) <= MAX_FRAME_BYTES
                frames.append(json.loads(raw))
        return frames

    frames = asyncio.run(scenario())
    activities = [f for f in frames if f["type"] == "activity"]
    assert len(activities) > 1
    assert sorted(c["file_path"] for a in activities for c in a["changes"]) == sorted(f"f{n}.py" for n in range(12))


def test_truncate_utf8_respects_character_boundaries() -> None:
    text = "é" * 100
    cut = truncate_utf8(text, 51, marker="")
    assert cut == "é" * 25
    assert truncate_utf8("short", 100) == "short"


def test_a_lone_surrogate_prompt_does_not_poison_later_sends(tmp_path: Path) -> None:
    raw = json.loads(json.dumps({"kind": "intent", "agent": "claude-code", "session_id": "s",
                                 "cwd": str(tmp_path), "prompt": "fix \ud83d"}))

    async def scenario() -> list[dict[str, Any]]:
        daemon = Daemon("TEAM42", "ada", port=4699, batch_seconds=30)
        await daemon.handle_event(NormalizedEvent.model_validate(raw))
        await daemon.handle_event(
            event("post_edit", tmp_path, tmp_path / "a.py", old_content="a\n", new_content="b\n")
        )
        daemon.flush_batch()
        return _drain_all(daemon.connection)

    kinds = [m["type"] for m in asyncio.run(scenario())]
    assert kinds == ["intent", "activity"]


def test_the_old_content_cache_skips_huge_files() -> None:
    conn = _conn()
    conn._remember_old("s", "big.bin", "x" * (daemon_module.symbols.MAX_SOURCE_BYTES + 1))
    conn._remember_old("s", "a.py", "abc")
    assert list(conn._old_content) == [("s", "a.py")] and conn._old_content_chars == 3


# --- frames that must not kill the link ----------------------------------------------------


def test_poison_frames_are_logged_and_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    conn = _conn()
    conn._handle_frame(json.dumps({"type": "user_left", "user_id": ["a", "b"]}))
    conn._handle_frame(json.dumps({"type": "user_joined", "users": [["x"], 5, "sam"]}))
    conn._handle_frame(json.dumps({"type": "rate_limited", "retry_after": "soon"}))
    conn._learn({"files": 5, "changes": "nope", "symbols": {"a": 1}})  # no key: no-op
    conn.e2e_key = daemon_module.crypto.derive("k" * 32, "TEAM42") if daemon_module.crypto.available() else None
    if conn.e2e_key is not None:
        conn._learn({"files": 5, "changes": [{"symbols": 3}], "symbols": "x"})
    assert conn._peers == {"sam"} and conn.retry_after == 0.0

    def boom(_data: dict[str, Any]) -> None:
        raise TypeError("unexpected shape")

    conn._dispatch_frame = boom  # type: ignore[method-assign]
    conn._handle_frame(json.dumps({"type": "activity_update"}))
    assert "could not be handled" in capsys.readouterr().err


# --- pacing and stale payloads ---------------------------------------------------------------


def test_activity_sends_are_spaced_but_edit_checks_are_not() -> None:
    async def scenario() -> tuple[str, str, float]:
        conn = _conn()
        conn._last_sent["activity"] = time.monotonic()
        conn._send(ActivityMessage(user_id="ada", changes=[FileChange(file_path="a.py", diff="")]))
        conn._send(EditCheckRequest(request_id="r1", file_path="b.py"))
        first = json.loads(await conn._next_payload())["type"]
        started = time.monotonic()
        second = json.loads(await asyncio.wait_for(conn._next_payload(), timeout=2.0))["type"]
        return first, second, time.monotonic() - started

    first, second, waited = asyncio.run(scenario())
    assert (first, second) == ("edit_check", "activity")
    assert waited >= SEND_SPACING - 0.2


def test_an_abandoned_edit_check_is_not_sent_late() -> None:
    async def scenario() -> str:
        conn = _conn()
        conn._send(EditCheckRequest(request_id="old", file_path="a.py"))
        conn._send(EditCheckRequest(request_id="new", file_path="a.py"))
        conn._abandon("old")
        return json.loads(await conn._next_payload())["request_id"]

    assert asyncio.run(scenario()) == "new"


# --- plan replies -----------------------------------------------------------------------------


def test_plan_replies_pair_by_request_id_or_fifo() -> None:
    async def scenario() -> None:
        conn = _conn()
        loop = asyncio.get_running_loop()
        first, second = loop.create_future(), loop.create_future()
        conn._plan_waiters["a"] = ("s1", first, time.monotonic())
        conn._plan_waiters["b"] = ("s2", second, time.monotonic())
        conn._on_plan_conflicts(PlanConflicts(), "b")
        assert second.done() and not first.done()
        conn._on_plan_conflicts(PlanConflicts())  # no echo: oldest waiting
        assert first.done() and not conn._plan_waiters
        # Overdue plans (no reply ever came) are pruned.
        stale = loop.create_future()
        conn._plan_waiters["c"] = ("s3", stale, time.monotonic() - 3600)
        conn._prune_plan_waiters()
        assert not conn._plan_waiters and stale.cancelled()

    asyncio.run(scenario())


# --- e2e on a shared connection, scoped keys -------------------------------------------------


@pytest.mark.skipif(not daemon_module.crypto.available(), reason="needs the e2e extra")
def test_a_shared_connection_never_carries_an_e2e_repo_in_cleartext(tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a")
    b = make_repo(tmp_path / "b")

    async def scenario() -> tuple[Any, list[str]]:
        async with ScriptedRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(a, ROOM, relay=relay.url, name="ada", settings=PLAIN)
            await wait_for(lambda: daemon.status()["repos"][0]["connected"], "the join")
            await daemon.register(b, ROOM, relay=relay.url, name="ada", settings=E2E, room_key=None)
            await daemon.handle_event(event("intent", b, prompt="top secret plan"))
            fatal = daemon.status()["repos"][1]["fatal"]
            await asyncio.sleep(0.2)
            await daemon.aclose()
            return fatal, list(relay.raw)

    fatal, wire = asyncio.run(scenario())
    assert fatal and "synergo key set" in fatal
    assert not any("top secret" in raw for raw in wire)


@pytest.mark.skipif(not daemon_module.crypto.available(), reason="needs the e2e extra")
def test_a_second_repo_with_a_different_key_is_refused(tmp_path: Path) -> None:
    a = make_repo(tmp_path / "a")
    b = make_repo(tmp_path / "b")

    async def scenario() -> None:
        async with ScriptedRelay() as relay:
            daemon = Daemon(batch_seconds=0.05)
            await daemon.start()
            await daemon.register(a, ROOM, relay=relay.url, name="ada", settings=E2E, room_key="k1" * 16)
            with pytest.raises(ValueError, match="different room key"):
                await daemon.register(b, ROOM, relay=relay.url, name="ada", settings=E2E, room_key="k2" * 16)
            await daemon.aclose()

    asyncio.run(scenario())


def test_the_bare_env_key_is_one_rooms_and_scoped_keys_win(monkeypatch: pytest.MonkeyPatch) -> None:
    config = cfg.SynergoConfig(room="TEAM-B", room_keys=(("TEAM-B", "from-file"),))
    monkeypatch.setenv(cfg.ROOM_KEY_ENV, "room-a-key")
    assert config.room_key() == "room-a-key"  # the CLI, in its own repo
    assert config.room_key(generic_env=False) == "from-file"  # the daemon
    monkeypatch.setenv("SYNERGO_ROOM_KEY_TEAM_B", "scoped")
    assert config.room_key(generic_env=False) == "scoped" == config.room_key()
    assert config.room_key_source() == "SYNERGO_ROOM_KEY_TEAM_B"


# --- repo identity, signals ---------------------------------------------------------------------


def test_repo_id_is_recomputed_after_git_remote_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_repo(tmp_path / "r")
    config = root / ".git" / "config"
    config.write_bytes(b"[core]\n")
    ids = iter(["", "with-origin"])
    monkeypatch.setattr(daemon_module, "compute_repo_id", lambda _root: next(ids))
    assert daemon_module.repo_id_for(root) == ""
    assert daemon_module.repo_id_for(root) == ""  # cached
    config.write_text('[remote "origin"]\n\turl = git@x:y/z\n', encoding="utf-8")
    later = time.time() + 5
    os.utime(config, (later, later))
    assert daemon_module.cached_repo_id(root) is None
    assert daemon_module.repo_id_for(root) == "with-origin"


def test_a_signal_stops_the_daemon_gracefully() -> None:
    daemon = Daemon()
    with daemon._capture_signals():
        signal.raise_signal(signal.SIGINT)
    assert daemon._stopping.is_set()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


# --- systemd, watch, mcp, githook, CLI --------------------------------------------------------


def test_systemd_working_directory_is_a_bare_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from synergo import service

    monkeypatch.setenv("SYNERGO_HOME", str(tmp_path / "my 100% home"))
    unit = service.systemd_unit(4600)
    line = next(line for line in unit.splitlines() if line.startswith("WorkingDirectory="))
    assert line == f"WorkingDirectory={tmp_path / 'my 100%% home'}"
    assert "network-online" not in unit


def test_watch_keeps_the_old_text_when_a_read_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from synergo import watch

    (tmp_path / "a.py").write_bytes(b"one\n")
    posted: list[dict[str, Any]] = []
    watcher = watch.Watcher(tmp_path, posted.append)
    watcher.prime()
    (tmp_path / "a.py").write_bytes(b"two\n")
    os.utime(tmp_path / "a.py", (time.time() + 5, time.time() + 5))
    real = watcher._read
    monkeypatch.setattr(watcher, "_read", lambda path: watch._UNREADABLE)
    assert watcher.poll() == []
    monkeypatch.setattr(watcher, "_read", real)
    events = watcher.poll()
    assert [(e["old_content"], e["new_content"]) for e in events] == [("one\n", "two\n")]


def test_watch_ignores_what_a_git_checkout_rewrites(tmp_path: Path) -> None:
    from synergo import watch

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (tmp_path / "a.py").write_bytes(b"one\n")
    watcher = watch.Watcher(tmp_path, lambda e: None)
    watcher.prime()
    later = time.time() + 5
    (tmp_path / "a.py").write_bytes(b"theirs\n")
    os.utime(tmp_path / "a.py", (later, later))
    (tmp_path / ".git" / "HEAD").write_bytes(b"ref: refs/heads/other\n")
    os.utime(tmp_path / ".git" / "HEAD", (later, later))
    assert watcher.poll() == []
    assert watcher.snap.texts[str(tmp_path / "a.py")] == "theirs\n"


def test_mcp_check_file_sends_the_current_content_and_survives_bad_params(tmp_path: Path) -> None:
    from synergo import mcp_server

    class Client:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def post_event(self, event: dict[str, Any]) -> dict[str, Any]:
            self.events.append(event)
            return {"decision": "allow"}

    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    client = Client()
    server = mcp_server.McpServer(client, agent="cursor", cwd=str(tmp_path))  # type: ignore[arg-type]
    server.check_file({"path": "a.py"})
    assert client.events[0]["old_content"] == "x = 1\n"
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1, 2]})
    assert reply is not None and "result" in reply


def test_githook_script_quotes_the_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    from synergo import githook

    monkeypatch.setattr(githook.sys, "executable", '/opt/py $HOME/"x"/python')
    script = githook.hook_script()
    assert "'/opt/py $HOME/\"x\"/python' -c" in script


def test_watch_interval_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    from synergo import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["watch", "--interval", "0"])
    assert "between 0.05 and 3600" in capsys.readouterr().err


def test_cli_constants_match_the_protocol() -> None:
    from synergo import constants, protocol

    assert constants.AGENT_KINDS == protocol.AGENT_KINDS
    assert constants.DEFAULT_SERVER == daemon_module.DEFAULT_SERVER


# --- crypto follow-ups ------------------------------------------------------------------------


@pytest.mark.skipif(not daemon_module.crypto.available(), reason="needs the e2e extra")
def test_an_e2e_room_sends_keyed_repo_tokens_and_maps_them_back() -> None:
    conn = RoomConnection("TEAM42", "ada", settings=daemon_module.RoomSettings(e2e=True))
    conn.e2e_key = daemon_module.crypto.derive("k" * 43, "TEAM42")
    conn.repo_id = "feedfacecafebeef"
    conn._send(EditCheckRequest(request_id="r", file_path="a.py", repo_id="feedfacecafebeef"))
    sent = _drain_all(conn)[0]
    token = conn.e2e_key.repo_token("feedfacecafebeef")
    assert sent["repo_id"] == token != "feedfacecafebeef"
    assert json.loads(conn._join_payload())["repo_id"] == token
    assert conn._local_repo_id(token) == "feedfacecafebeef"


def test_room_keys_are_found_under_any_spelling_of_the_room() -> None:
    config = cfg.SynergoConfig(room="k7q2-xm9p", room_keys=(("K7Q2XM9P", "secret"),))
    assert config.room_key(generic_env=False) == "secret"
    assert cfg.canonical_room(" k7q2-XM9P ") == "K7Q2XM9P"


def test_key_set_warns_about_a_weak_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from synergo import cli

    if getattr(daemon_module.crypto, "passphrase_problem", None) is None:
        pytest.skip("crypto has no passphrase_problem")
    monkeypatch.setenv("SYNERGO_HOME", str(tmp_path / "home"))
    assert cli.main(["key", "set", "TEAM42", "acme2024"]) == 0
    assert "weak" in capsys.readouterr().err


def test_a_plan_carries_a_request_id_its_reply_is_matched_by(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, Any]:
        daemon = Daemon("TEAM42", "ada", port=4699, edit_check_timeout=2.0)
        daemon.connected = True
        task = asyncio.create_task(
            daemon.handle_event(event("plan", tmp_path, summary="refactor", files=["a.py"]))
        )
        await asyncio.sleep(0.05)
        sent = _drain_all(daemon.connection)[0]
        daemon._handle_frame(json.dumps({
            "type": "plan_conflicts", "request_id": sent["request_id"],
            "conflicts": [{"file_path": "a.py", "user_id": "sam", "kind": "edited", "seconds_ago": 3}],
        }))
        return sent["request_id"], await task

    request_id, ack = asyncio.run(scenario())
    assert request_id and ack.decision == "warn" and "sam is editing a.py" in ack.reason
