#!/usr/bin/env bash
# Start the companion in the background and record its PID.
# Usage: companion/scripts/start_mac.sh   (works on macOS and Linux)
set -euo pipefail

COMPANION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$COMPANION_DIR/.." && pwd)"
PID_FILE="$COMPANION_DIR/.pid"
LOG_FILE="$COMPANION_DIR/companion.log"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Companion already running (PID $(cat "$PID_FILE"))."
  exit 0
fi

PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3 || command -v python)"
fi

cd "$REPO_ROOT"
nohup "$PYTHON" -m companion.main >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

sleep 1
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Companion started (PID $(cat "$PID_FILE")) on http://127.0.0.1:4500"
  echo "Logs: $LOG_FILE"
else
  rm -f "$PID_FILE"
  echo "Companion failed to start. Last log lines:" >&2
  tail -n 20 "$LOG_FILE" >&2
  exit 1
fi
