"""`hivelane wait FILE`: polls the daemon's POST /wait until the file is free
(exit 0) or the timeout passes (exit 1); with nothing to wait for, it says so
and lets the agent go ahead."""

from __future__ import annotations

from typing import Any

import pytest

from hivelane import cli
from hivelane.tests.cli_fakes import Machine, machine  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("machine")


def scripted(m: Machine, replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make the fake daemon answer POST /wait with `replies` in turn (the last
    one repeats); returns the bodies it was sent."""
    sent: list[dict[str, Any]] = []
    m.service.running = True
    m.service.REGISTER_TIMEOUT = 1.0

    def request(port: int, method: str, path: str, body: Any = None, timeout: float = 0.0) -> Any:
        assert (method, path) == ("POST", "/wait")
        sent.append(body)
        return replies[min(len(sent), len(replies)) - 1]

    m.service._request = request
    return sent


def test_wait_without_a_daemon_lets_the_agent_go(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["wait", "server/room.py"]) == 0
    assert "edits are not being checked; go ahead" in capsys.readouterr().out


def test_wait_polls_until_the_file_is_free(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    sent = scripted(
        machine,
        [
            {"free": False, "state": "waiting", "file_path": "server/room.py", "queue_position": 2},
            {"free": False, "state": "waiting", "file_path": "server/room.py", "queue_position": 1},
            {"free": True, "state": "released", "file_path": "server/room.py"},
        ],
    )
    assert cli.main(["wait", "room.py", "--timeout", "30"]) == 0
    out = capsys.readouterr().out
    assert out.count("waiting for server/room.py (#2 in line)") == 1  # announced once
    assert "server/room.py is free, go ahead" in out
    assert len(sent) == 3
    assert all(body["file_path"] == "room.py" and body["cwd"] for body in sent)
    assert all(0 < body["timeout"] <= cli.WAIT_SLICE for body in sent)


def test_wait_times_out_with_exit_1(machine: Machine, capsys: pytest.CaptureFixture[str]) -> None:
    scripted(machine, [{"free": False, "state": "waiting", "file_path": "a.py"}])
    assert cli.main(["wait", "a.py", "--timeout", "0"]) == 1
    captured = capsys.readouterr()
    assert "waiting for a.py…" in captured.out
    assert "a.py is still taken after 0s" in captured.err


@pytest.mark.parametrize(
    "state,text",
    [
        ("offline", "not connected to the room right now"),
        ("uncoordinated", "is not coordinated by hivelane"),
        ("ignored", "is not coordinated by hivelane"),
    ],
)
def test_wait_explains_why_there_is_nothing_to_wait_for(
    machine: Machine, capsys: pytest.CaptureFixture[str], state: str, text: str
) -> None:
    scripted(machine, [{"free": True, "state": state, "file_path": "a.py"}])
    assert cli.main(["wait", "a.py"]) == 0
    assert text in capsys.readouterr().out
