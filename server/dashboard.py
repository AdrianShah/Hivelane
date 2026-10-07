"""Serve the built dashboard (frontend/dist) from the relay itself.

Off unless SYNERGO_STATIC_DIR names a directory with an index.html: the Docker
image sets it, `synergo relay` sets it when it finds a build, and a dev relay
leaves it unset (the dashboard then runs on Vite's dev server).

Served as the router's not-found handler, so it can never shadow an API route:
/ws, /rooms, /health, /readyz and /metrics, and any route added later, match
first. On top of that, paths under those prefixes are never answered with the
dashboard, so GET /rooms/whatever is still a JSON 404 rather than an HTML page.

The dashboard is a single-page app, so any other GET that is not a file gets
index.html and the client router takes it from there (/r/<code> and so on).
A dashboard served this way talks to the relay on its own origin, which is
what frontend/src/relayUrl.ts falls back to when built without VITE_WS_URL;
the browser then sends no cross-origin requests and CORS_ORIGINS does not need
to list it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

STATIC_DIR_ENV = "SYNERGO_STATIC_DIR"

# Owned by the relay's API, never by the dashboard.
API_PREFIXES = ("/ws", "/rooms", "/health", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json")

# Vite fingerprints everything under assets/, so a cached copy never goes stale.
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
# index.html names the current fingerprints: always revalidate it.
INDEX_CACHE = "no-cache"
OTHER_CACHE = "public, max-age=3600"


def _is_api_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in API_PREFIXES)


def _looks_like_file(path: str) -> bool:
    """A missing /assets/app.js must be a 404, not index.html served as script."""
    last = path.rsplit("/", 1)[-1]
    return "." in last or path.startswith("/assets/")


class DashboardApp:
    """Static files with a single-page-app fallback to index.html."""

    def __init__(self, directory: Path, not_found: ASGIApp) -> None:
        self.directory = directory
        self.static = StaticFiles(directory=str(directory), html=False)
        self.not_found = not_found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path") or "/"
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD") or _is_api_path(path):
            await self.not_found(scope, receive, send)
            return
        try:
            response = await self.static.get_response(self.static.get_path(scope), scope)
            cache = IMMUTABLE_CACHE if path.startswith("/assets/") else OTHER_CACHE
        except HTTPException as exc:
            if exc.status_code != 404 or _looks_like_file(path):
                await self.not_found(scope, receive, send)
                return
            response = await self.static.get_response("index.html", scope)
            cache = INDEX_CACHE
        if path in ("/", "/index.html"):
            cache = INDEX_CACHE
        # Security headers (CSP, framing, nosniff) are added for every HTML
        # response by headers.SecurityHeadersMiddleware.
        response.headers.setdefault("cache-control", cache)
        await response(scope, receive, send)


def static_dir_problem(directory: Optional[str]) -> Optional[str]:
    """Why `directory` cannot be served as the dashboard, or None if it can."""
    if not directory:
        return f"{STATIC_DIR_ENV} is not set"
    if not (Path(directory) / "index.html").is_file():
        return f"{directory} has no index.html (build it with `npm run build` in frontend/)"
    return None


def install(app: Any, directory: Optional[str] = None) -> bool:
    """Serve the dashboard from `directory` (default: $SYNERGO_STATIC_DIR) as
    `app`'s fallback for unmatched requests. Returns whether it is served."""
    directory = os.environ.get(STATIC_DIR_ENV) if directory is None else directory
    if not directory:
        return False
    problem = static_dir_problem(directory)
    if problem is not None:
        log.warning("Not serving the dashboard: %s", problem)
        return False
    root = Path(directory).resolve()
    app.router.default = DashboardApp(root, app.router.default)
    log.info("Serving the dashboard from %s", root)
    return True
