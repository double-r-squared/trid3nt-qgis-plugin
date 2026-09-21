#!/usr/bin/env bash
# start_headless_qgis.sh -- the daemon's QGIS session, with no GUI.
# A borrowed-session row (every qgis_provider row: an overlay opened on the map,
# a raster materialised through the provider registry and uploaded) has no answer
# without a session attached, so the local stack carries one beside the daemon.
# The plugin dock supersedes it the moment a real QGIS connects.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$REPO_ROOT/logs/headless_qgis.log"
PID_FILE="$REPO_ROOT/run/headless_qgis.pid"

mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/run"

# PyQGIS lives in the SYSTEM interpreter, never in venvs/agent: the headless
# session is run by path for the same reason the module docstring gives.
if ! python3 -c "import qgis.core" >/dev/null 2>&1; then
  echo "[headless_qgis] PyQGIS not importable from python3 -- no session sidecar" >&2
  exit 0
fi

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[headless_qgis] stopping existing session (pid $OLD_PID)..."
    kill "$OLD_PID" && sleep 1
  fi
  rm -f "$PID_FILE"
fi

export QT_QPA_PLATFORM=offscreen
setsid nohup python3 "$REPO_ROOT/dev/testing/headless_qgis.py" \
  >>"$LOG_FILE" 2>&1 &
QGIS_PID=$!
echo "$QGIS_PID" > "$PID_FILE"
echo "[headless_qgis] session PID=$QGIS_PID (pidfile: $PID_FILE, log: $LOG_FILE)"

# The session connects to a daemon that has just opened its socket; a crash here
# is a crash of the FIXTURE, never of the stack, so it is reported and not fatal.
sleep 3
if ! kill -0 "$QGIS_PID" 2>/dev/null; then
  echo "[headless_qgis] ERROR: session died immediately -- check $LOG_FILE" >&2
  tail -20 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
echo "[headless_qgis] session is attached"
