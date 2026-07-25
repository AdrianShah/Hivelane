#!/usr/bin/env bash
# Stop the background companion. Spawned watchers are terminated by the
# companion's own SIGTERM handler.
set -euo pipefail

COMPANION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$COMPANION_DIR/.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No PID file; companion does not appear to be running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Stale PID file for $PID; removing."
  rm -f "$PID_FILE"
  exit 0
fi

kill "$PID"
for _ in $(seq 1 10); do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if kill -0 "$PID" 2>/dev/null; then
  echo "Companion ignored SIGTERM; sending SIGKILL."
  kill -9 "$PID" || true
fi

rm -f "$PID_FILE"
echo "Companion stopped."
