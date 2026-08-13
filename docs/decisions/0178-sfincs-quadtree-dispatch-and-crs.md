# ADR 0178 - SFINCS quadtree: real composer dispatch + coast-following refinement + native-mesh CRS

Date: 2026-08-07
Status: accepted

## Context

ADR 0176 delivered the genuine cht_sfincs quadtree build+solve worker-side and
surfaced two NATE-class defects it deliberately left unfixed (accumulate-and-
wait-for-go):

- **Finding A - the composer-side quadtree dispatch was COSMETIC.**
  `sfincs_flood(quadtree=True)` only flipped `is_coastal` (routing the DEM to
  `fetch_topobathy` + auto-surge). It then called `build_sfincs_model`
  (regular-grid hydromt, no quadtree) and `run_solver` on that regular deck. The
  genuine cht quadtree path existed ONLY in the worker image, reachable only by
  `scripts/run_sfincs_quadtree_direct.py`. `quadtree=True` through the product
  path produced a REGULAR grid.

- **Finding B - the native-mesh `crs_authid` mislabelled a UTM grid as 3857.**
  The genuine `sfincs_map.nc` carries a `crs` variable with `EPSG='-'` (value 0):
  cht_sfincs writes the real EPSG (32616 + WKT) into the DECK (`sfincs.nc`), but
  the SFINCS binary does NOT propagate it to the output `sfincs_map.nc`, so
  `_read_crs_from_dataset` fell back to EPSG:3857 while the nodes are UTM 16N
  metres. QGIS/MDAL would place the mesh at the wrong location.

This ADR fixes both, keeps the regular-grid path byte-identical, and closes with
the flood canary law (hot-path change).

## Decision

### Defect A.1 - coast-following refinement (deck_quadtree.py)

The 0176 run refined a horizontal latitude swath (the domain's middle 40% in y)
as a shoreline proxy. Replaced with a genuine COAST-FOLLOWING band:
`_coast_refinement_geom` reprojects the topobathy DEM to the grid UTM, extracts
the `z == 0` land-sea interface as contour lines, and buffers them by
`coast_band_m` -- the fine cells now hug the ACTUAL meandering shoreline. The
band (a MultiPolygon over disjoint coastal reaches) is EXPLODED into one
single-Polygon `refinement_polygons` row each (cht_sfincs `refine_in_polygon`
reads `polygon.exterior`, so a MultiPolygon row crashes). Refinement criteria are
first-class knobs on `options.quadtree`: `base_resolution_m`, `coast_refine_level`,
`max_refine_level`, and the new `coast_band_m` (half-width; default `max(2*base,
800) m`). When the AOI carries no land-sea interface (entirely wet or dry) the
builder degrades LOUDLY to the cross-shore center band (`refine_source` records
which fired).

### Defect A.2 - the composer dispatches the worker build+solve

`model_flood_scenario(quadtree=True)` now, after the fetcher chain, stages the
fetched topobathy DEM + a `sfincs_build_spec` (design-storm forcing + the
`options.quadtree` refinement knobs) to the cache bucket and dispatches the
worker image build+solve via `run_solver(solver="sfincs-quadtree")` -- a new
local-docker `LocalSolverSpec` mirroring the geoclaw/swan `--network host`
self-S3 build+solve form (`--build-spec-uri`; the container reaches MinIO itself
and writes straight to `s3://<runs>/<run_id>/`). `build_sfincs_model` +
`run_solver(solver="sfincs")` are SKIPPED on the quadtree path (`model_setup`
stays None; the autoscale/telemetry that reads it no-ops). New module
`workflows/sfincs/flood/quadtree_dispatch.py` owns the compose+stage+spec+register
seam; `SOLVER_WORKFLOW_REGISTRY` gains `"sfincs-quadtree"`. The regular-grid path
is untouched (test-locked; `"sfincs"` keeps the pre-built-deck volume-mount spec).

The design-storm surge water-level boundary rides in via the deck builder's
return-period fallback (`forcing={"forcing_type":"waterlevel"}` +
`options.return_period_yr`); the fetched CO-OPS/GTSM surge auto-wire is SKIPPED on
the quadtree path (it does not ride into the cht_sfincs deck), so it is not
fetched-then-discarded. Granularity lever: the four `quadtree_*` knobs are
first-class composer + `sfincs_flood` parameters with labeled defaults (AUTO
mode); staging failures raise a typed `QuadtreeDispatchError` -> failed envelope,
never a silent regular-grid fallback.

### Defect B - stamp the true EPSG into the output sfincs_map.nc

Verified empirically in-image that SFINCS does NOT propagate the deck's crs to
`sfincs_map.nc` (the deck `sfincs.nc` carries `crs=32616`+WKT; the output carries
`EPSG='-'`/0). So the fix lands on the OUTPUT, the most upstream honest point at
which the artifact itself becomes correct: the worker entrypoint stamps the
deck's `grid_crs_epsg` into `sfincs_map.nc`'s `crs` variable (`epsg_code` attr,
cheap netCDF4 `r+`) AFTER the solve and BEFORE the postprocess reads it -- so both
the depth COG and the published native mesh `LayerURI` carry the real authid.
`_read_crs_from_dataset` already reads `epsg_code` first. `None` (regular grid /
legacy manifest) skips the stamp -> byte-identical.

## Evidence

- **In-image behaviour smoke** (rebuilt `trid3nt-local/sfincs:latest`, absolute
  `-f`/context, `docker history` GRACE-2 refs = 0): coast-following deck built on
  the real Mexico Beach topobathy -> `refine_source="shoreline_z0_contour"`,
  12400 cells, 4 levels `[50,100,200,400] m`, adjacent-size ratios `[2.0,2.0,2.0]`
  (2:1 balanced), `grid_crs_epsg=32616`.

- **Regular-grid flood canary (no regression):** `sfincs_flood` pluvial,
  Chattanooga TN, rp-100/1 h -> status=**ok**, `LayerURI` peak-depth COG published
  (`s3://trid3nt-runs/01KZFRNNZM9Y9ZZ2G5CZ9Y6G9E/.../flood_depth_peak.tif`), run
  prefix + 7 frames in MinIO. The regular path (`solver="sfincs"`) is unchanged.

- **Full PRODUCT-path quadtree live run:** `model_flood_scenario(bbox=Mexico
  Beach, quadtree=True, coastal=True, rp=100, 12 h, base=400 m, coast_level=3)`
  -> fetch_topobathy -> stage build_spec -> `run_solver(solver="sfincs-quadtree")`
  build+solve -> wait -> postprocess (run `01KZFRZTEK1Z53DWRXAQ7H4TPD`, ~360 s
  wall):
  - native `sfincs_map.nc`: face-indexed UGRID, **12019 faces**, quadtree=True,
    nodes `x=[640065..654865] y=[3286232..3303432]` (UTM 16N, inside the AOI
    bbox -- xarray coordinate check PASS).
  - depth: max 19.99 m, mean 12.29 m, p95 19.35 m, 176085 flooded cells,
    COG `crs=EPSG:32616` (consistent with the 0176 physics: max 20.0 / mean 12.3).
  - **Defect B GATE MET:** the published native mesh `LayerURI` carries
    `crs_authid="EPSG:32616"` (was 3857) -- `layer_type="mesh"`,
    `style_preset="mesh_grid"`, name "SFINCS quadtree mesh (12019 cells)".

- **Proofs regenerated from THIS run** (`docs/proof/templates/`):
  `sfincs_native_quadtree_mesh_mesh.png` -- the fine 50 m band now HUGS the
  curved z=0 shoreline (coast-following), coarse 400 m offshore, visible 2:1
  stepping; `sfincs_native_quadtree_mesh_depth.png` -- peak depth correctly
  georeferenced over Mexico Beach (EPSG:32616). The `_FIXTURE` files stay.

- **Showcase:** `scripts/seed_showcase_cases.py` sfincs quadtree entry refreshed
  (now honest: dispatch wired, coast-following, + granularity knobs). Verified
  `!run` line round-trips 2/2 via the product parser:
  `!run sfincs_flood(bbox=[-85.5522, 29.6983, -85.3976, 29.8517], quadtree=True,
  coastal=True, return_period_yr=100, duration_hr=12, compute_class='small',
  quadtree_base_resolution_m=400.0, quadtree_coast_refine_level=3,
  quadtree_max_refine_level=3)`

- **Offline** (repo root, `env -u TRID3NT_CACHE_BUCKET pytest -p no:cacheprovider
  --timeout=300 -q`): test_postprocess_flood_quadtree + test_sfincs_numerical_physics
  + test_sfincs_archetype_decks + test_mesh_layer + test_model_flood_scenario_surge_plumbing
  + test_spec_strict_fields + **test_sfincs_quadtree_dispatch (new)** +
  **test_coast_following (new)** + test_template_hygiene = **78 passed**. Registry
  pins (226 tools / 68 templates) untouched (no new LLM tool; `sfincs-quadtree` is
  a solver name, not a template).

## Consequences

- `sfincs_flood(quadtree=True)` is now genuine end-to-end through the product
  path -- the refinement hugs the real shoreline and the native mesh places
  correctly in QGIS without a manual CRS override (the ADR 0176 Finding B caveat
  is retired). MDAL visual stays NATE's step.
- The design-storm surge (not the fetched CO-OPS/GTSM surge) drives the quadtree
  boundary -- consistent with 0176; threading the fetched surge into the cht deck
  is a documented future refinement (would require staging the surge CSV to S3).
- Local-docker `--network host` build+solve: the supervisor's completion.json
  (status only) supersedes the worker's (no `publish_manifest_uri`), so the
  composer runs the on-box `postprocess_flood` -- which is exactly what emits the
  native mesh `LayerURI` (register-only path does not). Correct by construction
  for the mesh; the depth COG postprocess is repeated on-box (acceptable for the
  single-user local substrate).

## Files changed

- `services/workers/_sfincs_build/deck_quadtree.py` -- `_coast_refinement_geom`
  (z=0 shoreline band) + `coast_band_m` knob + MultiPolygon explode + loud
  center-band degrade + `refine_source`/`coast_band_m` provenance.
- `services/workers/sfincs/entrypoint.py` -- `_stamp_sfincs_map_crs` + thread
  `grid_crs_epsg` through `_solve_postprocess_sweep` (quadtree build path only).
- `server/src/trid3nt_server/agent/workflows/sfincs/flood/quadtree_dispatch.py`
  -- NEW: compose/stage build_spec + `sfincs-quadtree` LocalSolverSpec + registration.
- `server/src/trid3nt_server/agent/workflows/sfincs/flood/flood.py` -- quadtree
  branch (stage+dispatch instead of build_sfincs_model+run_solver), `quadtree_*`
  knobs on both signatures, surge auto-wire skip, `QuadtreeDispatchError` handler,
  `model_setup=None` guards.
- `server/src/trid3nt_server/agent/tools/simulation/solver/solver.py` --
  `SOLVER_WORKFLOW_REGISTRY["sfincs-quadtree"]`.
- `scripts/seed_showcase_cases.py` -- refreshed quadtree showcase entry + knobs.
- `server/tests/test_sfincs_quadtree_dispatch.py`,
  `services/workers/_sfincs_build/test_coast_following.py` -- NEW offline tests.
- `docs/proof/templates/sfincs_native_quadtree_mesh_{mesh,depth}.png` -- regenerated
  from the genuine product-path run (`_FIXTURE` files retained).
