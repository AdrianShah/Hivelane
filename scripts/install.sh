#!/bin/sh
# Install the Synergo CLI on macOS or Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/AdrianShah/Synergo/main/scripts/install.sh | sh
#
# Uses uv if it is installed, else pipx. With neither, it installs uv with uv's
# official installer first (uv also fetches a suitable Python if you lack 3.12+).
# It asks no questions; every command is printed before it runs.
#
# Options:
#   --dry-run   print the commands without running any of them
#   --help      show this help
#
# Environment:
#   SYNERGO_VERSION=0.1.0   install that exact version instead of the latest
#   SYNERGO_DRY_RUN=1       same as --dry-run (handy with `curl ... | sh`)
set -eu

# Inline rather than read back from "$0": under `curl ... | sh` there is no file.
usage() {
  cat <<'EOF'
Install the Synergo CLI with uv (preferred) or pipx; installs uv if neither exists.

Usage: install.sh [--dry-run] [--help]

  --dry-run, -n   print the commands without running any of them
  --help, -h      show this help

Environment:
  SYNERGO_VERSION=0.1.0   install that exact version instead of the latest
  SYNERGO_DRY_RUN=1       same as --dry-run (for `curl ... | sh`)
EOF
}

say() {
  printf 'synergo-install: %s\n' "$*"
}

die() {
  printf 'synergo-install: error: %s\n' "$*" >&2
  exit 1
}

DRY_RUN=0
case "${SYNERGO_DRY_RUN:-}" in
  1 | true | yes) DRY_RUN=1 ;;
esac
for arg in "$@"; do
  case "$arg" in
    --dry-run | -n) DRY_RUN=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

# Print a command, then run it unless this is a dry run.
run() {
  printf '  $ %s\n' "$*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

VERSION=${SYNERGO_VERSION:-}
VERSION=${VERSION#v}
case "$VERSION" in
  *[!0-9A-Za-z.+-]*) die "SYNERGO_VERSION does not look like a version: $SYNERGO_VERSION" ;;
esac
if [ -n "$VERSION" ]; then
  SPEC="synergo==$VERSION"
else
  SPEC="synergo"
fi

UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

install_uv() {
  say "neither uv nor pipx found; installing uv from $UV_INSTALLER_URL"
  if command -v curl >/dev/null 2>&1; then
    printf '  $ curl -LsSf %s | sh\n' "$UV_INSTALLER_URL"
    if [ "$DRY_RUN" -eq 0 ]; then
      curl -LsSf "$UV_INSTALLER_URL" | sh
    fi
  elif command -v wget >/dev/null 2>&1; then
    printf '  $ wget -qO- %s | sh\n' "$UV_INSTALLER_URL"
    if [ "$DRY_RUN" -eq 0 ]; then
      wget -qO- "$UV_INSTALLER_URL" | sh
    fi
  else
    die "need curl or wget to download uv; install one, or install uv or pipx yourself"
  fi
  # The installer edits shell profiles for future shells but cannot change this
  # one's PATH, so look where it installs (same precedence as the installer).
  for dir in "${UV_INSTALL_DIR:-}" "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -n "$dir" ]; then
      PATH="$dir:$PATH"
    fi
  done
  export PATH
  if [ "$DRY_RUN" -eq 0 ] && ! command -v uv >/dev/null 2>&1; then
    die "uv was installed but is not on PATH; open a new terminal and re-run this script"
  fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  say "dry run: commands are printed, nothing is installed"
fi

if command -v uv >/dev/null 2>&1; then
  TOOL=uv
elif command -v pipx >/dev/null 2>&1; then
  TOOL=pipx
else
  install_uv
  TOOL=uv
fi

say "installing $SPEC with $TOOL"
if [ "$TOOL" = uv ]; then
  # --upgrade so re-running the script moves an existing install to the latest.
  run uv tool install --upgrade "$SPEC"
else
  # --force so re-running the script reinstalls (pipx has no in-place upgrade-to-spec).
  run pipx install --force "$SPEC"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

# Agent hooks run `synergo` from PATH, so a missing PATH entry silently turns
# coordination off (hooks fail open). Nothing later would say so; say it now.
if ! command -v synergo >/dev/null 2>&1; then
  if [ "$TOOL" = uv ]; then
    bin_dir=$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")
    fix="uv tool update-shell"
  else
    bin_dir="$HOME/.local/bin"
    fix="pipx ensurepath"
  fi
  say "installed, but $bin_dir is not on your PATH yet."
  say "run \`$fix\`, then open a new terminal. Agent hooks need \`synergo\` on PATH."
  PATH="$bin_dir:$PATH"
fi

installed=$(synergo --version 2>/dev/null || echo "synergo")
cat <<EOF

$installed is installed.

Next, in a repository you work in:

  cd path/to/your-repo
  synergo up        # install agent hooks, create or join the room, start the daemon
  synergo doctor    # if anything looks off

Docs: https://github.com/AdrianShah/Synergo/blob/main/docs/install.md
EOF
