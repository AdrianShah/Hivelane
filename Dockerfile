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
# requirements-image.txt is requirements.txt plus redis (optional for the
# relay, baked in so one image serves every topology; see that file).
COPY server/requirements.txt server/requirements-image.txt ./server/
RUN pip install --no-cache-dir -r server/requirements-image.txt

COPY server/ ./server/
COPY --from=dashboard /frontend/dist/ ./static/

# Rooms are only durable if SYNERGO_STATE_PATH points at a mounted volume;
# without one the container still runs, and logs a warning at startup.
# SYNERGO_STATIC_DIR serves the dashboard at / (unset it to serve the API only).
ENV SYNERGO_STATE_PATH=/data/synergo.db \
    SYNERGO_STATIC_DIR=/app/static
RUN mkdir -p /data && useradd --create-home --uid 10001 synergo \
    && chown -R synergo:synergo /data /app
COPY --chmod=755 deploy/docker-entrypoint.sh /usr/local/bin/synergo-entrypoint

# The commit this image was built from (image.yml passes it), for /readyz to
# report so a post-deploy check can tell the new deploy from the old one.
ARG SYNERGO_COMMIT=""
ENV SYNERGO_COMMIT=$SYNERGO_COMMIT

# No `USER synergo` here on purpose: Fly volumes and Render disks mount
# root-owned over /data, so the entrypoint starts as root only to chown the
# state directory, then drops to uid 10001 before the relay starts. The relay
# itself never runs as root. Hosts that start containers as a non-root user
# (Helm sets runAsUser 10001 and fsGroup 10001) skip the chown; the entrypoint
# then checks the directory is writable and, with SYNERGO_ENV=production,
# refuses to start on a store it could not keep.
ENTRYPOINT ["/usr/local/bin/synergo-entrypoint"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4).status==200 else 1)"]

# No CMD: with no arguments the entrypoint runs uvicorn on ${PORT:-8000} with
# WebSocket limits matching the relay's 512 KiB frame cap (--ws-max-size, from
# SYNERGO_MAX_FRAME_BYTES) and a short per-connection queue (--ws-max-queue 8).
# Any arguments given replace that command (still run as uid 10001).
