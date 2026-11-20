"""Daemon-side pieces that let two agents share a file: a whole-file Write is
judged by what it actually changes, line numbers travel with the version of
the file they count, and a safe "different function" warning does not tell
the agent to stop and ask."""

from __future__ import annotations

import pytest

from hivelane import crypto
from hivelane.adapters import base, claude_code
from hivelane.daemon import RoomConnection, edit_symbols, write_symbols
from hivelane.protocol import RoomSettings

PY = (
    "import os\n"
    "\n"
    "LIMIT = 100\n"
    "\n"
    "\n"
    "def alpha(x):\n"
    "    return x + 1\n"
    "\n"
    "\n"
    "def beta(y):\n"
    "    return y * 2\n"
)


def test_a_write_names_only_what_it_changes() -> None:
    assert write_symbols("a.py", PY, PY.replace("y * 2", "y * 3")) == (["beta"], [(11, 11)])
    assert write_symbols("a.py", PY, PY.replace("x + 1", "x + 2").replace("100", "200"))[0] == ["LIMIT", "alpha"]
    # Appending a function claims no existing one, only the new one.
    names, ranges = write_symbols("a.py", PY, PY + "\n\ndef gamma(z):\n    return z\n")
    assert names == ["<module>", "gamma"] and ranges == [(11, 11)]
    assert write_symbols("a.py", PY, PY) == ([], [])


def test_write_hints_reach_the_prediction() -> None:
    payload = {
        "hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/r", "tool_name": "Write",
        "tool_input": {"file_path": "/r/a.py", "content": PY.replace("y * 2", "y * 3")},
    }
    event = claude_code.normalize_payload(payload)
    assert event["edit_hints"] == [{"write": PY.replace("y * 2", "y * 3")}]
    assert edit_symbols("a.py", PY, event["edit_hints"]) == (["beta"], [(11, 11)])
    # Too big to diff: no hint, so the file is judged whole as before.
    assert base.write_hints("x" * (base.MAX_WRITE_HINT_CHARS + 1)) is None


def test_content_versions_are_stable_and_keyed_in_e2e_rooms() -> None:
    plain = RoomConnection("ROOM", "ada")
    assert plain.content_version(PY) == plain.content_version(PY)
    assert plain.content_version(PY) != plain.content_version(PY + "\n")
    if not crypto.available():
        pytest.skip("needs the e2e extra (cryptography)")
    key = crypto.derive("correct horse battery", "ROOM")
    sealed = RoomConnection("ROOM", "ada", settings=RoomSettings(e2e=True), e2e_key=key)
    # Not the plain hash: the relay cannot confirm a guessed file content.
    assert sealed.content_version(PY) == key.hash(PY) != plain.content_version(PY)


def test_a_safe_warning_does_not_tell_the_agent_to_stop() -> None:
    ack = {
        "decision": "warn", "scope": "symbol", "file_path": "a.py", "others": [],
        "reason": "sam's cursor is editing alpha in a.py (2s ago), different function, safe to proceed",
    }
    message = base.ack_message(ack, "warn", "a.py")
    assert message.endswith("safe to proceed")
    assert "check with them" not in message
    # A merge-conflict warning still asks the agent to coordinate.
    branch = {**ack, "scope": "branch", "reason": "sam edited a.py on branch x 2s ago, this will conflict at merge"}
    assert "check with them" in base.ack_message(branch, "warn", "a.py")
