# 0055 - fetcher fold wave-9: multi_url VRT fan-out + gzip_object whole-object mode

Context: Phase-2 wave-9 builds the last two viable raster mode-enablers ADR-0047
scoped and ADR-0053/0054 deferred: the VRT-fan-out / multi-URL opener (ADR-0047's
highest-leverage single enabler) and the whole-object gzip mode. Two sources FOLD
this wave, each proven value-identical against the LIVE twin before its twin is
cut; every new knob is STRICTLY NO-OP for the 22 prior specs (gated on a new
`ingest.access` value -- `multi_url` / `gzip_object` -- that no prior spec sets).
Fewer-fully doctrine: two sources FULLY (built + tested + folded + twins deleted +
tests migrated + live-proven), soilgrids stopped with evidence, griddap HELD
(ERDDAP unreachable in-env per ADR-0054), jrc colormap-DSL DEAD by stop-rule.

Decision (2026-07-31):

1. **fetch_hrsl_population FOLDED via a new `multi_url` VRT fan-out mode (LANDED).**
   The Meta + CIESIN HRSL global mosaic is a multi-tile `.vrt`; ADR-0047's LIVE
   probe showed the single-URL opener reads the VRT grid but returns ALL-NaN (it
   re-serves the VRT bytes for every sub-tile open, never fanning out). The new
   `multi_url` mode, all no-op for priors:
   - `_resolve_multi_url_members` fetches the `.vrt` whole-object through the
     transport (`get_bytes`) and `_parse_vrt` lifts the mosaic geotransform / raster
     size / SRS / band NoDataValue + each `(Simple|Complex)Source`'s SourceFilename
     (relativeToVRT-resolved to a member URL) + SrcRect + DstRect -- the exact fields
     GDAL uses to fan a windowed read out to the tiles. Member discovery is isolated
     (`mode: vrt`) so a future declarative tile-grid reuses the identical read path.
   - `_multi_url_to_array` windows the mosaic to bbox (from_bounds -> floor offsets,
     ceil lengths, clip -- the twin's window math), finds the INTERSECTING members,
     reads each member's sub-window through the SAME coalescing transport opener
     (bounded parallel, `MAX_PARALLEL`), and pastes the non-nodata pixels into the
     window. An all-nodata window (open water / off coverage) -> typed EMPTY (the
     honesty floor); ANY intersecting-member read failure -> typed UPSTREAM (never a
     silent partial), matching the twin whose GDAL read fails the whole window.
   Live edge-matrix PASS 17/18: value-identical arrays (maxabsdiff=0.0 over a Fort
   Myers bbox, 234,362/234,362 valid pixels identical), band/dtype/crs/nodata/bounds
   /min/max/mean identical, docstring verbatim (2,481 chars), ocean window ->
   HRSL_EMPTY + degenerate bbox -> HRSL_INPUT_INVALID both sides. The one non-gating
   divergence: bbox=None stamps HRSL_INPUT_INVALID (router) vs BBOX_REQUIRED (twin,
   unprefixed) -- the ADR-0047 unprefixed-error-code schema gap, flagged not gated.

2. **fetch_chirps_precipitation FOLDED via a new `gzip_object` whole-object mode
   (LANDED).** CHIRPS-2.0 is a date-templated `.tif.gz`; a gzip stream is not a
   byte-servable COG (no windowable layout), so ADR-0054 deferred it for a distinct
   whole-object mode. The new `gzip_object` mode, all no-op for priors:
   - `_resolve_gzip_url` builds the period-selected date-templated URL (monthly vs
     daily path patterns) from a parsed `date` param; a template referencing `{day}`
     requires a full YYYY-MM-DD, a monthly template accepts YYYY-MM. Coverage bounds
     (`min_year` floor, no-future) raise a typed INPUT error -- the twin's date
     validation reproduced.
   - `_gzip_object_to_array` GETs the whole object through the transport (accepting
     the whole-object cost -- gated honestly by the payload estimator), gunzips,
     opens in memory, and windows to bbox (`None` -> the full grid, supports_global
     _query). The source nodata sentinel (`arr <= -9000`) collapses to NaN; an
     all-nodata window -> typed EMPTY; a 404 -> typed NOT_AVAILABLE (unpublished
     date); any other fetch/gunzip failure -> typed UPSTREAM. The shared `serialize`
     directive (`{nodata: -9999, dtype: float32}`) stamps the twin's -9999 nodata.
   Live edge-matrix PASS 36/36: value-identical arrays across monthly + daily +
   global (bbox=None full 2000x7200 grid) over a Western Ghats bbox, dtype/nodata/crs
   /bounds/min/max/mean identical, docstring verbatim (3,447 chars); forced 404 ->
   CHIRPS_NOT_AVAILABLE, forced network error -> CHIRPS_UPSTREAM_ERROR, ocean ->
   CHIRPS_EMPTY, bad/future date + degenerate bbox -> CHIRPS_INPUT_ERROR both sides.
   Global payload path verified: the synthesized estimator reports 14.05 MB for
   bbox=None (< the 25 MB warn threshold), so the global case does not spuriously
   warn/block.

3. **fetch_soilgrids STOPPED with evidence (stack does NOT close cleanly).** The
   VRT fan-out unlocks soilgrids' multi-tile `.vrt`, but the twin additionally needs
   (a) a `transform_bounds` 4326->Interrupted-Goode-Homolosine windowing (the VRT
   CRS is projected, so bbox cannot index it directly), (b) a native-Homolosine ->
   4326 bilinear reproject at a target resolution, and (c) a per-property scale
   divisor (`/10`, `/100`) + physical-unit selection by the `soil_property` param.
   The existing `serialize` (fill NaN -> sentinel + dtype) and `stac_float`
   `transform` (scalar scale/offset) directives cover NONE of these three -- they do
   not stack onto the current directives; folding soilgrids needs a distinct
   projected-VRT reproject-and-scale-by-param branch, its own scoped job. STOP RULE
   per the kickoff gate ("fold soilgrids ONLY if it stacks cleanly on existing
   serialize/scale directives, else STOP with evidence"). Deferred.

4. **griddap HELD, jrc DEAD.** noaa_sst (griddap) stays HELD: the CoastWatch ERDDAP
   host is unreachable from this build environment (ADR-0054), so a live parity gate
   cannot run. jrc_global_surface_water (colormap-ramp DSL for a single current
   consumer) stays DEAD by stop-rule.

Registry accounting: registry total unchanged (two twins died, two spec-driven
surfaces took their names under `_router._promoted`). CODED tools 168 -> 166 (-2);
coded fetchers 77 -> 75 (-2); spec-served data sources 22 -> 24 (+2). Retrieval
index unshifted: both docstrings carried VERBATIM (hrsl 2,481, chirps 3,447 chars
via `inspect.getdoc`); both corpus phrasing sets rank their tool in the model-free
top-8 (hrsl 6/6, chirps 8/8). Coverage migrated: the two twin test files (407 + 314
= 721 lines) deleted; multi_url (VRT parse + member mosaic + all-nodata EMPTY +
member-failure UPSTREAM) and gzip_object (URL templating + bad/future date INPUT +
sentinel-collapse + all-nodata EMPTY + 404 NOT_AVAILABLE) unit coverage added to
`test_router_executors.py` (9 new tests); `test_catalog_surfacing` spec-served
count 22 -> 24 (the expected metric, not a regression). Twin py removed: 1,065
lines (489 + 576); the raster_cog executor grew ~331 lines of NEW capability. No
consumer re-point needed (neither source feeds a sfincs/flood leg -- verified by
grep). Daemon import clean. Offline suite FAILED set unchanged at the baseline 9
(test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5). FLOOD CANARY NOT
run (verified by grep: neither hrsl nor chirps feeds sfincs/flood).

Consequence: the router's raster surface now carries a VRT fan-out / multi-URL
windowed-mosaic read (any multi-tile `.vrt` mosaic, and -- via the isolated member
resolver -- any future declarative tile-grid source) and a whole-object gzip mode
(any date-templated `.tif.gz` with sentinel-collapse + global-query support), each
proven value-identical on live data. This closes the last two viable ADR-0047
raster enablers; soilgrids (projected-VRT reproject) is the only remaining raster
fold candidate and is a scoped job. Supersedes nothing; extends the wave-4/5/6/7/8
fold-some-defer-rest precedent (ADR-0045/0047/0052/0053/0054).
