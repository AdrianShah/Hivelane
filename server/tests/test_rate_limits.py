"""Per-IP rate limiting on the unauthenticated surface: room creation,
WebSocket connection attempts, and how the client IP is derived."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

try:
    from server import config, limits
    from server.limits import FixedWindowLimiter, client_ip
    from server.main import WS_CLOSE_RATE_LIMITED, app
except ImportError:  # Render rootDir=server
    import config
    import limits
    from limits import FixedWindowLimiter, client_ip
    from main import WS_CLOSE_RATE_LIMITED, app


def _connection(
    *, peer: tuple[str, int] | None, forwarded: str | None = None
) -> HTTPConnection:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return HTTPConnection({"type": "http", "headers": headers, "client": peer})


# --- the limiter itself -------------------------------------------------------


def test_limiter_allows_up_to_the_limit_then_denies() -> None:
    limiter = FixedWindowLimiter(limit=2, window_seconds=60, max_keys=8)

    assert limiter.check("ip", now=0.0).allowed
    assert limiter.check("ip", now=1.0).allowed

    denied = limiter.check("ip", now=2.0)
    assert not denied.allowed
    assert denied.retry_after_seconds == 58


def test_limiter_keys_are_independent() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=60, max_keys=8)

    assert limiter.check("a", now=0.0).allowed
    assert not limiter.check("a", now=0.0).allowed
    assert limiter.check("b", now=0.0).allowed


def test_limiter_budget_returns_in_the_next_window() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=60, max_keys=8)

    assert limiter.check("ip", now=0.0).allowed
    assert not limiter.check("ip", now=30.0).allowed
    assert limiter.check("ip", now=60.0).allowed


def test_limiter_evicts_oldest_keys_instead_of_growing() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=60, max_keys=2)

    assert limiter.check("a", now=0.0).allowed
    assert not limiter.check("a", now=0.0).allowed

    limiter.check("b", now=0.0)
    limiter.check("c", now=0.0)

    assert limiter.tracked_keys == 2
    # "a" was evicted, so its window restarted rather than being remembered.
    assert limiter.check("a", now=0.0).allowed
    assert limiter.tracked_keys == 2


# --- client_ip ----------------------------------------------------------------


def test_client_ip_uses_socket_peer_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)

    connection = _connection(peer=("10.0.0.7", 51234), forwarded="203.0.113.9")

    assert client_ip(connection) == "10.0.0.7"


def test_client_ip_honours_forwarded_header_when_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    connection = _connection(
        peer=("10.0.0.7", 51234), forwarded="203.0.113.9, 70.41.3.18"
    )

    assert client_ip(connection) == "203.0.113.9"


def test_client_ip_falls_back_to_peer_when_header_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)

    connection = _connection(peer=("10.0.0.7", 51234), forwarded="   ")

    assert client_ip(connection) == "10.0.0.7"


def test_client_ip_survives_a_missing_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)

    assert client_ip(_connection(peer=None)) == limits.UNKNOWN_IP


# --- POST /rooms --------------------------------------------------------------


def test_room_creation_returns_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits.room_creation_limiter, "limit", 2)
    client = TestClient(app)

    assert client.post("/rooms").status_code == 200
    assert client.post("/rooms").status_code == 200

    response = client.post("/rooms")

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_a_different_ip_keeps_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", True)
    monkeypatch.setattr(limits.room_creation_limiter, "limit", 1)
    client = TestClient(app)

    flooder = {"X-Forwarded-For": "203.0.113.9"}
    assert client.post("/rooms", headers=flooder).status_code == 200
    assert client.post("/rooms", headers=flooder).status_code == 429

    assert (
        client.post("/rooms", headers={"X-Forwarded-For": "198.51.100.4"}).status_code
        == 200
    )


def test_forwarded_header_cannot_buy_extra_budget_when_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "TRUST_PROXY_HEADER", False)
    monkeypatch.setattr(limits.room_creation_limiter, "limit", 1)
    client = TestClient(app)

    first = client.post("/rooms", headers={"X-Forwarded-For": "203.0.113.1"})
    second = client.post("/rooms", headers={"X-Forwarded-For": "203.0.113.2"})

    assert first.status_code == 200
    assert second.status_code == 429


def test_readyz_reports_the_environment() -> None:
    client = TestClient(app)

    body = client.get("/readyz").json()

    assert body["status"] == "ready"
    assert body["env"] == config.ENV
    assert body["cors_origins_configured"] == len(config.CORS_ORIGINS)


# --- WS /ws/{room_id} ---------------------------------------------------------


def test_websocket_attempts_are_refused_after_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]
    monkeypatch.setattr(limits.ws_attempt_limiter, "limit", 1)

    with client.websocket_connect(f"/ws/{room_id}") as first:
        first.send_json({"type": "join", "name": "adrian", "agent": "claude-code"})

    with client.websocket_connect(f"/ws/{room_id}") as refused:
        frame = refused.receive_json()
        with pytest.raises(WebSocketDisconnect) as excinfo:
            refused.receive_json()

    assert frame["type"] == "rate_limited"
    assert frame["reason"]
    assert frame["retry_after"] > 0
    assert excinfo.value.code == WS_CLOSE_RATE_LIMITED


def test_rate_limited_socket_is_refused_before_the_room_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown room must not be distinguishable from a known one once the
    caller is throttled — otherwise the limiter leaks a brute-force oracle."""
    monkeypatch.setattr(limits.ws_attempt_limiter, "limit", 0)
    client = TestClient(app)

    with client.websocket_connect("/ws/ZZZZZZ") as refused:
        frame = refused.receive_json()

    assert frame["type"] == "rate_limited"
