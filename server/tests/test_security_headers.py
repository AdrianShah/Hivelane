"""Transport hardening on the relay's HTTP surface, and what it keeps out of
its logs: security headers on every response (HSTS only where TLS is certain,
CSP only on HTML), production refusing a wildcard CORS policy, and prompts
and diffs never written to the log."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

try:
    from server import config, main
    from server.headers import DASHBOARD_CSP, HSTS_VALUE, SecurityHeadersMiddleware
    from server.main import app
    from server.models import PROTOCOL_VERSION
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore
except ImportError:  # Render rootDir=server
    import config  # type: ignore[no-redef]
    import main  # type: ignore[no-redef]
    from headers import (  # type: ignore[no-redef]
        DASHBOARD_CSP,
        HSTS_VALUE,
        SecurityHeadersMiddleware,
    )
    from main import app  # type: ignore[no-redef]
    from models import PROTOCOL_VERSION  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore  # type: ignore[no-redef]

PRODUCTION = {"SYNERGO_ENV": "production", "CORS_ORIGINS": "https://app.example.com"}


def _site(*, hsts: bool) -> TestClient:
    async def page(_request: Any) -> HTMLResponse:
        return HTMLResponse("<!doctype html><title>dash</title>")

    async def data(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def own_policy(_request: Any) -> HTMLResponse:
        return HTMLResponse("<p>x</p>", headers={"Content-Security-Policy": "default-src 'none'"})

    site = Starlette(
        routes=[Route("/", page), Route("/data", data), Route("/own", own_policy)]
    )
    site.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    return TestClient(site)


# --- headers -----------------------------------------------------------------------


def test_relay_responses_carry_the_baseline_headers() -> None:
    response = TestClient(app).get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    # JSON: no page policy needed.
    assert "content-security-policy" not in response.headers


def test_error_responses_carry_them_too() -> None:
    response = TestClient(app).get("/definitely-not-a-route")

    assert response.headers["x-content-type-options"] == "nosniff"


def test_relay_sends_hsts_exactly_when_configured() -> None:
    response = TestClient(app).get("/health")

    assert ("strict-transport-security" in response.headers) is config.HSTS_ENABLED
    registered = [m for m in app.user_middleware if m.cls is SecurityHeadersMiddleware]
    assert len(registered) == 1
    assert registered[0].kwargs["hsts"] is config.HSTS_ENABLED


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, False),
        ({"SYNERGO_ENV": "development"}, False),
        ({"SYNERGO_ENV": "staging", "CORS_ORIGINS": "https://a.example"}, True),
        (PRODUCTION, True),
    ],
)
def test_hsts_only_outside_development(env: dict[str, str], expected: bool) -> None:
    assert config.load_settings(env).hsts_enabled is expected


def test_hsts_header_when_enabled() -> None:
    with_hsts = _site(hsts=True)
    without = _site(hsts=False)

    assert with_hsts.get("/data").headers["strict-transport-security"] == HSTS_VALUE
    assert with_hsts.get("/").headers["strict-transport-security"] == HSTS_VALUE
    assert "strict-transport-security" not in without.get("/data").headers


def test_csp_and_framing_only_on_html() -> None:
    site = _site(hsts=False)

    page = site.get("/")
    data = site.get("/data")

    assert page.headers["content-security-policy"] == DASHBOARD_CSP
    assert page.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" not in data.headers
    assert "x-frame-options" not in data.headers
    assert data.headers["x-content-type-options"] == "nosniff"


def test_a_route_can_keep_its_own_policy() -> None:
    assert _site(hsts=False).get("/own").headers["content-security-policy"] == (
        "default-src 'none'"
    )


def test_dashboard_csp_allows_what_the_dashboard_needs_and_little_else() -> None:
    directives = dict(
        (part.split(" ", 1) + [""])[:2] for part in DASHBOARD_CSP.split("; ")
    )

    assert directives["default-src"] == "'self'"
    assert directives["connect-src"] == "'self' ws: wss: https:"
    assert directives["img-src"] == "'self' data:"
    assert directives["style-src"].startswith("'self' 'unsafe-inline'")
    assert directives["frame-ancestors"] == "'none'"
    assert directives["object-src"] == "'none'"


def test_websockets_are_untouched_by_the_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "room_manager", RoomManager(store=MemoryRoomStore()))
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as dashboard:
        dashboard.send_json({"type": "join", "name": None})
        assert dashboard.receive_json()["type"] == "room_settings"


# --- CORS in production -------------------------------------------------------------


@pytest.mark.parametrize(
    "regex", [".*", ".+", "^.*$", "https?://.*", "https://.*", "(.*)", r"[\s\S]*"]
)
def test_production_refuses_a_wildcard_origin_regex(regex: str) -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({**PRODUCTION, "CORS_ORIGIN_REGEX": regex})

    assert "CORS_ORIGIN_REGEX" in str(excinfo.value)


def test_production_refuses_a_star_origin() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({**PRODUCTION, "CORS_ORIGINS": "https://app.example.com,*"})

    assert "CORS_ORIGINS" in str(excinfo.value)


def test_production_accepts_a_regex_scoped_to_a_domain() -> None:
    settings = config.load_settings(
        {**PRODUCTION, "CORS_ORIGIN_REGEX": r"https://[a-z0-9-]+\.example\.com"}
    )

    assert settings.cors_origin_regex == r"https://[a-z0-9-]+\.example\.com"


def test_wildcards_are_tolerated_outside_production() -> None:
    assert config.load_settings({"CORS_ORIGIN_REGEX": ".*"}).cors_origin_regex == ".*"
    staging = {"SYNERGO_ENV": "staging", "CORS_ORIGINS": "*"}
    assert config.load_settings(staging).cors_origins == ("*",)


def test_an_invalid_origin_regex_fails_the_boot() -> None:
    with pytest.raises(config.ConfigError) as excinfo:
        config.load_settings({"CORS_ORIGIN_REGEX": "https://(unclosed"})

    assert "not a valid regex" in str(excinfo.value)


# --- logs ------------------------------------------------------------------------------


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    yield manager


def _join(socket: Any, name: str) -> None:
    socket.send_json(
        {"type": "join", "name": name, "agent": "cursor", "protocol_version": PROTOCOL_VERSION}
    )


def _next_event(socket: Any, type_name: str) -> dict:
    for _ in range(20):
        frame = socket.receive_json()
        events = frame["events"] if frame.get("type") == "synergo_feed" else [frame]
        for event in events:
            if event.get("type") == type_name:
                return event
    raise AssertionError(f"never received {type_name}")


def test_prompts_and_diffs_never_reach_the_log(
    relay: RoomManager, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json({"type": "intent", "user_id": "sam", "prompt": "SECRET-INTENT-1"})
        sam.send_json(
            {
                "type": "activity",
                "user_id": "sam",
                "prompt": "SECRET-PROMPT-2",
                "changes": [{"file_path": "a.py", "diff": "+SECRET-DIFF-3"}],
            }
        )
        sam.send_json(
            {"type": "plan", "user_id": "sam", "summary": "SECRET-PLAN-4", "files": ["a.py"]}
        )
        _next_event(sam, "plan_conflicts")
        _next_event(sam, "activity_update")
        # Malformed: pydantic's own message would echo the input.
        sam.send_json({"type": "activity", "user_id": "sam", "prompt": "SECRET-BAD-5"})
        sam.send_json({"type": "intent", "prompt": ["SECRET-BAD-6"]})
        sam.send_json({"type": "edit_check", "request_id": "r1", "file_path": "a.py"})
        _next_event(sam, "edit_check_response")

    for secret in (
        "SECRET-INTENT-1",
        "SECRET-PROMPT-2",
        "SECRET-DIFF-3",
        "SECRET-PLAN-4",
        "SECRET-BAD-5",
        "SECRET-BAD-6",
    ):
        assert secret not in caplog.text
    assert "intent from sam (15 chars)" in caplog.text
    assert "Ignoring malformed activity from sam: changes: Field required" in caplog.text


def test_intents_are_not_logged_at_info(
    relay: RoomManager, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    client = TestClient(app)
    room_id = client.post("/rooms").json()["room_id"]

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _join(sam, "sam")
        sam.send_json({"type": "intent", "user_id": "sam", "prompt": "refactor billing"})
        _next_event(sam, "intent_update")

    assert "intent from" not in caplog.text
