# 0078 - Satellite family: slider_timestamps folded (record shape's first live-no-cache source); the animation cluster STOPs on the frames-list output shape; 3 permanent verdicts closed

Context: the satellite family is the last major coded fetcher cluster -- 13 twins
(a SLIDER availability index, single-scene/keyed/STAC imagery, GLM gridding, and a
GOES/VIIRS ANIMATION cluster) plus 3 permanent-verdict candidates (nexrad, noaa_sst,
cama). This wave reads every twin end-to-end, FOLDS the one that fits the EXISTING
record machinery (ADR 0076) with only pure hooks + one minimal no-op enabler, STOPs the
rest with residuals sharpened by the twin read, characterizes the family's systemic gap
(the ANIMATION fetchers return a frames LIST, not one LayerURI), and finalizes the 3
permanent verdicts that close NATE's "all covered" definition.

Decision (2026-08-01):

## BUILT: the live-no-cache metadata enabler (2 lines, strict no-op)

Both `AtomicToolMetadata` synthesis sites (`router.synthesize_metadata` +
`registration.register_spec`) hardcoded `cacheable=True`. The cross-field validator
FORBIDS `cacheable=True` with `ttl_class=live-no-cache` -- so the record shape could not
serve an uncacheable-by-construction source. Both sites now compute
`cacheable = spec.cache.ttl_class != "live-no-cache"`, so a live-no-cache spec registers
`cacheable=False` and `read_through` short-circuits it (no bucket write, always a fresh
read). STRICT no-op for all 63 cacheable priors (none are live-no-cache).

## FOLDED: fetch_slider_timestamps (the record shape's FIRST live-no-cache source)

The SLIDER `latest_times.json` availability + cadence index maps cleanly onto the
record-return shape (ADR 0076) with ZERO new machinery beyond the enabler above:

- `slider_timestamps.build_request` -- one GET of `latest_times.json` via the SHARED
  `_satellite_slider.build_times_url` (the single source of truth the animation cluster
  imports directly; `_satellite_slider` is UNCHANGED and STAYS -- the fold deletes only
  the thin registered-tool wrapper).
- `slider_timestamps.record` -- parses the JSON body + enriches into the dict the twin
  returned (echoed sat/sector/product, `count`, `timestamps_int` ascending,
  `earliest_iso` / `latest_iso`, `cadence_seconds` median gap). A missing
  `timestamps_int` key or a non-JSON body raises `SLIDER_UPSTREAM_ERROR` (byte-identical
  to `SliderUpstreamError.error_code` via `error_prefix: SLIDER`); an EMPTY index is a
  VALID zero-frame result (count 0), never a fabricated typed error.

`cache.ttl_class: live-no-cache` reproduces the twin's `cacheable=False` (an availability
index turns over every few minutes). The docstring is carried VERBATIM; the sibling
corpus.yaml is untouched, so retrieval is UNSHIFTED (6/6 corpus phrasings top-8,
model-free `retrieve_visible_tools`). Consumers: NONE functional -- the registered TOOL
had no importers; `fetch_viirs_day_fire` / `fetch_goes_animation` call the
`_satellite_slider` list[int] helper (not the tool); the `__init__` twin import became a
fold comment. The twin had NO dedicated test -> value coverage is NEW at
`test_router_slider_timestamps.py` (11 tests: registration-as-uncacheable, the record
shape, the URL, enrich+sort, non-int skip, empty-is-valid, single-frame cadence None,
missing-key/non-JSON upstream, end-to-end route()->dict). LIVE proof: goes-19/conus/
geocolor -> 100 frames VALUE-IDENTICAL to the `_satellite_slider` data path (300 s
cadence, correct ISOs). Catalog `n_specs` 63 -> 64; registry UNCHANGED at 190 (one coded
twin died, one spec-driven surface took its name).

Non-gating divergences (slider): (a) a missing required `sat`/`sector`/`product` raises a
typed `SLIDER_INPUT_ERROR` where the twin would raise a Python `TypeError` (typed is
cleaner; the twin never had an input error class). (b) the record shape has no LayerURI
surface (it returns a dict), so the layer-cosmetics divergence class does not apply.

## THE SYSTEMIC GAP: the ANIMATION cluster returns a FRAMES LIST (STOP-RULED)

Confirmed by reading every animation twin: `fetch_goes_animation`,
`fetch_goes_blend_animation`, `fetch_goes_archive_animation`, `fetch_goes_active_fire`,
`fetch_viirs_day_fire` (and `fetch_glm_lightning` in its opt-in `accumulation_window_s`
mode) all return `list[LayerURI]` -- an ORDERED, per-timestamp list, one LayerURI per
frame, each independently cached. `route()` today returns a single `LayerURI` or a record
`dict`; the two existing transforms MERGE (`fan_out` -> one FGB, `tiled_mosaic` -> one
COG) -- the OPPOSITE of keeping frames separate. So the named mechanism is a FRAMES-LIST
output shape: `route() -> LayerURI | dict | list[LayerURI]`, an opt-in `shape:
animation_frames` (or `ingest.multi_frame`) with a per-frame `read_through` loop, the
`"step <N>"` name-token stamp the plugin scrubber groups on (the `workflows/shared/
frames.py` precedent; a distinct product-label STEM keeps sibling products in separate
scrubber groups), and a per-frame graceful-degrade honesty floor (skip an empty/failed
frame, raise only when EVERY frame fails).

The frames-list shape is minimal + opt-in-no-op + serves 5+ sources -- it CLEARS the
ratchet bar. BUT building it ALONE folds ZERO sources: each animation source ALSO needs
its own unbuilt per-frame COG builder --

- `fetch_goes_animation` / `fetch_viirs_day_fire`: a SLIDER-tile stitch-mosaic access mode
  (`_satellite_slider.stitch_slider_mosaic` -> `mosaic_to_cog_bytes`).
- `fetch_goes_archive_animation` / `fetch_goes_active_fire`: the `netcdf_cf_object`
  CF-scaled netCDF RGB/RGBA composite mode (the same access mode fetch_goes_satellite
  STOPs on, below).
- `fetch_glm_lightning`: a numpy POINT-GRIDDING mode (`numpy.add.at` energy binning,
  NEVER a warp -- GLM lat/lon carries parallax -- + a log-ramp RGBA).

Building the frames-list shape with no source it can immediately fold is SPECULATIVE INFRA
(the ADR 0056 `post_process` bar: a hook point nobody can yet use). So the whole cluster
STOPs: the frames-list output shape + each source's per-frame access mode, each its own
wave.

## STOP-RULED with sharper residuals (each a wave-sized new-mechanism build)

- **fetch_goes_satellite** -- the S3 list-then-pick-newest-key step folds onto the
  EXISTING `resolve_build`/`resolve_parse` pair (the mrms_qpe precedent), BUT the READ
  step has NO matching access mode: rasterio `NETCDF:<path>:<var>` subdataset open + a
  separate `netCDF4.Dataset` CF `scale_factor`/`add_offset`/`_FillValue` read + a
  POST-warp DN->physical scaling. Unblock: a `netcdf_cf_object` raster access mode
  (generalizing `grib_object`'s whole-object-GET+decode to CF-scaled netCDF vars). Reused
  by 4+ GOES siblings.
- **fetch_firms_active_fire** -- FOLD-READY onto the EXISTING keyed http_json path (the
  ebird/movebank precedent): a MAP_KEY resolver-blob build_request + a PURE CSV->GeoJSON
  parse_response (the FGB write is the executor's) + a classify_status auth split. ONE
  residual off the precedent: FIRMS signals a bad key via a 200-with-error-BODY (not a
  non-2xx), so the auth-body check lives in parse_response (classify_status fires only on
  a TransportError). KEY IS LIVE (`TRID3NT_FIRMS_MAP_KEY`) -> full live parity is
  provable; deferred to a fold+live-drive session (not landed this pass).
- **fetch_landsat_imagery + fetch_sentinel2_truecolor** -- a NEW multi-asset RGB-composite
  STAC mode: up to 4 SEPARATE single-band assets (RGB + a QA/SCL mask) per item + a
  categorical/bitmask mask + a JOINT 2/98-percentile cross-band stretch + a cloud-cover
  query filter + a coverage-then-least-cloudy scene rank (stac_float select = latest/
  intersect_all only). Unblock: a `stac_multi_asset_rgb` mode + a cloud/coverage selector.
- **fetch_naip** -- a single-asset MULTI-BAND uint8 read mode (loop bands 1..3 of ONE
  asset, no math). CAVEAT: `compute_canopy_height` imports the symbol directly -> a fold
  re-points it to the registry seam.
- **fetch_sentinel1_sar** -- CLOSEST of the STAC class: folds onto stac_float with the
  EXISTING `asset_by_param` (vv/vh) + `collection_by_param` (rtc/grd), plus TWO precise
  gaps: a `log10_db` transform kind for `10*log10(power)` (the -9999.0 nodata is covered
  by the existing serialize directive) + a coverage-fraction-then-recency scene select
  with an asset-presence pre-filter.

## PERMANENT VERDICTS (the ledger closes NATE's "all covered" definition)

- **fetch_nexrad_reflectivity -- PERMANENT-BESPOKE.** A ZERO-BYTE WMS-URL passthrough: the
  tool makes NO HTTP call; it pure-composes a WMS GetMap service URL (verified live
  2026-06-08) the client renders. Every router executor assumes a byte-producing fetch to
  cache + post-process; this tool never touches the network (`cacheable=False`). No
  generalizable spec shape. NOT dead. A future display-services / zero-byte-service-URL
  pool shape is the only thing that would re-open a fold; until then it stays a coded tool.
- **fetch_noaa_sst -- premise REFUTED (NOT held).** The "dead upstream" premise is false:
  the ERDDAP griddap endpoint (NOAA_DHW CRW_SST) is LIVE + working (no dead language;
  `SSTNoDataError` is honest land/unpublished-date no-data). It is a raster_cog FOLD
  CANDIDATE (single GET .nc bbox+day subset -> NetCDF->COG), not a HELD row; enters the
  fold backlog.
- **fetch_cama_flood_discharge -- HELD (dead upstream confirmed).** The twin's own
  docstring + `CaMaFloodUnreachableError` say the U.Tokyo Hydra URL family (as of
  2026-02-12) returns an HTML redirect to global-hydrodynamics.github.io; new distribution
  is GATED (Google-Form + Dropbox password). The code is otherwise complete. RE-OPENS the
  moment a live no-auth mirror URL is supplied via `TRID3NT_CAMA_FLOOD_BASE_URL` env or a
  `base_url=` kwarg (requires the Yamazaki Lab gated flow OR the github.io successor
  publishing a no-auth path); ticket OQ-0133. Consumers: SFINCS fluvial forcing -> a
  future fold would be flood-canary-gated. Stays a coded tool (held).

## Metrics

Coded fetchers -1 (slider_timestamps); coded tools -1. Spec-served data sources 63 -> 64
(+1). Registry UNCHANGED at 190. `test_catalog_surfacing`: n_specs 63 -> 64, arm2/arm3
declarable delta -62 -> -63, stratum tool count 62 -> 63 (the expected promote metrics,
not regressions). Offline baseline UNCHANGED (the enabler + fold are strict no-op for the
63 priors -- the record path added no new router mechanism, only made an existing metadata
synth respect live-no-cache).

Consequence: the record shape now serves an UNCACHEABLE (live-no-cache) source
(slider_timestamps, the availability primitive the frame-animation recipe stands on), via
a 2-line no-op metadata enabler. The satellite family's systemic gap is NAMED and STOPPED:
a FRAMES-LIST output shape (`route() -> list[LayerURI]`) that clears the ratchet bar
(minimal + no-op + 5+ sources) but folds zero sources without ALSO building each source's
per-frame access mode (SLIDER-stitch / netcdf_cf_object / point-gridding) -- so it STOPs as
one interlocked cluster of waves rather than speculative infra. goes_satellite
(netcdf_cf_object), firms (fold-ready keyed http_json), and the STAC class (multi-asset RGB
composite / multi-band uint8 / log10_db+coverage-select) STOP on precisely-named modes. The
3 permanent verdicts close the ledger: nexrad PERMANENT-BESPOKE, noaa_sst premise-refuted
fold candidate, cama HELD. Extends the tier-3 contract (ADR 0056/.../0076) with the
live-no-cache metadata enabler; supersedes nothing.
