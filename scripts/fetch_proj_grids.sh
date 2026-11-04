#!/usr/bin/env bash
# fetch_proj_grids.sh -- idempotent: the PROJ vertical grids the offline datum seam reads (CONUS and Alaska).
# usage: bash scripts/fetch_proj_grids.sh
# Installs to third_party/proj (TRID3NT_PROJ_GRIDS overrides it); each file is sha256-pinned.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GRID_DIR="${TRID3NT_PROJ_GRIDS:-$REPO_ROOT/third_party/proj}"
CDN="https://cdn.proj.org"

# name  sha256
GRIDS="
us_nga_egm08_25.tif 4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a
us_noaa_g2012ba0.tif 3b854ebf5aac8442426665a033bea918249c570cd355d99b812e3ec59b4b9e41
us_noaa_g2018u0.tif fa9a407ac7ee3f5a3694008e4bcd09ce9cc250452f0c3b11700a4960340abce2
us_noaa_vertconc.tif 2e56823c84d9e191e3c5157da37e31ce6489e9e750f162524173ba3a940916c9
us_noaa_vertcone.tif 867f1a943bce71a8c8ed5b43cce0c3590d49f94c1054793497e083cc53f3d3b0
us_noaa_vertconw.tif c9f314340de964cbe21f499b20615475ef3f73a2a02bab3a3f80a6d21af55522
"

mkdir -p "$GRID_DIR"
while read -r name sha; do
  [ -n "$name" ] || continue
  if [ -f "$GRID_DIR/$name" ] && echo "$sha  $GRID_DIR/$name" | sha256sum -c - >/dev/null 2>&1; then
    echo "[fetch_proj_grids] $name already present, skipping"
    continue
  fi
  echo "[fetch_proj_grids] downloading $name..."
  curl -fL --progress-bar "$CDN/$name" -o "$GRID_DIR/$name.part"
  echo "$sha  $GRID_DIR/$name.part" | sha256sum -c - || {
    rm -f "$GRID_DIR/$name.part"
    echo "[fetch_proj_grids] ERROR: $name checksum mismatch" >&2
    exit 1
  }
  mv "$GRID_DIR/$name.part" "$GRID_DIR/$name"
done <<< "$GRIDS"
echo "[fetch_proj_grids] OK: $GRID_DIR"
