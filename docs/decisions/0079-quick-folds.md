# 0079 - Quick folds: firms_active_fire (keyed CSV http_json), noaa_sst (griddap raster mode), sentinel1_sar (stac_float + log10_db + coverage-select)

Context: three near-ready folds the ADR 0078 verdicts named -- fetch_firms_active_fire
(FOLD-READY keyed http_json), fetch_noaa_sst (premise-refuted raster_cog candidate),
fetch_sentinel1_sar (the closest STAC straggler). Each folds onto an EXISTING executor
with a small pure hook or a minimal no-op access-mode extension; no new router shape.

Decision (2026-08-01):

## FOLDED: fetch_firms_active_fire (keyed CSV http_json, the ebird/movebank precedent)

NASA FIRMS active-fire detections fold onto the http_json vector path with one hook
module (`firms_active_fire`):

- `build_request` resolves the MAP_KEY (kwarg -> str secret_ref -> `TRID3NT_FIRMS_MAP_KEY`
  env -> a pre-network `FIRMS_MISSING_KEY`, the ebird precedent) INTO the URL path (FIRMS
  carries the key in the path, not a header) and builds the AREA-endpoint CSV URL
  byte-identically to the twin (`<base>/<key>/<source>/<w,s,e,n>/<days>[/<date>]`; a
  `date` forces the day-range to 1).
- `parse_response` decodes the CSV into Point features (stdlib `csv`) carrying the
  retained schema; the ONE residual off the ebird precedent -- FIRMS signals a
  bad/rate-limited key via a 200-WITH-ERROR-BODY -- is handled by the auth-body check
  FIRST in parse (`FIRMS_AUTH_ERROR`; the 200-envelope check IS the doctrine). A
  header-only body is an honest 0-feature FGB.
- `classify_status` re-applies the same body markers on a non-2xx `TransportError` (FIRMS
  returns the `Invalid MAP_KEY.` body under HTTP 400 too); a non-auth non-2xx keeps the
  default retryable upstream.

`error_prefix: FIRMS`, `input_error_suffix: ARG_INVALID` reproduce the twin's
`FIRMS_ARG_INVALID` / `FIRMS_AUTH_ERROR` / `FIRMS_MISSING_KEY` codes -- the exact tokens
`credential_registry.TOOL_AUTH_ERROR_CODES["fetch_firms_active_fire"]` expects (name-keyed,
no re-point). `ingest.properties` declares the 12 retained columns so the honest-empty FGB
carries a STABLE schema (the twin dropped source-absent columns, so a VIIRS row now reads
null brightness/bright_t31 instead of omitting them -- a stable-schema improvement).

## FOLDED: fetch_noaa_sst (the new raster_cog `griddap` access mode)

The ERDDAP griddap bracket-selector `.nc` REST endpoint (server-side bbox+day subset)
folds onto a NEW `griddap` raster_cog access mode: one GET of
`<base>/griddap/NOAA_DHW.nc?<var>[(<ts>)][(<lat_hi>):(<lat_lo>)][(<lon_lo>):(<lon_hi>)]`
through the shared transport -> in-memory xarray subset + squeeze -> a north-up float32
`(array, transform, crs)` -> the executor's default NaN-nodata COG serialize. A 404 whose
body carries the ERDDAP no-matching / axis-range markers is honest `NOAA_SST_NO_DATA`
(the twin's SSTNoDataError, `empty_error_suffix: NO_DATA`); an all-NaN (fully-land) window
is the same honest empty; any other non-2xx / parse failure is `NOAA_SST_UPSTREAM_ERROR`.
The mode is fully declarative (`ingest.griddap`: dataset / var_by_param / time_of_day /
lat_descending / nodata_body_markers) and STRICT no-op for every prior raster spec.

## FOLDED: fetch_sentinel1_sar (stac_float + `coverage` select + `log10_db`)

Sentinel-1 SAR folds onto the EXISTING `stac_float` mode with the EXISTING `asset_by_param`
(vv/vh) + `collection_by_param` (rtc/grd, with product_aliases) plus two minimal additions,
both opt-in-no-op for the 3 prior stac_float specs:

- a `select: coverage` mode (coverage-fraction-then-recency rank with an asset-presence
  pre-filter; no scene carrying the polarization asset -> `SENTINEL1_NO_IMAGERY`).
- a `transform.log10_db` directive (`10*log10(power)`; non-positive/non-finite -> NaN,
  filled to the -9999 dB sentinel by the EXISTING `serialize.nodata` directive; an
  all-invalid window -> the existing typed EMPTY = the twin's NO_IMAGERY).

`error_prefix: SENTINEL1`; `emit_bbox: false` (the twin omits LayerURI.bbox);
`units_by_param` stamps the per-polarization dB units string.

## Live evidence

- **sentinel1_sar LIVE PASS** (PC STAC reachable): Houston AOI (0.2x0.2 deg), coverage-select
  picked a scene, `log10_db` produced a full-valid 2226x1932 EPSG:4326 dB COG -- VV median
  -7.7 dB (min -27.6 / max 37.0), VH median -14.5 dB (min -31.4 / max 23.4). Textbook
  C-band land backscatter (VH < VV cross-pol), plausible urban double-bounce brights.
- **noaa_sst**: the griddap access mode emits a byte-correct bracket-selector URL
  (`CRW_SST[(<date>T12:00:00Z)][(<north>):(<south>)][(<west>):(<east>)]`, lat high:low); the
  live COG parity is PENDING -- `coastwatch.pfeg.noaa.gov` (the NOAA ERDDAP host) was timing
  out (a transient upstream availability issue; general egress + PC STAC both fast). The
  transport honestly retries + surfaces the network timeout (upstream-provider-error rule).
  Offline griddap value coverage (URL, north-up orientation, 404-marker EMPTY, all-land
  EMPTY, COG serialize) all pass.
- **firms_active_fire**: live parity BLOCKED -- `TRID3NT_FIRMS_MAP_KEY` is not present in this
  environment (a NATE credential step). Offline hook parity (URL build rolling+historical,
  CSV->Point parse, 200-body + non-2xx auth split, missing-key pre-network) all pass.

## Metrics

Coded fetchers -3; coded tools -3. Spec-served sources 64 -> 67. Registry name-count
UNCHANGED (three coded twins died, three spec surfaces took their names). `test_catalog_surfacing`:
n_specs 64 -> 67, arm declarable delta -63 -> -66, stratum index 63 -> 66 (expected promote
metrics). Offline baseline UNCHANGED (the FAILED set is EXACTLY the 9 pre-existing:
test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5; 9688 passed). Retrieval
UNSHIFTED (9/9 corpus phrasings rank #1 top-8, model-free). Consumer tests green
(test_credential_pipeline / resolver / categories / tools_registry / validator: 3874 passed).

## Non-gating divergences

- **firms**: the twin's vault-first `_resolve_map_key` (Persistence.get_secret_value +
  `set_persistence_for_secrets` + a `demo` literal fallback + a key_fp cache fingerprint)
  is REMOVED; the hook resolves kwarg -> str secret_ref -> env -> MISSING_KEY (the
  ebird/movebank keyed precedent). An explicitly-passed `map_key`/`secret_ref` kwarg now
  enters the cache key (the ebird precedent; the common env path keeps the key out of the
  cache entirely) rather than being fingerprinted. Layer_id/name synthesized.
- **noaa_sst**: an out-of-coverage date (pre-1985 / future) surfaces as `NOAA_SST_NO_DATA`
  from ERDDAP's own axis-range 404 rather than the twin's pre-network `NOAA_SST_INPUT_ERROR`
  (both honest, non-fabricating). A DEFAULT date (today-1) does not enter the cache key
  (the stac_float `latest` precedent); an explicit date does. `variable` aliases beyond
  sst/anomaly dropped. Layer_id/name synthesized.
- **sentinel1_sar**: the polarization aliases (co-pol / cross-pol) are dropped (canonical
  vv/vh only); collection aliases (rtc/grd/s1-rtc) preserved via `product_aliases`. Asset
  signing uses `_pc_sign_two_tier` (per-href sign primary) vs the twin's direct
  `sas_sign_href` (functionally equivalent, more robust). Layer_id/name synthesized.

Consequence: the raster_cog executor gains a `griddap` access mode (ERDDAP bracket-selector
NetCDF -> COG) and a `log10_db` transform + `coverage` stac select, all opt-in-no-op for the
prior specs; the http_json path gains a keyed source whose key lives in the URL path with a
200-body auth split. Three ADR 0078 fold-ready verdicts close. Supersedes nothing; extends
the tier-3 hook contract (ADR 0056/.../0078) and the raster access-mode family.
