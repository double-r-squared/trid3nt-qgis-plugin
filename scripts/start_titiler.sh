#!/usr/bin/env bash
# start_titiler.sh -- start TiTiler venv uvicorn on :8080 pointing at local MinIO
# Writes PID to ./run/titiler.pid, logs to ./logs/titiler.log
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/venvs/titiler"
LOG_FILE="$REPO_ROOT/logs/titiler.log"
PID_FILE="$REPO_ROOT/run/titiler.pid"

mkdir -p "$REPO_ROOT/logs" "$REPO_ROOT/run"

if [ ! -f "$VENV_DIR/bin/uvicorn" ]; then
  echo "[start_titiler] ERROR: titiler venv not found at $VENV_DIR" >&2
  echo "[start_titiler] Run: uv venv --python 3.12 $VENV_DIR && uv pip install --python $VENV_DIR/bin/python titiler.application==2.0.4 uvicorn" >&2
  exit 1
fi

# Stop any existing instance
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start_titiler] stopping existing titiler (pid $OLD_PID)..."
    kill "$OLD_PID" && sleep 1
  fi
  rm -f "$PID_FILE"
fi

echo "[start_titiler] starting titiler on :8080..."

setsid env \
  AWS_ACCESS_KEY_ID=trid3nt \
  AWS_SECRET_ACCESS_KEY=trid3nt-local-dev \
  AWS_ENDPOINT_URL=http://127.0.0.1:9000 \
  AWS_REGION=us-east-1 \
  GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
  VSI_CACHE=TRUE \
  AWS_S3_ENDPOINT=127.0.0.1:9000 \
  AWS_HTTPS=NO \
  AWS_VIRTUAL_HOSTING=FALSE \
  "$VENV_DIR/bin/uvicorn" titiler.application.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  >> "$LOG_FILE" 2>&1 &

TITILER_PID=$!
echo "$TITILER_PID" > "$PID_FILE"
echo "[start_titiler] titiler started, pid=$TITILER_PID, log=$LOG_FILE"

# Wait for it to be ready
echo "[start_titiler] waiting for titiler to become ready..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8080/healthz > /dev/null 2>&1; then
    echo "[start_titiler] titiler is ready"
    exit 0
  fi
  sleep 1
done

echo "[start_titiler] WARNING: titiler did not respond within 30s" >&2
tail -20 "$LOG_FILE" >&2
exit 1
