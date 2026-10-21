#!/usr/bin/env bash
# Assembles the GitHub Pages site: the dashboard at <base>/ and the docs site at
# <base>/docs/, in one directory ready for actions/upload-pages-artifact.
#
#   scripts/build_pages.sh [OUT_DIR]        (default: _site)
#
# Expects `npm ci` to have run in frontend/ and docs-site/. Environment:
#   HIVELANE_BASE   path the site is served under (default /Hivelane/)
#   PAGES_SITE     origin of the site (default https://adrianshah.github.io)
#   VITE_WS_URL    optional relay baked into the dashboard; without it the
#                  dashboard asks for one with ?relay= (or uses localhost)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-_site}"
base="${HIVELANE_BASE:-/Hivelane/}"
base="/${base#/}"
base="${base%/}/"
site="${PAGES_SITE:-https://adrianshah.github.io}"

case "$out" in
  /* | [A-Za-z]:*) ;; # absolute (the second form for a Windows checkout)
  *) out="$PWD/$out" ;;
esac
rm -rf "$out"
mkdir -p "$out"

echo "== dashboard (base $base)"
(
  cd "$root/frontend"
  # No relay is served from the Pages origin; VITE_STATIC_HOST makes the
  # dashboard fall back to ?relay= / localhost instead of its own origin.
  HIVELANE_BASE="$base" VITE_STATIC_HOST=1 npm run build -- --outDir "$out" --emptyOutDir
)
# Pages has no rewrites: an unknown path such as /Hivelane/r/K7M2PQ2X gets
# 404.html, which is the app itself, so deep links still boot the router.
cp "$out/index.html" "$out/404.html"

echo "== docs site (${site}${base}docs/)"
(
  cd "$root/docs-site"
  DOCS_SITE="$site" DOCS_BASE="${base}docs" npm run build
)
mkdir -p "$out/docs"
cp -R "$root/docs-site/dist/." "$out/docs/"

for required in index.html 404.html docs/index.html; do
  if [ ! -f "$out/$required" ]; then
    echo "::error title=Pages artifact incomplete::missing $required" >&2
    exit 1
  fi
done
echo "Pages site assembled in $out"
