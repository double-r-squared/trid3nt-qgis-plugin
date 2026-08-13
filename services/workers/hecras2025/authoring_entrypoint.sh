#!/usr/bin/env bash
# HEC-RAS 2D AUTHORING stage (OI-B, ADR 0139): DEM + mesh seeds -> topology + tables.
#
# Runs INSIDE trid3nt-local/hecras2025-authoring (the ADR 0129 substituted natives).
# Inputs bind-mounted at /work (terrain + nvalue GeoTIFF) and /in (the RASMapper
# mesh seeds); outputs the full topology + subgrid-table dump to /out, which the
# host/orchestrator adapts (authormesh_to_mesh2d) + composes (compose_pure2d_deck)
# + solves (trid3nt-local/hecras:latest). See ADR 0139 for the end-to-end chain.
#
#   /work/terrain.tif  /work/nvalue.tif   the AOI DEM + Manning-n rasters (any US CRS)
#   /in/perimeter_ccw_open.f64            the AOI perimeter (CCW, open; float64 x,y)
#   /in/centers.f64                       the cell-center seeds (grid @ resolution_m)
#   /out                                  the dumped arrays (Mesh2D + SubgridTables)
set -euo pipefail

APP=/opt/hecras2025/app
WORK="${TRID3NT_HECRAS_WORK:-/work}"
IN="${TRID3NT_HECRAS_IN:-/in}"
OUT="${TRID3NT_HECRAS_OUT:-/out}"
TERRAIN_TIF="${TRID3NT_HECRAS_TERRAIN_TIF:-$WORK/terrain.tif}"
NVALUE_TIF="${TRID3NT_HECRAS_NVALUE_TIF:-$WORK/nvalue.tif}"
mkdir -p "$OUT"

# The CRS reaches createterrain's -j as EITHER an ESRI .prj FILE (fresh-AOI custom
# ftUS CRS have no EPSG code -- TRID3NT_HECRAS_PRJ) OR an EPSG string
# (TRID3NT_HECRAS_EPSG, e.g. a State-Plane fixture). A .prj file wins if set.
if [ -n "${TRID3NT_HECRAS_PRJ:-}" ]; then
  JARG="$TRID3NT_HECRAS_PRJ"
elif [ -n "${TRID3NT_HECRAS_EPSG:-}" ]; then
  JARG="$TRID3NT_HECRAS_EPSG"
else
  echo "set TRID3NT_HECRAS_PRJ (ESRI .prj path) or TRID3NT_HECRAS_EPSG (e.g. EPSG:2966)" >&2
  exit 3
fi

echo "== [1/2] ras createterrain (DEM -> HEC terrain, substituted GDAL/HDF5) -j=$JARG =="
# Build terrains IN the mount so the stored tile paths resolve (ADR 0129 note).
# Clear any stale outputs first (createterrain refuses to overwrite its .hdf +
# the "<stem>.terrain.tif" overview it exports beside it).
rm -f "$WORK/terrain.hdf" "$WORK/nvalue.hdf" \
      "$WORK/terrain.terrain.tif" "$WORK/nvalue.terrain.tif"
( cd "$APP"
  dotnet ras.dll createterrain -f "$TERRAIN_TIF" -o "$WORK/terrain.hdf" -j "$JARG"
  dotnet ras.dll createterrain -f "$NVALUE_TIF"  -o "$WORK/nvalue.hdf"  -j "$JARG" )

echo "== [2/2] AuthorMesh (TryCreateMesh topology + ComputeFrom subgrid tables) =="
dotnet "$APP/authormesh.dll" "$IN" "$OUT" "$WORK/terrain.hdf" "$WORK/nvalue.hdf"

echo "== authoring stage complete -> $OUT (topology + subgrid tables) =="
ls -1 "$OUT"
