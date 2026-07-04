#!/usr/bin/env bash
# init_minio.sh -- create buckets trid3nt-runs and trid3nt-cache in local MinIO
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MC="$REPO_ROOT/bin/mc"

MINIO_URL="http://127.0.0.1:9000"
ALIAS="local"

echo "[init_minio] configuring mc alias '$ALIAS' -> $MINIO_URL..."
"$MC" alias set "$ALIAS" "$MINIO_URL" trid3nt trid3nt-local-dev --api S3v4 > /dev/null

for BUCKET in trid3nt-runs trid3nt-cache; do
  if "$MC" ls "$ALIAS/$BUCKET" > /dev/null 2>&1; then
    echo "[init_minio] bucket $BUCKET already exists, skipping"
  else
    echo "[init_minio] creating bucket $BUCKET..."
    "$MC" mb "$ALIAS/$BUCKET"
  fi
done

echo "[init_minio] current buckets:"
"$MC" ls "$ALIAS/"

echo "[init_minio] done"
