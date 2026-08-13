# ADR 0118 -- SCHISM engine landing (engine #12, barotropic tidal archetype)

Status: accepted (2026-08-04)
Follows: ADR 0115 (the SCHISM feasibility spike -- GO: the built v5.11.0 worker,
the QuarterAnnulus verification green in-image, the coastal_tin -> hgrid.gr3
bridge proven), ADR 0116 (remote streaming + THE output contract: clip-to-AOI +
COG, never a raw continental netCDF layer; the mesh live-stream fallback ready for
a `layer_type="mesh"` row), ADR 0109 (the HEC-RAS landing pattern:
contract/template/dispatch/postprocess/acceptance, template-first), ADR 0101 (the
oceanmesh coastal_tin the gr3 bridge consumes), ADR 0105/0106/0107 (one-file
composers, structured synthetic_inputs, the two-mode input gate).

## Context

The spike (ADR 0115) answered the make-or-break questions YES: SCHISM builds, one
official verification case reproduces its analytical solution to 0.6% amplitude
error, and our coastal TIN feeds SCHISM's grid preprocessor cleanly. It shipped
the worker + the gr3 bridge + the QuarterAnnulus fixture but NO registered tool,
contract, or template. This wave lands SCHISM as engine #12 with ONE registered
template. Registry 175 -> 176 (in-process).

## Decisions

### 1. ONE archetype: `schism_tidal_hydro` (barotropic tidal, no forcing legs)

The v1 archetype is the class that needs NO external forcing legs (no HYCOM/ESPC-D
open-ocean fields, no sflux atmosphere, no river discharge): a BAROTROPIC tidal
circulation forced only by an analytical/constituent open boundary. The bigger
archetypes (CORIE estuary, WWM_Duck nearshore waves, STOFS-class surge with real
ESPC-D/G-RTOFS/sflux forcing) are the sign-off candidates (section "What the
candidates need"), NOT this wave.

Two mesh sources under the one archetype:

- **`bundled_quarterannulus`** -- SCHISM's own Test_QuarterAnnulus (Lynch-Gray
  analytical M2 tidal channel). The staged deck is the spike's proven-green
  fixture; the deliverable is the analytical RMSE/amplitude VERIFICATION at the
  station point. An IDEALIZED, NON-GEOGRAPHIC mesh -- so the output is the
  verification (+ a native-frame elevation raster + mesh + station chart), NOT a
  georeferenced clipped COG. The demonstration-geometry honesty is LOUD.
- **`coastal_tin`** -- an oceanmesh `coastal_tin` TIN for a real US coastal AOI,
  bathymetry sampled from fetch_topobathy/fetch_dem onto the TIN nodes (the
  `tin_to_hgrid` bridge -- the spike's placeholder depth REPLACED), a
  spatially-uniform constituent tidal boundary. The deliverable is a max
  water-surface elevation surface CLIPPED to the AOI + COG (THE ADR 0116 contract)
  + the mesh preview + a station elevation-timeseries chart.

### 2. Contract + template + dispatch (the HEC-RAS pattern)

- Contract `contracts/schism_contracts.py`: `SCHISMRunArgs` (archetype literal +
  mesh_source + location/bbox + constituents + tidal_amplitude_m + sim_days +
  open_boundary_side + input_mode), `SchismElevationLayerURI` (extends `LayerURI`;
  carries `elev_max_m` / `elev_min_m` / `tidal_range_m` / `n_nodes` /
  `station_elev_amplitude_m` + the verification triple `analytical_rmse_m` /
  `analytical_amp_err_m` / `analytical_correlation`), typed errors
  (`SCHISM_SOLVE_FAILED`, `SCHISM_MESH_INVALID`, `SCHISM_INPUT_INVALID`,
  `SCHISM_OUTPUT_EMPTY`). The elevation raster HONESTLY REUSES the flood-depth
  family style preset (`continuous_flood_depth`) -- a max-elevation envelope is an
  all-positive high-water surface -- with a data-driven legend.
- Template `workflows/schism/tidal_hydro/tidal_hydro.py` (post-0105 one-file
  composer): params -> author/stage deck (QA fixture OR the authored coastal_tin
  deck) -> input-review gate (0107) -> stage manifest (UPLOAD the generated case
  files as `inputs[]`, the SFINCS-builder pattern) -> dispatch worker -> download
  out2d + station output -> postprocess -> (QA) analytical verification.
- Dispatch `workflows/schism/run_schism.py`: `run_solver('schism_tidal_hydro')`
  -> `trid3nt-local/schism:latest` via a `LocalSolverSpec` (volume-mount `/data`,
  the image ENTRYPOINT drives mpirun, `classify_exit` reads `schism_metrics.json`
  and gates on the worker's sentinel-derived `status` -- SCHISM exits 0 even on a
  mid-run abort, the HEC-RAS lesson).
- Deck authoring `workflows/schism/deck_authoring.py`: stages the QA fixture
  verbatim; for coastal_tin loads the worker's `tin_to_hgrid` bridge BY FILE PATH
  (single source of truth, no duplication), samples bathymetry onto nodes
  (positive-down; land nodes clamped to a wet floor -- a documented screening
  choice), reuses the QA param.nml/vgrid.in as the proven hydro-core template with
  rnday/dt/ihfskip substituted, and authors bctides.in analytically from the
  constituents.
- Postprocess `workflows/schism/postprocess_schism.py`: the out2d UGRID -> per-node
  max elevation -> a nearest-node rasterization masked to the mesh hull -> an
  EPSG:4326 clipped COG (geographic) or a native-frame GeoTIFF (idealized) +
  published; PLUS the out2d UGRID itself as a `layer_type="mesh"` LayerURI (the
  plugin opens it via MDAL); PLUS the station elevation-timeseries chart.

### 3. The mesh emission row (the 0116 handoff)

The live `loaded_layers` contract gains the mesh row: `LayerURI.layer_type` +
`ProjectLayerSummary.layer_type` + `CaseManifestLayer.layer_type` grow
`"mesh"`, and all three gain an optional `crs_authid` (MDAL reports an empty
crs() for a UGRID / quadtree grid, so the plugin's ready `_add_mesh` (ADR 0116)
setCrs()'s from this string). Additive, default-None -- every existing raster/
vector row is byte-for-byte unchanged. The pipeline emitter threads `crs_authid`
onto the WS row (`add_loaded_layer` -> `ProjectLayerSummary.model_dump` ->
`event.raw`); persistence carries it. SCHISM emits the row; SFINCS map meshes are
retroactively emittable through the same contract (NOT wired this wave -- no
flood-seam edit, so no flood canary mandated). Verified end-to-end offline
(`test_add_loaded_layer_threads_mesh_row_end_to_end`).

### 4. Discovery + fidelity line

`schism_tidal_hydro` (tier=template, engine=schism) surfaces in
`retrieve_visible_tools(q, None, 8)` for all corpus queries (tidal circulation
simulation / coastal ocean model schism / barotropic tide on an unstructured mesh
/ cross-scale coastal hydrodynamics / run a SCHISM tidal simulation /
unstructured-grid coastal hydrodynamic model -- 6/6 top-8). Fidelity line
(docstring, verbatim): "A REFINEMENT-GRADE BAROTROPIC TIDAL circulation on an
unstructured coastal mesh (SCHISM) ... Do NOT use this for: FAST arbitrary-AOI
flood screening -- use sfincs_flood; storm SURGE, wind WAVES, or COMPOUND coastal
flooding -- those need the forcing legs and are the coming SCHISM candidates."

## Consequences

- Registry 175 -> 176 (in-process); CODED tools +1 (the `schism_tidal_hydro`
  template). New: contract `schism_contracts.py`, `workflows/schism/` (run_schism +
  deck_authoring + postprocess_schism + `_template_card` + tidal_hydro/ template +
  corpus). Additive contract change: `layer_type` gains `"mesh"` + a `crs_authid`
  field on `LayerURI` / `ProjectLayerSummary` / `CaseManifestLayer`. Worker:
  `services/workers/mesh/entrypoint.py` gains an additive raw-nodes/cells `.npz`
  emission (the gr3 bridge needs them; the geojson is edge-wireframe only). Wired:
  `workflows/__init__.py` (run_schism solver reg), `tools/__init__.py` (template
  import), `categories.py` (`hazard_modeling`).
- **ACCEPTANCE (a) GREEN (live, 2026-08-04)**: the QuarterAnnulus verification
  archetype through the REGISTERED TEMPLATE -- a real docker solve (out2d_1.nc +
  staout_1 in MinIO), postprocessed, verified vs the bundled analytical M2
  solution: **RMSE 0.01551 m** (gate <= 0.030), **amplitude error 0.0027 m** (gate
  <= 0.010), **correlation 0.99882**, station amplitude 0.4393 m, tidal range
  0.8805 m, 130 nodes -- MATCHING the spike's ADR 0115 numbers, re-proven through
  the product path.
- No flood.py / SFINCS seam touched (grep-verified; the mesh-row contract change is
  additive and SFINCS emits no mesh row this wave) -> no flood canary mandated. The
  SCHISM acceptance is this wave's own canary.
- Offline suite baseline preserved (EXACTLY 9 by SET). New-engine test set
  (`test_schism_landing.py`, 15 tests) green: contract round-trip, the mesh row
  end-to-end, gr3+bathymetry bridge, deck determinism, out2d postprocess read,
  run_schism spec + classify_exit, registration pins.

## What the sign-off candidates need next (ADR 0115 4b)

- **coastal_tin live drive (acceptance b)** -- the code path is complete +
  unit-tested; the live Galveston/Mexico-Beach run needs (1) a worker-visible
  GSHHG L1 shoreline shapefile (`TRID3NT_GSHHG_SHP`), (2) the mesh image rebuilt
  with the additive nodes/cells `.npz` emission, (3) a real fetch_topobathy COG for
  the AOI, (4) a multi-day tidal solve on the ~10k-node TIN. Prepared for NATE's
  remote live drive.
- **Test_CORIE** (Columbia River Estuary, OR/WA) -- real elev/currents/T/S vs
  ADCP+CTD; needs the NARR sflux atmospheric packer + multi-station output.
- **Test_WWM_Duck** (Duck NC FRF) -- nearshore wave transformation; needs the
  WWM-III module variant (`pschism_WWM_*`) + `wwminput.nml` + spectral boundary.
- **STOFS-3D-Atlantic replication** -- the US operational system; needs FES2014/
  TPXO tides + G-RTOFS/HYCOM open-ocean + GFS/HRRR sflux, clipped to a sub-domain.
- Per-node FES2014/TPXO tidal boundary (replacing the spatially-uniform screening
  amplitude), and the sflux atmospheric packer (both via pyschism).
