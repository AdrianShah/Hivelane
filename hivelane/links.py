"""Dashboard links: where a room opens in the browser. Standard library
only, so the CLI (whose import every hook pays for) and the desktop bee can
share them without importing each other."""

from __future__ import annotations

import os

DEFAULT_DASHBOARD_LOCAL = "http://localhost:5173"
DEFAULT_DASHBOARD = "https://adrianshah.github.io/Hivelane"
DASHBOARD_ENV = "HIVELANE_DASHBOARD"


def relay_is_local(relay: str) -> bool:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(relay.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def dashboard_base(relay: str) -> str:
    override = os.environ.get(DASHBOARD_ENV, "").strip()
    if override:
        return override.rstrip("/")
    return DEFAULT_DASHBOARD_LOCAL if relay_is_local(relay) else DEFAULT_DASHBOARD


def room_url(relay: str, room: str) -> str:
    """The dashboard link for room. A non-local relay rides along as
    ?relay=<url-encoded>, so whoever opens the link reaches the relay the
    room lives on (frontend/src/relayUrl.ts reads it)."""
    url = f"{dashboard_base(relay)}/r/{room}"
    if relay and not relay_is_local(relay):
        from urllib.parse import quote

        url += "?relay=" + quote(relay.strip(), safe="")
    return url
