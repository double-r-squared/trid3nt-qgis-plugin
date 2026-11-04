#!/usr/bin/env bash
# fetch_opentelemac.sh -- idempotent: the opentelemac v9.0.0 python tree and keyword dictionaries
# the server reads and writes TELEMAC steering files with (no compiled solver).
# usage: bash scripts/fetch_opentelemac.sh
# Installs to third_party/opentelemac (HOMETEL for the server; TRID3NT_OPENTELEMAC_HOME overrides it).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THIRD_PARTY_DIR="$REPO_ROOT/third_party"
HOME_DIR="$THIRD_PARTY_DIR/opentelemac"
VERSION="v9.0.0"
SHA256="3797b9573a78e610e99c89507446783905d10f450500cfedc64a46f9dd21e5f8"
TARBALL="$THIRD_PARTY_DIR/telemac-mascaret-${VERSION}.tar.gz"

mkdir -p "$THIRD_PARTY_DIR"

if [ ! -f "$HOME_DIR/.fetched-${VERSION}" ]; then
  if [ ! -f "$TARBALL" ] || ! echo "${SHA256}  ${TARBALL}" | sha256sum -c - >/dev/null 2>&1; then
    echo "[fetch_opentelemac] downloading opentelemac ${VERSION} source (about 1.2 GB)..."
    curl -L --progress-bar \
      "https://gitlab.pam-retd.fr/otm/telemac-mascaret/-/archive/${VERSION}/telemac-mascaret-${VERSION}.tar.gz" \
      -o "$TARBALL"
    echo "${SHA256}  ${TARBALL}" | sha256sum -c - || {
      echo "[fetch_opentelemac] ERROR: tarball checksum mismatch" >&2
      exit 1
    }
  fi
  rm -rf "$HOME_DIR"
  mkdir -p "$HOME_DIR"
  tar xzf "$TARBALL" -C "$HOME_DIR" --strip-components=1 \
    --wildcards '*/scripts/python3/*' '*/sources/*/*.dico'
  # The launcher's dictionary gate learns the wildcard the solver answers to, exactly as the TELEMAC image does.
  HOMETEL="$HOME_DIR" python3 "$REPO_ROOT/workers/telemac/engine_patch.py" "$HOME_DIR/scripts/python3/execution/telemac_cas.py"
  touch "$HOME_DIR/.fetched-${VERSION}"
  rm -f "$TARBALL"
else
  echo "[fetch_opentelemac] opentelemac ${VERSION} already present, skipping download"
fi
echo "[fetch_opentelemac] OK: HOMETEL=$HOME_DIR"
