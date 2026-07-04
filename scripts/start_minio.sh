#!/usr/bin/env bash
# start_minio.sh -- start MinIO server for local TRID3NT development
# Writes PID to ./run/minio.pid, logs to ./logs/minio.log
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/bin"
DATA_DIR="$REPO_ROOT/data/minio"
LOG_FILE="$REPO_ROOT/logs/minio.log"
PID_FILE="$REPO_ROOT/run/minio.pid"

mkdir -p "$DATA_DIR" "$REPO_ROOT/logs" "$REPO_ROOT/run"

# Stop any existing instance
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start_minio] stopping existing minio (pid $OLD_PID)..."
    kill "$OLD_PID" && sleep 1
  fi
  rm -f "$PID_FILE"
fi

export MINIO_ROOT_USER=trid3nt
export MINIO_ROOT_PASSWORD=trid3nt-local-dev

echo "[start_minio] starting minio server on :9000 (console :9001)..."
setsid "$BIN_DIR/minio" server "$DATA_DIR" \
  --address :9000 \
  --console-address :9001 \
  >> "$LOG_FILE" 2>&1 &

MINIO_PID=$!
echo "$MINIO_PID" > "$PID_FILE"
echo "[start_minio] minio started, pid=$MINIO_PID, log=$LOG_FILE"

# Wait for it to be ready
echo "[start_minio] waiting for minio to become ready..."
for i in $(seq 1 20); do
  if curl -sf http://127.0.0.1:9000/minio/health/live > /dev/null 2>&1; then
    echo "[start_minio] minio is ready"
    exit 0
  fi
  sleep 1
done

echo "[start_minio] WARNING: minio did not respond within 20s" >&2
tail -20 "$LOG_FILE" >&2
exit 1
