# ADR 0176 - SFINCS real quadtree run: cht_sfincs generator + native mesh, off the fixture

Date: 2026-08-07
Status: accepted

## Context

ADR 0159 landed the native quadtree mesh deliverable (a face-indexed UGRID
`sfincs_map.nc` published as a `layer_type="mesh"` `LayerURI`, the QGIS-native
variable-resolution grid). Its publish-side code was proven against a HAND-BUILT
UGRID fixture -- the cht_sfincs deck-builder worker image was absent locally, so
no genuine quadtree ever ran. NATE caught the fixture authorship (haphazard block
placement, not generator output) and the proofs were relabeled with a `_FIXTURE`
suffix. The cht_sfincs build lane itself was blocked (ADR 0158): a from-scratch
image build failed because `cht_sfincs==1.0.0`'s `grid_v2.py` imports
`matplotlib`, absent from the worker image pins.

This ADR delivers the genuine article: the image fix, one real quadtree
build+solve on a coastal US AOI, proofs regenerated from the genuine
`sfincs_map.nc`, and the showcase entry.

## Decision

### 1 - Image fix (matplotlib), behavior-proven

`services/workers/sfincs/Dockerfile`: `matplotlib>=3.6,<4` added to the
with-deps pip block (so its own deps resolve; `cht_sfincs` stays `--no-deps`).
The cht import smoke now `import matplotlib; matplotlib.use('Agg')` before
`from cht_sfincs import SFINCS` (which pulls `grid_v2`). Built with ABSOLUTE `-f`
+ context paths (the ADR 0158 context-drift law -- never a `cd &&` prefix on a
backgrounded build); `docker history` grepped clean of any `GRACE-2` residue.
Behavior proof beyond import: inside the image the cht_sfincs generator built a
2:1-balanced quadtree (526 cells, 4 distinct levels [1,2,3,4], intermediate
levels auto-inserted) -- the matplotlib fix unblocks the actual generator, not
just a bare import.

### 2 - Genuine quadtree build+solve (the worker `--build-spec-uri` path)

Ran the rebuilt `trid3nt-local/sfincs:latest` in build+solve mode against MinIO
(`scripts/run_sfincs_quadtree_direct.py`): a real Mexico Beach / Hurricane
Michael topobathy DEM (EPSG:32616, 3 m, 86% wet, z -24..+16 m -- the classic
lineage AOI), a design-storm surge water-level boundary (rp-100 triangular
fallback), `options.quadtree = {base_resolution_m: 400, coast_refine_level: 3,
max_refine_level: 3}`. The worker localizes the DEM, `build_sfincs_quadtree_deck`
authors the grid via cht_sfincs, the SFINCS binary solves it, and the face-aware
raster postprocess runs -- identical to the product's Batch build+solve path.

Result (`completion.json` status=**ok**, exit 0):
- deck: **42772 cells, 4 levels, sizes [50, 100, 200, 400] m**, active 28697,
  boundary 48, sea_edge=west.
- native `sfincs_map.nc`: face-indexed UGRID, **171088 nodes / 42772 faces**,
  `mesh2d_node_x/_y` + `mesh2d_face_nodes` (1-based). `_is_quadtree_output` True.
- 2:1 balance verified QUANTITATIVELY from the output faces: distinct cell sizes
  50/100/200/400 m with adjacent-size ratios **[2.0, 2.0, 2.0]** -- perfect 2:1
  nesting (41440 finest 50 m in the refined band, 296/148 in the 100/200 m
  transition rings the balance auto-inserts, 888 coarse 400 m).
- depth: max 20.0 m, mean 12.3 m, p95 19.4 m, 173396 flooded cells.
- native mesh `LayerURI` (ADR 0159 `_maybe_native_mesh_layer`):
  `layer_type="mesh"`, `uri=s3://trid3nt-runs/<run>/sfincs_map.nc`,
  `style_preset="mesh_grid"`, `role="context"`, name "SFINCS quadtree mesh
  (42772 cells)". GATE met.

### 3 - Proofs (regenerated from the genuine output)

`docs/proof/templates/`:
- `sfincs_native_quadtree_mesh_mesh.png` -- the RAW cht_sfincs grid over Esri
  World Imagery: coarse 400 m offshore, dense 50 m coastal band, visible 2:1
  stepping, one colour, density = refinement. Correctly georeferenced over
  Mexico Beach (mesh nodes reprojected from the deck's true EPSG:32616).
- `sfincs_native_quadtree_mesh_depth.png` -- the rasterized peak depth from the
  same run, over Esri.
The `_FIXTURE`-suffixed files are KEPT (honest history).

### 4 - Showcase entry

Added a `sfincs_flood` quadtree Showcase to `scripts/seed_showcase_cases.py`
(Mexico Beach, `quadtree=True, coastal=True, rp=100, 12 h`). The reconstructed
`!run` line round-trips through the product parser (2/2):
`!run sfincs_flood(bbox=[-85.5522, 29.6983, -85.3976, 29.8517], quadtree=True,
coastal=True, return_period_yr=100, duration_hr=12, compute_class='small')`.
A LIVE daemon seed is deferred pending Finding A below (a live seed today would
build a regular-grid coastal deck, not a quadtree -- recording that as a
"quadtree" Case would misrepresent it).

## Findings the genuine run exposed (surfaced, not reactively patched)

- **A - the composer-side quadtree dispatch is not wired.**
  `sfincs_flood(quadtree=True)` / `model_flood_scenario` only feeds
  `is_coastal` (routing the DEM to `fetch_topobathy` + auto-surge). It then
  calls `build_sfincs_model` (regular-grid hydromt, no `quadtree` parameter) and
  `run_solver` on that regular deck. It does NOT compose a `sfincs_build_spec`
  nor dispatch the worker's `--build-spec-uri` build+solve mode -- despite the
  flood.py / ADR 0113 docstrings describing exactly that composition. The genuine
  quadtree path exists ONLY in the worker image today; the local-docker
  dispatch (`solver.py::_sfincs_local_spec`) runs a pre-built deck and has no
  build+solve variant. Fix candidate: a build+solve local-docker dispatch
  (stage DEM+spec to cache, run the image with `--build-spec-uri`, wait) gated
  on `quadtree`. Sizeable, hot-path change -- a follow-on job, not a reactive
  edit.

- **B - native mesh `crs_authid` mislabels a UTM grid as EPSG:3857.**
  The real cht_sfincs `sfincs_map.nc` carries a `crs` variable with `EPSG='-'`
  (placeholder, value 0) -- cht_sfincs does not stamp the actual code even though
  the deck was built `crs=32616`. `_read_crs_from_dataset` therefore cannot parse
  it and `_maybe_native_mesh_layer` falls back to `crs_authid="EPSG:3857"`, while
  the mesh nodes are UTM 16N metres (640065..654865 E, 3286232..3303432 N). The
  ADR 0159 FIXTURE hid this -- it carried `crs = <EPSG int>`. Consequence: QGIS
  would place the MDAL mesh at the wrong location (the plugin sets the mesh CRS
  from this field). Fix candidate: stamp the real EPSG into the deck's grid `crs`
  var in `deck_quadtree.py` after `sf.grid.write()` (verify SFINCS propagates it
  to `sfincs_map.nc`), OR thread the completion `deck.grid_crs_epsg` through
  `postprocess_flood` into the helper. Until fixed, NATE's MDAL visual must set
  the layer CRS to EPSG:32616 manually. Both findings held for NATE per the
  accumulate-and-wait-for-go norm.

## Consequences

- The matplotlib fix makes the cht_sfincs quadtree build lane buildable +
  runnable locally; the worker image now carries a working generator.
- The `_FIXTURE` proofs are superseded by genuine renders; the fixture files
  stay as honest provenance.
- No server/agent code changed (image + two scripts only), so the offline suite
  and the ADR 0159 publish path are untouched.

## Evidence

- Image: build EXIT=0; `docker history` GRACE-2 refs = 0; in-image behavior smoke
  built a 2:1-balanced 526-cell quadtree.
- Run: `completion.json` status=ok exit=0; UGRID 171088 nodes / 42772 faces;
  cell-size ratios [2.0, 2.0, 2.0]; depth max 20.0 / mean 12.3 / p95 19.4 m,
  173396 flooded; native mesh LayerURI emitted.
- MDAL not in the venv (`import qgis` / `import mdal` fail) -> UGRID validated via
  xarray (structure + 2:1 balance above); the MDAL/QGIS native-mesh VISUAL is
  NATE's step (note Finding B's manual-CRS caveat).
- Offline (repo root, `env -u TRID3NT_CACHE_BUCKET pytest -p no:cacheprovider
  --timeout=300 -q`): `test_postprocess_flood_quadtree.py`,
  `test_sfincs_numerical_physics.py`, `test_sfincs_archetype_decks.py`,
  `_sfincs_build/test_spec_strict_fields.py` = 42 passed.
- Showcase `!run` line: 2/2 round-trip via `parse_run_invocation`.

## Files changed

- `services/workers/sfincs/Dockerfile` -- matplotlib pin + grid_v2/matplotlib
  build smoke.
- `scripts/run_sfincs_quadtree_direct.py` -- new genuine build+solve driver
  (stage -> worker `--build-spec-uri` -> readback -> native mesh + depth ->
  Esri proofs).
- `scripts/seed_showcase_cases.py` -- `_MEXBEACH` + the quadtree Showcase entry.
- `docs/proof/templates/sfincs_native_quadtree_mesh_{mesh,depth}.png` -- genuine
  renders (the `_FIXTURE` files retained).
