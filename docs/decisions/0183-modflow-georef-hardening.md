# ADR 0183 - MODFLOW postprocess georef hardening: identity-affine fallback removed

Date: 2026-08-07
Status: accepted

## Context

ADR 0180 (layer-emission audit) flagged one real error path adjacent to its
fixture-orphan mission: `postprocess_modflow._write_reprojected_cog` silently
fell back to `rasterio.Affine.identity()` (null island) when
`_grid_georegistration_from_deck` returned `None` on a flopy deck-load
failure, instead of raising. This is the class NATE bans: a misplaced map
layer instead of an honest error. The same module's `modflow_mesh` path was
already inconsistent with it -- `emit_modflow_mesh_artifact` SKIPS (returns
`None`, logs a warning) on `geo is None` rather than emitting a mis-placed
mesh.

A second, identical fallback existed in the same module:
`_modflow_src_transform` (feeding `publish_modflow_quantities`'s
concentration-animation + water-table rasters, both `default_on=True` active
map layers in the output-quantities registry) had the same
`rasterio.Affine.identity()` degrade.

## Finding: two writer functions, ten-plus callers, two honest behaviors needed

Both `_write_reprojected_cog` and `_modflow_src_transform` are shared across
every raster-emitting MODFLOW postprocess entrypoint. Auditing each caller's
actual deliverable split them into two classes:

**RASTER-IS-THE-DELIVERABLE** (the raster's spatial pattern IS the finding;
an unplaced raster is a confidently-wrong map layer, strictly worse than an
honest failure): `postprocess_modflow` (plume concentration),
`postprocess_multi_species` (N per-species plumes), `postprocess_river_seepage`
(diverging gaining/losing reach), `postprocess_drawdown` (cone of depression),
`postprocess_mounding` (mound rise), `postprocess_subsidence` (CSUB bowl),
`postprocess_dewatering` (DRN-rate raster), `postprocess_wetland_hydroperiod`
(seasonal head-range raster), and `publish_modflow_quantities`'s two
`_modflow_src_transform` call sites (concentration-ts animation frames +
water-table). **Decision: loud typed error** -- `_write_reprojected_cog` /
`_modflow_src_transform` now raise `PostprocessMODFLOWError(
"MODFLOW_GEOREGISTRATION_MISSING")` and propagate; no COG is ever written or
uploaded.

**SCALAR-IS-THE-DELIVERABLE** (the narrated numbers are computed independent
of `geo`; the raster is a spatial-context convenience for them):
`postprocess_budget_partition` (deliverable = the budget-partition dict; head
COG is "the spatial carrier the partition summarizes" per its own docstring)
and `postprocess_asr` (deliverable = `recovery_efficiency` +
`head_timeseries`; head COG is "the spatial carrier" per its own docstring).
`postprocess_budget_partition` already wrapped its COG step in
`try/except PostprocessMODFLOWError` and degraded to an unplaced fallback URI
(`final_uri = run_outputs_uri`, `bbox=None`, warning logged) -- this is
EXACTLY the `modflow_mesh` skip convention, already correct; the centralized
raise slots into that existing except clause with no caller-side change.
`postprocess_asr` had no such wrapper (a bare call that would have hard-failed
a perfectly good sawtooth/efficiency result on a COG-only failure); it now
gets the identical wrapper, mirroring `postprocess_budget_partition` line for
line. **Decision: loud skip** -- the narrated scalars survive; the layer
degrades honestly (unplaced URI, no bbox, warned).

`postprocess_capture_zone` / `postprocess_stream_reaches` /
`postprocess_saltwater_intrusion` do not use either writer (they derive
georeferencing their own way and already carry their own honesty guards --
e.g. `postprocess_capture_zone` already refuses to emit a polygon at the
equator when the true UTM offset is unavailable); unchanged, out of scope.

## Decision

- `_write_reprojected_cog(final2d, model_crs, geo, ...)`: `geo is None` now
  raises `PostprocessMODFLOWError("MODFLOW_GEOREGISTRATION_MISSING",
  details={"model_crs": ...})` instead of building an identity transform. The
  `rasterio.Affine.identity()` branch is deleted.
- `_modflow_src_transform(geo, nrow)`: same raise, same deleted branch.
- `postprocess_asr`: the head-COG write/upload/publish block is now wrapped in
  `try/except PostprocessMODFLOWError`, degrading to `final_uri =
  run_outputs_uri` / `bbox=None` on a caught georegistration failure (mirrors
  `postprocess_budget_partition`).
- `_grid_georegistration_from_deck`'s docstring updated to describe the new
  contract (no more "the caller falls back to identity").
- New tests (`server/tests/test_modflow_georef_hardening.py`, 11 cases):
  structural guards that the identity-affine fallback is gone from both
  writers' source; unit tests that both writers raise
  `MODFLOW_GEOREGISTRATION_MISSING` on `geo=None` and are byte-identical on
  the happy path; a simulated flopy deck-load-failure fixture
  (`_grid_georegistration_from_deck` stubbed to return `None`, exactly what
  the real function does on any load exception) driven end-to-end through
  `postprocess_modflow`, `postprocess_multi_species`, and `postprocess_drawdown`
  (covering both the GWT-plume and GWF-only archetype families) proving the
  typed error propagates and no COG is ever uploaded; and end-to-end proof
  that `postprocess_budget_partition` / `postprocess_asr` degrade gracefully
  (scalar deliverable intact, unplaced fallback URI, no bbox).

## Live evidence (MODFLOW engine canary)

Ran the repo's existing direct-call driver (`scripts/run_modflow_direct.py`,
sustainable_yield archetype -> `postprocess_drawdown`, unmodified) against a
real local mf6 binary and the shared MinIO backend (full `.env.local` AWS/S3
env block):

```
deck built: archetype=sustainable_yield transient=True gwt_present=False crs=EPSG:32611
mf6 exit=0 converged=True
postprocess_drawdown run_id=fresno-sy-n8z9l7r8 max_drawdown_m=6.13371 steps_ts=41
uploaded plume COG to s3://trid3nt-runs/fresno-sy-n8z9l7r8/drawdown_4326.tif (boto3)
DrawdownLayerURI bbox=(-119.78411428147353, 36.737664378144686, -119.76111389156732, 36.75606469006966)
```

Fetched the uploaded object back from MinIO directly (boto3 + rasterio,
bypassing the driver script's own unrelated re-upload step, which assumes a
local `file://` URI and is a pre-existing bug outside this ADR's scope):

```
CRS: EPSG:4326
bounds: BoundingBox(left=-119.78411428147353, bottom=36.737664378144686,
                     right=-119.76111389156732, top=36.75606469006966)
shape: (36, 45); 1546 finite cells, min=0.0, max=5.165 m
```

The bbox is centered on the well location (Fresno CA, 36.7468 N / -119.7726
W) -- a correctly-placed, non-null-island COG confirming the happy path
(real `deck_dir` -> real georegistration -> real CRS/bounds) is unaffected by
the hardening.

## Consequence

- A flopy deck-load failure on any RASTER-IS-THE-DELIVERABLE MODFLOW template
  now fails loud and typed (`MODFLOW_GEOREGISTRATION_MISSING`) instead of
  silently publishing a null-island raster -- closing the ADR 0180 finding.
- `postprocess_budget_partition` / `postprocess_asr` keep working (scalar
  deliverable intact) on the same failure, now BOTH following the
  `modflow_mesh` loud-skip convention consistently -- the module-level
  inconsistency ADR 0180 flagged is resolved for every caller, not just the
  mesh.
- Offline suite (`test_modflow_georef_hardening.py` + every modflow
  postprocess/template test + `test_template_hygiene.py`): 167 passed, 1
  pre-existing unrelated skip.
