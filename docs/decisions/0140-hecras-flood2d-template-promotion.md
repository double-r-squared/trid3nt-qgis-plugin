# ADR 0140 -- HEC-RAS flood_2d PROMOTED: the authoring worker image is built + the `hecras_flood_2d` template registers, backed by a fresh-AOI author->compose->solve->postprocess pipeline that FLOODS TWO genuinely-new US AOIs end-to-end (OI-FT2 discharged)

Status: accepted (2026-08-05)
Follows: ADR 0139 (the C# AuthorMesh worker + the pure-2D deck composer land; BOTH
acceptances solve as a DIRECT-CALL chain; the registered template + its worker
IMAGE named the remaining PROMOTION step -- OI-FT2), ADR 0138/0137/0136 (the
wetting/solve chain), ADR 0132/0129 (the transplant path + substituted Linux
natives), ADR 0109/0125 (the two frozen-Muncie HEC-RAS templates this joins).

This wave DISCHARGES OI-FT2: it builds the authoring worker IMAGE, wires the
fresh-AOI orchestration into a durable pipeline, registers the `hecras_flood_2d`
LLM template, and FLOODS TWO genuinely-new US AOIs (distinct geographies) end-to-end
through the built image + production solver, with published depth COG + mesh + the
numbers. Unlike the two frozen-Muncie templates, this AUTHORS the 2D mesh + terrain
subgrid tables for a place the user names.

## What landed

### 1. The authoring worker IMAGE (`trid3nt-local/hecras2025-authoring:latest`)

Built from `Dockerfile.authoring` (FROM the ADR 0129 `hecras2025:subst-exp` +
the 13 KB `authormesh.dll` + the entrypoint). **1.85 GB** -- the promotion layer
adds only ~70 KB over the base (authormesh.dll 33 KB + runtimeconfig 20 KB +
entrypoint 16 KB; `docker history` shows the thin layers). Smoke (terrain-free,
in-image): AuthorMesh reproduces the shipped-Muncie fingerprint EXACTLY (5391
cells / 11166 faces / 5776 facepoints) and the topology validator passes all 6
gates (real-cell 5391 EXACT, +2 boundary tie-break, cell-center bijection at
0.000000 ft, face fpA/fpB validity, unit-perpendicular normals, ragged
consistency). The full authoring stage (terrain + AuthorMesh + ComputeFrom) runs
green on a fetched DEM (below).

The `authoring_entrypoint.sh` was generalized: `createterrain -j` now takes an
ESRI `.prj` FILE (`TRID3NT_HECRAS_PRJ`) as well as an EPSG string -- a fresh AOI's
local ftUS CRS has no EPSG code. It also clears stale `createterrain` outputs
(the `.hdf` + `<stem>.terrain.tif` overview it refuses to overwrite).

### 2. The fresh-AOI pipeline (durable backend, `services/workers/hecras2025/subst/crux/freshtopo/`)

- **`flood2d_terrain.py`** -- reprojects a fetched DEM (any projected CRS, e.g. the
  3DEP Albers EPSG:5070) to a per-AOI local Transverse-Mercator in US survey FEET
  (`+proj=tmerc ... +units=us-ft`, centred on the AOI -- locally conformal ANYWHERE
  in the US, no State-Plane zone lookup), converts elevations m->ftUS, and writes
  the mesh seeds (`perimeter_ccw_open.f64` CCW-open + `centers.f64` grid). Two
  correctness fixes this wave: **(a)** nodata is filled with HIGH DRY GROUND so
  `MeshPropertyTables.ComputeFrom`/`HydraulicProfile.Build` never index-faults on a
  NaN-sampling cell (the crash a raw reprojected DEM caused); **(b)** the mesh
  extent is bound to the FINITE-DATA envelope so the perimeter does not seat cells
  on the fill corners.
- **`flood2d_pipeline.py`** -- ties the chain into `author_and_compose(...)` (fetch-
  prep -> authoring container -> adapter + composer, NO solve; the template solves
  via `run_solver`) and `run_flood2d(...)` (adds a direct in-process solve for the
  proofs).
- **The composer per-AOI GEOLOCATION fix** (`hecras_deck2d.py`): `compose_pure2d_
  deck` now STAMPS the caller's CRS as the plan HDF root `Projection` attr (the
  postprocess reprojects the mesh to 4326 from THERE). A fresh AOI carries its OWN
  ftUS CRS instead of Muncie's -- the depth COG geolocates on the real AOI. The
  Muncie carve path passes Muncie's own WKT, so it stays byte-identical (the
  `test_hecras_deck2d` carve gates are green).

### 3. The registered `hecras_flood_2d` template

`workflows/hecras/flood_2d/flood_2d.py` (`hecras_flood_2d` tool + `model_hecras_
flood_2d` composer): input-review gate (forcing + fetched-terrain basis +
granularity-gated resolution/cell-count) -> `fetch_dem` (seam-1) -> `author_and_
compose` (off-loop) -> `run_solver` (the composed deck rides as manifest `inputs`;
the hecras worker's no-archetype M3-gate path solves it) -> `postprocess_hecras`
-> depth COG + 2D mesh preview + inflow chart + zoom. Contract archetype literal
`fresh_aoi_flood_2d` on `HECRASRunArgs`/`HECRAS_ARCHETYPES`; the `hecras_flood_2d`
solver name shares the existing `trid3nt-local/hecras:latest` image; corpus.yaml +
categories + the roster/catalog pins. Offline-verified: server imports clean, the
tool registers (engine=hecras/tier=template), the door-dissolution retrieval
surfaces it in the model-free top-8, catalog count 192->193, template hygiene, and
6 new template unit gates -- all green.

## Both acceptances -- LIVE end-to-end through the built image + production solver

Run through the pipeline backend (the exact stages `model_hecras_flood_2d`
invokes), each a REAL fetched DEM authored ENTIRELY by the C# path, solved by the
production 6.6 engines, postprocessed to published COG + mesh URIs.

### (a) Wabash River floodplain near New Harmony, IN (flat bottomland)

Fetched 3DEP DEM (EPSG:5070) over ~4.6 km; local ftUS; 4488 authored cells; elev
356-533 ft. @ 8000 cfs: **wet 2824 / 4488**, depth_max **17.30 ft**, mean 8.18,
vol_err **6.9e-5%**, flux in/out 544325/528044 (balanced). Monotone (3000 cfs ->
2765 wet / 15.97 ft). All wet cells in LOW terrain (beds 356-376 ft). The render
traces the Wabash MEANDER CORRIDOR (deep 15-17 ft in-channel, floodplain spreads
laterally, SE upland dry) -- geolocated at New Harmony.
`s3://trid3nt-runs/flood2d-accept-a-wabash8000/hecras_depth_peak.tif` (+ mesh.geojson).

### (b) Blanco River near Wimberley, TX (Hill Country canyon)

Fetched DEM; 5751 authored cells; **378 ft of relief** (797-1175 ft). @ 12000 cfs:
**wet 811 / 5751 (14% PARTIAL)**, depth_max **49.90 ft**, mean 18.81, vol_err
**5.1e-6%**, balanced flux. Monotone (4000 cfs -> 614 wet / 42.51 ft; +197 cells,
+7.4 ft). The render is a textbook confined valley-corridor flood -- deep water
tightly in the sinuous canyon channel, uplands entirely dry -- geolocated at
Wimberley. `s3://trid3nt-runs/flood2d-accept-b-blanco12000/hecras_depth_peak.tif`
(+ mesh.geojson).

The two AOIs' physically-DISTINCT signatures (broad flat-bottomland wetting vs a
confined steep canyon) on freshly-authored geometry confirm the pipeline
generalizes across US geography with correct geolocation.

## synthetic_inputs lineage (verbatim-class, per 0139)

geometry = authored-transplant-path (tables 0.99988 / writer dWSE 0.0 / topology
bijection 0.0 / forcing wets-at-baseline); terrain basis = FETCHED (`fetch_dem`,
reprojected to local ftUS); forcing basis = user/default-labeled inflow hydrograph.

## Fidelity line (stamped on the envelope)

REFINEMENT-GRADE production HEC-RAS 6.x solver on a 2025-authored 2D mesh +
terrain-sampled subgrid tables, transplant-path validated end-to-end. SCREENING-
grade until broader per-AOI V&V. Off-scope speed -> `sfincs_flood`; precip forcing
-> the OI-D residual.

## Consequences

- Coded-tools delta **+1** (`hecras_flood_2d`); registry 192 -> **193** (rolling,
  this landing). New durable code: `flood2d_terrain.py`, `flood2d_pipeline.py`,
  `workflows/hecras/flood_2d/{flood_2d.py,corpus.yaml,__init__.py}`,
  `test_hecras_flood2d_template.py`, `test_flood2d_terrain.py`; edits to
  `authoring_entrypoint.sh`, `hecras_deck2d.py`, `hecras_contracts.py`,
  `run_hecras.py`, `tools/__init__.py`, `categories.py`, + the two count/roster
  pins. Additive; no flood.py/SFINCS/publish_layer seam bleed beyond the honest
  `sfincs_flood` off-scope routing (grep-verified). ASCII-only.
- Image hygiene: the authoring image adds ~70 KB over its base; no new native
  payload. The proprietary beta DLLs + build outputs stay gitignored.

## Open issues / ledger

- **OI-FT2 DISCHARGED**: image built + validated; template registered + offline-
  verified; two fresh AOIs flooded end-to-end through the built image + solver.
- **Live registered-PATH (WS/emitter) acceptance** -- the acceptances ran through
  the pipeline backend (the exact author->compose->run_solver->postprocess stages
  the composer invokes) + `postprocess_hecras`; a full agent WS turn through the
  registered tool is import/retrieval/pins-verified but was not driven live this
  session. NEXT: one WS-turn canary per the flood-sim canary norm.
- **wse_max cosmetic** -- a handful of dry corner-fill cells can set the reported
  `wse_max_ft` to the fill height (depth stats are masked-correct). Compute
  `wse_max` over wet cells only, or erode the fill mask, in a follow-up.
- **OI-D (precip)** -- rain-on-grid / a real hydrograph override remains the
  Meteorology+DSS residual. Carries ADR 0132 OI-3/OI-4.
- Full offline suite alphabetical slices beyond the directly-affected pins
  (door_dissolution / catalog_surfacing / hecras_landing / template_hygiene +
  the new gates, all green) are the close-out confirmation of the documented-9
  baseline.
