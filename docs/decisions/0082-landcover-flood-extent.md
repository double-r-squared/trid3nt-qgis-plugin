# 0082 - Landcover + flood-extent: the last two ADR-0077 finishers folded

Context: ADR 0077 STOP-RULEd three per-source residuals as wave-sized new-router-
mechanism builds; ADR 0081 folded the first (fetch_fault_sources) and re-confirmed
the other two. This wave builds the mechanisms those two named and FOLDS both:
fetch_landcover (a WCS GetCoverage access mode + a paletted-COG background remap + a
dict sidecar + an auto-coarsen, flood-canary-gated because it touches the SFINCS seam)
and fetch_flood_extent_observation (a categorical first-valid tiled-mosaic mode + a
LANCE dir-walk date resolve, docstring-coupled V&V consumer so non-fold-breaking).

Decision (2026-08-01):

## BUILT: three minimal, opt-in-no-op router mechanisms + two result models

### 1. `hooks.pre_resolve` -- a generic pre-cache-key resolve (router)

`(spec, params) -> dict`, run in `route()` AFTER type/gate validation and BEFORE
`read_through`, its return MERGED into params so a resolved value enters the cache
key. The keyless-HTTP sibling of the socket `delegate_resolve` (ADR 0076) and the
single-round `resolve_build`/`resolve_parse` (ADR 0063): the multi-step case neither
expresses (the LANCE MCDWD year->doy dir-walk; landcover's PURE dataset-alias +
vintage + auto-coarsen derivation). Validated as a resolvable hook at load. STRICT
no-op for every prior spec (none declare it).

### 2. `access: categorical_tile_grid` -- categorical first-valid tiled mosaic (raster_cog)

A global CATEGORICAL raster cut into a fixed h/v degree grid of per-tile direct-GET
GeoTIFFs (NASA LANCE MCDWD flood tiles): for each covering tile, GET the object
through the shared transport (404 -> skip, a coverage gap), reproject-window its
band-1 to the AOI nearest (uint8), and paste its non-nodata pixels where the mosaic is
still nodata (FIRST-VALID wins). `execute` bakes the declarative palette into a
256-entry band-1 color table (nodata index transparent). The first-valid uint8 variant
of `fixed_tile_grid` (continuous NaN-merge, zip-wrapped) that ADR 0077 named. An
all-nodata / no-tile mosaic -> typed EMPTY. STRICT no-op for every prior raster spec.

### 3. `access: wcs_getcoverage` -- WCS 1.0.0 GetCoverage + palette COG (raster_cog)

A WCS 1.0.0 GetCoverage templated GET of a categorical coverage (NLCD via the MRLC
GeoServer, canonical class integers in the band) through the shared ogc adapter (the
twin's Tier-2 seam), a NLCD background(0)->nodata pixel remap (class 0 is never a real
NLCD code; the twin's `_fix_nlcd_background_transparency` at the pixel level), and a
palette COG serialize preserving the source's embedded band-1 color table. Coverage id
resolves from the vintage year (declarative `wcs.coverage_by_year`); the effective
resolution + quantized bbox are the pre_resolve auto-coarsen (in the cache key). STRICT
no-op for every prior raster spec.

`FloodExtentObservationResult` (product / observation_date / class_breakdown /
flood_area / caveats / notes) and `LandcoverResult` (nlcd_vintage_year / dataset /
source / effective_resolution_m / native_resolution_m / downsampled / downsampling_note)
moved into `contracts/execution.py` `LAYER_RESULT_MODELS` (the HighWaterMarksLayerURI /
FaultSourcesResult precedent). `LandcoverResult` exists because `LayerURI` is FROZEN
(`extra="forbid"`): the twin returned a `{"layer": LayerURI, ...sidecar}` dict for
exactly this reason; the subclass carries the sidecar directly instead.

## FOLDED: fetch_flood_extent_observation (categorical_tile_grid + pre_resolve + envelope)

Twin -> `source.yaml` (raster-cog, error_prefix FLOOD_EXTENT, empty NO_COVERAGE) +
`flood_extent_observation` hooks: `pre_resolve` (date/None -> year/doy; the dir-walk
over the transport when latest) and the post-emit `envelope` (class_breakdown /
flood_area / caveats / categorical legend -> FloodExtentObservationResult). The 50 deg^2
guardrail folds to `gates.max_bbox_deg2`. The V&V consumer `compute_flood_extent_skill`
couples by raster SHAPE in a docstring (no import) -> NO re-point, NO canary owed.

## FOLDED: fetch_landcover (wcs_getcoverage + pre_resolve auto-coarsen + envelope sidecar)

Twin -> `source.yaml` (raster-cog, error_prefix LANDCOVER, role input, auto_publish
false) + `landcover` hooks: `pre_resolve` (PURE: normalize the bare `nlcd`/`nlcd_`
aliases + parse `nlcd_YYYY` + the 4000-px MRLC auto-coarsen -> effective resolution +
quantized bbox, all into the cache key; the ESA WorldCover branch raises the twin's
reserved/not-implemented typed error) and `envelope` (the Manning's-validation sidecar
-> LandcoverResult). The 5e6 km^2 hard ceiling folds to `gates.max_bbox_km2`.

CONSUMER re-point: `flood.py` imported the twin symbol + read `landcover_result["layer"]`
/ `.get("nlcd_vintage_year")`. Re-pointed to a thin module-level `fetch_landcover`
wrapper resolving `TOOL_REGISTRY["fetch_landcover"].fn` (kept as the patch point the
flood-scenario consumer tests monkeypatch); the call site reads the LandcoverResult's
`.uri` + `.nlcd_vintage_year` (tolerating the twin's legacy dict). test_model_flood_
scenario{,_coastal,_v2,_surge_plumbing}.py + test_compute_impervious_surface.py +
test_catalog_tools.py migrated (85 + green). FLOOD CANARY: run_sfincs_direct.py.

## Evidence

- **TWIN-vs-ROUTER value parity PASS** (SPEC-IDENTITY gate, twins present, same fixture
  + in-memory cache, run BEFORE deletion): flood_extent -- observation_date / product /
  class_breakdown / flood_pixel_count / flood_area_km2 / caveats / style_preset / units /
  categorical legend all value-identical. landcover -- nlcd_vintage_year / dataset /
  source / effective_resolution_m / native_resolution_m / downsampled + the LayerURI
  surface (style_preset / units / role / layer_type) value-identical, AND the paletted
  COG pixel-identical (same class array with background(0)->nodata remapped, same
  colormap, same nodata).
- Value coverage -> `test_router_flood_extent_observation.py` (7) + `test_router_
  landcover.py` (7). Twin py + `test_fetch_flood_extent_observation.py` DELETED; the
  landcover twin-internal cluster in `test_data_fetch.py` (~995 lines) + the resolution-
  gate auto-coarsen tests migrated with notes.
- Retrieval UNSHIFTED (landcover 8/8, flood_extent 6/6 corpus phrasings top-8, model-
  free). Docstrings carried VERBATIM (landcover 3,708 / flood_extent 2,460 chars).
- Offline suite FAILED set unchanged from the 9-failure baseline (both folds strict
  no-op for the priors; the +2 promote metrics updated in test_catalog_surfacing).
- **FLOOD CANARY** (landcover, mandatory -- the SFINCS seam): run_sfincs_direct.py
  PASSED. Live: the spec-driven fetch_landcover issued the WCS 1.0.0 GetCoverage
  (mrlc_display:NLCD_2021_Land_Cover_L48, image/tiff 38 KB) -> paletted COG cached to
  MinIO; the SFINCS builder read the canonical NLCD class codes
  [11,21,22,23,24,31,41,42,43,52,71,81,90,95] with vintage_year=2021 (the sidecar
  contract end-to-end); local-docker solver status=ok; depth_cogs=1 published.

## Non-gating divergences

- **layer_id**: the router synthesizes `<source_class>-<variable>` vs the twins'
  bespoke ids (the standing layer-id-cosmetics fold divergence); style_preset / role /
  units / bbox / legend / name are reproduced.
- **landcover COG byte layout**: the router serializes via `array_to_cog_bytes` (COG
  driver, internal overviews) rather than the twin's clip -> gdal_translate/rasterio COG
  dance; the class array + embedded palette + nodata are pixel-identical (proven), only
  the exact tiling/overview byte layout differs.
- **cache-version salt DROPPED**: the twin's `_LANDCOVER_CACHE_VERSION` busted stale
  pre-fix COGs; with the twin deleted the router's fresh cache key naturally misses any
  stale twin COG, so the salt is unneeded (the movebank cross-twin-cache-parity-moot
  precedent).
- **error prefixes**: the twins raised generic `BboxInvalidError` / `UpstreamAPIError`;
  the folds stamp source-scoped LANDCOVER_* / FLOOD_EXTENT_* (more faithful to the A.6
  typed-error surface, not less).
- **auto_publish false** (landcover): role=input intermediate feeding SFINCS setup (the
  fetch_dem precedent); flood.py publishes it explicitly. The twin returned a dict, so
  it was never server-auto-published either.

## Metrics

Coded fetchers -2 (landcover + flood_extent); coded tools -2. Spec-served sources
71 -> 73 (+2). Registry name-count UNCHANGED (two coded twins died, two spec surfaces
took their names). test_catalog_surfacing: n_specs 71 -> 73, arm declarable delta
-70 -> -72, stratum index 70 -> 72 (expected promote metrics). Offline baseline
UNCHANGED (FAILED set == the 9 pre-existing).

Consequence: the raster-cog family gains a `categorical_tile_grid` first-valid mosaic
mode + a `wcs_getcoverage` paletted-COG mode, and the router gains a generic
`pre_resolve` pre-cache-key HTTP resolve, all opt-in-no-op; fetch_landcover and
fetch_flood_extent_observation fold with no coded twin. This CLOSES the ADR 0077
finishers backlog -- all four per-source residuals (movebank, fault_sources, landcover,
flood_extent) are now folded.
