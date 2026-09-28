"""Claude Code adapter: payload translation and the stdout/exit contract."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from synergo import cli
from synergo.adapters import claude_code
from synergo.events import EventAck, NormalizedEvent
from synergo.protocol import PeerTouch


def pre_tool_use_payload(target: Path, cwd: Path) -> dict:
    return {
        "session_id": "abc123",
        "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
        "transcript_path": str(cwd / ".claude" / "transcript.jsonl"),
        "cwd": str(cwd),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_use_id": "toolu_01ABC123",
        "tool_input": {
            "file_path": str(target),
            "old_string": "def room():",
            "new_string": "def room(code: str):",
        },
    }


def test_pre_tool_use_normalizes(tmp_path: Path) -> None:
    target = tmp_path / "server" / "room.py"
    target.parent.mkdir()
    target.write_text("def room():\n    pass\n", encoding="utf-8")

    event = claude_code.normalize(pre_tool_use_payload(target, tmp_path))

    assert event == NormalizedEvent(
        kind="pre_edit",
        agent="claude-code",
        session_id="abc123",
        cwd=str(tmp_path),
        file_path=str(target),
        old_content="def room():\n    pass\n",
        tool="Edit",
    )


def test_user_prompt_submit_normalizes_to_intent(tmp_path: Path) -> None:
    event = claude_code.normalize(
        {
            "session_id": "abc123",
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "refactor the room registry",
        }
    )
    assert event is not None
    assert event.kind == "intent"
    assert event.prompt == "refactor the room registry"


def test_post_tool_use_reads_new_content(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("after\n", encoding="utf-8")
    event = claude_code.normalize(
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "tool_response": {"success": True},
        }
    )
    assert event is not None
    assert event.kind == "post_edit"
    assert event.new_content == "after\n"


def test_unrelated_tools_and_events_are_ignored(tmp_path: Path) -> None:
    assert (
        claude_code.normalize(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s",
                "cwd": str(tmp_path),
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        is None
    )
    assert claude_code.normalize({"hook_event_name": "SessionStart"}) is None


def run_hook(monkeypatch: pytest.MonkeyPatch, payload: dict, ack: EventAck | None) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    if ack is None:

        def explode(*_args: object, **_kwargs: object) -> EventAck:
            raise RuntimeError("daemon exploded")

        monkeypatch.setattr(cli, "post_event", explode)
    else:
        monkeypatch.setattr(cli, "post_event", lambda event, port: ack)
    code = cli.main(["hook", "claude-code"])
    return code, stdout.getvalue()


def test_allow_emits_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    code, out = run_hook(
        monkeypatch, pre_tool_use_payload(target, tmp_path), EventAck(decision="allow")
    )
    assert code == 0
    assert out == ""


def test_block_emits_deny_with_named_peer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "server" / "room.py"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    ack = EventAck(
        decision="block",
        reason="Sam's cursor edited server/room.py 8s ago",
        file_path="server/room.py",
        others=[PeerTouch(user_id="Sam", agent="cursor", seconds_ago=8.4)],
    )

    code, out = run_hook(monkeypatch, pre_tool_use_payload(target, tmp_path), ack)

    assert code == 0
    payload = json.loads(out)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"] == (
        "Synergo: Sam's cursor edited server/room.py 8s ago — wait, or pick another file"
    )
    assert "permissionDecision" not in payload


def test_warn_surfaces_context_without_deciding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "server" / "room.py"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    ack = EventAck(
        decision="warn",
        reason="Sam's cursor touched this file 42s ago",
        file_path="server/room.py",
        others=[PeerTouch(user_id="Sam", agent="cursor", seconds_ago=42.0)],
    )

    code, out = run_hook(monkeypatch, pre_tool_use_payload(target, tmp_path), ack)

    assert code == 0
    payload = json.loads(out)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    # A warn must not decide: emitting "allow" would auto-approve the edit.
    assert "permissionDecision" not in specific
    assert "Sam's cursor touched this file 42s ago" in specific["additionalContext"]
    assert "Sam's cursor touched this file 42s ago" in payload["systemMessage"]
    # A warn lets the edit through, so it must not borrow the block imperative.
    assert "check with them before you overlap" in specific["additionalContext"]
    assert "wait, or pick another file" not in specific["additionalContext"]


def test_post_edit_never_emits_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    payload = {
        "session_id": "s1",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }
    code, out = run_hook(monkeypatch, payload, EventAck(decision="block", reason="too late"))
    assert code == 0
    assert out == ""


def test_intent_only_peer_is_not_reported_as_having_edited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A peer matched only by declared intent has not touched the file; the
    relay words that case, so the adapter must not overwrite it with "edited"."""
    target = tmp_path / "server" / "room.py"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    ack = EventAck(
        decision="warn",
        reason="Sam's cursor said they are working on server/room.py",
        file_path="server/room.py",
        others=[
            PeerTouch(
                user_id="Sam",
                agent="cursor",
                seconds_ago=3.0,
                prompt="refactor the broadcast queue",
            )
        ],
    )

    code, out = run_hook(monkeypatch, pre_tool_use_payload(target, tmp_path), ack)

    assert code == 0
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "said they are working on" in context
    assert "edited" not in context
    assert 'their task: "refactor the broadcast queue"' in context
