#!/bin/bash
# ADR 0251 culvert-through-embankment A/B/C seam proof.
#   cv_free  (C): no ridge,  no culvert -> flows freely
#   cv_block (B): ridge,     no culvert -> ponds upstream
#   cv_pass  (A): ridge  +   culvert    -> passes flow under the ridge
set -e
P=/home/nate/hecras_probe2025
IMG=trid3nt-local/hecras2025-authoring:latest

# --- 0. wipe prior case dirs so authoring is from scratch (Save() won't overwrite) ---
for CASE in cv_free cv_block cv_pass; do
  rm -rf "$P/$CASE" "$P/${CASE}_r2r" "$P/${CASE}_result.h5"
done

# --- 1. author all three projects in-image ---
docker run --rm -v "$P:/probe" --entrypoint /bin/sh "$IMG" -c '
  cd /opt/hecras2025/app
  cp /probe/driver/out/synthdrv.dll .
  cp ras.runtimeconfig.json synthdrv.runtimeconfig.json
  dotnet synthdrv.dll culvertdemo /probe/cv_free  0
  dotnet synthdrv.dll culvertdemo /probe/cv_block 0
  dotnet synthdrv.dll culvertdemo /probe/cv_pass  1
'

# --- 2. host-side embankment ridge on the two embankment cases ---
PY=/home/nate/Documents/trid3nt-local/venvs/agent/bin/python
$PY $P/raise_ridge.py /probe/cv_block 2>/dev/null || $PY $P/raise_ridge.py $P/cv_block
$PY $P/raise_ridge.py $P/cv_pass

# --- 3. prepare + solve all three in-image ---
docker run --rm -v "$P:/probe" --entrypoint /bin/sh "$IMG" -c '
  cd /opt/hecras2025/app
  for CASE in cv_free cv_block cv_pass; do
    echo "===== prepare+solve $CASE ====="
    RAS=$(ls /probe/$CASE/*.ras | head -1)
    rm -rf /probe/${CASE}_r2r; mkdir -p /probe/${CASE}_r2r
    dotnet ras.dll prepare -s "$RAS" -o /probe/${CASE}_r2r -f 2>&1 | tail -3
    R2R=$(ls /probe/${CASE}_r2r/*.r2r.h5 | head -1)
    dotnet ras.dll solve "$R2R" /probe/${CASE}_result.h5 --solver CPU -f 2>&1 | tail -3
  done
'
echo "ALL DONE"
