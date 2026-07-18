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

# NATE 2026-07-12: no follow-up offers in replies - the user asks for what
# they want next. Appended to the local model's system prompt (openai path).
# 2026-07-13: + publish_layer handle discipline / honest-empty stop (OPEN-17
# class: 0-event fetch -> fabricated publish_layer handle in the same turn).
export GRACE2_OPENAI_EXTRA_SYSTEM="${GRACE2_OPENAI_EXTRA_SYSTEM:-Never end a reply with an offer, suggestion, or recommendation for a next step (no 'Would you like...', no 'I can also...'). State what was done or found, then stop. The user decides what happens next. Fetch and composer tools publish their own layers - only call publish_layer when you have a handle returned by a previous tool result, passed verbatim. If a fetch returns no data, say so and stop.}"

# 2026-07-18: TiTiler owns nothing in the stack startup, so when it dies the
# agent+MinIO come up fine and every raster layer silently renders blank
# ("map says it's there but it's not"). Health-gate it here: probe, and if
# down, start it via its own script. Non-fatal -- vector-only work is still
# valid with TiTiler down, so warn instead of exiting.
TILE_BASE="${GRACE2_TILE_SERVER_BASE:-http://127.0.0.1:8080}"
if ! curl -sf -m 3 "$TILE_BASE/healthz" >/dev/null 2>&1; then
  echo "[start_agent] TiTiler not responding at $TILE_BASE -- starting it..."
  if ! bash "$REPO_ROOT/scripts/start_titiler.sh"; then
    echo "[start_agent] WARNING: TiTiler failed to start; raster layers will not render" >&2
  fi
fi

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

# OPEN-5: best-effort Ollama keep-alive pin. qwen3:8b-16k unloads after
# Ollama's default 5m idle window, so the FIRST prompt after any idle gap
# pays a ~60-75s cold-load. A manual keep_alive=24h pin works but does not
# survive an Ollama restart, so re-arm it here on every agent (re)start
# instead. Backgrounded (a cold model load can itself take the full 60-75s)
# and fully non-fatal -- Ollama may be down, or the box may be pointed at a
# non-Ollama provider (remote/Bedrock), and neither should fail agent
# startup. Derives the model from the same env the agent itself just
# loaded ($ENV_FILE, sourced above); falls back to qwen3:8b-24k if unset.
WARM_MODEL="${GRACE2_OPENAI_MODEL:-qwen3:8b-24k}"
(
  curl -s -m 90 http://127.0.0.1:11434/api/generate \
    -d '{"model":"'"$WARM_MODEL"'","keep_alive":"24h"}' \
    >/dev/null 2>&1 || true
) &
disown
echo "[start_agent] Ollama keep-alive warmup fired in background (model=$WARM_MODEL keep_alive=24h)"

echo "[start_agent] agent is running -- tail $LOG_FILE to follow startup"
