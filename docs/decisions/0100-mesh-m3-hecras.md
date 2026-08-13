# ADR 0100 -- Mesh layer, wave M3 (HECRAS WRITER + worker)

Status: accepted (2026-08-03)
Spec: docs/specs/mesh-layer-extraction.md (SIGNED, NATE 2026-08-03), wave M3.
Follows: ADR 0098 (M1 EXTRACT), ADR 0099 (M2 GENERALIZE), and the NATE-signed
ras-commander feasibility spike (reports/design/ras-commander-feasibility-2026-08-03.md
-- 6.x Linux Computation Engines NOW, HEC-RAS 2025 migration ledgered).
Scope: M3 = the HEC-RAS worker image carrying the 6.x Linux computation engines
+ the `hecras_geometry` mesh component, gated on MUNCIE REPLICATION. NOT the
HEC-RAS engine landing (no registered tool / template / contract archetype).

## Context

The spec's WRITE stage named a new `hecras_geometry` terminal writer ("cells +
breaklines to the geometry HDF; hydraulic property tables computed by HEC's own
Linux RasGeomPreprocess inside the worker, NOT by us"). M3 builds the worker that
runs those Linux engines, proves the geometry pipeline on HEC's own shipped
Muncie test project, and resolves the writer's feasibility honestly.

## Decisions

### 1. HEC-RAS worker image (`services/workers/hecras/`)

Multi-stage `python:3.11-slim` image (mirror of the modflow worker's base +
download-and-SHA-verify discipline):
- Stage `fetch`: downloads HEC's OFFICIAL `Linux_RAS_v66.zip`
  (`https://www.hec.usace.army.mil/software/hec-ras/downloads/Linux_RAS_v66.zip`,
  SHA-256 `e77271a473da5da28b5a95ebf019f77ba3d32fb6341ad43be3ad4a6004c60e4a`,
  verified BEFORE extraction), unpacks the three engines
  (RasGeomPreprocess/RasUnsteady/RasSteady) + the bundled Intel/MKL/rhel_8 libs +
  the Muncie test case.
- Stage `runtime`: bakes the engines (`/opt/hecras/bin`, `COPY --chmod=755`) +
  libs (`/opt/hecras/libs`), installs `ras-commander` (MIT) + `h5py` + `numpy`,
  copies the worker code + Muncie fixture. `LD_LIBRARY_PATH=libs:libs/mkl:libs/
  rhel_8` (HEC's own run-script order), engines on PATH.

Engine ABI: 6.6 compiled x64 under RHEL 8 (glibc 2.28); Bookworm's glibc 2.36 is
forward-compatible (verified -- every dependent .so resolves against the bundled
libs + system glibc, and Muncie runs green). `libexpat1` added to apt (the
modflow lesson: ras-commander pulls rasterio, whose wheel dlopen()s libexpat).

Provenance/licensing: HEC-RAS binaries are public-domain U.S. Federal Government
software, freely redistributable (feasibility report S6); HEC acknowledgment
carried in the image LABEL. ras-commander is MIT.

**IMAGE SIZE (container-hygiene hard rule -- inspected via `docker history`),
single-platform amd64, ~1.69 GB uncompressed:**
| layer | size |
| --- | --- |
| ras-commander venv closure (scipy/pandas/geopandas/rasterio/xarray) | 777 MB |
| HEC-RAS libs (Intel runtime + MKL + rhel_8) | 599 MB |
| HEC-RAS engines (3 binaries, `COPY --chmod`) | 170 MB |
| Debian base + apt/python | ~140 MB |
| worker code + Muncie fixture | 4.4 MB |

Hygiene actions taken: multi-stage (the 218 MB zip + curl/unzip stay in `fetch`,
never in runtime); `.dockerignore` (tests/pyc/run-dirs); `COPY --chmod=755`
instead of a `chmod +x` RUN (eliminated a duplicate 170 MB binary layer,
~1.86 -> 1.69 GB); single-platform `--platform linux/amd64 --provenance=false`
(the engines are amd64-only -- a multi-arch/attestation build ballooned the
manifest to 2.39 GB for no benefit). Two dominant costs, both CHARACTERIZED as
future trims (not done in M3): (a) MKL (~570 MB of the libs layer) dispatches the
CPU-appropriate kernel at run time, so trimming variants needs per-CPU runtime
validation for a refinement-grade solver; (b) the ras-commander closure trims to
~370 MB via `--no-deps` + a minimal set (h5py/numpy/pandas/xarray) once the
engine-landing wave pins exactly which `Hdf*` methods it calls.

### 2. Muncie replication -- the acceptance gate (GREEN)

HEC's shipped Muncie test (White River, Muncie IN): a combined 1D (61
cross-sections) + 2D (5765-cell flow area) unsteady model. Two comparison bases,
both proven on this Linux stack (host + in-container, 2026-08-03):

- **GATE A -- hydraulic property tables**: `RasGeomPreprocess Muncie.p04.tmp.hdf
  x04` rebuilds the 1D cross-section conveyance tables from the geometry. Since
  the geometry is unchanged, the Linux-recomputed tables reproduce the
  GUI-computed baseline (`wrk_source/`) EXACTLY -- `max|diff| == 0` on the 1D
  `XSEC Value`/`Cell Value` AND the 2D `Cells Volume Elevation Values` /
  `Faces Area Elevation Values` / cell+face min elevations (all bit-identical).
- **GATE B -- volume accounting**: `RasUnsteady Muncie.p04.tmp.hdf x04` mass
  balance `Error Percent = 0.005835%` (well inside the < 0.05% tolerance; the
  community neeraip Docker repro reports Muncie at ~0.00% / 0.001 ft WSE vs the
  Windows GUI). 2D max water surface = 951.9 ft (matches the White-River stage
  range in HEC's release notes).

The gate driver `fixtures/muncie_smoke/muncie_smoke.py` runs both gates and exits
nonzero on any divergence. Honest-failure surface: the engines exit 0 even on a
solve error, so both the entrypoint and the driver gate on the `Finished`
sentinel, not the exit code (verified: a deck missing the 2D subgrid tables
prints "Unsteady flow encountered an error", exit 0 -- correctly flagged failed).

### 3. `hecras_geometry` writer -- WRITE STOPPED, READ/preview BUILT

The write direction (from-scratch cells+breaklines -> a geometry HDF the
preprocessor accepts) is BLOCKED, proven empirically offline-first:
- HEC-RAS 2D hydraulics are SUBGRID: each cell carries a terrain-sampled
  volume<->elevation table (`Cells Volume Elevation Values`), each face an
  area<->elevation table (`Faces Area Elevation Values`). `RasUnsteady`'s
  `Subroutine READBathymetry` requires them.
- `RasGeomPreprocess` does NOT build the 2D subgrid tables. EXPERIMENT: stripping
  them from the Muncie HDF and re-running the preprocessor left them ABSENT (it
  reprocessed only the 1D cross-sections); the subsequent `RasUnsteady` then
  failed with `object 'Cells Volume Elevation Info' doesn't exist`. Those tables
  are authored by RASMapper (Windows RASMapperLib DLLs) -- exactly the feasibility
  report's named caveat ("headless 2D mesh authoring is the real frontier;
  ras-commander's GeomMesh needs Windows DLLs").

So a topology-only 2D flow area (perimeter + cell points + faces/facepoints)
is INSUFFICIENT -- nothing on the Linux stack computes the subgrid tables, so the
deck never solves. Replicating RASMapper's terrain subgrid sampling into HEC's
undocumented internal table format is the genuine blocker. Per the signed spec,
the write sub-item STOPS honestly here; building a topology-only writer no Linux
engine can consume would be dead code.

TEMPLATE-FIRST FALLBACK (the engine-landing wave's path, PROVEN by the Muncie
gate): reuse a RASMapper-built 2D geometry HDF as a template and reparameterize
terrain association + Manning's n + forcing/BCs (ras-commander RasGeo/RasUnsteady
editors).

BUILT (the tractable, reusable half): `agent/mesh/hecras_geometry.py`
`read_2d_flow_area_cells(hdf) -> (FeatureCollection, stats)` reads a geometry
HDF's 2D flow-area cell mesh (topology mapped offline-first against the real
Muncie fixture, never guessed) into an EPSG:4326 mesh-preview layer -- cell
polygons + the domain perimeter, reprojected from the model projection. This is
the M1/M2 `style_preset="mesh_grid"` preview paradigm extended to HEC-RAS 2D, it
renders the NATE spot-check proof (the Muncie 2D mesh, 5391 cells over White
River), and the engine-landing wave's postprocess/preview reuses it. h5py+pyproj
are lazy-imported so the mesh package stays offline-suite-safe with no new hard
dep.

## Consequences

- New worker `services/workers/hecras/` (image + entrypoint + Muncie fixture +
  tests) + one new server mesh component (`hecras_geometry.py`). Registry /
  coded-tool / spec-served counts UNCHANGED (registry 175 byte-identical -- worker
  + component code only, no registered tool). No new WS event, no contract enum.
- Muncie gate GREEN host + in-container (property tables bit-identical, volume
  error 0.005835%).
- The HEC-RAS 2025 native-Linux migration stays ledgered (feasibility report S2b)
  -- when it exits beta, its native mesher may retire the write blocker.
- Offline baseline unchanged (the documented 6-failure baseline at M2 head).
  Flood canary NOT mandated (no SFINCS/flood-solve seam touched -- this is a new
  isolated worker + a read-only mesh component).

## M4 hand-off (queued) + what the HEC-RAS engine-landing wave needs next

- M4 QUADTREE TRUTH (signed BUILD): the SFINCS quadtree leg atop stage 2.
- HEC-RAS engine landing (separate wave, template-first): contract
  (`HECRASRunArgs` + `FloodDepthLayerURI`), a run_solver('hecras') local-docker
  dispatch reusing this image, a shipped-geometry template + reparameterize path
  (terrain via fetch_dem, forcing via NWM/USGS), postprocess plan-HDF ->
  depth/WSE COG via ras-commander `HdfResultsMesh`, discovery/corpus +
  retrieve_visible_tools check, a second archetype (Bald Eagle Creek 2D levee).
