#!/usr/bin/env bash
# Install the TRID3NT QGIS plugin into the live QGIS profile -- this script IS
# the plugin deploy step. QGIS loads a COPY of the plugin from the profile dir
# below, NOT the repo checkout, so a fix committed under plugin/ that is never
# synced there silently never reaches the user.
#
# The package lives at repo-root plugin/ but installs under the name trid3nt_qgis/
# (its QGIS-loaded name); the co-located docs/, Makefile, README, and
# build output NEVER ship -- an explicit exclude list re-roots plugin/ -> the
# profile's trid3nt_qgis/ carrying shipped code + LICENSE only.
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
PROFILE="${TRID3NT_QGIS_PROFILE:-$HOME/.local/share/QGIS/QGIS3/profiles/default}"
DST="$PROFILE/python/plugins/trid3nt_qgis/"
OLD="$PROFILE/python/plugins/trid3nt"

# Shipped surface only: re-root plugin/ -> trid3nt_qgis/, dropping the co-located
# tests/, docs/, Makefile, README, and build output (LICENSE ships). Leading
# '/' anchors each exclude to the transfer root (SRC).
SHIP_EXCLUDES=(
    --exclude '/docs' --exclude '/Makefile'
    --exclude '/README.md' --exclude '/dist'
    --exclude '__pycache__' --exclude '*.pyc'
    --exclude '.git*' --exclude '.pytest_cache'
)

if [[ ! -d "$SRC" ]]; then
    echo "source plugin dir not found: $SRC" >&2
    exit 1
fi

# Never rsync through a dev symlink into the checkout.
if [[ -L "${DST%/}" ]]; then
    test -f "$DST/metadata.txt"
    echo "dev symlink active: ${DST%/}"
elif [[ "${1:-}" == "--check" ]]; then
    echo "diff-check (what a sync WOULD change; empty output = in sync):"
    rsync -a --delete "${SHIP_EXCLUDES[@]}" --dry-run --itemize-changes "$SRC" "$DST"
else
    mkdir -p "$DST"
    rsync -a --delete "${SHIP_EXCLUDES[@]}" "$SRC" "$DST"
    echo "synced: $SRC -> $DST"
fi
if [[ "${1:-}" == "--check" ]]; then
    if [[ -e "$OLD" || -L "$OLD" ]]; then
        echo "pending: migrate legacy settings and remove $OLD"
    fi
    exit 0
fi

if [[ -e "$OLD" || -L "$OLD" ]]; then
    if [[ -f "$PROFILE/QGIS/QGIS3.ini" ]]; then
        /usr/bin/python3 - "$PROFILE/QGIS/QGIS3.ini" <<'PYQT'
import sys

from qgis.PyQt.QtCore import QSettings

settings = QSettings(sys.argv[1], QSettings.IniFormat)
settings.beginGroup("trid3nt")
values = {f"trid3nt_qgis/{key}": settings.value(key) for key in settings.allKeys()}
settings.endGroup()
old_enabled = "PythonPlugins/trid3nt"
if settings.contains(old_enabled):
    values["PythonPlugins/trid3nt_qgis"] = settings.value(old_enabled)
for key, value in values.items():
    if not settings.contains(key):
        settings.setValue(key, value)
settings.remove(old_enabled)
settings.sync()
if settings.status() != QSettings.NoError or not all(settings.contains(key) for key in values):
    sys.exit("QGIS settings migration failed")
PYQT
    fi
    rm -rf -- "$OLD"
    echo "migrated the legacy QGIS package and removed $OLD"
fi

if [[ -L "${DST%/}" ]]; then
    echo "restart QGIS to load TRID3NT"
    exit 0
fi

# Version stamp: the source commit this sync came from, written into the
# INSTALLED copy so a human can eyeball what is actually installed vs. the repo.
# Two lines: short sha, branch. Honest "unknown" fallback if this checkout is
# not a git repo (e.g. an extracted zip) rather than failing the sync.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
printf '%s\n%s\n' "$GIT_SHA" "$GIT_BRANCH" > "$DST/installed_version.txt"
echo "stamped: installed_version.txt ($GIT_SHA $GIT_BRANCH)"

echo "restart QGIS to load TRID3NT"
