#!/usr/bin/env bash
# build_telemac_image.sh -- build the TRID3NT TELEMAC-2D river-dye local worker
# image (trid3nt-local/telemac:latest). Mirrors the swan build line in
# docs/site/install.md: build context = the worker dir itself (NOT the 588 MB
# services/workers tree), Dockerfile discovered in it, a .dockerignore trims tests/pyc.
#
# Usage: bash scripts/build_telemac_image.sh   (run docker-group as needed, e.g.
#        `sg docker -c 'bash scripts/build_telemac_image.sh'`).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="$REPO_ROOT/services/workers/telemac"
IMAGE="${TRID3NT_TELEMAC_IMAGE:-trid3nt-local/telemac:latest}"
echo "[build] $IMAGE  (context $CTX)"
docker build -t "$IMAGE" "$CTX"
echo "[build] done: $IMAGE"
docker images "$IMAGE" --format '  size: {{.Size}}'
