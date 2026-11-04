#!/usr/bin/env bash
# fetch_binaries.sh -- idempotent downloader for mf6, triangle, gridgen, minio, mc
# usage: bash scripts/fetch_binaries.sh
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$REPO_ROOT/bin"
THIRD_PARTY_DIR="$REPO_ROOT/third_party"

mkdir -p "$BIN_DIR" "$THIRD_PARTY_DIR"

# ---- MODFLOW 6.7.0 static linux binary ----
# 6.5.0 does not recognise the PRP options flopy 3.10 emits (EXTEND_TRACKING /
# COORDINATE_CHECK_METHOD), which breaks the PRT capture-zone / wellhead solves;
# the pin never goes below 6.7.0.
MF6_VERSION="6.7.0"
MF6_SHA256="f83e743bac1b3009836a44e6e4aa4b4c522ac2e2c7d291e280db47f68353ebdc"
MF6_ZIP="$THIRD_PARTY_DIR/mf${MF6_VERSION}_linux.zip"
MF6_BIN="$BIN_DIR/mf6"

if [ ! -f "$MF6_BIN" ]; then
  echo "[fetch_binaries] downloading mf6 ${MF6_VERSION}..."
  curl -L --progress-bar \
    "https://github.com/MODFLOW-USGS/modflow6/releases/download/${MF6_VERSION}/mf${MF6_VERSION}_linux.zip" \
    -o "$MF6_ZIP"
  echo "[fetch_binaries] verifying mf6 zip checksum..."
  echo "${MF6_SHA256}  ${MF6_ZIP}" | sha256sum -c - || {
    echo "[fetch_binaries] ERROR: mf6 zip checksum mismatch" >&2
    exit 1
  }
  echo "[fetch_binaries] unzipping mf6..."
  unzip -o "$MF6_ZIP" -d "$THIRD_PARTY_DIR/mf${MF6_VERSION}_linux" > /dev/null
  # binary lives under bin/mf6 inside the zip
  MF6_EXTRACTED=$(find "$THIRD_PARTY_DIR/mf${MF6_VERSION}_linux" -name "mf6" -type f | head -1)
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

# ---- triangle + gridgen: two of the MODFLOW-ORG executables release's binaries ----
EXEC_RELEASE="29.0"
EXEC_SHA256="ab17192a3531dec61d4f4eab3da10f618e85d1f792d5edc71306a3e06e0e22c3"
EXEC_ZIP="$THIRD_PARTY_DIR/executables_${EXEC_RELEASE}_linux.zip"

if [ ! -f "$BIN_DIR/triangle" ] || [ ! -f "$BIN_DIR/gridgen" ]; then
  echo "[fetch_binaries] downloading triangle + gridgen (executables ${EXEC_RELEASE})..."
  curl -L --progress-bar \
    "https://github.com/MODFLOW-ORG/executables/releases/download/${EXEC_RELEASE}/linux.zip" \
    -o "$EXEC_ZIP"
  echo "${EXEC_SHA256}  ${EXEC_ZIP}" | sha256sum -c - || {
    echo "[fetch_binaries] ERROR: executables zip checksum mismatch" >&2
    exit 1
  }
  python3 - "$EXEC_ZIP" "$BIN_DIR" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
names = [n for n in z.namelist() if n.rsplit("/", 1)[-1] in ("triangle", "gridgen")]
assert len(names) == 2, names
for n in names:
    open(sys.argv[2] + "/" + n.rsplit("/", 1)[-1], "wb").write(z.read(n))
PY
  chmod +x "$BIN_DIR/triangle" "$BIN_DIR/gridgen"
else
  echo "[fetch_binaries] triangle + gridgen already present, skipping download"
fi

echo "[fetch_binaries] verifying triangle + gridgen..."
"$BIN_DIR/triangle" -h 2>&1 | grep -qi "delaunay"
"$BIN_DIR/gridgen" 2>&1 | grep -q "GRIDGEN Version"

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
