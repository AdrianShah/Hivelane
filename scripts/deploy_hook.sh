#!/usr/bin/env bash
# Trigger one component's deploy by POSTing its host's deploy hook.
#
# Deploy hooks are the one mechanism Render, Cloudflare Pages, Netlify and most
# other PaaS share, so this stays host-agnostic: set the URL as an environment
# secret and nothing here changes when the host does.
#
# Usage: deploy_hook.sh <component> <hook-url>
set -euo pipefail

component=${1:?component name required}
hook=${2:-}

if [[ -z "$hook" ]]; then
  echo "::notice title=$component deploy skipped::No deploy hook configured for this environment. Add the secret to enable it."
  exit 0
fi

echo "Triggering $component deploy"
# The URL is a secret; never echo it, and let curl fail the step on a bad status.
curl -fsS -X POST "$hook" -o /dev/null
echo "$component deploy triggered"
