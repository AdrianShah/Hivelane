"""Dashboard links: where a room opens in the browser. Standard library
only, so the CLI (whose import every hook pays for) and the desktop bee can
share them without importing each other.

Which dashboard a room link points at:

1. HIVELANE_DASHBOARD, when set (working on the dashboard under Vite, say).
2. A relay on this machine that serves the dashboard itself (`hivelane relay`
   finds frontend/dist, the Docker image): its own origin. That page talks to
   the relay it came from, so the link needs nothing else.
3. Otherwise the hosted dashboard on GitHub Pages, told which relay to reach
   with ?relay=. That is the one every teammate can open: it needs no local
   build and no `npm run dev`.
"""

from __future__ import annotations

import os

DEFAULT_DASHBOARD = "https://adrianshah.github.io/Hivelane"
DASHBOARD_ENV = "HIVELANE_DASHBOARD"
# How long a probe of a local relay's own dashboard waits; loopback answers
# (or refuses) in well under this.
PROBE_TIMEOUT = 0.5
# A long-running caller (the daemon, for the bee) asks again after this long.
PROBE_TTL = 60.0
# Annotations stay strings (__future__): typing is too slow an import for
# the hook path, which imports this module.
_probed: dict[str, tuple[float, str | None]] = {}


def relay_is_local(relay: str) -> bool:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(relay.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def relay_http_base(relay: str) -> str | None:
    """ws://host:port/ws as http://host:port (wss:// as https://), or None."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(relay.strip())
    except ValueError:
        return None
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(parts.scheme)
    if not scheme or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/ws"):
        path = path[: -len("/ws")]
    # 0.0.0.0 is where a relay listens, not an address a browser can open.
    netloc = parts.netloc.replace("0.0.0.0", "127.0.0.1")
    return f"{scheme}://{netloc}{path}"


def local_relay_dashboard(relay: str) -> str | None:
    """The dashboard a relay on this machine serves itself, or None (not
    local, not running, or no build bundled). Cached for PROBE_TTL."""
    if not relay_is_local(relay):
        return None
    base = relay_http_base(relay)
    if base is None:
        return None
    import time

    now = time.monotonic()
    hit = _probed.get(base)
    if hit is not None and now - hit[0] < PROBE_TTL:
        return hit[1]
    found = base if serves_dashboard(base) else None
    _probed[base] = (now, found)
    return found


def serves_dashboard(base: str) -> bool:
    """Whether GET base/ answers with an HTML page (the relay's bundled
    dashboard; a relay without one answers 404 JSON)."""
    import urllib.request

    try:
        with urllib.request.urlopen(base + "/", timeout=PROBE_TIMEOUT) as resp:  # noqa: S310 - loopback only
            return bool(resp.status == 200 and resp.headers.get("content-type", "").lower().startswith("text/html"))
    except (OSError, ValueError):
        return False


def dashboard_base(relay: str) -> str:
    override = os.environ.get(DASHBOARD_ENV, "").strip()
    if override:
        return override.rstrip("/")
    return local_relay_dashboard(relay) or DEFAULT_DASHBOARD


class InviteTarget:
    """Parsed invite link or room code: room code, optional relay, optional
    e2e key, optional invite secret (what lets the holder in without an
    owner's approval)."""

    __slots__ = ("room", "relay", "key", "invite")

    def __init__(
        self,
        room: str,
        relay: str | None = None,
        key: str | None = None,
        invite: str | None = None,
    ) -> None:
        self.room = room
        self.relay = relay
        self.key = key
        self.invite = invite

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InviteTarget):
            return NotImplemented
        return (self.room, self.relay, self.key, self.invite) == (
            other.room,
            other.relay,
            other.key,
            other.invite,
        )

    def __repr__(self) -> str:
        return (
            f"InviteTarget(room={self.room!r}, relay={self.relay!r}, "
            f"key={self.key!r}, invite={self.invite!r})"
        )


def _canonical_room(raw: str) -> str:
    return "".join((raw or "").split()).replace("-", "").upper()


def _valid_room_code(code: str) -> bool:
    if not code or len(code) > 64:
        return False
    return all(ch.isascii() and (ch.isalnum() or ch == "_") for ch in code)


def room_url(relay: str, room: str) -> str:
    """The dashboard link for room. Unless the relay serves this dashboard
    itself, the relay rides along as ?relay=<url-encoded>, so whoever opens
    the link reaches the relay the room lives on (frontend/src/relayUrl.ts
    reads it)."""
    base = dashboard_base(relay)
    url = f"{base}/r/{room}"
    if relay and base != relay_http_base(relay):
        from urllib.parse import quote

        url += "?relay=" + quote(relay.strip(), safe="")
    return url


# The fragment parameter carrying a room's invite secret. Mirrors
# INVITE_PARAM in frontend/src/relayUrl.ts.
INVITE_PARAM = "invite"


def invite_url(relay: str, room: str, key: str | None = None, invite: str | None = None) -> str:
    """The one share link for `room` (every caller builds it here):
    `<dashboard>/j/<ROOM>` with `?relay=` when the dashboard is hosted apart
    from the relay, then a fragment (never sent to any server, so it stays out
    of logs and Referer) carrying `key=<e2e passphrase>` and
    `invite=<invite secret>` when given, e.g. `#key=...&invite=...`.

    The invite secret is what lets whoever opens the link in without an
    owner's approval; the room code alone only knocks."""
    from urllib.parse import quote

    base = dashboard_base(relay)
    code = _canonical_room(room)
    url = f"{base}/j/{code}"
    if relay and base != relay_http_base(relay):
        url += "?relay=" + quote(relay.strip(), safe="")
    fragment: list[str] = []
    if key and key.strip():
        fragment.append("key=" + quote(key.strip(), safe=""))
    if invite and invite.strip():
        fragment.append(f"{INVITE_PARAM}=" + quote(invite.strip(), safe=""))
    if fragment:
        url += "#" + "&".join(fragment)
    return url


def _parse_fragment_invite(fragment: str) -> str | None:
    """The invite secret from `#invite=...` (alone or beside `key=`)."""
    body = (fragment or "").lstrip("#").strip()
    if "=" not in body:
        return None
    from urllib.parse import unquote

    for part in body.split("&"):
        name, sep, value = part.partition("=")
        if sep and name == INVITE_PARAM:
            decoded = unquote(value).strip()
            if decoded:
                return decoded
    return None


INSTALL_COMMAND = "pipx install git+https://github.com/AdrianShah/Hivelane"


def share_link(relay: str, room: str, invite: str | None = None) -> str:
    """The link teammates are given to join `room`: the one place the CLI
    builds it (`hivelane up`'s share line, the agent prompt, the MCP server),
    so a change to what a share link carries is made here once. Never carries
    an e2e room key: that goes to teammates separately."""
    return invite_url(relay, room, invite=invite)


def agent_join_prompt(relay: str, room: str, *, insecure: bool = False, invite: str | None = None) -> str:
    """The sentence a person pastes into any coding agent to bring a repo into
    `room` (mirrors agentJoinPrompt in frontend/src/links.ts). The link is
    quoted: `?` in it is a glob character in zsh."""
    flags = " --insecure" if insecure else ""
    code = _canonical_room(room)
    return (
        f"Join Hivelane room {code} in this repo: install hivelane if needed "
        f"(`{INSTALL_COMMAND}`), then run `hivelane up --room \"{share_link(relay, room, invite)}\" -y{flags}`."
    )


def _parse_fragment_key(fragment: str) -> str | None:
    """Extract an e2e room key from `#key=...`, `#k=...`, or a bare `#<key>`."""
    body = (fragment or "").lstrip("#").strip()
    if not body:
        return None
    from urllib.parse import unquote

    if "=" in body:
        parts = [p for p in body.split("&") if p]
        for target_name in ("key", "k"):
            for part in parts:
                if "=" not in part:
                    continue
                name, _, value = part.partition("=")
                if name == target_name:
                    decoded = unquote(value).strip()
                    if decoded:
                        return decoded
        return None
    decoded = unquote(body).strip()
    return decoded or None


def parse_invite_link(link_or_room: str) -> InviteTarget:
    """Parse an invite URL (`/j/<ROOM>?relay=...#key=...&invite=...`,
    `/r/<ROOM>...`) or bare room code into an `InviteTarget(room, relay, key,
    invite)`. Raises ValueError if invalid."""
    raw = (link_or_room or "").strip()
    if not raw:
        raise ValueError("invite link or room code is empty")

    if "://" not in raw and "/" not in raw and "?" not in raw and "#" not in raw:
        code = _canonical_room(raw)
        if not _valid_room_code(code):
            raise ValueError(f"{raw!r} is not a room code (letters and digits, e.g. K7Q2XM)")
        return InviteTarget(room=code, relay=None, key=None)

    from urllib.parse import parse_qs, urlsplit

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ValueError(f"invalid invite link {raw!r}: {exc}") from None

    if parts.scheme and parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"invalid invite link {raw!r}: expected an http(s) dashboard URL or room code")

    segments = [seg for seg in parts.path.split("/") if seg]
    room_seg: str | None = None
    for idx in range(len(segments) - 1):
        if segments[idx] in ("j", "r", "room"):
            room_seg = segments[idx + 1]
    if room_seg is None:
        raise ValueError(f"invite link {raw!r} has no /j/<ROOM> room code")

    code = _canonical_room(room_seg)
    if not _valid_room_code(code):
        raise ValueError(f"{room_seg!r} in invite link is not a valid room code")

    query = parse_qs(parts.query, keep_blank_values=False)
    relay_values = query.get("relay") or []
    relay = relay_values[0].strip() if relay_values and relay_values[0].strip() else None
    key = _parse_fragment_key(parts.fragment)
    invite = _parse_fragment_invite(parts.fragment)
    return InviteTarget(room=code, relay=relay, key=key, invite=invite)
