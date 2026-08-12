#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# fetch_core.sh -- clone the upstream CV32E40X core at the pinned revision.
#
# vendor/ is gitignored, so this reproduces it from scratch. Pinning matters:
# the core's parameter list and CV-X-IF struct layout change between revisions,
# and rtl/a7lite_soc_top.sv is written against this one.
# ---------------------------------------------------------------------------
set -euo pipefail

REV="d952cd63bc1b4eb58cd893c28ef8283c781e345e"
URL="https://github.com/openhwgroup/cv32e40x.git"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HERE/vendor/cv32e40x"

if [ -d "$DEST/.git" ]; then
    echo "[core] already present at $DEST"
else
    mkdir -p "$(dirname "$DEST")"
    echo "[core] cloning $URL"
    git clone "$URL" "$DEST"
fi

cd "$DEST"
git fetch --depth 50 origin "$REV" 2>/dev/null || git fetch origin
git checkout --quiet "$REV"
echo "[core] pinned at $(git rev-parse --short HEAD)"

