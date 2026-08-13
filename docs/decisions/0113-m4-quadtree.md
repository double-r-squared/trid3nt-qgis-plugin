# ADR 0113 -- M4 QUADTREE TRUTH: the real SFINCS quadtree leg (cht_sfincs)

Status: accepted (2026-08-04)
Spec: docs/specs/mesh-layer-extraction.md (SIGNED, NATE 2026-08-03) -- the M4
QUADTREE TRUTH wave, signed as BUILD ("BUILD the quadtree leg for real atop the
mesh layer; the inert stub dies when the real leg replaces it").
Follows: ADR 0098 (M1 EXTRACT), 0099 (M2 GENERALIZE), 0100 (M3 HECRAS),
0101 (OCEANMESH wave), 0106 (structured provenance).

## Context -- the census truth being corrected

The mesh census (2026-08-03) found the SFINCS "quadtree+SnapWave" path was an
INERT STUB: `model_flood_scenario(quadtree=True)` fell through to the regular
grid; `build_sfincs_quadtree_deck` existed NOWHERE; only the read-side probe
(`postprocess_sfincs._is_quadtree_output` + `_read_face_coords`) was ever built;
and the flood.py docstrings described a phantom AWS-Batch "deck-builder job-def"
that surfaced a `DECK_BUILD_FAILED` envelope -- code that never existed (the
narrative was fiction). The flagship SFINCS validation case's mesh leg was 0%
implemented.

## The tooling decision (the load-bearing choice)

hydromt_sfincs CANNOT author a quadtree grid from scratch -- in EVERY released
version, including the newest:

- `SfincsModel.setup_grid` carries a literal `# TODO gdf_refinement for quadtree`
  and only writes a regular-grid config.
- `SfincsModel.setup_dep` raises `NotImplementedError("Create dep not yet
  implemented for quadtree grids.")`.
- The changelog is explicit: v1.2.0 (23-4-2025) "added reading/writing of
  quadtree models, but NOT building them from scratch (#226)"; v2.0.0-rc3
  (22-5-2026, the newest) still only improves quadtree roughness/infiltration
  READS. `quadtree.py`'s `QuadtreeGrid` reads/writes a quadtree netcdf but does
  not generate one from refinement polygons.

So an "extend the existing _sfincs_build worker's hydromt" path is impossible --
hydromt has no quadtree authoring at any version, and a version bump does not
change that. The ONLY tool that builds a SFINCS quadtree from scratch is Deltares'
**cht_sfincs** (the Coastal Hazards Toolkit / DelftDashboard engine the census's
old docstrings named). Pinned: `cht_sfincs==1.0.0`, GNU GPL v3.

GPL isolation: cht_sfincs is worker-side ONLY -- the always-on agent NEVER imports
it (same boundary hydromt_sfincs already lives behind in the `grace2-sfincs`
worker image). Because the local-docker deployment builds the REGULAR deck
agent-side via hydromt, the quadtree deck cannot be built in the agent process;
it is built inside the WORKER image (which carries cht_sfincs + the SFINCS binary)
run in its existing `--build-spec-uri` build+solve mode.

Dependency note: cht_sfincs 1.0.0 has module-level imports of an OLD cht_utils
API (`cht_utils.misc_tools` / `.pli_file`, both dropped in cht_utils 2.x), of
`cht_bathymetry` (a bathymetry DATABASE we do not use -- we sample our own
fetched topobathy), and of `datashader` (map overlays we never render). Rather
than pin the whole brittle cht dependency web, `deck_quadtree._ensure_cht_
importable()` injects lightweight `sys.modules` stand-ins for exactly those
unused module-level imports, so the image needs ONLY `cht_sfincs` + `tabulate`
(--no-deps) on top of the geo stack hydromt_sfincs already provides. A stubbed
callable raises loudly (`QUADTREE_UNSUPPORTED_OP`) rather than degrading
silently. The quadtree BUILD path (grid + mask + boundary + write) touches none
of the stubbed callables.

## What was BUILT

`services/workers/_sfincs_build/deck_quadtree.py` -- `build_sfincs_quadtree_deck
(spec, scratch, download)`, mirroring `deck.build_sfincs_deck`'s contract:

1. Localizes the topobathy DEM (dem_uri) + waterlevel forcing.
2. Builds a refined quadtree via cht_sfincs: base resolution + a coastal
   refinement band (fine at the shore, coarse offshore -- the SFINCS-native
   pattern) + any drawn `refine_region` polygons (the M2 role), all reprojected
   to the AOI's best UTM zone. The granularity lever stays the user's
   (`options.quadtree.base_resolution_m` + `coast_refine_level` +
   `refine_regions[].refinement_level`, capped at `max_refine_level`).
3. Samples per-face bed levels directly from OUR topobathy COG at the face
   centroids (rioxarray reproject + nearest) -- NO cht_bathymetry database; a
   missing-footprint face fills to the active-land ceiling (never a silent hole).
4. Builds the active mask; the seaward open-water-level boundary (msk==2) is
   placed on the domain edge whose adjacent cells have the LOWEST mean bed (the
   sea) -- auto-detected, no coastline lookup.
5. Wires the surge water-level timeseries (the fetched CSV from ForcingSpec.
   waterlevel, else a design triangular surge scaled by return_period_yr --
   provenance-stamped, never silently fabricated).
6. Writes `sfincs.inp` (qtrfile=sfincs.nc) + the quadtree `sfincs.nc` +
   `sfincs.bnd/.bzs` + a `mesh.geojson` preview (variable cells VISIBLE, per-face
   cell_size_m + refine_level).
7. Returns a provenance dict (nr_cells, nr_refinement_levels, refinement block,
   sea_boundary_edge, n_active/boundary_cells) folded into completion.json ->
   the 0106 synthetic_inputs channel.

Worker routing: `services/workers/sfincs/entrypoint.py` branches on
`spec.options.quadtree` -> `build_sfincs_quadtree_deck` vs `build_sfincs_deck`;
from the solve onward the tail is IDENTICAL (SFINCS consumes `qtrfile` natively;
the read-side postprocess is already face-aware). `spec.validate_job_spec` relaxed
so a quadtree spec needs only `dem_uri` (Manning constants, no landcover raster).
Dockerfile: cht_sfincs + tabulate + rioxarray + xugrid pinned --no-deps with a
self-contained import smoke.

flood.py: the phantom AWS-Batch `DECK_BUILD_FAILED` narrative is dead; the
`quadtree` docstring + inline comments rewritten to the real cht_sfincs
worker-image build+solve contract.

## The read-side probe verdict: WORKED (not bit-rotted)

The never-exercised read-side code was validated against a REAL quadtree solve
and works unchanged: `_is_quadtree_output` detects the `nmesh2d_face` UGRID
output (True), `_read_face_coords` reads the 5010/5610 per-face centroids,
`_select_peak_depth` prefers `hmax`, and `rasterize_face_field` grids the per-face
depth (nearest, no smoothing) in the FACE UTM CRS. No fixes were needed.

## Canary comparison (offline, deltares/sfincs-cpu:sfincs-v2.3.3)

The offline-first gate: the PRODUCTION `build_sfincs_quadtree_deck` on a synthetic
Mexico Beach topobathy (planar beach, EPSG:32616) -> 5610 cells, 3 levels
(200/100/50 m), sea edge auto-detected "south", 5575 active + 35 boundary cells
-> SFINCS v2.3.3 solved it in 0.9 s -> face-indexed `sfincs_map.nc` (5610 faces),
`_is_quadtree_output` True, peak depth 8.0 m over 2740 flooded faces. The
variable resolution is VISIBLE in the mesh preview (coarse offshore -> fine at
the coast); the depth is physically sane (deepest seaward, fading landward). The
quadtree leg is a strict opt-in: `quadtree=False` leaves the regular
`build_sfincs_model` path byte-identical.

## What is deferred (honest)

- SnapWave incident/infragravity wave coupling: the quadtree grid already carries
  the `snapwave_mask`; wiring the SnapWave boundary is a follow-up (SWAN already
  covers the spectral-wave comparison lane).
- Quadtree subgrid tables: cht_sfincs's `subgrid.build` requires the cht_bathymetry
  database; the M4 canary runs on per-face bed levels (dep), which SFINCS supports
  natively. Subgrid is an accuracy follow-up, not a correctness gate.
- The flood.py WS-driven live canary (full agent workflow) rides on building +
  tagging the `grace2-sfincs` worker image locally and a worker-image build+solve
  dispatch in the local-docker solver -- staged behind the worker-level canary
  above (which exercises the identical build+solve+postprocess chain with the
  production module).

## Consequences

The census truth-correction chain closes: the flagship SFINCS validation case's
mesh leg is real (not a stub), the phantom deckbuilder narrative is gone, and the
read-side probe is proven against real output. The mesh landscape stays
heterogeneous by construction (ADR 0098 principle) -- cht_sfincs is one more
worker-isolated generator, not a universal mesher.
