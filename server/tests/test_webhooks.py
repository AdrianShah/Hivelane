"""Owner-configured webhooks (server/webhooks.py): the owner API, URL and SSRF
checks, payloads per kind, e2e redaction, retries, the circuit breaker and
the per-room budget. No network: a fake httpx transport and resolver."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

try:
    from server import main, webhooks
    from server.main import app
    from server.models import PROTOCOL_VERSION, RoomSettings
    from server.room import Room, add_event_sink, remove_event_sink
    from server.room_manager import RoomManager
    from server.store import MemoryRoomStore, RedisRoomStore, SqliteRoomStore
    from server.tests.fake_redis import FakeRedis
except ImportError:  # Render rootDir=server
    import main  # type: ignore[no-redef]
    import webhooks  # type: ignore[no-redef]
    from fake_redis import FakeRedis  # type: ignore[no-redef]
    from main import app  # type: ignore[no-redef]
    from models import PROTOCOL_VERSION, RoomSettings  # type: ignore[no-redef]
    from room import Room, add_event_sink, remove_event_sink  # type: ignore[no-redef]
    from room_manager import RoomManager  # type: ignore[no-redef]
    from store import MemoryRoomStore, RedisRoomStore, SqliteRoomStore  # type: ignore[no-redef]

SLACK = "https://hooks.slack.com/services/T000/B000/SECRETsecret1234"
DISCORD = "https://discord.com/api/webhooks/123/SECRETdiscordTOKEN"
GENERIC = "https://example.com/synergo/SECRETgeneric"
PUBLIC_IP = "93.184.216.34"
HEADER = webhooks.OWNER_TOKEN_HEADER


class Clock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeNet:
    """A resolver and an httpx transport that record what was asked of them."""

    def __init__(self) -> None:
        self.addresses: dict[str, list[str]] = {}
        self.requests: list[httpx.Request] = []
        self.responses: list[Any] = []  # httpx.Response | Exception | int, popped in order
        self.default: Any = 200

    async def resolve(self, host: str, port: int) -> list[str]:
        if host not in self.addresses:
            raise OSError("no such host")
        return list(self.addresses[host])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if self.responses else self.default
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            return httpx.Response(outcome)
        return outcome

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


def _service(
    net: FakeNet,
    *,
    development: bool = False,
    clock: Optional[Callable[[], float]] = None,
    load: Optional[Callable[[str], Optional[dict]]] = None,
    save: Optional[Callable[[str, dict], None]] = None,
) -> tuple[webhooks.WebhookService, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    service = webhooks.WebhookService(
        load=load or (lambda code: main.room_manager.load_webhooks(code)),
        save=save or (lambda code, record: main.room_manager.save_webhooks(code, record)),
        owner_check=lambda code, token: main.room_manager.verify_owner_token(code, token),
        transport_factory=net.transport,
        resolver=net.resolve,
        development=development,
        sleep=sleep,
        clock=clock or time.monotonic,
    )
    return service, slept


@pytest.fixture
def net() -> FakeNet:
    fake = FakeNet()
    for host in ("hooks.slack.com", "discord.com", "example.com"):
        fake.addresses[host] = [PUBLIC_IP]
    return fake


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch, net: FakeNet) -> Iterator[RoomManager]:
    manager = RoomManager(store=MemoryRoomStore())
    monkeypatch.setattr(main, "room_manager", manager)
    service, _ = _service(net)
    monkeypatch.setattr(main, "webhook_service", service)
    webhooks.api_limiter.reset()
    yield manager
    webhooks.api_limiter.reset()


def _room(client: TestClient, settings: Optional[dict] = None) -> tuple[str, str]:
    body = client.post("/rooms", json={"settings": settings} if settings else None).json()
    return body["room_id"], body["owner_token"]


def _add(client: TestClient, room_id: str, token: Optional[str], **body: Any) -> httpx.Response:
    payload = {"url": SLACK, "kind": "slack", "events": ["block"], **body}
    headers = {HEADER: token} if token else {}
    return client.post(f"/rooms/{room_id}/webhooks", json=payload, headers=headers)


def _block(**overrides: Any) -> dict[str, Any]:
    event = {
        "type": "edit_decision",
        "user_id": "sam",
        "agent": "cursor",
        "file_path": "server/room.py",
        "decision": "block",
        "others": ["priya"],
        "reason": "priya's claude-code edited server/room.py 3s ago <!channel>",
        "timestamp": 1_700_000_000.0,
        "scope": "symbol",
        "decision_id": "d1",
    }
    event.update(overrides)
    return event


def _notice(e2e: bool = False, **overrides: Any) -> webhooks.Notice:
    notice = webhooks.build_notice("K7Q2XMAB", e2e, _block(**overrides))
    assert notice is not None
    return notice


def _hook(url: str = SLACK, kind: str = "slack", events: tuple[str, ...] = ("block",)) -> webhooks.Webhook:
    return webhooks.Webhook("h1", url, kind, events, 0.0)


# --- auth -----------------------------------------------------------------------------


def test_only_the_owner_token_manages_webhooks(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _room(client)

    assert _add(client, room_id, None).status_code == 403
    assert _add(client, room_id, "not-the-token").status_code == 403
    assert client.get(f"/rooms/{room_id}/webhooks").status_code == 403
    assert client.delete(f"/rooms/{room_id}/webhooks/x", headers={HEADER: "nope"}).status_code == 403
    # An unknown code gets the same answer: no room-code oracle.
    unknown = _add(client, "ZZZZZZZZ", token)
    assert unknown.status_code == 403
    assert unknown.json()["detail"] == _add(client, room_id, "wrong").json()["detail"]

    created = _add(client, room_id, token)
    assert created.status_code == 201
    hook = created.json()["webhook"]
    assert hook["kind"] == "slack" and hook["events"] == ["block"]
    assert "SECRET" not in hook["url"] and hook["url"].startswith("https://hooks.slack.com/")

    listed = client.get(f"/rooms/{room_id}/webhooks", headers={HEADER: token}).json()
    assert [h["id"] for h in listed["webhooks"]] == [hook["id"]]
    assert "SECRET" not in json.dumps(listed)
    assert listed["max"] == 3

    gone = client.delete(f"/rooms/{room_id}/webhooks/{hook['id']}", headers={HEADER: token})
    assert gone.status_code == 204
    again = client.delete(f"/rooms/{room_id}/webhooks/{hook['id']}", headers={HEADER: token})
    assert again.status_code == 404
    assert client.get(f"/rooms/{room_id}/webhooks", headers={HEADER: token}).json()["webhooks"] == []


def test_owner_is_checked_against_the_stored_hash_for_a_room_with_no_live_state(
    relay: RoomManager,
) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    # The relay restarted: the code and its access record are in the store,
    # no Room in memory. Managing webhooks must not revive (and pin) it.
    restarted = RoomManager(store=relay._store)
    assert restarted.verify_owner_token(room_id, token)
    assert not restarted.verify_owner_token(room_id, token + "x")
    assert not restarted.verify_owner_token(room_id, None)
    assert restarted.room_count == 0


def test_the_owner_api_is_rate_limited_per_ip(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    codes = [
        client.get(f"/rooms/{room_id}/webhooks", headers={HEADER: token}).status_code
        for _ in range(webhooks.API_REQUESTS_PER_MINUTE + 1)
    ]
    assert codes[:-1] == [200] * webhooks.API_REQUESTS_PER_MINUTE
    assert codes[-1] == 429


def test_cors_lets_the_dashboard_send_the_owner_header_and_delete(relay: RoomManager) -> None:
    client = TestClient(app)
    response = client.options(
        "/rooms/ABCDEFGH/webhooks/x",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-synergo-owner-token",
        },
    )
    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]
    assert "x-synergo-owner-token" in response.headers["access-control-allow-headers"].lower()


# --- validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"url": "http://hooks.slack.com/services/x"},
        {"url": "ftp://example.com/x"},
        {"url": "https://user:pass@example.com/x"},
        {"url": "https://example.com/x#frag"},
        {"url": "https://"},
        {"url": "https://example.com/" + "a" * 2100},
        {"kind": "teams"},
        {"events": []},
        {"events": ["deploy"]},
        {"url": "https://nowhere.invalid/x"},
    ],
)
def test_bad_webhooks_are_refused(relay: RoomManager, body: dict) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    assert _add(client, room_id, token, **body).status_code == 422


def test_events_are_deduplicated_and_ordered(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    hook = _add(client, room_id, token, events=["join_request", "block", "block"]).json()["webhook"]
    assert hook["events"] == ["block", "join_request"]


def test_at_most_three_per_room_and_no_duplicates(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    assert _add(client, room_id, token, url=SLACK).status_code == 201
    assert _add(client, room_id, token, url=SLACK).status_code == 409
    assert _add(client, room_id, token, url=DISCORD, kind="discord").status_code == 201
    assert _add(client, room_id, token, url=GENERIC, kind="generic").status_code == 201
    full = _add(client, room_id, token, url=GENERIC + "2", kind="generic")
    assert full.status_code == 409 and "at most 3" in full.json()["detail"]


def test_http_localhost_only_on_a_development_relay(net: FakeNet) -> None:
    for host in ("localhost", "127.0.0.1", "[::1]", "app.localhost"):
        webhooks.parse_url(f"http://{host}:9000/hook", development=True)
        with pytest.raises(webhooks.WebhookUrlError, match="https"):
            webhooks.parse_url(f"http://{host}:9000/hook", development=False)
    with pytest.raises(webhooks.WebhookUrlError):
        webhooks.parse_url("http://example.com/hook", development=True)


# --- SSRF -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.1",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2002:a00:1::1",  # 6to4 of 10.0.0.1
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not webhooks.is_public_address(ipaddress.ip_address(address))


def test_public_addresses_are_allowed() -> None:
    for address in (PUBLIC_IP, "1.1.1.1", "2606:4700:4700::1111", "64:ff9b::808:808"):
        assert webhooks.is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "answers",
    [["10.0.0.5"], [PUBLIC_IP, "169.254.169.254"], ["::1"]],
)
def test_a_host_resolving_to_any_private_address_is_refused_in_production(
    relay: RoomManager, net: FakeNet, answers: list[str]
) -> None:
    net.addresses["internal.example.net"] = answers
    client = TestClient(app)
    room_id, token = _room(client)
    response = _add(client, room_id, token, url="https://internal.example.net/hook", kind="generic")
    assert response.status_code == 422
    assert "non-public" in response.json()["detail"]


def test_ip_literal_targets_are_checked_too(relay: RoomManager) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    assert _add(client, room_id, token, url="https://169.254.169.254/latest").status_code == 422
    assert _add(client, room_id, token, url="https://[::ffff:127.0.0.1]/x").status_code == 422


def test_development_relays_may_target_private_hosts(net: FakeNet) -> None:
    net.addresses["box.lan"] = ["192.168.1.20"]
    target = webhooks.parse_url("https://box.lan/hook", development=True)
    address = asyncio.run(webhooks.checked_address(target, resolver=net.resolve, development=True))
    assert address == "192.168.1.20"


def test_delivery_connects_to_the_checked_address_with_the_real_host_name(net: FakeNet) -> None:
    service, _ = _service(net, load=lambda code: None, save=lambda code, record: None)
    url = "https://example.com:8443/synergo/SECRETgeneric?x=1"
    assert asyncio.run(service.deliver(_hook(url, "generic"), _notice()))
    request = net.requests[0]
    assert request.url.host == PUBLIC_IP and request.url.port == 8443
    assert request.url.path == "/synergo/SECRETgeneric" and request.url.query == b"x=1"
    assert request.headers["host"] == "example.com:8443"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.headers["content-type"] == "application/json"


def test_an_unreachable_address_gives_way_to_the_next_checked_one(net: FakeNet) -> None:
    service, slept = _service(net, load=lambda code: None, save=lambda code, record: None)
    net.addresses["example.com"] = ["2606:4700::1111", PUBLIC_IP]
    net.responses = [httpx.ConnectError("refused"), 200]
    assert asyncio.run(service.deliver(_hook(GENERIC, "generic"), _notice()))
    assert [r.url.host for r in net.requests] == ["2606:4700::1111", PUBLIC_IP]
    assert slept == []  # one attempt, not a retry


def test_dns_rebinding_after_registration_is_caught_at_delivery(net: FakeNet) -> None:
    service, _ = _service(net, load=lambda code: None, save=lambda code, record: None)
    net.addresses["example.com"] = ["10.1.2.3"]  # was public when it was added
    assert not asyncio.run(service.deliver(_hook(GENERIC, "generic"), _notice()))
    assert net.requests == []  # nothing was sent anywhere
    status = service.status_of(_hook(GENERIC, "generic"))
    assert status["status"] == "failing" and "non-public" in status["last_error"]


# --- payloads -------------------------------------------------------------------------


def test_slack_payload_has_blocks_and_escapes_control_sequences() -> None:
    body = webhooks.format_slack(_notice())
    assert body["text"].startswith("Edit blocked: sam's cursor was blocked from editing server/room.py")
    text = json.dumps(body["blocks"])
    assert "`server/room.py`" in text
    assert "&lt;!channel&gt;" in text and "<!channel>" not in text
    fields = body["blocks"][1]["fields"]
    assert [f["text"].split("\n")[0] for f in fields] == ["*Who*", "*File*", "*Decision*", "*With*"]
    assert "block · symbol" in fields[2]["text"]
    assert "room K7Q2XMAB" in json.dumps(body["blocks"][-1])


def test_discord_payload_is_an_embed_that_never_pings() -> None:
    body = webhooks.format_discord(_notice(user_id="sam", reason="@everyone look"))
    assert body["allowed_mentions"] == {"parse": []}
    embed = body["embeds"][0]
    assert embed["title"] == "Edit blocked"
    assert {f["name"] for f in embed["fields"]} >= {"Who", "File", "Decision", "Reason"}
    assert embed["timestamp"] == "2023-11-14T22:13:20Z"
    assert embed["color"] == webhooks._DISCORD_COLORS["block"]


def test_generic_payload_carries_who_what_file_decision_scope() -> None:
    body = webhooks.format_generic(_notice())
    assert body["event"] == "block" and body["room"] == "K7Q2XMAB" and body["e2e"] is False
    assert body["data"] == {
        "user": "sam",
        "agent": "cursor",
        "file": "server/room.py",
        "decision": "block",
        "scope": "symbol",
        "others": ["priya"],
        "reason": "priya's claude-code edited server/room.py 3s ago <!channel>",
    }


def test_conflict_alerts_and_join_requests_have_notices_but_unrelated_ones_do_not() -> None:
    conflict = webhooks.build_notice(
        "ROOM",
        False,
        {
            "type": "conflict_alert",
            "users": ["sam", "priya"],
            "file_path": "a.py",
            "verdict": "overlapping",
            "explanation": "both renamed it",
            "timestamp": 1.0,
        },
    )
    assert conflict is not None and conflict.event == "conflict_alert"
    assert webhooks.format_generic(conflict)["data"]["users"] == ["sam", "priya"]
    assert webhooks.build_notice("ROOM", False, {"type": "conflict_alert", "verdict": "unrelated"}) is None
    join = webhooks.build_notice("ROOM", False, {"type": "join_request", "name": "kim", "agent": "codex"})
    assert join is not None and "kim" in join.summary
    assert webhooks.build_notice("ROOM", False, _block(decision="allow")) is None
    assert webhooks.build_notice("ROOM", False, {"type": "activity_update"}) is None


@pytest.mark.parametrize("kind", ["slack", "discord", "generic"])
def test_e2e_rooms_send_no_paths_names_or_reasons(kind: str) -> None:
    hashed = "h:0a1b2c3d4e5f60718293a4b5c6d7e8f9"
    notice = _notice(e2e=True, file_path=hashed, reason=f"sam edited {hashed}", user_id="sam")
    body = json.dumps(webhooks.FORMATTERS[kind](notice))
    assert "a blocked edit in room K7Q2XMAB" in body
    for leaked in (hashed, "sam", "priya", "edited", "symbol"):
        assert leaked not in body


# --- delivery: retries, breaker, budget --------------------------------------------------


def test_server_errors_are_retried_twice_with_backoff(net: FakeNet) -> None:
    service, slept = _service(net, load=lambda code: None, save=lambda code, record: None)
    net.responses = [500, 503, 200]
    assert asyncio.run(service.deliver(_hook(), _notice()))
    assert len(net.requests) == 3
    assert slept == list(webhooks.RETRY_BACKOFF_SECONDS)


def test_network_errors_and_429_are_retried_then_give_up(net: FakeNet) -> None:
    service, _ = _service(net, load=lambda code: None, save=lambda code, record: None)
    net.responses = [httpx.ConnectError("refused"), 429, httpx.ReadTimeout("slow")]
    assert not asyncio.run(service.deliver(_hook(), _notice()))
    assert len(net.requests) == 1 + webhooks.DELIVERY_RETRIES
    assert service.status_of(_hook())["last_error"] == "ReadTimeout"


def test_client_errors_are_not_retried(net: FakeNet) -> None:
    service, slept = _service(net, load=lambda code: None, save=lambda code, record: None)
    net.responses = [404]
    assert not asyncio.run(service.deliver(_hook(), _notice()))
    assert len(net.requests) == 1 and slept == []
    net.responses = [302]  # redirects are never followed
    assert not asyncio.run(service.deliver(_hook(), _notice()))
    assert len(net.requests) == 2


def test_the_breaker_opens_after_ten_consecutive_failures_and_recovers(net: FakeNet) -> None:
    clock = Clock()
    service, _ = _service(net, clock=clock, load=lambda code: None, save=lambda code, record: None)
    hook = _hook()
    net.default = 400  # one attempt per delivery
    for _ in range(webhooks.BREAKER_THRESHOLD):
        assert not asyncio.run(service.deliver(hook, _notice()))
    assert len(net.requests) == webhooks.BREAKER_THRESHOLD
    assert service.status_of(hook)["status"] == "paused"

    assert not asyncio.run(service.deliver(hook, _notice()))
    assert len(net.requests) == webhooks.BREAKER_THRESHOLD  # skipped, not sent

    clock.now += webhooks.BREAKER_COOLDOWN_SECONDS
    net.default = 200
    assert asyncio.run(service.deliver(hook, _notice()))
    assert service.status_of(hook) == {"status": "ok", "consecutive_failures": 0, "last_error": ""}


def test_a_success_resets_the_failure_count(net: FakeNet) -> None:
    service, _ = _service(net, load=lambda code: None, save=lambda code, record: None)
    net.responses = [400] * 9 + [200, 400]
    for _ in range(11):
        asyncio.run(service.deliver(_hook(), _notice()))
    assert service.status_of(_hook())["consecutive_failures"] == 1


def test_each_room_gets_thirty_notices_a_minute(net: FakeNet) -> None:
    clock = Clock()
    record = webhooks.hooks_record([_hook()])
    service, _ = _service(net, clock=clock, load=lambda code: record, save=lambda code, rec: None)

    async def burst(n: int) -> int:
        results = [await service.dispatch("ROOMA", False, _block()) for _ in range(n)]
        return sum(results)

    assert asyncio.run(burst(31)) == webhooks.ROOM_NOTICES_PER_MINUTE
    assert asyncio.run(service.dispatch("ROOMB", False, _block())) == 1  # per room
    clock.now += 60
    assert asyncio.run(burst(1)) == 1


def test_only_subscribed_events_are_delivered(net: FakeNet) -> None:
    record = webhooks.hooks_record([_hook(events=("conflict_alert",))])
    service, _ = _service(net, load=lambda code: record, save=lambda code, rec: None)
    assert asyncio.run(service.dispatch("ROOM", False, _block())) == 0
    assert net.requests == []


def test_urls_never_reach_the_logs(net: FakeNet, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    record = webhooks.hooks_record([_hook(SLACK), _hook(GENERIC, "generic")])
    service, _ = _service(net, load=lambda code: record, save=lambda code, rec: None)
    net.default = httpx.ConnectError(f"cannot reach {SLACK}")
    asyncio.run(service.dispatch("ROOM", False, _block()))
    net.default = 200  # httpx itself logs a URL per delivered request
    asyncio.run(service.dispatch("ROOM", False, _block()))
    assert len(net.requests) > 2
    assert "SECRET" not in caplog.text


# --- persistence ------------------------------------------------------------------------


def _round_trip(store: Any) -> None:
    store.add("ROOMCODE", time.time())
    assert store.get_webhooks("ROOMCODE") is None
    record = webhooks.hooks_record([_hook()])
    store.set_webhooks("ROOMCODE", record)
    assert webhooks.hooks_from_record(store.get_webhooks("ROOMCODE")) == [_hook()]
    store.set_webhooks("UNKNOWN1", record)  # unknown codes get nothing
    assert store.get_webhooks("UNKNOWN1") is None
    store.remove("ROOMCODE")
    assert store.get_webhooks("ROOMCODE") is None


def test_memory_store_keeps_webhooks() -> None:
    _round_trip(MemoryRoomStore())


def test_sqlite_store_keeps_webhooks_and_migrates_old_files(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE rooms (room_id TEXT PRIMARY KEY, created_at REAL NOT NULL, "
        "settings TEXT, access TEXT)"
    )
    conn.execute("INSERT INTO rooms VALUES ('OLDROOM1', ?, NULL, NULL)", (time.time(),))
    conn.commit()
    conn.close()
    store = SqliteRoomStore(str(path))
    assert store.get_webhooks("OLDROOM1") is None
    _round_trip(store)


def test_redis_store_keeps_webhooks() -> None:
    _round_trip(RedisRoomStore(FakeRedis()))


def test_webhooks_survive_a_restart_with_the_room(relay: RoomManager, net: FakeNet) -> None:
    client = TestClient(app)
    room_id, token = _room(client)
    assert _add(client, room_id, token).status_code == 201
    restarted = RoomManager(store=relay._store)
    hooks = webhooks.hooks_from_record(restarted.load_webhooks(room_id))
    assert [hook.url for hook in hooks] == [SLACK]


def test_unreadable_records_are_ignored() -> None:
    assert webhooks.hooks_from_record(None) == []
    assert webhooks.hooks_from_record({"hooks": "nope"}) == []
    assert webhooks.hooks_from_record({"hooks": [{"id": "x"}, 3, {"id": "y", "url": "u", "kind": "slack", "events": ["block"]}]}) == [
        webhooks.Webhook("y", "u", "slack", ("block",), 0.0)
    ]


# --- through the relay -------------------------------------------------------------------


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def _member(socket: Any, name: str) -> None:
    socket.send_json(
        {"type": "join", "name": name, "agent": "cursor", "protocol_version": PROTOCOL_VERSION}
    )


def _receive_until(socket: Any, wanted: str) -> dict:
    for _ in range(30):
        frame = socket.receive_json()
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"never received {wanted}")


@pytest.mark.parametrize("e2e", [False, True])
def test_a_blocked_edit_in_the_room_reaches_the_webhook(
    relay: RoomManager, net: FakeNet, e2e: bool
) -> None:
    client = TestClient(app)
    room_id, token = _room(client, {"e2e": True} if e2e else None)
    assert _add(client, room_id, token, kind="generic", url=GENERIC).status_code == 201
    path = "h:0a1b2c3d4e5f60718293a4b5c6d7e8f9" if e2e else "src/app.py"

    with client.websocket_connect(f"/ws/{room_id}") as sam:
        _member(sam, "sam")
        with client.websocket_connect(f"/ws/{room_id}") as priya:
            _member(priya, "priya")
            sam.send_json(
                {"type": "activity", "user_id": "sam", "changes": [{"file_path": path, "diff": ""}]}
            )
            _receive_until(priya, "synergo_feed")
            priya.send_json({"type": "edit_check", "request_id": "r1", "file_path": path})
            assert _receive_until(priya, "edit_check_response")["decision"] == "block"
            _wait_for(lambda: len(net.requests) >= 1)

    body = net.bodies()[0]
    assert body["event"] == "block" and body["room"] == room_id and body["e2e"] is e2e
    if e2e:
        assert body["data"] == {} and body["text"] == f"a blocked edit in room {room_id}"
        assert path not in json.dumps(body) and "priya" not in json.dumps(body)
    else:
        assert body["data"]["file"] == path and body["data"]["user"] == "priya"
        assert body["data"]["others"] == ["sam"]


def test_join_requests_reach_the_event_sinks() -> None:
    seen: list[tuple[str, str]] = []

    def sink(room: Room, payload: dict) -> None:
        seen.append((room.code, payload["type"]))

    add_event_sink(sink)
    try:
        room = Room()
        room.code = "ROOMCODE"

        async def scenario() -> None:
            pending = room.open_pending("kim", "codex")
            assert pending is not None
            await room.send_to_owners(pending.request())
            await room.send_to_owners(room.policy_update())  # not a join request

        asyncio.run(scenario())
    finally:
        remove_event_sink(sink)
    assert seen == [("ROOMCODE", "join_request")]


def test_a_failing_sink_never_breaks_the_broadcast() -> None:
    def broken(room: Room, payload: dict) -> None:
        raise RuntimeError("boom")

    add_event_sink(broken)
    try:
        room = Room(RoomSettings())

        async def scenario() -> None:
            await room.broadcast(room.policy_update())

        asyncio.run(scenario())
    finally:
        remove_event_sink(broken)
