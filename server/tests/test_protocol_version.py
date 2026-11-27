"""Version negotiation. An old daemon connects happily and simply never sends
edit_check, so conflict prevention would be silently off, the handshake has to
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
        frame = ws.receive_json()

    assert frame["type"] == "room_settings"



def _join(protocol: int, e2e: bool) -> dict:
    frame: dict = {"type": "join", "name": "adrian", "agent": "claude-code", "protocol_version": protocol}
    if e2e:
        frame["settings"] = {"e2e": True}
    return frame


def test_pre_v3_member_is_refused_from_an_e2e_room(room_id: str) -> None:
    """A protocol-2 daemon hashes names with a v2 key, which never match a v3
    member's: in an e2e room its edits would go unchecked, so it is refused."""
    client = TestClient(app)
    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json(_join(2, e2e=True))
        frame = ws.receive_json()
    assert frame["type"] == "version_rejected"
    assert "encrypted" in frame["reason"]


def test_pre_v3_member_is_refused_once_the_room_is_e2e(room_id: str) -> None:
    client = TestClient(app)
    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json(_join(PROTOCOL_VERSION, e2e=True))
        ws.receive_json()
        with client.websocket_connect(f"/ws/{room_id}") as old:
            old.send_json({**_join(2, e2e=False), "name": "bob"})
            assert old.receive_json()["type"] == "version_rejected"


def test_pre_v3_member_still_joins_a_cleartext_room(room_id: str) -> None:
    client = TestClient(app)
    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json(_join(2, e2e=False))
        ws.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
        frame = ws.receive_json()
        while frame["type"] != "edit_check_response":
            frame = ws.receive_json()
    assert frame["decision"] == "allow"
