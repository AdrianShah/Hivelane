# syntax=docker/dockerfile:1
# One image for the relay and the dashboard it serves. Host-agnostic: anything
# that runs a container can run this, so the deploy target stays a choice
# rather than a rewrite. Published as ghcr.io/adrianshah/synergo-relay
# (.github/workflows/image.yml); guide: docs/self-hosting.md.

# --- dashboard ---------------------------------------------------------------
# A static build, identical on every architecture, so it runs on the build
# host's own platform even when the image is cross-built for arm64.
FROM --platform=$BUILDPLATFORM node:20-slim AS dashboard

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Unset by default: the bundled dashboard then talks to the relay that serves
# it, on its own origin (frontend/src/relayUrl.ts). Pass
# --build-arg VITE_WS_URL=wss://relay.example.com/ws to point it elsewhere.
ARG VITE_WS_URL
RUN npm run build

# --- relay ---------------------------------------------------------------------
FROM python:3.12-slim AS relay

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the install layer.
# redis is optional for the relay, imported only when SYNERGO_REDIS_URL is set
# (shared room codes, room leases and rate limits across instances; see
# docs/self-hosting.md#scaling-out), but baked in so one image serves every
# topology. It stays out of server/requirements.txt so a plain
# single-instance install does not need it.
COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt redis==8.1.0

COPY server/ ./server/
COPY --from=dashboard /frontend/dist/ ./static/

# Rooms are only durable if SYNERGO_STATE_PATH points at a mounted volume;
# without one the container still runs, and logs a warning at startup.
# SYNERGO_STATIC_DIR serves the dashboard at / (unset it to serve the API only).
ENV SYNERGO_STATE_PATH=/data/synergo.db \
    SYNERGO_STATIC_DIR=/app/static
RUN mkdir -p /data && useradd --create-home --uid 10001 synergo \
    && chown -R synergo:synergo /data /app
USER synergo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4).status==200 else 1)"

# $PORT is honoured because most hosts inject it; 8000 locally.
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
