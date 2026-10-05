"""Every registered adapter honours the contract in synergo.adapters.base,
checked against golden payloads under fixtures/adapters/<agent>/."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from synergo import adapters, detect
from synergo.adapters import base
from synergo.events import EventAck, NormalizedEvent

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
AGENTS = adapters.ids()
REQUIRED_FIXTURES = ("prompt", "pre_edit", "post_edit", "irrelevant_tool", "malformed")

ACKS: dict[str, Any] = {
    "allow": {"decision": "allow", "reason": "", "file_path": None, "others": []},
    "warn": {
        "decision": "warn",
        "reason": "Sam's cursor declared intent on room.py",
        "file_path": "room.py",
        "others": [{"user_id": "Sam", "agent": "cursor", "seconds_ago": 3.0, "prompt": "rename room"}],
    },
    "block": {
        "decision": "block",
        "reason": "Sam's cursor edited room.py 8s ago",
        "file_path": "room.py",
        "others": [{"user_id": "Sam", "agent": "cursor", "seconds_ago": 8.0}],
    },
}
GARBAGE_ACKS: list[Any] = [None, 1, "block", [], {"decision": "maybe"}, {"decision": "block", "others": "x"}, {}]


def _sub(value: Any, tmp: str) -> Any:
    if isinstance(value, str):
        return value.replace("{tmp}", tmp)
    if isinstance(value, list):
        return [_sub(v, tmp) for v in value]
    if isinstance(value, dict):
        return {k: _sub(v, tmp) for k, v in value.items()}
    return value


def load_fixture(agent: str, name: str, tmp: Path) -> tuple[Any, list[dict[str, Any]]]:
    data = json.loads((FIXTURES / agent / f"{name}.json").read_text(encoding="utf-8"))
    return _sub(data["payload"], str(tmp)), _sub(data["expect"], str(tmp))


def fixture_names(agent: str) -> list[str]:
    return sorted(p.stem for p in (FIXTURES / agent).glob("*.json"))


CASES = [(agent, name) for agent in AGENTS for name in fixture_names(agent)]


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "room.py").write_text("def room():\n    pass\n", encoding="utf-8")
    return tmp_path


def _norm(path: Any) -> Any:
    return os.path.normcase(os.path.normpath(path)) if isinstance(path, str) else path


# --- registry ---------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_adapter_shape(agent: str) -> None:
    adapter = adapters.load(agent)
    assert isinstance(adapter, base.Adapter)
    assert adapter.id == agent
    assert adapter.hook_command == f"synergo hook {agent}"
    assert set(adapter.capabilities) == {"can_block", "has_intent", "has_post_edit"}
    assert all(isinstance(v, bool) for v in adapter.capabilities.values())
    for name in REQUIRED_FIXTURES:
        assert (FIXTURES / agent / f"{name}.json").is_file(), f"{agent} lacks fixture {name}"


@pytest.mark.parametrize("agent", AGENTS)
def test_detect_registry_agrees(agent: str) -> None:
    spec = detect.get_agent(agent)
    adapter = adapters.load(agent)
    assert spec.has_adapter
    assert spec.label == adapter.label
    assert spec.executables == adapter.executables
    assert spec.config_dirs == adapter.config_dirs


def test_every_adapter_in_detect_is_registered() -> None:
    assert {s.id for s in detect.KNOWN_AGENTS if s.has_adapter} == set(AGENTS)


# --- normalize -----------------------------------------------------------------------


@pytest.mark.parametrize(("agent", "name"), CASES)
def test_normalize_matches_golden(agent: str, name: str, workdir: Path) -> None:
    payload, expect = load_fixture(agent, name, workdir)
    events = adapters.load(agent).normalize(payload)
    assert isinstance(events, list)
    assert len(events) == len(expect)
    for event, want in zip(events, expect):
        assert set(event) == set(base.EVENT_FIELDS)
        NormalizedEvent(**event)  # validates against the daemon's model
        assert event["agent"] == agent
        assert event["session_id"] == payload.get("session_id")
        for key, value in want.items():
            if key == "file_path":
                assert _norm(event[key]) == _norm(value)
            else:
                assert event[key] == value, key


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("payload", [None, 1, "x", [], {}, {"hook_event_name": 5}, {"hook_event_name": "PreToolUse"}])
def test_normalize_never_raises_on_garbage(agent: str, payload: Any) -> None:
    assert adapters.load(agent).normalize(payload) == []


def test_codex_patch_paths() -> None:
    from synergo.adapters import codex

    patch = (
        "*** Begin Patch\n*** Add File: a.py\n+1\n*** Update File: b.py\n*** Move to: c.py\n"
        "@@\n-x\n+y\n*** Delete File: a.py\n*** End Patch"
    )
    assert codex.patch_paths(patch) == ["a.py", "b.py", "c.py"]
    assert codex.patch_paths("no patch here") == []


# --- render --------------------------------------------------------------------------


def _pre_edit(agent: str, workdir: Path) -> dict[str, Any]:
    payload, _ = load_fixture(agent, "pre_edit", workdir)
    (event,) = adapters.load(agent).normalize(payload)
    return event


def _is_allow(output: base.HookOutput) -> bool:
    return output.stdout == "" and output.exit_code == 0


def _approves(output: base.HookOutput) -> bool:
    if not output.stdout:
        return False
    data = json.loads(output.stdout)
    hso = data.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "allow" or data.get("decision") == "approve"


def _denies(output: base.HookOutput) -> bool:
    if output.exit_code == 2:
        return True
    if not output.stdout:
        return False
    data = json.loads(output.stdout)
    hso = data.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "deny" or data.get("decision") == "block"


@pytest.mark.parametrize("agent", AGENTS)
def test_render_allow_warn_block(agent: str, workdir: Path) -> None:
    adapter = adapters.load(agent)
    event = _pre_edit(agent, workdir)

    assert _is_allow(adapter.render(event, ACKS["allow"]))

    warn = adapter.render(event, ACKS["warn"])
    assert warn.exit_code == 0
    assert warn.stdout, "a warn must reach the user"
    assert not _approves(warn), "warn must never auto-approve"
    assert not _denies(warn)
    assert "Sam" in warn.stdout

    block = adapter.render(event, ACKS["block"])
    if adapter.capabilities["can_block"]:
        assert _denies(block), "block must never be emitted as allow"
    else:
        assert block.stdout and not _approves(block)
    assert not _approves(block)


@pytest.mark.parametrize("agent", AGENTS)
def test_render_matches_pydantic_ack(agent: str, workdir: Path) -> None:
    """Acks shaped by the daemon's real model render the same as raw dicts."""
    adapter = adapters.load(agent)
    event = _pre_edit(agent, workdir)
    for ack in ACKS.values():
        assert adapter.render(event, EventAck(**ack).model_dump()) == adapter.render(event, ack)


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("name", REQUIRED_FIXTURES)
def test_render_never_raises(agent: str, name: str, workdir: Path) -> None:
    adapter = adapters.load(agent)
    payload, _ = load_fixture(agent, name, workdir)
    events = adapter.normalize(payload) or [dict.fromkeys(base.EVENT_FIELDS)]
    for event in events + [{}, {"kind": "pre_edit"}]:
        for ack in list(ACKS.values()) + GARBAGE_ACKS:
            out = adapter.render(event, ack)
            assert isinstance(out, base.HookOutput)
            if event.get("kind") != "pre_edit":
                assert _is_allow(out), "only pre_edit may decide anything"
        for garbage in GARBAGE_ACKS:
            assert _is_allow(adapter.render(event, garbage)), "malformed acks read as allow"
        notice = adapter.render_notice(event, "Synergo offline")
        assert notice.exit_code == 0 and not _denies(notice) and not _approves(notice)


def test_strictest_ack_wins() -> None:
    a, b, c = {"file_path": "a"}, {"file_path": "b"}, {"file_path": "c"}
    picked = base.strictest([(a, ACKS["allow"]), (b, ACKS["block"]), (c, ACKS["warn"])])
    assert picked == (b, ACKS["block"])
    assert base.strictest([(a, None), (b, {"decision": "nope"})]) == (a, None)
    assert base.strictest([]) is None


# --- install -------------------------------------------------------------------------

UNRELATED = {"matcher": "Bash", "hooks": [{"type": "command", "command": "./audit.sh", "timeout": 30}]}


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(detect, "home", lambda: home)
    return home


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("scope", ["project", "user"])
def test_install_idempotent_and_uninstall_clean(
    agent: str, scope: str, tmp_path: Path, fake_home: Path
) -> None:
    adapter = adapters.load(agent)
    root = tmp_path / "repo"
    path = adapter.settings_path(scope, root)
    assert (fake_home if scope == "user" else root) in path.parents
    path.parent.mkdir(parents=True)
    original = {"model": "x", "hooks": {"PreToolUse": [UNRELATED]}}
    path.write_text(json.dumps(original, indent=2), encoding="utf-8")

    assert not adapter.is_installed(scope, root)
    added = adapter.install(scope, root)
    assert added
    assert adapter.is_installed(scope, root)
    content = path.read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["model"] == "x"
    assert UNRELATED in data["hooks"]["PreToolUse"]
    assert adapter.hook_command in content

    assert adapter.install(scope, root) == []
    assert path.read_text(encoding="utf-8") == content

    assert sorted(adapter.uninstall(scope, root)) == sorted(added)
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert not adapter.is_installed(scope, root)
    assert adapter.uninstall(scope, root) == []


@pytest.mark.parametrize("agent", AGENTS)
def test_install_into_missing_and_unparseable(agent: str, tmp_path: Path) -> None:
    adapter = adapters.load(agent)
    root = tmp_path / "repo"
    assert adapter.uninstall("project", root) == []
    assert adapter.install("project", root)
    path = adapter.settings_path("project", root)
    assert path.is_file()

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(base.SettingsError):
        adapter.install("project", root)
    with pytest.raises(base.SettingsError):
        adapter.uninstall("project", root)
    assert path.read_text(encoding="utf-8") == "{not json"
    assert not adapter.is_installed("project", root)


@pytest.mark.parametrize("agent", AGENTS)
def test_cli_hook_install_uninstall(
    agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from synergo import cli

    monkeypatch.chdir(tmp_path)
    adapter = adapters.load(agent)
    assert cli.main(["hook", "install", agent]) == 0
    assert adapter.is_installed("project", tmp_path)
    assert cli.main(["hook", "install", agent]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert cli.main(["hook", "uninstall", agent]) == 0
    assert not adapter.is_installed("project", tmp_path)
