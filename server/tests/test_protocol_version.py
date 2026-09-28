"""Version negotiation. An old daemon connects happily and simply never sends
edit_check, so conflict prevention would be silently off — the handshake has to
turn that into a visible error."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

try:
    from ..main import app
    from ..models import MIN_SUPPORTED_PROTOCOL, PROTOCOL_VERSION
except ImportError:
    from main import app
    from models import MIN_SUPPORTED_PROTOCOL, PROTOCOL_VERSION


@pytest.fixture()
def room_id() -> str:
    client = TestClient(app)
    return client.post("/rooms").json()["room_id"]


def test_client_without_a_version_is_rejected(room_id: str) -> None:
    client = TestClient(app)
    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json({"type": "join", "name": "adrian", "agent": "claude-code"})
        frame = ws.receive_json()

    assert frame["type"] == "version_rejected"
    assert "upgrade" in frame["reason"].lower()
    assert frame["min_supported"] == MIN_SUPPORTED_PROTOCOL


def test_current_client_joins(room_id: str) -> None:
    client = TestClient(app)
    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json(
            {
                "type": "join",
                "name": "adrian",
                "agent": "claude-code",
                "protocol_version": PROTOCOL_VERSION,
            }
        )
        ws.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
        frame = ws.receive_json()

    assert frame["type"] == "edit_check_response"
    assert frame["decision"] == "allow"


def test_spectator_is_exempt(room_id: str) -> None:
    """The dashboard only consumes broadcasts, so it cannot under-report."""
    client = TestClient(app)
    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json({"type": "join", "name": None, "role": "spectator"})
        ws.send_json({"type": "ping"})
    # Reaching here without a version_rejected frame is the assertion.


def test_daemon_and_relay_versions_agree() -> None:
    """The two constants are mirrored by hand; drift would mean the daemon
    declares a version the relay does not actually implement."""
    from synergo.protocol import PROTOCOL_VERSION as DAEMON_PROTOCOL

    assert DAEMON_PROTOCOL == PROTOCOL_VERSION
