#!/usr/bin/env bash
# Poll a freshly deployed relay until it serves the version that was just
# deployed, so a broken deploy fails the pipeline instead of sitting there
# looking green.
#
# Usage: post_deploy_check.sh <relay-base-url> [expected-version]
#
# A deploy hook only *starts* a deploy; the old instance keeps answering
# /readyz until the new one takes over. So when an expected version (a commit
# SHA or a release version) is given, this waits until /readyz reports it in
# its `commit` or `version` field. Environment:
#   TIMEOUT   seconds to wait in total (default 600)
#   INTERVAL  seconds between polls (default 10)
#
# A relay that reports neither field cannot be told apart from the old one;
# the check then warns and passes once /readyz answers, as it always did.
set -euo pipefail

base=${1:-}
expected=${2:-}
timeout=${TIMEOUT:-600}
interval=${INTERVAL:-10}

if [[ -z "$base" ]]; then
  echo "::notice title=Post-deploy check skipped::Set the RELAY_URL environment variable to enable it."
  exit 0
fi
base=${base%/}

# Prints the commit/version /readyz reports ("" if neither), or fails if the
# body is not JSON from a ready relay.
reported_version() {
  python3 -c '
import json, sys
body = json.load(sys.stdin)
if body.get("status") != "ready":
    sys.exit(1)
print(body.get("commit") or body.get("version") or "")
'
}

deadline=$(( $(date +%s) + timeout ))
last=""
while :; do
  if body=$(curl -fsS --max-time 10 "$base/readyz" 2>/dev/null) \
    && seen=$(printf '%s' "$body" | reported_version 2>/dev/null); then
    if [[ -z "$expected" ]]; then
      echo "relay ready at $base"
      exit 0
    fi
    if [[ -z "$seen" ]]; then
      echo "::warning title=Deployed version not verified::$base/readyz reports no commit or version, so the new deploy cannot be told apart from the old one. The relay is ready."
      exit 0
    fi
    # A short SHA on either side still matches.
    if [[ "$seen" == "$expected"* || "$expected" == "$seen"* ]]; then
      echo "relay at $base is serving $seen"
      exit 0
    fi
    if [[ "$seen" != "$last" ]]; then
      echo "relay at $base still reports $seen; waiting for $expected"
      last=$seen
    fi
  fi
  if (( $(date +%s) >= deadline )); then
    break
  fi
  sleep "$interval"
done

if [[ -n "$expected" ]]; then
  echo "::error title=Deploy not live::$base/readyz did not report $expected within ${timeout}s (last seen: ${last:-no ready answer})"
else
  echo "::error title=Relay not ready::$base/readyz did not respond within ${timeout}s"
fi
exit 1
