"""Security headers on every HTTP response the relay sends.

A pure ASGI middleware rather than Starlette's BaseHTTPMiddleware: it only
rewrites the response-start message, so streaming bodies, background tasks and
WebSocket upgrades pass through untouched (WebSocket scopes are skipped).

- X-Content-Type-Options: nosniff and Referrer-Policy: no-referrer everywhere.
  no-referrer matters more than usual here: a dashboard URL carries the room
  code (/r/<code>), so it must never leak to another site through a Referer
  header.
- Strict-Transport-Security only when the deploy is known to sit behind TLS
  (HIVELANE_ENV=staging or production): sent from a plain-http dev relay it
  would pin the browser to https for localhost.
- Content-Security-Policy (plus X-Frame-Options) on HTML responses only, i.e.
  when the relay serves the bundled dashboard. JSON and metrics need none.
- An unhandled exception still gets a 500 with these headers: Starlette's
  ServerErrorMiddleware sits outside every user middleware, so its own 500
  would otherwise go out bare.

The CSP fits the dashboard as built today: frontend/index.html loads Tailwind's
browser build from cdn.tailwindcss.com, configures it in an inline script, sets
the theme in another inline script, and pulls fonts from Google Fonts.

- script-src: no 'unsafe-inline'. The dashboard server (dashboard.py) hashes
  the inline scripts of the index.html it serves and allows exactly those
  ('sha256-...'), so injected markup cannot run script. DASHBOARD_CSP below,
  with 'unsafe-inline', is only the fallback for HTML the relay serves without
  knowing its scripts.
- style-src keeps 'unsafe-inline': the Tailwind Play CDN injects its generated
  CSS as <style> elements at runtime.
- connect-src allows ws:, wss: and https: because the relay URL is baked in
  at build time (VITE_WS_URL) and may differ from the page's origin; the
  dashboard then also fetches POST /rooms and the webhook API over https.

Follow-up once the redesigned dashboard compiles Tailwind at build time (no
Play CDN, config in the bundle, theme script in a bundled file): drop
https://cdn.tailwindcss.com from script-src, drop 'unsafe-inline' from
style-src, and narrow connect-src to 'self' plus the configured relay origin.
See docs/security.md.

frame-ancestors 'none' keeps the dashboard out of other sites' frames, so
nobody can overlay an owner's "Approve" button.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterable

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

HSTS_VALUE = "max-age=31536000; includeSubDomains"

TAILWIND_CDN = "https://cdn.tailwindcss.com"


def csp_hash(script: str) -> str:
    """The CSP source expression allowing one inline script body."""
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def build_dashboard_csp(script_hashes: Iterable[str] | None = None) -> str:
    """The dashboard's policy. With `script_hashes` (csp_hash values) inline
    scripts are allowed by hash; without, by 'unsafe-inline'."""
    if script_hashes is None:
        inline = "'unsafe-inline'"
    else:
        inline = " ".join(sorted(set(script_hashes)))
    script_src = " ".join(part for part in ("'self'", inline, TAILWIND_CDN) if part)
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src {script_src}",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com",
            "img-src 'self' data:",
            "connect-src 'self' ws: wss: https:",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


DASHBOARD_CSP = build_dashboard_csp()


class SecurityHeadersMiddleware:
    """Adds the headers above. Headers a route already set are left alone,
    so one endpoint can still opt into a different policy."""

    def __init__(self, app: ASGIApp, *, hsts: bool, csp: str = DASHBOARD_CSP) -> None:
        self.app = app
        self.hsts = hsts
        self.csp = csp

    def _apply(self, headers: MutableHeaders) -> None:
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "no-referrer")
        if self.hsts:
            headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        content_type = headers.get("content-type", "")
        if content_type.lower().startswith("text/html"):
            headers.setdefault("Content-Security-Policy", self.csp)
            headers.setdefault("X-Frame-Options", "DENY")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                self._apply(MutableHeaders(scope=message))
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if started:
                raise
            # Answer the 500 here, with the headers, then re-raise so the
            # error is still logged by ServerErrorMiddleware / the server
            # (which see the response as started and send nothing more).
            started = True
            message: Message = {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
            self._apply(MutableHeaders(scope=message))
            await send(message)
            await send({"type": "http.response.body", "body": b"Internal Server Error"})
            raise
