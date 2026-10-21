#!/bin/sh
# Entrypoint of the relay image (Dockerfile). Makes sure the room store's
# directory is writable by the unprivileged `hivelane` user (uid 10001), then
# runs the relay as that user.
#
# Why: Fly volumes and Render disks are mounted root-owned over /data, hiding
# the image's own /data (owned by hivelane). The relay would then fail to open
# its SQLite store and silently fall back to memory, losing every room code on
# each deploy. Started as root (the image default), this script chowns the
# directory and drops to uid 10001. Started as a non-root user (Helm's
# securityContext, `docker run --user`), it only checks, and in production
# refuses to start on a store it cannot write.
set -eu

APP_UID=10001
APP_GID=10001

state_path="${HIVELANE_STATE_PATH:-}"
state_dir=""
if [ -n "$state_path" ]; then
  state_dir=$(dirname "$state_path")
fi

if [ "$#" -eq 0 ]; then
  # Frame limits match the relay's own cap (HIVELANE_MAX_FRAME_BYTES, 512 KiB by
  # default), so an oversized frame is refused by the server before it is
  # buffered; the queue bound keeps a slow consumer to a few frames in memory.
  # $PORT is honoured because most hosts inject it; 8000 locally.
  set -- uvicorn server.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --ws-max-size "${HIVELANE_MAX_FRAME_BYTES:-524288}" \
    --ws-max-queue "${HIVELANE_WS_MAX_QUEUE:-8}" \
    --timeout-graceful-shutdown "${HIVELANE_GRACEFUL_SHUTDOWN_SECONDS:-25}"
fi

if [ "$(id -u)" = "0" ]; then
  if [ -n "$state_dir" ]; then
    mkdir -p "$state_dir"
    chown "$APP_UID:$APP_GID" "$state_dir"
    # Files left by an earlier root-owned run, if any.
    find "$state_dir" -maxdepth 1 -user 0 -exec chown "$APP_UID:$APP_GID" {} + 2>/dev/null || true
  fi
  export HOME=/home/hivelane
  # The image has no gosu/su-exec; Python is already there and does the same.
  exec python -c '
import os, sys
os.setgroups([])
os.setgid(int(sys.argv[1]))
os.setuid(int(sys.argv[2]))
os.execvp(sys.argv[3], sys.argv[3:])
' "$APP_GID" "$APP_UID" "$@"
fi

if [ -n "$state_dir" ]; then
  probe="$state_dir/.hivelane-write-test.$$"
  if ! (mkdir -p "$state_dir" && : > "$probe" && rm -f "$probe") 2>/dev/null; then
    echo "hivelane: $state_dir is not writable by uid $(id -u); room codes would not survive a restart." >&2
    echo "hivelane: mount the volume owned by uid $APP_UID (Kubernetes: fsGroup $APP_GID), or start the container as root so this script can fix it." >&2
    if [ "${HIVELANE_ENV:-}" = "production" ]; then
      exit 1
    fi
  fi
fi

exec "$@"
