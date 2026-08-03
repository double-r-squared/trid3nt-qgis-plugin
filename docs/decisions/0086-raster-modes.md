# ADR 0086 -- Raster-modes wave: JRC + SoilGrids folded (two new access modes + a colormap hook); topobathy STOP re-affirmed; the dead ERA5/GTSM credential class-name entries pruned

Status: accepted (2026-08-02)
Supersedes: the ADR 0085 fetch_jrc_global_surface_water "QUEUED (continuous-STAC
mosaic mode + colormap hook; fetch-side-gated)" row (now FOLDED); the ADR 0068
fetch_soilgrids "QUEUED (projected-window reproject wave)" row (now FOLDED). Re-affirms
the long-standing fetch_topobathy STOP (ADR 0068/0059).

## Context

The remaining RASTER-mode fetcher twins on `refactor/engine-doors`: the three ADR
0068/0085 residuals whose blockers were specifically fetch-side raster machinery
(continuous STAC mosaic; projected-CRS window reproject; multi-source UTM composite),
plus a rider pruning the dead ERA5/GTSM credential class-name entries the CDS fold
(ADR 0085) left unreachable.

Two folded with LIVE byte-identical parity vs the twin; the heaviest (topobathy) STOP
re-affirmed with a sharpened residual.

## Decisions

### 1. fetch_jrc_global_surface_water -- FOLDED (stac_continuous_mosaic mode + colormap hook)

The COLORMAP blocker (ADR 0085) was already resolved-in-principle; this wave built the
fetch side. TWO strictly-additive pieces, both no-op for every prior spec:

- The `stac_continuous_mosaic` raster_cog access mode: a CONTINUOUS-value uint8 STAC
  mosaic (search -> `_pc_sign_two_tier` -> BILINEAR windowed reproject into a uint8
  window with a PER-BAND nodata sentinel `mosaic.nodata_by_param` 0/253 -> first-valid
  mosaic; all-nodata / no-item -> the typed NO_COVERAGE). Distinct from `stac_search`
  (categorical, Resampling.nearest, single static nodata, token `sas_sign_href`).
- The `hooks.colormap` HookSpec field + the `jrc_global_surface_water.colormap` pure
  hook: the per-band occurrence/recurrence/seasonality/change ramp is a PURE function
  of the `band` param alone (no array read, no I/O), so `execute` bakes it into the
  band-1 palette via the existing `array_to_cog_bytes(colormap=...)` seam -- NOT a
  declarative colormap DSL (the ramp is computed math, one consumer, so the
  one-consumer-DSL bar does not apply).

PROOF (LIVE, real PC STAC, Lake Okeechobee FL): all four bands are BYTE-IDENTICAL to
the twin -- array `==`, embedded palette `==`, per-band nodata (0/0/0/253), CRS, and
transform all identical; a dry mid-Sahara AOI raises `JRC_GSW_NO_COVERAGE` from BOTH.
Metadata flags twin-identical; retrieval unshifted (5/5 corpus queries top-8). Offline
`test_router_jrc.py` (12) covers spec identity + gates + the colormap bake + mosaic +
honesty paths via a patched opener. Twin + twin test DELETED; the eager import
re-pointed to a fold comment (auto-registered).

Divergences (non-gating, flagged): the router synthesizes a generic `LayerURI.name` /
`layer_id` (`jrc_global_surface_water ...`) rather than the twin's `"JRC Global Surface
Water (Water occurrence)"` (the router has no per-param name template); the cache-key
params omit the twin's static `collection` key. Neither touches the honesty floor,
values, style_preset, units, or the rendered palette.

### 2. fetch_soilgrids -- FOLDED (projected_vrt_window mode)

The wave-9 STOP correctly identified that `multi_url` is a SAME-CRS mosaic paster (no
reproject) and soilgrids needs a projected-window branch. This wave built it as a new
`projected_vrt_window` access mode, strictly no-op for every prior spec:

- `transform_bounds(4326 -> source CRS, densify_pts=21)` -> the Homolosine window, with
  the twin's exact `floor`/`ceil` rounding + 2 px pad + extent clip.
- The intersecting VRT members read through the SAME coalescing transport opener
  (reusing `_resolve_multi_url_members`) and pasted into the native window array.
- `reproject` native -> EPSG:4326 at the twin's ~250 m target grid (bilinear), the
  fixed-point Int16 scaled to physical units per property
  (`projected_window.scale_by_param`; NaN fill -> the `serialize` directive's -9999).
- A `coverage_bbox` fast-reject + a `max_window_pixels` sanity cap reproduce the twin's
  honest EMPTY / refuse paths.
- `_resolve_multi_url_members` gained `url_template.format(**params)` so the
  per-(property,depth) ISRIC VRT URL fills -- strict no-op for hrsl (the only prior
  multi_url spec, which declares a placeholder-free static `url`).

PROOF (LIVE, real ISRIC, Louisiana AOI): clay/phh2o/soc/bdod (across two depths) are
BYTE-IDENTICAL to the twin -- valid mask `==`, per-pixel value maxdiff 0, nodata
(-9999), CRS, transform all identical; an ocean AOI raises `SOILGRIDS_EMPTY` from BOTH.
Metadata flags twin-identical; retrieval unshifted (8/8 top-8). Offline
`test_router_soilgrids.py` (22) covers spec identity + property/depth enum+alias tables
+ URL templating + the per-property scale/serialize + the coverage/all-nodata honesty
paths (synthetic 4326 source). Twin + twin test DELETED; the eager import re-pointed.

Divergences (non-gating, flagged): a MISSING bbox stamps `SOILGRIDS_INPUT_INVALID`
(the router's single per-param suffix) rather than the twin's distinct
`SOILGRIDS_BBOX_REQUIRED` (a malformed bbox is INPUT_INVALID in both); the depth/
property enum alias TABLES cover the realistic synonym set (`ph`, `organic_carbon`,
`"0-5"`, `"100-200 cm"`, ...) but not the twin's fully-algorithmic normalization of
exotic space/underscore combinations; the emitted COG omits the twin's descriptive
GDAL tags; the cache-key param is `soil_property` (== the public signature name) rather
than the twin's internal `property`.

### 3. fetch_topobathy -- STOP (genuinely needs more)

Read in full and re-affirmed. It is NOT a single-source raster read but a
cross-collection PRECEDENCE COMPOSITE with four irreducible new-machinery gaps: (1)
FOUR distinct discovery legs (CUDEM urllist tile-index intersect; ETOPO 15-deg
global-block naming; NCEI_REGIONAL_COASTAL_DEMS STAC ItemCollections; and 3DEP-land via
a NESTED `fetch_dem` CALL inside the fetcher body -- the stateless declarative router
executor composes no nested fetcher tool); (2) a finest-resolution UTM target grid
computed ACROSS heterogeneous sources (`_compute_target_grid`) + a precedence per-source
warp merge (`_merge_sources_rasterio`) + a `min_pixel_m` floor; (3) a per-tile vertical
-datum NAVD88 gate + documented-offset application; and (4) a `TopobathyResult(LayerURI)`
subclass (`bathymetry_present`/`fallback_warning`) NOT registered in
`LAYER_RESULT_MODELS`. Plus the DO-NOT-REGRESS flood leg: `flood.py` imports
`fetch_topobathy` + `TopobathyError` directly, so a fold mandates a flood-consumer
re-point + the FLOOD CANARY. Far beyond a bounded raster-mode extension. Twin untouched;
the DELETION_LEDGER residual sharpened. Because the twin was NOT folded, the flood.py
import is unchanged and no flood canary was mandated by this wave.

### 4. RIDER -- the dead ERA5/GTSM credential class-name entries pruned

`credential_registry.is_credential_shaped_error`'s explicit class-name fallback tuple
listed `ERA5AuthError` / `ERA5MissingKeyError` / `GTSMAuthError` / `GTSMMissingKeyError`
-- classes the CDS fold (ADR 0085) DELETED, and which the same function already catches
via its `cls_name.endswith("AuthError")` / `endswith("MissingKeyError")` checks. Pruned
the four dead entries. VERIFIED: a class named `ERA5MissingKeyError` / `GTSMAuthError`
still returns True via the endswith path; a `PlainError` still returns False.
`test_credential_pipeline.py` 35/35 green.

## Consequences

- Coded fetchers: the true coded-fetcher count (fetchers-package tools minus
  geocode_location + lookup_precip_return_period) 16 -> 14 (fetch_jrc_global_surface_water,
  fetch_soilgrids deleted; fetch_topobathy STOPped, still coded). n_specs 81 -> 83;
  registry 190 unchanged (both folds name-preserving).
- Two new raster access modes, both strict no-op for every prior spec:
  `stac_continuous_mosaic` and `projected_vrt_window`. One new HookSpec field
  (`hooks.colormap`) + one new hook module (`jrc_global_surface_water`). One shared-router
  hardening rode along: `_resolve_multi_url_members` now fills a templated VRT URL from
  params (no-op for hrsl).
- Offline baseline UNCHANGED: exactly 9 failures (test_fetch_resolution_gate x4 +
  test_run_river_dye_scenario x5) from the repo root; the keyed/stations cluster remains
  a documented order-flake. test_catalog_surfacing counts updated (n_specs 83; arm-ON
  declarable-pool delta -82; index tool_names 82).
