"""GET /metrics: Prometheus text exposition, hand-written so the relay image
needs no metrics dependency. The format is checked as a scraper reads it."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

try:
    from server import limits, metrics
    from server.main import app
    from server.models import PROTOCOL_VERSION
except ImportError:  # Render rootDir=server
    import limits
    import metrics
    from main import app
    from models import PROTOCOL_VERSION

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})? (\S+)$")


@pytest.fixture(autouse=True)
def _fresh_counters() -> Iterator[None]:
    metrics.registry.reset()
    yield
    metrics.registry.reset()


def _scrape(client: TestClient) -> dict[str, float]:
    """series (name plus label block, verbatim) -> value."""
    response = client.get("/metrics")
    assert response.status_code == 200
    samples: dict[str, float] = {}
    for line in response.text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        assert match, f"not a valid exposition line: {line!r}"
        name, labels, value = match.groups()
        samples[name + (labels or "")] = float(value)
    return samples


def _join(socket: Any, name: str, **extra: Any) -> None:
    socket.send_json(
        {
            "type": "join",
            "name": name,
            "agent": "claude-code",
            "protocol_version": PROTOCOL_VERSION,
            **extra,
        }
    )


# --- format -------------------------------------------------------------------


def test_metrics_endpoint_speaks_the_exposition_format() -> None:
    client = TestClient(app)
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = response.text
    for name, kind in (
        ("synergo_rooms_live", "gauge"),
        ("synergo_rooms_durable", "gauge"),
        ("synergo_members", "gauge"),
        ("synergo_spectators", "gauge"),
        ("synergo_edit_checks_total", "counter"),
        ("synergo_activity_messages_total", "counter"),
        ("synergo_intents_total", "counter"),
        ("synergo_ws_rejections_total", "counter"),
        ("synergo_edit_check_seconds", "histogram"),
    ):
        assert f"# TYPE {name} {kind}\n" in text
        assert f"# HELP {name} " in text
    assert text.endswith("\n")


def test_labelled_counters_start_at_zero_for_every_known_value() -> None:
    """A dashboard sees an explicit 0, not a missing series, before the first
    block of the day."""
    samples = _scrape(TestClient(app))

    for decision in ("allow", "warn", "block"):
        assert samples[f'synergo_edit_checks_total{{decision="{decision}"}}'] == 0
    for reason in ("rate_limited", "room_not_found", "version", "name"):
        assert samples[f'synergo_ws_rejections_total{{reason="{reason}"}}'] == 0


def test_histogram_buckets_are_cumulative_and_end_in_inf() -> None:
    histogram = metrics.Histogram("h", "test", (0.01, 0.1))
    for value in (0.005, 0.01, 0.05, 2.0):
        histogram.observe(value)

    lines = histogram.render()

    assert lines[:2] == ["# HELP h test", "# TYPE h histogram"]
    assert lines[2:5] == [
        'h_bucket{le="0.01"} 2',  # upper bounds are inclusive
        'h_bucket{le="0.1"} 3',
        'h_bucket{le="+Inf"} 4',
    ]
    assert lines[5].startswith("h_sum 2.065")
    assert lines[6] == "h_count 4"


def test_edit_check_histogram_has_the_documented_buckets() -> None:
    samples = _scrape(TestClient(app))

    bounds = [
        key.split('le="')[1].rstrip('"}')
        for key in samples
        if key.startswith("synergo_edit_check_seconds_bucket")
    ]
    assert bounds == ["0.001", "0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "+Inf"]


def test_label_values_are_escaped() -> None:
    counter = metrics.Counter("c", "test", label="why")
    counter.inc('say "hi"\\now')

    assert counter.render()[-1] == 'c{why="say \\"hi\\"\\\\now"} 1'


# --- what is counted ----------------------------------------------------------


def test_edit_checks_are_counted_by_decision_and_timed() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as ws:
        _join(ws, "adrian")
        for n in range(3):
            ws.send_json({"type": "edit_check", "request_id": f"r{n}", "file_path": "a.py"})
            assert ws.receive_json()["type"] == "edit_check_response"
        samples = _scrape(client)

    assert samples['synergo_edit_checks_total{decision="allow"}'] == 3
    assert samples['synergo_edit_checks_total{decision="block"}'] == 0
    assert samples["synergo_edit_check_seconds_count"] == 3
    assert samples['synergo_edit_check_seconds_bucket{le="+Inf"}'] == 3
    assert samples["synergo_edit_check_seconds_sum"] >= 0


def test_activity_and_intents_are_counted() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as ws:
        _join(ws, "adrian")
        ws.send_json({"type": "intent", "user_id": "adrian", "prompt": "tidy up"})
        ws.send_json(
            {
                "type": "activity",
                "user_id": "adrian",
                "changes": [{"file_path": "a.py", "diff": "@@"}],
            }
        )
        # The edit_check reply is sent after both earlier frames are handled.
        ws.send_json({"type": "edit_check", "request_id": "sync", "file_path": "b.py"})
        while ws.receive_json().get("type") != "edit_check_response":
            pass
        samples = _scrape(client)

    assert samples["synergo_intents_total"] == 1
    assert samples["synergo_activity_messages_total"] == 1


def test_members_and_spectators_are_gauged_live() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    before = _scrape(client)

    with client.websocket_connect(f"/ws/{room_id}") as member:
        _join(member, "adrian")
        member.send_json({"type": "edit_check", "request_id": "sync", "file_path": "a.py"})
        member.receive_json()
        with client.websocket_connect(f"/ws/{room_id}") as spectator:
            spectator.send_json({"type": "join", "name": None})
            # Catch-up (room settings first) is sent after the spectator is
            # registered.
            assert spectator.receive_json()["type"] == "room_settings"
            during = _scrape(client)

    assert during["synergo_members"] == before["synergo_members"] + 1
    assert during["synergo_spectators"] == before["synergo_spectators"] + 1
    assert during["synergo_rooms_live"] >= 1
    assert during["synergo_rooms_durable"] >= 1


def test_unknown_room_is_counted_as_a_rejection() -> None:
    client = TestClient(app)

    with client.websocket_connect("/ws/ZZZZZZ") as ws:
        assert ws.receive_json()["type"] == "room_rejected"

    assert _scrape(client)['synergo_ws_rejections_total{reason="room_not_found"}'] == 1


def test_old_protocol_is_counted_as_a_rejection() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as ws:
        ws.send_json({"type": "join", "name": "adrian"})
        assert ws.receive_json()["type"] == "version_rejected"

    assert _scrape(client)['synergo_ws_rejections_total{reason="version"}'] == 1


def test_bad_name_is_counted_as_a_rejection() -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as ws:
        _join(ws, "!!!")
        assert ws.receive_json()["type"] == "name_rejected"

    assert _scrape(client)['synergo_ws_rejections_total{reason="name"}'] == 1


def test_rate_limited_connection_is_counted_as_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits.ws_attempt_limiter, "limit", 0)
    client = TestClient(app)

    with client.websocket_connect("/ws/ZZZZZZ") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "rate_limited"
    assert _scrape(client)['synergo_ws_rejections_total{reason="rate_limited"}'] == 1
