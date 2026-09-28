# Relay image. Host-agnostic: anything that runs a container can run this, so
# the deploy target stays a choice rather than a rewrite.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the install layer.
COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ ./server/

# Rooms are only durable if this points at a mounted volume; without one the
# container still runs, and logs a warning at startup.
ENV SYNERGO_STATE_PATH=/data/synergo.db
RUN mkdir -p /data && useradd --create-home --uid 10001 synergo \
    && chown -R synergo:synergo /data /app
USER synergo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# $PORT is honoured because most hosts inject it; 8000 locally.
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
