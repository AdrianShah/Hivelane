#!/usr/bin/env bash
# One-shot local setup: install the hivelane daemon and enable the repo git hooks.
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
"$VENV_PY" -m pip install -e .

# Graph auto-refresh on commit + conflict-free graph.json merges (see .githooks/)
git config core.hooksPath .githooks
git config merge.graphify.name "graphify union merge"
git config merge.graphify.driver "graphify merge-driver %O %A %B"
if ! command -v graphify >/dev/null && [[ ! -x "$HOME/.local/bin/graphify" ]]; then
  echo "Tip: install graphify for the code graph:  uv tool install 'graphifyy[mcp]'"
fi

echo
echo "Setup complete."
echo "  Install agent hooks: .venv/bin/hivelane hook install claude-code"
echo "  Join a room:         .venv/bin/hivelane join <ROOM> --name <your-name>"
