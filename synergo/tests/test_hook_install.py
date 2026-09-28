"""`synergo hook install claude-code` must merge, not clobber, and be idempotent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synergo import cli

UNRELATED = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "./.claude/hooks/audit.sh", "timeout": 30}],
}


def write_existing(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"PreToolUse": [UNRELATED]},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def synergo_hooks(groups: list[dict]) -> list[dict]:
    return [
        hook
        for group in groups
        for hook in group.get("hooks", [])
        if "synergo hook" in hook.get("command", "")
    ]


def test_install_merges_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    write_existing(path)

    assert cli.main(["hook", "install", "claude-code"]) == 0
    first = json.loads(path.read_text(encoding="utf-8"))

    assert first["model"] == "opus"
    assert UNRELATED in first["hooks"]["PreToolUse"]
    assert len(synergo_hooks(first["hooks"]["PreToolUse"])) == 1
    assert len(synergo_hooks(first["hooks"]["PostToolUse"])) == 1
    assert len(synergo_hooks(first["hooks"]["UserPromptSubmit"])) == 1

    pre = next(g for g in first["hooks"]["PreToolUse"] if synergo_hooks([g]))
    assert pre["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert pre["hooks"][0]["timeout"] == 5
    assert first["hooks"]["PostToolUse"][0]["hooks"][0]["timeout"] == 10
    # UserPromptSubmit has no matcher support in Claude Code.
    assert "matcher" not in first["hooks"]["UserPromptSubmit"][0]

    capsys.readouterr()
    assert cli.main(["hook", "install", "claude-code"]) == 0
    second = json.loads(path.read_text(encoding="utf-8"))

    assert second == first
    assert "already installed" in capsys.readouterr().out


def test_install_creates_missing_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["hook", "install", "claude-code"]) == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PostToolUse"}


def test_uninstall_removes_only_synergo_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    write_existing(path)
    cli.main(["hook", "install", "claude-code"])

    assert cli.main(["hook", "uninstall", "claude-code"]) == 0
    settings = json.loads(path.read_text(encoding="utf-8"))

    assert settings["model"] == "opus"
    assert settings["hooks"] == {"PreToolUse": [UNRELATED]}
