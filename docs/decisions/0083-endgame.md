# ADR 0083 -- Endgame sweep: HRRR-Zarr + FTW GeoParquet folds; population/nwis/buildings STOPs

Status: accepted (2026-08-02)
Supersedes: the ADR 0076 HRRR "live-parity finish" QUEUED row; the ADR 0070
fetch_field_boundaries "new pushdown transport" STOP.

## Context

The endgame sweep of the remaining non-animation residuals per the deletion
ledger. Six sources examined against the mature mechanism inventory (record
shape, library_delegate + delegate_resolve, pre_resolve, constant_cache,
variant_by_emptiness, the vector/raster delegate paths). Two folded with live
parity; four STOP with a named missing mechanism (one is a NATE flag-not-copy
decision, not a mechanism gap).

## Decisions

### 1. fetch_hrrr_forecast + fetch_hrrr_smoke -- FOLDED (library_delegate + delegate_resolve)

The mechanism was COMPLETE since ADR 0076 (delegate_resolve BUILT); the fold was
deferred ONLY on live-parity provability (no offline zarr fixture). Folded now
WITH a live drive as the parity gate. ONE shared hook module `hooks/hrrr.py`
serves both twins (identical Zarr body); the per-source difference (the variable
-> level/s3_var table, the forecast-only derived `10m_wind_speed`, the smoke-only
`-9999.0` fill mask) is declared in `ingest.hrrr`. The HRRR-grid physical facts
(LCC proj4, CONUS envelope, 18/48 h horizons, 6 h backstop) stay module constants.

- `hrrr.resolve_cycle` (delegate_resolve): the s3fs `fs.exists` backward cycle
  walk, socketed pre-cache-key; returns `{cycle_date, cycle_hour}` merged into
  params so the resolved cycle enters the cache key. Backstop exhaustion -> a
  retryable `<PREFIX>_NOT_AVAILABLE` (the hook owns the taxonomy).
- `hrrr.read` (delegate): opens the outer+inner Zarr groups, merges, picks the
  forecast lead index, masks the fill value, LCC->EPSG:4326 rioxarray reproject +
  clip, and (forecast `10m_wind_speed`) synthesizes `hypot(u, v)` -- returns
  `(array_2d_float32, affine, EPSG:4326)` for the shared COG writer.
- `hrrr.validate` (delegate_validate): the twin CONUS gate + the forecast_hour
  vs cycle-horizon ceiling (both typed INPUT, pre-cache).

LIVE PROOF (twin-vs-router value parity, same pinned cycle, direct-compute both
paths): cycle 2026-08-02T05:00Z verified published on S3; VALUE-IDENTICAL on all
5 variables (2m_temperature, surface_precip_1hr, derived 10m_wind_speed hypot,
near_surface_smoke fill-masked, aerosol_optical_depth) + both delegate_validate
error edges (non-CONUS, forecast_hour>horizon). HRRR feeds NO flood seam (grep) ->
no canary owed. Value coverage: test_router_hrrr.py (16). Retrieval unshifted
(forecast 8/8, smoke 7/7 top-8). Docstrings verbatim (5773 / 6079 chars).
Divergences (non-gating): the `cycle` kwarg enters the cache key when pinned (the
default cycle=None path is stable); synthesized layer_id/name; NOT_AVAILABLE code
via a hook-owned RouterError.

### 2. fetch_field_boundaries -- FOLDED (VECTOR library_delegate; ADR 0070 STOP refuted)

The ADR 0070 STOP ("needs a new GeoParquet row-group pushdown TRANSPORT") is
REFUTED by the library_delegate mode (BUILT in ADR 0074, after the 0070 stop):
the GeoParquet 1.1 CRS-aware row-group bbox pushdown lives INSIDE
`geopandas.read_parquet(fh, bbox=...)` over an fsspec HTTPS handle -- the parquet
reader issues its own HTTP range requests. That is a maintained LIBRARY owning
discovery + the socket (the pfdf / HRRR-Zarr pattern), so the pushdown does NOT
need to become a router transport. The noaa_sst refutation pattern.

- `field_boundaries.select` (pre_resolve, PURE): bbox -> FTW/fiboa dataset-key
  selection from the declared `ingest.field_boundaries.datasets` table (or an
  explicit `dataset`), merged into params (enters the cache key). Twin
  `FIELDS_NO_COVERAGE` / `FIELDS_INPUT_INVALID` pre-cache, byte-identical.
- `field_boundaries.read` (delegate, VECTOR): the CRS-aware pushdown read (reproject
  the query bbox into the file CRS -- USDA CSB is NAD83/Albers -- prune row groups,
  clip, cap, normalize the crop label) -> WGS84 GeoJSON polygon features for the
  shared `vector_fgb` serializer.

LIVE PROOF (Source Cooperative GeoParquet, twin-GDF vs router select+read+serialize):
select PASS (auto-picks us_usda_cropland), empty-AOI + both error edges (no-coverage,
unknown-key) value-identical; non-empty pushdown read parity on a rural Story County
IA cropland AOI (feature count + total area + crop_name set). Only consumer was the
tools/__init__ import (name-preserving; re-pointed). Value coverage:
test_router_field_boundaries.py (8). Retrieval unshifted (8/8 top-8). Docstring
verbatim (3145 chars). Divergences (non-gating): twin had no payload estimator -> a
tiny per_feature estimate (never warns); GDF -> GeoJSON -> FGB round-trip (geometrically
identical); synthesized layer_id/name.

### 3. fetch_population -- STOP (NATE flag-not-copy + a new raster mode)

Returns a raster (`.tif`, layer_type=raster, WorldPop) OR a vector (`.json`,
layer_type=vector, ACS) selected at RUNTIME by the `dataset` param prefix -- an
output-SHAPE-and-ext switch by a param value that `select_executor` cannot express
(no variant-by-param-output-shape mechanism). The ACS leg is HALF-BUILT
(geometry=None "follow-up", heuristic 15-state FIPS + 9-country ISO3 tables). Even
the WorldPop-only leg needs a new whole-object-GeoTIFF-download-then-window raster
access mode (WorldPop serves HTTP 200 for range requests -> `direct_window` /vsicurl
cannot windowed-read; the twin downloads the whole ~50 MB country file then windows).
Consumer compute_exposure_summary imports it directly (re-point on fold). UNBLOCK:
a NATE flag-not-copy call to drop the half-built ACS leg (a behavior change, the
river-NHDPlus precedent) + a whole-object-download-then-window raster mode; OR a
variant-by-param output-shape mechanism to keep both legs.

### 4. fetch_usgs_nwis_gauges -- STOP (output-shape switch + cross-parser fallback; flood-canary-gated)

Always a LayerURI(vector), but the FGB PROPERTY SCHEMA switches at runtime by
window-presence (`_resolve_window`): instantaneous per-station scalar
(`_build_flatgeobuf`) vs a full hydrograph `time_series_csv` + summary stats
(`_build_window_flatgeobuf`) -- the excluded output-shape-switch-by-param-value.
PLUS a two-tier cross-endpoint/cross-parser fallback (IV WaterML-JSON empty ->
Site RDB) that `endpoint_fallback` (same-request mirror chain) does not express.
FEEDS the flood seam: sfincs_forcing_autowire.py imports + calls it directly in
HYDROGRAPH mode (line 1051/1054) -> a fold MANDATES the flood canary. Left UNTOUCHED
(seam not re-pointed -> no canary owed). UNBLOCK: a derived-output-shape selector +
a parse-fallback chain + the flood-leg re-point + mandatory canary.

### 5. fetch_buildings -- STOP (S3 sidecar-WRITE seam)

Overpass-primary polygon source (foldable via the overpass mode + a heavy polygon
`parse_response`), BUT the blocker is a click-to-enrich TAGS SIDECAR: an explicit
`boto3.client("s3").put_object(...)` of a `.tags.json` object keyed off the same
cache key as the `.fgb`, INSIDE the fetch, consumed cross-module by
tool_catalog_http.py `/api/building-detail` (which imports `BUILDINGS_TAGS_SIDECAR_EXT`
+ `_FETCH_BUILDINGS_METADATA` to re-derive the sidecar key). The router is
read-through-only; no sidecar-write seam. The dead msft/abfs GeoParquet leg stays
flag-not-copy. Consumers (compute_exposure_summary, sfincs autowire, swmm model) call
`fetch_buildings(bbox)` by name (foldable). UNBLOCK: a declarative sidecar-write
executor extension (constrained like the delegate) + the /api/building-detail re-point.

### 6. fetch_lehd_jobs -- CHARACTERIZED FOLD-READY (deferred; join-values path)

TIGERweb tract GeoJSON (paged, `next_page`) LEFT-JOIN LODES8 WAC gzip-CSV values on
11-digit GEOID (`gzip_object` per discovered state) -> tract choropleth FGB. Always a
LayerURI(vector); no shape switch. The per-state LODES fan-out (states discovered from
step-1 geometry) fits `enrich_plan`/`enrich_merge`; the GEOID choropleth join fits the
`join` transform SHAPE, but `join.py`'s VALUES leg is hardcoded to the census Data-API,
not a gzip-CSV -- so it needs a join VALUES-hook seam (the storm_events gzip-CSV
precedent) OR the enrich route wired. No NAMED mechanism strictly blocks it, but the
values-leg composition is unbuilt + needs a live drive. Only consumer: tools/__init__
import. Deferred (the one remaining tractable fold outside animation/composite/nwis).

## Consequences

- Coded fetchers: 25 -> 22 (fetch_hrrr_forecast, fetch_hrrr_smoke,
  fetch_field_boundaries deleted). n_specs 73 -> 76; registry 190 unchanged (folds
  are name-preserving). The <= 20 merge trigger is NOT crossed (22 > 20): the four
  remaining folds each need a new router mechanism (nwis output-switch, buildings
  sidecar-write, population whole-object-window, lehd join-values), a NATE
  flag-not-copy decision (population ACS leg), or a flood canary (nwis).
- Offline baseline UNCHANGED: exactly 9 failures (test_fetch_resolution_gate x4 +
  test_run_river_dye_scenario x5) from the repo root.
- Two new hook modules (hrrr, field_boundaries), no new router mechanism -- both
  folds ride the EXISTING library_delegate raster/vector paths + delegate_resolve +
  pre_resolve. Strict no-op for all 73 priors.
