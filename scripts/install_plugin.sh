#!/usr/bin/env bash
# Install the TRID3NT QGIS plugin into the live QGIS profile.
#
# Why this exists (live-feedback 2026-07-12): QGIS loads a COPY of the
# plugin from the profile dir below, NOT the repo checkout -- a fix
# committed under qgis-plugin/trid3nt/ that is never synced there silently
# never reaches the user (this drift happened live: the profile carried a
# stale dock.py). This script IS the plugin deploy step.
#
# Usage:
#   scripts/install_plugin.sh          sync source -> profile (rsync -a --delete)
#   scripts/install_plugin.sh --check  diff-check only: itemize what WOULD change
#
# After a sync QGIS still runs the OLD code until the plugin is reloaded:
# Plugins > Plugin Reloader (or restart QGIS).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/qgis-plugin/trid3nt/"
DST="$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/trid3nt/"

if [[ ! -d "$SRC" ]]; then
    echo "source plugin dir not found: $SRC" >&2
    exit 1
fi

if [[ "${1:-}" == "--check" ]]; then
    echo "diff-check (what a sync WOULD change; empty output = in sync):"
    rsync -a --delete --dry-run --itemize-changes "$SRC" "$DST"
    exit 0
fi

mkdir -p "$DST"
rsync -a --delete "$SRC" "$DST"
echo "synced: $SRC -> $DST"

# Version stamp (Update button, NATE): write the source commit this sync came
# from into the INSTALLED copy so the plugin can read back what's actually
# running vs. what's in the repo (trid3nt.update.read_installed_version).
# Two lines: short sha, branch. Honest "unknown" fallback if this checkout is
# not a git repo (e.g. an extracted zip) rather than failing the sync.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
printf '%s\n%s\n' "$GIT_SHA" "$GIT_BRANCH" > "$DST/installed_version.txt"
echo "stamped: installed_version.txt ($GIT_SHA $GIT_BRANCH)"

echo "reload required: QGIS > Plugins > Plugin Reloader (or restart QGIS)"
