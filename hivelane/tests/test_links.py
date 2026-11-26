"""Where a room's dashboard link points: a local relay's own dashboard when
it serves one, else the hosted one told which relay to reach."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator

import pytest

from hivelane import links

# conftest.py turns the probe off for every test; these put the real one back.
REAL_PROBE = links.serves_dashboard
HOSTED = links.DEFAULT_DASHBOARD


def test_a_remote_relay_opens_the_hosted_dashboard_with_the_relay() -> None:
    assert (
        links.room_url("wss://relay.example/ws", "R2345678")
        == f"{HOSTED}/r/R2345678?relay=wss%3A%2F%2Frelay.example%2Fws"
    )


def test_a_local_relay_without_a_dashboard_opens_the_hosted_one() -> None:
    assert (
        links.room_url("ws://127.0.0.1:8010/ws", "R2345678")
        == f"{HOSTED}/r/R2345678?relay=ws%3A%2F%2F127.0.0.1%3A8010%2Fws"
    )


def test_a_local_relay_that_serves_the_dashboard_opens_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "serves_dashboard", lambda base: True)
    assert links.room_url("ws://127.0.0.1:8010/ws", "R2345678") == "http://127.0.0.1:8010/r/R2345678"
    # Where a relay listens on every interface, the browser goes to loopback.
    assert links.room_url("ws://0.0.0.0:8000/ws", "R2345678") == "http://127.0.0.1:8000/r/R2345678"


def test_a_remote_relay_is_never_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(base: str) -> bool:
        raise AssertionError(f"probed {base}")

    monkeypatch.setattr(links, "serves_dashboard", boom)
    assert links.local_relay_dashboard("wss://relay.example/ws") is None


def test_the_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "serves_dashboard", lambda base: True)
    monkeypatch.setenv(links.DASHBOARD_ENV, "http://localhost:5173/")
    assert (
        links.room_url("ws://127.0.0.1:8010/ws", "R2345678")
        == "http://localhost:5173/r/R2345678?relay=ws%3A%2F%2F127.0.0.1%3A8010%2Fws"
    )


def test_the_probe_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(links, "serves_dashboard", lambda base: calls.append(base) or True)
    for _ in range(3):
        links.local_relay_dashboard("ws://localhost:8010/ws")
    assert calls == ["http://localhost:8010"]


class _Page(http.server.BaseHTTPRequestHandler):
    kind = "text/html; charset=utf-8"

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self.send_response(200)
        self.send_header("content-type", self.kind)
        self.end_headers()
        self.wfile.write(b"<!doctype html>")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[http.server.HTTPServer]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Page)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_the_probe_sees_an_html_page(server: http.server.HTTPServer) -> None:
    port = server.server_address[1]
    assert REAL_PROBE(f"http://127.0.0.1:{port}") is True
    _Page.kind = "application/json"
    try:
        assert REAL_PROBE(f"http://127.0.0.1:{port}") is False
    finally:
        _Page.kind = "text/html; charset=utf-8"


def test_the_probe_is_false_when_nothing_listens() -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert REAL_PROBE(f"http://127.0.0.1:{port}") is False
