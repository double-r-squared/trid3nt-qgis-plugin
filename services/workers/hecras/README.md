# HEC-RAS 6.x Linux worker (mesh wave M3)

Runtime image carrying HEC's official public-domain **6.6 Linux computation
engines** (`RasGeomPreprocess` / `RasUnsteady` / `RasSteady`) so the geometry
preprocess + unsteady solve run headless -- no Windows, no GUI. This is the mesh
layer's **M3** wave (prove the geometry pipeline), NOT the full HEC-RAS engine
landing: there is no registered tool / template / contract archetype yet.

## What lands here

- `Dockerfile` -- multi-stage: stage 1 downloads HEC's `Linux_RAS_v66.zip`
  (SHA-256 pinned + verified before extraction), stage 2 (`python:3.11-slim`)
  bakes the engines + bundled Intel/MKL/rhel_8 libs + `ras-commander` (MIT) +
  `h5py`. HEC binaries are public-domain U.S. Federal Government software.
- `entrypoint.py` -- bind-mount worker (telemac pattern): reads `/data/manifest.json`,
  runs `RasGeomPreprocess` (optional) -> `RasUnsteady`, extracts volume
  accounting + max WSE via pure h5py -> `hecras_metrics.json`. Honest failure:
  gates on the engine `Finished` sentinel (the engines exit 0 even on a solve
  error), raising rather than silently passing.
- `fixtures/muncie_smoke/` -- the M3 acceptance gate (see its README).

## Build + run

```sh
docker build -t trid3nt-local/hecras:latest services/workers/hecras/

# Muncie gate in-container (bind-mount a rundir seeded from the fixture):
rundir=$(mktemp -d)
cp services/workers/hecras/fixtures/muncie_smoke/wrk_source/*.* "$rundir/"
cp services/workers/hecras/fixtures/muncie_smoke/manifest.json  "$rundir/"
docker run --rm -v "$rundir":/data trid3nt-local/hecras:latest
cat "$rundir/hecras_metrics.json"
```

## ABI + engine facts

- Engines: HEC-RAS 6.6, compiled x64 under RHEL 8 (glibc 2.28), Intel oneAPI
  Fortran 2021.4.0 + MKL. Invocation: `RasGeomPreprocess <plan.hdf> <geom_suffix>`
  then `RasUnsteady <plan.hdf> <geom_suffix>` with
  `LD_LIBRARY_PATH=libs:libs/mkl:libs/rhel_8`.
- Base `python:3.11-slim` (Bookworm, glibc 2.36) is forward-compatible with the
  glibc-2.28 target; the zip bundles `libgfortran.so.5`/`libquadmath.so.0` + the
  Intel runtime + MKL, so no Fortran-runtime apt package is needed.
- Scope (feasibility report): 1D steady (`RasSteady`) + 1D/2D unsteady
  (`RasUnsteady`) + geometry preprocess (`RasGeomPreprocess`). Sediment /
  water-quality legs are not in HEC's Linux test set -- out of scope.

## The 2D geometry-writer boundary (ADR 0100)

HEC-RAS 2D hydraulics are subgrid: each cell/face carries a terrain-sampled
elevation<->volume/area table. `RasGeomPreprocess` does NOT build those 2D
tables (RASMapper / Windows RASMapperLib does), and `RasUnsteady` cannot run
without them. So a from-scratch 2D geometry writer is blocked; the engine-landing
path is **template-first** (reparameterize a shipped RASMapper-built geometry),
which the Muncie gate proves. The server-side `agent/mesh/hecras_geometry.py`
component reads a geometry HDF's 2D mesh into a publishable preview layer (the
tractable half).
