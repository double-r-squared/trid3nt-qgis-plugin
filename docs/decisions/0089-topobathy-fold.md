# ADR 0089 -- Topobathy fold wave: fetch_topobathy STOP re-affirmed and SHARPENED (the envelope-provenance gap is decisive)

Status: accepted (2026-08-03)
Supersedes: the ADR 0086 fetch_topobathy "STOP re-affirmed (genuinely needs
more)" row -- restated here against the CURRENT router surface at HEAD a0dfed5
with one decisive new finding that makes the STOP a hard gate failure, not a
judgement call.

## Context

fetch_topobathy is the last flood-adjacent coded data-fetcher in the
universal-ingest fold campaign (campaign coded-data-fetcher counter at 9; target
9 -> 8). This wave read the twin in full again, re-audited it against the router
surface AS IT STANDS after the two intervening raster folds (jrc + soilgrids,
ADR 0086; the animation folds, ADR 0087/0088 -- none of which added any
multi-source / composite / route-recursion machinery), and asked the sharper
question the task posed: can the composite be expressed as a bounded
composite/precedence mode OR a resolve-phase / delegate hook chain WITHOUT a
sprawling one-source mode that violates the ADR 0056 generality bar?

Answer: no. And the reason is now crisper than "it needs more machinery" -- a
fold cannot pass its OWN first acceptance gate (edge-matrix parity vs the twin,
`TopobathyResult` field-for-field), because the router has no channel to carry
fetch-time provenance into the emitted envelope.

## Decision -- fetch_topobathy STOP (re-affirmed, sharpened residual)

The four new-machinery gaps ADR 0086 named all still hold at a0dfed5 (verified
by re-reading `spec.py`, `router.py::route`, `executors/raster_cog.py`,
`executors/library_delegate.py`, and `contracts/execution.py::LAYER_RESULT_MODELS`):

1. FOUR distinct discovery legs with NO declarative or single-mode expression:
   CUDEM `urllist8483.txt` tile-index intersect (filename-encoded NW corners);
   ETOPO 2022 15-degree global-block filename formula; the
   `NCEI_REGIONAL_COASTAL_DEMS` STAC ItemCollection per-tile-bbox intersect; and
   3DEP land via a NESTED `fetch_dem(...)` CALL inside the fetcher body. There is
   still no `fetch_dem` SPEC (fetch_dem remains a coded twin -- `flood.py` and
   `fetch_topobathy` both import it directly), so the task's "delegate hook
   calling `route()` recursively on the dem spec" is not even available: the
   topo leg must import the coded twin, exactly as today, yielding no cleanliness
   gain. The raster executor dispatches ONE `ingest.access` mode to ONE
   `(array, transform, crs)`; it composes no nested fetcher tool.

2. A finest-resolution UTM target grid computed ACROSS heterogeneous sources
   (`_compute_target_grid`: per-source native-pixel span reprojected into UTM
   metres, min-across-sources, `min_pixel_m` floor, `_MAX_DIM` coarsen) plus a
   precedence per-source warp merge (`_merge_sources_rasterio`: reproject EACH
   source from its OWN CRS -- CUDEM EPSG:4269 / 3DEP EPSG:5070 / ETOPO EPSG:9518
   / CoNED EPSG:3717 -- onto the shared grid, then LAST-wins composite). No
   router access mode is a multi-heterogeneous-source precedence compositor;
   every existing raster mode (opendap / direct_window / multi_url /
   projected_vrt_window / stac_* / fixed_tile_grid / library_delegate) yields a
   SINGLE array from a SINGLE-CRS source or a same-CRS mosaic.

3. A per-tile vertical-datum NAVD88 gate (`_assert_navd88` reads each tile's CRS
   WKT + GDAL tags over `/vsicurl/` -- network I/O per tile --, then
   `_classify_vertical_datum` decides accept / apply-offset / raise
   `TopobathyDatumError`). A router hook "only computes, performs no I/O" (ADR
   0056); this gate is I/O-per-tile inside the discovery loop, so it is not a
   pure hook, and there is no declarative datum-gate surface.

4. A `TopobathyResult(LayerURI)` subclass carrying `bathymetry_present` /
   `fallback_warning` / `cudem_tile_count` / `regional_tile_count`, not
   registered in `LAYER_RESULT_MODELS`.

### The decisive new finding: the envelope-provenance gap

Gap 4 is not merely "register a subclass" (that part is cheap, like
FaultSourcesResult / LandcoverResult). The blocker is that the router CANNOT
POPULATE those four fields, so a fold cannot reproduce the twin's output
field-for-field -- it fails GATE 1 (edge-matrix parity) before any live drive.

Why: the fields are FETCH-TIME PROVENANCE -- they record which of the four
heterogeneous sources actually painted the merge (did any CUDEM tile contribute?
how many? did the run degrade to the ETOPO global fallback? to land-only?). They
are NOT recoverable from the final single-band float32 COG: a merged topo-bathy
COG carries no per-source-tile-count attribution.

The twin threads this state through a closure `_flags` dict that its `_fetch()`
mutates during the merge, then reads when constructing `TopobathyResult`. The
router's only post-serialize seam is the envelope hook, and its signature is
`_apply_envelope(spec, params, layer, result.data)` -- PURE over the FINAL bytes,
with `result.data` the produced COG. Worse, on a cache hit `route()` never calls
`fetch_fn` at all (the executor that would compute provenance does not run). So:

- there is no fetch_fn -> envelope provenance channel in `route()`;
- the envelope hook, by contract, does no I/O and only reads the final bytes,
  from which the source attribution is unrecoverable;
- a cache hit bypasses the fetch entirely, so even an impure envelope hook could
  not reconstruct provenance on the hot path.

Carrying provenance would require a NEW cross-cutting seam -- a "fetch returns
bytes PLUS a provenance sidecar" contract touching `read_through`, `route`, and
the envelope builder, persisted so a cache hit can replay it. That is a
general-purpose router capability, not a bounded raster-mode extension, and it
is speculative infra with exactly one consumer -- the precise shape the ADR 0056
generality bar and the simplicity-over-completeness doctrine tell us NOT to build
on a single source's behalf.

### Why not force it through a delegate hook

The one shape that could "type-check" -- `ingest.access: library_delegate` +
`hooks.delegate: topobathy.build_merged` returning `(array, transform, crs)` --
is a forced fold, not a clean one, and is rejected:

- The delegate hook exists for a source whose MAINTAINED EXTERNAL LIBRARY owns
  discovery + the socket (pfdf, HRRR-Zarr). Topobathy has no such library; the
  4-leg discovery + heterogeneous warp merge + datum gate IS our bespoke code.
  Declaring it a "delegate" moves ~600 LOC of bespoke orchestration verbatim
  into a hook module -- relabeling, not folding. LOC delta ~0; the coded-tool
  count decrements only by moving a file and adding a `source.yaml`, a hollow
  metric win the clean-as-you-go doctrine warns against.
- Even so, it STILL fails GATE 1: the delegate returns only `(array, transform,
  crs)`; the four `TopobathyResult` fields remain unreachable (the envelope-
  provenance gap above). A fold that cannot reproduce the twin's output shape is
  not a fold.

### The DO-NOT-REGRESS flood leg (unchanged, no canary this wave)

`flood.py` imports `fetch_topobathy` + `TopobathyError` directly (the coastal
branch of `model_flood_scenario`, reading `bathymetry_present` /
`cudem_tile_count` / `fallback_warning` off the result). Because the twin was NOT
folded, that import and the coastal seam are UNCHANGED -- no flood-consumer
re-point occurred, so no FLOOD CANARY was mandated or run by this wave (the same
disposition ADR 0086 recorded). The canary is mandatory the day topobathy folds,
never for a STOP that leaves the seam byte-identical.

## Consequences

- No code change. `fetch_topobathy` twin untouched; router / spec / contracts
  untouched. Campaign coded-data-fetcher counter UNCHANGED at 9 (target 9 -> 8
  NOT met -- honestly deferred). Registry, n_specs, coded-tool count all
  unchanged.
- The offline baseline is unchanged by construction (docs-only wave): EXACTLY 9
  failures (test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5) from
  the repo root. The two `test_fetch_resolution_gate` members parametrized
  `[fetch_topobathy-topobathy]` and `[fetch_dem-dem]` are pre-existing baseline
  members whose failure mode is untouched (no topobathy or router code changed).
- The DELETION_LEDGER `fetch_topobathy fold` row is refined (STOP re-affirmed,
  ADR 0089) with the envelope-provenance gap added to the unblock list.
- Unblock condition (unchanged in spirit, sharpened): a `utm_multi_source_composite`
  access mode + a 4-leg source-discovery surface + a nested-tool-composition
  primitive (or a `fetch_dem` spec to `route()`-compose) + a datum-classification
  step + the `TopobathyResult` envelope AND a fetch-time provenance channel
  (bytes + sidecar, cache-replayable) + the flood.py re-point + FLOOD CANARY.
  This is a scoped multi-source-composite job, not a fold wave.

An honest STOP with a named, gate-level residual beats a forced fold that cannot
reproduce the twin's output.
