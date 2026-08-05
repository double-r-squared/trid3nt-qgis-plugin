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
EPSG="${TRID3NT_HECRAS_EPSG:?set TRID3NT_HECRAS_EPSG to the DEM CRS, e.g. EPSG:2966}"
TERRAIN_TIF="${TRID3NT_HECRAS_TERRAIN_TIF:-$WORK/terrain.tif}"
NVALUE_TIF="${TRID3NT_HECRAS_NVALUE_TIF:-$WORK/nvalue.tif}"
mkdir -p "$OUT"

echo "== [1/2] ras createterrain (DEM -> HEC terrain, substituted GDAL/HDF5) =="
# Build terrains IN the mount so the stored tile paths resolve (ADR 0129 note).
( cd "$APP"
  dotnet ras.dll createterrain -f "$TERRAIN_TIF" -o "$WORK/terrain.hdf" -j "$EPSG"
  dotnet ras.dll createterrain -f "$NVALUE_TIF"  -o "$WORK/nvalue.hdf"  -j "$EPSG" )

echo "== [2/2] AuthorMesh (TryCreateMesh topology + ComputeFrom subgrid tables) =="
dotnet "$APP/authormesh.dll" "$IN" "$OUT" "$WORK/terrain.hdf" "$WORK/nvalue.hdf"

echo "== authoring stage complete -> $OUT (topology + subgrid tables) =="
ls -1 "$OUT"
