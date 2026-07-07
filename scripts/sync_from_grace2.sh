#!/usr/bin/env bash
# sync_from_grace2.sh -- vendor GRACE-2 subtrees into ./vendor/
# Usage: bash scripts/sync_from_grace2.sh [/path/to/GRACE-2]
# Writes vendor/UPSTREAM_COMMIT with the source repo HEAD.
#
# Also syncs the OTHER direction for documentation: docs/site/ (the canonical
# "TRID3NT Local" MkDocs pages, edited HERE) is pushed into the GRACE-2 docs
# site at docs-site/docs/local/ so the one MkDocs Material site carries the
# local section. Commit the updated copies in GRACE-2 after running.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-/home/nate/Documents/GRACE-2}"
VENDOR="$REPO_ROOT/vendor"

if [ ! -d "$SOURCE/.git" ]; then
  echo "[sync] ERROR: $SOURCE does not look like a git repo (no .git dir)" >&2
  exit 1
fi

UPSTREAM_COMMIT="$(git -C "$SOURCE" rev-parse HEAD)"
echo "[sync] source HEAD: $UPSTREAM_COMMIT"

SUBTREES=(
  services/agent
  services/workers/modflow
  services/workers/sfincs
  services/workers/_raster_postprocess
  services/workers/_sfincs_build
  services/workers/_modflow_build
  services/workers/_modflow_postprocess
  packages/contracts
  web
)

RSYNC_EXCLUDES=(
  --exclude='.git'
  --exclude='node_modules'
  --exclude='dist'
  --exclude='build'
  --exclude='__pycache__'
  --exclude='*.egg-info'
  --exclude='.venv'
)

for subtree in "${SUBTREES[@]}"; do
  src="$SOURCE/$subtree/"
  dst="$VENDOR/$subtree/"
  mkdir -p "$dst"
  echo "[sync] rsync $subtree ..."
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$src" "$dst"
done

echo "$UPSTREAM_COMMIT" > "$VENDOR/UPSTREAM_COMMIT"

# ---- docs: push the canonical TRID3NT Local pages into the GRACE-2 docs site ----
DOCS_SRC="$REPO_ROOT/docs/site/"
DOCS_DST="$SOURCE/docs-site/docs/local/"
if [ -d "$DOCS_SRC" ] && [ -d "$SOURCE/docs-site/docs" ]; then
  echo "[sync] rsync docs/site -> $DOCS_DST ..."
  mkdir -p "$DOCS_DST"
  rsync -a --delete "$DOCS_SRC" "$DOCS_DST"
else
  echo "[sync] skipping docs sync (docs/site or GRACE-2 docs-site/docs missing)"
fi

echo "[sync] done -- UPSTREAM_COMMIT=$UPSTREAM_COMMIT"
