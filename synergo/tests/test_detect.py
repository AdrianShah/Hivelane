"""Agent detection: a registry where adding an adapter is one entry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synergo import cli, detect


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    executables: dict[str, str] = {}
    monkeypatch.setattr(detect, "home", lambda: home)
    monkeypatch.setattr(detect, "which", lambda name: executables.get(name))
    return executables


def test_registry_shape() -> None:
    ids = [spec.id for spec in detect.KNOWN_AGENTS]
    assert ids == ["claude-code", "codex", "cursor", "gemini-cli", "aider"]
    assert len(set(ids)) == len(ids)
    assert [spec.id for spec in detect.KNOWN_AGENTS if spec.has_adapter] == ["claude-code"]
    assert detect.get_agent(detect.PRIMARY_AGENT).has_adapter
    with pytest.raises(KeyError):
        detect.get_agent("nope")


def test_nothing_installed_detects_nothing(machine: dict[str, str]) -> None:
    assert detect.detect_agents() == []


def test_detects_by_executable_and_by_config_dir(machine: dict[str, str]) -> None:
    machine["claude"] = "/usr/local/bin/claude"
    machine["cursor"] = "/usr/bin/cursor"  # fallback name when cursor-agent is absent
    machine["aider"] = "/usr/bin/aider"
    (detect.home() / ".codex").mkdir()
    (detect.home() / ".gemini").write_text("a file, not a config dir", encoding="utf-8")

    found = {agent.id: agent for agent in detect.detect_agents()}

    assert list(found) == ["claude-code", "codex", "cursor", "aider"]
    assert found["claude-code"].evidence == "claude on PATH"
    assert found["claude-code"].has_adapter
    assert found["codex"].executable is None
    assert found["codex"].evidence == "~/.codex"
    assert found["cursor"].evidence == "cursor on PATH"
    assert not found["aider"].has_adapter


def test_cursor_agent_preferred_over_cursor(machine: dict[str, str]) -> None:
    machine["cursor-agent"] = "/usr/bin/cursor-agent"
    machine["cursor"] = "/usr/bin/cursor"
    (found,) = detect.detect_agents()
    assert found.executable == "/usr/bin/cursor-agent"


def test_unsupported_agent_cannot_install(tmp_path: Path) -> None:
    codex = detect.get_agent("codex")
    with pytest.raises(LookupError, match="no hook adapter"):
        codex.install(detect.HookScope.project(tmp_path))
    assert not codex.is_installed(detect.HookScope.project(tmp_path))


def test_claude_adapter_installs_idempotently(machine: dict[str, str], tmp_path: Path) -> None:
    claude = detect.get_agent("claude-code")
    scope = detect.HookScope.project(tmp_path / "repo")
    assert not claude.is_installed(scope)

    first = claude.install(scope)
    assert first.changed
    assert set(first.added) == {"UserPromptSubmit", "PreToolUse", "PostToolUse"}
    assert first.path == tmp_path / "repo" / ".claude" / "settings.json"
    assert claude.is_installed(scope)
    content = first.path.read_text(encoding="utf-8")

    second = claude.install(scope)
    assert not second.changed
    assert first.path.read_text(encoding="utf-8") == content


def test_claude_adapter_global_scope_uses_home(machine: dict[str, str]) -> None:
    claude = detect.get_agent("claude-code")
    result = claude.install(detect.HookScope.user())
    assert result.path == detect.home() / ".claude" / "settings.json"
    assert claude.is_installed(detect.HookScope.user())


def test_partial_install_is_not_installed(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "synergo hook claude-code"}]}]}}),
        encoding="utf-8",
    )
    assert not detect.get_agent("claude-code").is_installed(detect.HookScope.project(tmp_path))


def test_unparseable_settings_are_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    claude = detect.get_agent("claude-code")
    with pytest.raises(cli.ConfigError):
        claude.install(detect.HookScope.project(tmp_path))
    assert path.read_text(encoding="utf-8") == "{not json"
    assert not claude.is_installed(detect.HookScope.project(tmp_path))
