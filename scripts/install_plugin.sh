#!/usr/bin/env bash
# Install the TRID3NT QGIS plugin into the live QGIS profile.
#
# Why this exists (live-feedback 2026-07-12): QGIS loads a COPY of the
# plugin from the profile dir below, NOT the repo checkout -- a fix
# committed under plugin/ that is never synced there silently never reaches
# the user (this drift happened live: the profile carried a stale dock.py).
# This script IS the plugin deploy step.
#
# The package lives at repo-root plugin/ but installs under the name trid3nt/
# (its QGIS-loaded name); the co-located tests/, docs/, Makefile, README, and
# build output NEVER ship -- an explicit exclude list re-roots plugin/ -> the
# profile's trid3nt/ carrying shipped code + LICENSE only.
#
# Usage:
#   scripts/install_plugin.sh          sync source -> profile (rsync -a --delete)
#   scripts/install_plugin.sh --check  diff-check only: itemize what WOULD change
#
# After a sync QGIS still runs the OLD code until the plugin is reloaded:
# Plugins > Plugin Reloader (or restart QGIS).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/plugin/"
DST="$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/trid3nt/"

# Shipped surface only: re-root plugin/ -> trid3nt/, dropping the co-located
# tests/, docs/, Makefile, README, and build output (LICENSE ships). Leading
# '/' anchors each exclude to the transfer root (SRC).
SHIP_EXCLUDES=(
    --exclude '/tests' --exclude '/docs' --exclude '/Makefile'
    --exclude '/README.md' --exclude '/dist'
    --exclude '__pycache__' --exclude '*.pyc'
    --exclude '.git*' --exclude '.pytest_cache'
)

if [[ ! -d "$SRC" ]]; then
    echo "source plugin dir not found: $SRC" >&2
    exit 1
fi

# Dev-symlink mode: when the profile plugin dir IS a symlink to the checkout,
# QGIS already runs the repo code directly - a sync is a no-op and rsync
# --delete would write THROUGH the link into the repo. Reload is all you need.
if [[ -L "${DST%/}" ]]; then
    echo "dev symlink active: ${DST%/} -> $(readlink "${DST%/}")"
    echo "no sync needed; reload via QGIS > Plugins > Plugin Reloader"
    exit 0
fi

if [[ "${1:-}" == "--check" ]]; then
    echo "diff-check (what a sync WOULD change; empty output = in sync):"
    rsync -a --delete "${SHIP_EXCLUDES[@]}" --dry-run --itemize-changes "$SRC" "$DST"
    exit 0
fi

mkdir -p "$DST"
rsync -a --delete "${SHIP_EXCLUDES[@]}" "$SRC" "$DST"
echo "synced: $SRC -> $DST"

# Version stamp (install-script provenance): write the source commit this sync
# came from into the INSTALLED copy so a human can eyeball what's actually
# installed vs. the repo. (The in-plugin Update button was removed; QGIS Plugin
# Manager owns updates now -- this stamp is now just informational provenance.)
# Two lines: short sha, branch. Honest "unknown" fallback if this checkout is
# not a git repo (e.g. an extracted zip) rather than failing the sync.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
printf '%s\n%s\n' "$GIT_SHA" "$GIT_BRANCH" > "$DST/installed_version.txt"
echo "stamped: installed_version.txt ($GIT_SHA $GIT_BRANCH)"

echo "reload required: QGIS > Plugins > Plugin Reloader (or restart QGIS)"
