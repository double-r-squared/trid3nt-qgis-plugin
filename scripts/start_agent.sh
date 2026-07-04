#!/usr/bin/env bash
# start_agent.sh -- start the GRACE-2 agent (vendored) for local TRID3NT dev
# Loads .env.local, starts the agent via python -m grace2_agent.main
# Logs to ./logs/agent.log, writes PID to ./run/agent.pid (setsid/nohup style)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO_ROOT/venvs/agent/bin/python"
LOG_FILE="$REPO_ROOT/logs/agent.log"
PID_FILE="$REPO_ROOT/run/agent.pid"
ENV_FILE="$REPO_ROOT/.env.local"

mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/run" "$REPO_ROOT/data/persistence"

if [ ! -x "$PYTHON" ]; then
  echo "[start_agent] ERROR: venv not found at $PYTHON -- run 'make venv' first" >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "[start_agent] ERROR: $ENV_FILE not found" >&2
  exit 1
fi

# Stop any existing instance
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start_agent] stopping existing agent (pid $OLD_PID)..."
    kill "$OLD_PID" && sleep 1
  fi
  rm -f "$PID_FILE"
fi

echo "[start_agent] loading $ENV_FILE..."

# Build env from .env.local (export all vars)
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

# Override: ensure host is LAN-accessible, not loopback-only
export GRACE2_AGENT_HOST="${GRACE2_AGENT_HOST:-0.0.0.0}"

# Ensure no AWS Cognito pool is set (local = anonymous auth)
unset GRACE2_COGNITO_USER_POOL_ID 2>/dev/null || true
unset GRACE2_COGNITO_APP_CLIENT_ID 2>/dev/null || true

echo "[start_agent] starting agent (WS :8765, HTTP :8766)..."
echo "[start_agent] MODEL_PROVIDER=$MODEL_PROVIDER GRACE2_OPENAI_MODEL=$GRACE2_OPENAI_MODEL"
echo "[start_agent] logs -> $LOG_FILE"

setsid nohup "$PYTHON" -m grace2_agent.main \
  >> "$LOG_FILE" 2>&1 &

AGENT_PID=$!
echo "$AGENT_PID" > "$PID_FILE"
echo "[start_agent] agent PID=$AGENT_PID (pidfile: $PID_FILE)"

# Wait briefly and verify it didn't immediately crash
sleep 2
if ! kill -0 "$AGENT_PID" 2>/dev/null; then
  echo "[start_agent] ERROR: agent process died immediately -- check $LOG_FILE" >&2
  cat "$LOG_FILE" | tail -20 >&2
  exit 1
fi

echo "[start_agent] agent is running -- tail $LOG_FILE to follow startup"
