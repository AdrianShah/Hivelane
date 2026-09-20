#!/usr/bin/env bash
# One-shot local setup: create .venv and install companion + watcher deps.
# Usage: ./setup.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PYTHON="$(command -v python3 || command -v python)"
if [[ -z "$PYTHON" ]]; then
  echo "Python 3 not found on PATH." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Creating virtualenv at .venv"
  "$PYTHON" -m venv .venv
fi

VENV_PY=".venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r companion/requirements.txt
"$VENV_PY" -m pip install -r watcher/requirements.txt

# Graph auto-refresh on commit + conflict-free graph.json merges (see .githooks/)
git config core.hooksPath .githooks
git config merge.graphify.name "graphify union merge"
git config merge.graphify.driver "graphify merge-driver %O %A %B"
if ! command -v graphify >/dev/null && [[ ! -x "$HOME/.local/bin/graphify" ]]; then
  echo "Tip: install graphify for the code graph:  uv tool install 'graphifyy[mcp]'"
fi

chmod +x companion/scripts/start_mac.sh companion/scripts/stop_mac.sh 2>/dev/null || true

echo
echo "Setup complete."
echo "  Start companion: companion/scripts/start_mac.sh"
echo "  Stop companion:  companion/scripts/stop_mac.sh"
