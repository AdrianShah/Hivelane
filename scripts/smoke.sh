#!/usr/bin/env bash
# End-to-end check of the one claim the product rests on: a second agent is
# denied an edit to a file a teammate just touched — even when the two agents run
# from different directories in the repo, which is the case unit tests on either
# side alone cannot cover.
#
# Usage: scripts/smoke.sh        (needs `synergo` and `uvicorn` on PATH)
set -euo pipefail

RELAY_PORT=${RELAY_PORT:-8791}
PORT_A=${PORT_A:-4791}
PORT_B=${PORT_B:-4792}
WORK="$(mktemp -d)"
PIDS=()
# Both daemons and every hook share one private SYNERGO_HOME, so they share
# the daemon token (synergo/auth.py) and nothing lands in the real home.
export SYNERGO_HOME="$WORK/home"
mkdir -p "$SYNERGO_HOME"

cleanup() {
  local pid alive
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done
  # Wait for them to actually go: the daemon unwinds over several seconds, and a
  # back-to-back run would otherwise trip the preflight port guard.
  for _ in $(seq 1 60); do
    alive=0
    for pid in "${PIDS[@]:-}"; do
      [[ -n "$pid" ]] || continue
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    if [[ "$alive" -eq 0 ]]; then break; fi
    sleep 0.25
  done
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -9 "$pid" 2>/dev/null || true
  done
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

wait_for() {
  local url=$1 label=$2
  for _ in $(seq 1 60); do
    curl -fsS "$url" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  fail "$label did not come up at $url"
}

for port in "$RELAY_PORT" "$PORT_A" "$PORT_B"; do
  if curl -fsS --max-time 1 "http://127.0.0.1:$port/status" >/dev/null 2>&1 \
     || curl -fsS --max-time 1 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    fail "port $port is already serving — stop it first (a stale daemon can fake a pass)"
  fi
done

echo "== starting relay on :$RELAY_PORT"
uvicorn server.main:app --port "$RELAY_PORT" --log-level warning >"$WORK/relay.log" 2>&1 &
PIDS+=($!)
disown
wait_for "http://127.0.0.1:$RELAY_PORT/health" "relay"

ROOM=$(curl -fsS -X POST "http://127.0.0.1:$RELAY_PORT/rooms" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["room_id"])')
echo "== room $ROOM"

# A repo whose agents run from two different directories.
REPO="$WORK/repo"
mkdir -p "$REPO/server"
git -C "$REPO" init -q
printf 'def broadcast():\n    return 1\n' > "$REPO/server/room.py"

start_daemon() {
  local name=$1 agent=$2 port=$3 cwd=$4
  ( cd "$cwd" && exec synergo join "$ROOM" --name "$name" --agent "$agent" \
      --server "ws://127.0.0.1:$RELAY_PORT/ws" --port "$port" \
      >"$WORK/$name.log" 2>&1 ) &
  PIDS+=($!)
  disown
  wait_for "http://127.0.0.1:$port/health" "daemon $name"
}

# Every daemon route but /health wants the install's token.
daemon_status() {
  curl -fsS -H "X-Synergo-Token: $(cat "$SYNERGO_HOME/token")" "http://127.0.0.1:$1/status"
}

echo "== daemon adrian (cwd = repo root), daemon sam (cwd = repo/server)"
start_daemon adrian claude-code "$PORT_A" "$REPO"
start_daemon sam cursor "$PORT_B" "$REPO/server"

for _ in $(seq 1 40); do
  a=$(daemon_status "$PORT_A")
  b=$(daemon_status "$PORT_B")
  if [[ "$a" == *'"sam"'* && "$b" == *'"adrian"'* ]]; then break; fi
  sleep 0.5
done
[[ "$a" == *'"sam"'* && "$b" == *'"adrian"'* ]] \
  || fail "daemons never saw each other as peers (a=$a b=$b)"
echo "   both daemons see each other"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT_A/status")
[[ "$code" == "401" ]] || fail "a request without the daemon token got HTTP $code, not 401"
echo "   a request without the token is refused"

hook_event() {
  local event=$1 session=$2 cwd=$3 port=$4
  python3 -c '
import json, sys
print(json.dumps({"hook_event_name": sys.argv[1], "session_id": sys.argv[2],
                  "cwd": sys.argv[3], "tool_name": "Edit",
                  "tool_input": {"file_path": sys.argv[4]}}))' \
    "$event" "$session" "$cwd" "$REPO/server/room.py" \
    | SYNERGO_PORT="$port" synergo hook claude-code
}

echo "== adrian edits server/room.py from the repo root"
printf 'def broadcast():\n    return 2\n' > "$REPO/server/room.py"
hook_event PostToolUse s-adrian "$REPO" "$PORT_A" >/dev/null

echo "== sam attempts the same file from repo/server"
decision=""
for _ in $(seq 1 30); do
  out=$(hook_event PreToolUse s-sam "$REPO/server" "$PORT_B" || true)
  if [[ -n "$out" ]]; then
    decision=$(printf '%s' "$out" | python3 -c '
import sys, json
try:
    print(json.load(sys.stdin).get("hookSpecificOutput", {}).get("permissionDecision", ""))
except Exception:
    print("")')
  fi
  [[ "$decision" == "deny" ]] && break
  sleep 0.5
done

[[ "$decision" == "deny" ]] || fail "expected deny across differing cwds, got '${decision:-allow}' (raw: ${out:-<empty>})"

reason=$(printf '%s' "$out" | python3 -c '
import sys, json
print(json.load(sys.stdin)["hookSpecificOutput"]["permissionDecisionReason"])')
echo "   denied: $reason"

case "$reason" in
  *server/room.py*) ;;
  *) fail "reason should name the repo-relative path, got: $reason" ;;
esac

echo "SMOKE PASS"
