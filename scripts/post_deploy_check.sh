#!/usr/bin/env bash
# Poll a freshly deployed relay until it reports ready, so a broken deploy fails
# the pipeline instead of sitting there looking green.
#
# Usage: post_deploy_check.sh <relay-base-url>
set -euo pipefail

base=${1:-}
attempts=${ATTEMPTS:-30}

if [[ -z "$base" ]]; then
  echo "::notice title=Post-deploy check skipped::Set the RELAY_URL environment variable to enable it."
  exit 0
fi

base=${base%/}
for i in $(seq 1 "$attempts"); do
  if curl -fsS --max-time 10 "$base/readyz" >/dev/null 2>&1; then
    echo "relay ready at $base"
    exit 0
  fi
  sleep 5
done

echo "::error title=Relay not ready::$base/readyz did not respond after $((attempts * 5))s"
exit 1
