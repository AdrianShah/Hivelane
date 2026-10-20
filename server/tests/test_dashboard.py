"""The relay serving the bundled dashboard: static files, the single-page-app
fallback, and never shadowing the API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

try:
    from server import dashboard, main
except ImportError:  # Render rootDir=server
    import dashboard  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]

INDEX = "<!doctype html><title>Hivelane</title><div id=root></div>"


@pytest.fixture
def build(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-3f2a1b.js").write_text("console.log('hi')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the build", encoding="utf-8")
    return root


@pytest.fixture
def client(build: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real relay app with the dashboard installed; the fallback is put
    back afterwards so other tests see the relay as configured."""
    monkeypatch.setattr(main.app.router, "default", main.app.router.default)
    assert dashboard.install(main.app, str(build)) is True
    return TestClient(main.app)


def test_not_installed_without_a_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dashboard.STATIC_DIR_ENV, raising=False)
    monkeypatch.setattr(main.app.router, "default", main.app.router.default)

    assert dashboard.install(main.app) is False
    assert TestClient(main.app).get("/").status_code == 404


def test_not_installed_from_a_directory_without_index_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main.app.router, "default", main.app.router.default)

    assert dashboard.install(main.app, str(tmp_path)) is False
    assert "has no index.html" in (dashboard.static_dir_problem(str(tmp_path)) or "")


def test_the_root_serves_the_dashboard(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/r/ABCDEFGH", "/room/K7M2PQ", "/some/client/route"])
def test_client_routes_fall_back_to_index_html(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-cache"


def test_fingerprinted_assets_are_cached_for_good(client: TestClient) -> None:
    response = client.get("/assets/index-3f2a1b.js")

    assert response.status_code == 200
    assert response.text == "console.log('hi')"
    assert "immutable" in response.headers["cache-control"]


def test_other_files_are_served_as_themselves(client: TestClient) -> None:
    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.text == "<svg/>"


@pytest.mark.parametrize("path", ["/assets/gone-123.js", "/missing.css", "/robots.txt"])
def test_a_missing_file_is_a_404_not_the_dashboard(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert INDEX not in response.text


def test_head_requests_work(client: TestClient) -> None:
    assert client.head("/r/ABCDEFGH").status_code == 200


def test_files_outside_the_build_are_not_reachable(client: TestClient) -> None:
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        response = client.get(path)
        assert "outside the build" not in response.text


# --- the API keeps working -------------------------------------------------------------


def test_health_readyz_and_metrics_are_still_the_relays(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"
    assert client.get("/metrics").headers["content-type"].startswith("text/plain")


def test_room_creation_still_works(client: TestClient) -> None:
    response = client.post("/rooms")

    assert response.status_code == 200
    assert "room_id" in response.json()


@pytest.mark.parametrize("path", ["/rooms/ABCDEFGH", "/ws/ABCDEFGH", "/health/deep", "/metrics/x"])
def test_api_paths_never_get_the_dashboard(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code in (404, 405)
    assert INDEX not in response.text


def test_non_get_requests_are_not_answered_with_the_dashboard(client: TestClient) -> None:
    response = client.post("/r/ABCDEFGH")

    assert response.status_code in (404, 405)
    assert INDEX not in response.text


def test_websockets_still_reach_the_relay(client: TestClient) -> None:
    with client.websocket_connect("/ws/ZZZZZZZZ") as ws:
        assert ws.receive_json()["type"] == "room_rejected"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_unknown_websocket_paths_are_refused(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/not-a-socket"):
            pass
