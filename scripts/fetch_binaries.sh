#!/usr/bin/env bash
# fetch_binaries.sh -- idempotent downloader for mf6, minio, mc
# usage: bash scripts/fetch_binaries.sh
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/bin"
THIRD_PARTY_DIR="$REPO_ROOT/third_party"

mkdir -p "$BIN_DIR" "$THIRD_PARTY_DIR"

# ---- MODFLOW 6.5.0 static linux binary ----
MF6_ZIP="$THIRD_PARTY_DIR/mf6.5.0_linux.zip"
MF6_BIN="$BIN_DIR/mf6"

if [ ! -f "$MF6_BIN" ]; then
  echo "[fetch_binaries] downloading mf6 6.5.0..."
  curl -L --progress-bar \
    "https://github.com/MODFLOW-USGS/modflow6/releases/download/6.5.0/mf6.5.0_linux.zip" \
    -o "$MF6_ZIP"
  echo "[fetch_binaries] unzipping mf6..."
  unzip -o "$MF6_ZIP" -d "$THIRD_PARTY_DIR/mf6.5.0_linux" > /dev/null
  # binary lives under bin/mf6 inside the zip
  MF6_EXTRACTED=$(find "$THIRD_PARTY_DIR/mf6.5.0_linux" -name "mf6" -type f | head -1)
  if [ -z "$MF6_EXTRACTED" ]; then
    echo "[fetch_binaries] ERROR: could not find mf6 binary in zip" >&2
    exit 1
  fi
  cp "$MF6_EXTRACTED" "$MF6_BIN"
  chmod +x "$MF6_BIN"
  echo "[fetch_binaries] mf6 installed at $MF6_BIN"
else
  echo "[fetch_binaries] mf6 already present, skipping download"
fi

echo "[fetch_binaries] verifying mf6..."
"$MF6_BIN" --version

# ---- MinIO server binary ----
MINIO_BIN="$BIN_DIR/minio"

if [ ! -f "$MINIO_BIN" ]; then
  echo "[fetch_binaries] downloading minio server..."
  curl -L --progress-bar \
    "https://dl.min.io/server/minio/release/linux-amd64/minio" \
    -o "$MINIO_BIN"
  chmod +x "$MINIO_BIN"
  echo "[fetch_binaries] minio installed at $MINIO_BIN"
else
  echo "[fetch_binaries] minio already present, skipping download"
fi

echo "[fetch_binaries] verifying minio..."
"$MINIO_BIN" --version

# ---- MinIO mc client ----
MC_BIN="$BIN_DIR/mc"

if [ ! -f "$MC_BIN" ]; then
  echo "[fetch_binaries] downloading mc client..."
  curl -L --progress-bar \
    "https://dl.min.io/client/mc/release/linux-amd64/mc" \
    -o "$MC_BIN"
  chmod +x "$MC_BIN"
  echo "[fetch_binaries] mc installed at $MC_BIN"
else
  echo "[fetch_binaries] mc already present, skipping download"
fi

echo "[fetch_binaries] verifying mc..."
"$MC_BIN" --version

echo "[fetch_binaries] all binaries OK"
