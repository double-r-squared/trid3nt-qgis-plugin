# 0069 - weather/GRIB: MRMS folds via a whole-object grib_object mode; the rest STOP with named gaps

Context: the weather / model-raster family (GRIB / netCDF cycle fetchers) was read
IN FULL against the phase + mode inventory the intervening waves landed
(pre_resolve resolve phase ADR 0063/0064, offset paging, PHASE E enrichment,
gzip_object / get_zip whole-object, fixed_tile_grid, mapserver_export, the
serialize dtype/nodata directive). Six targets: fetch_mrms_qpe,
fetch_hrrr_forecast, fetch_hrrr_smoke, fetch_nexrad_reflectivity,
fetch_noaa_nwm_streamflow, fetch_cama_flood_discharge. Every target was re-read in
full before a verdict -- the mission framing ("GRIB is whole-object by nature; a
decode hook receiving bytes is pure") anticipated a GRIB whole-object fold, and
exactly ONE target (MRMS) is that shape. The family is otherwise dominated by two
shapes the router's transport-owns-the-socket + single-source model does not fit:
opaque-library socket ownership (HRRR-Zarr via fsspec/xarray, the ADR-0068
fetch_3dep_extra precedent) and multi-source composites.

Decision (2026-08-01):

1. **fetch_mrms_qpe FOLDED -- ONE new minimal raster access mode `grib_object` +
   the EXISTING S3-listed-key resolve phase.** NOAA MRMS MultiSensor QPE Pass2 is
   a whole-object `.grib2.gz` behind an S3 key; the honest fold is the gzip_object
   precedent (ADR 0055) at GRIB scale.
   - **`grib_object` access mode (raster_cog).** Whole-object GET through the
     shared transport `get_bytes` (ADR-0044 owns the socket), gunzip, GRIB decode
     via a tempfile (the GDAL GRIB driver needs a real path -- a MemoryFile cannot
     host its tabular index, so a tempfile, unlike gzip_object's in-memory read),
     read band 1 float32, collapse the source sentinels (`sentinel_equals` [-3, -1]
     + `sentinel_below` -0.5) to `nodata` (-9999), clip on the SOURCE grid
     (floor-offset / ceil-length / clip-to-extent, the twin's window math), and
     reproject to EPSG:4326 ONLY when the decoded CRS is not already 4326
     (calculate_default_transform + nearest, nodata-preserving). `bbox=None` reads
     the full CONUS grid (supports_global_query). The mode returns the array with
     the in-band nodata; `execute`'s `serialize` block (nodata=-9999, dtype float32)
     writes it through unchanged (every pixel finite -> the fill is a no-op). A
     404 -> typed NOT_AVAILABLE; a window off-extent -> typed EMPTY; any other fetch
     / gunzip / decode failure -> typed UPSTREAM. STRICTLY no-op for every prior
     raster spec (a new access string + a new decode function; no prior spec sets
     `ingest.access: grib_object`).
   - **Key discovery via the EXISTING single-round resolve phase (ADR 0063/0064).**
     The S3 list-object walk fits the resolve phase WITHOUT new machinery: the
     `mrms_qpe.resolve_build` hook emits the candidate list-object probes (a
     targeted `valid_time` -> one exact-key `?prefix=<key>&max-keys=1` probe per
     hour of the 24 h walkback; `latest` -> the last 2 date-dir listings), the
     router GETs them (all return HTTP 200 with-or-without the key -- a list-object
     probe is never a 404, so no plan fails), and `mrms_qpe.resolve_parse` scrapes
     the `<Key>` set + picks the resolved key (targeted = first-present hour;
     latest = the max key), merging `{"_grib_key": key}` into `params` PRE-cache-key.
     The `str`-alias table on `accumulation` canonicalizes 1h/6h/... -> 01H/06H/...
     at validate-time (cache-key parity with the twin); the resolve hook re-raises
     the twin's unknown-accumulation / bad-valid_time INPUT errors pre-network.
   - **Live edge-matrix parity PASS vs twin (value-identical, ADR 0053/0054).** The
     twin `_grib2_to_geotiff` decode was restored and run against the router mode
     over real NOAA MRMS S3: FL 24h + TX 1h targeted + upper-alias 24H all array /
     transform / crs / dtype / nodata / shape VALUE-IDENTICAL reading both rasters
     back; latest (independent resolve) resolved the SAME key + value-identical; and
     4 error codes byte-identical (unknown accumulation -> MRMS_QPE_INPUT_ERROR;
     bad valid_time -> MRMS_QPE_INPUT_ERROR; far-future -> MRMS_QPE_NOT_AVAILABLE;
     bbox outside CONUS -> MRMS_QPE_EMPTY; retryable flags identical). Twin +
     test_fetch_mrms_qpe.py DELETED; value-bearing coverage migrated to
     `test_router_grib.py` (24 offline tests: spec wiring, accumulation
     normalize/alias/raise, valid_time parse, resolve targeted-first-present /
     latest-max-key / NOT_AVAILABLE / UPSTREAM-empty, grib_object sentinel-collapse
     + bbox-clip + offshore-empty via a synthetic-GeoTIFF-as-GRIB stand-in, payload
     estimator). Docstring carried VERBATIM (`inspect.getdoc` at fold time) + the
     sibling corpus.yaml untouched, so the retrieval index is UNSHIFTED: 7/7 corpus
     phrasings rank fetch_mrms_qpe in the model-free top-8.
   - **One consumer re-point + FLOOD CANARY green.** `model_nws_flood_event_scenario`
     (the NWS -> MRMS -> SFINCS Case-3 composer) imported the twin's `fetch_mrms_qpe`
     directly; re-pointed to a `TOOL_REGISTRY["fetch_mrms_qpe"].fn` resolver local
     (the ADR 0063 nws_alerts precedent); its degrade test re-pointed off the
     deleted `MRMSQPEUpstreamError` to the shared `router_upstream_error`. Because a
     SFINCS flood-workflow consumer seam was touched, the FLOOD CANARY was run:
     `scripts/run_sfincs_direct.py` PASSED (status=ok, peak flood-depth COG
     published to `s3://trid3nt-runs/.../overviews/*.tif`, 7 depth frames + peak in
     MinIO).

2. **The other FIVE STOP with EVIDENCED, NAMED gaps (fold-fewer-fully beats
   force-fitting).**
   - **fetch_hrrr_forecast -- STOP.** NOT GRIB2 (despite the family name): it reads
     the University-of-Utah HRRR-Zarr S3 mirror via `fsspec.get_mapper` +
     `xarray.open_zarr` + `rioxarray`. Gaps: (a) a Zarr store is opened by many
     small fsspec/zarr-coordinated range reads -- the SOCKET is owned by
     zarr/fsspec, not the router transport (the ADR-0044 violation the
     fetch_3dep_extra STOP named); (b) an unbuilt native LCC(+proj=lcc ...) ->
     EPSG:4326 rioxarray reproject+clip_box raster mode; (c) the DERIVED
     `10m_wind_speed` variable pulls BOTH UGRD/VGRD components and combines them
     (`hypot`) -- a multi-array synthesis no single-array mode expresses; (d) the
     cycle walkback probes `fs.exists` (s3fs, transport-external). Not a no-op
     extension.
   - **fetch_hrrr_smoke -- STOP.** The IDENTICAL HRRR-Zarr shape (shared LCC proj,
     cycle walkback, reproject+clip). Same gaps (a)/(b)/(d) plus a fill-value
     (-9999) -> NaN mask; no derived variable. Folds together with fetch_hrrr_forecast
     if an HRRR-Zarr mode is ever built (both are the same twin body).
   - **fetch_nexrad_reflectivity -- STOP.** A no-fetch WMS-URL PASSTHROUGH:
     `cacheable=False`, `ttl_class="live-no-cache"`, `source_class=None`, it builds
     an Iowa-Mesonet WMS GetMap URL string and returns a LayerURI -- it fetches NO
     bytes and never caches (radar refreshes ~5 min; a cached PNG would misrepresent
     the live storm). The router's entire model is fetch-bytes -> serialize ->
     read_through -> LayerURI, and `SourceSpec` MANDATES `source_class` (min_length=1)
     + `cache` + `payload_estimate` a live passthrough has none of. There is no
     compose-a-URL-and-return-nothing executor. A clean STOP (tiny, mostly docstring);
     not worth a bespoke no-fetch executor for one source.
   - **fetch_noaa_nwm_streamflow -- STOP.** A multi-source COMPOSITE: (1) resolve
     the NWM channel_rt S3 key (latest-date scan / targeted key probe), (2)
     whole-object netCDF download -> xarray decode into a `{feature_id: streamflow}`
     LOOKUP DICT (not a feature list), (3) NLDI 5x5-grid bbox spatial-sample (25
     point-snap requests) -> COMIDs, (4) per-reach NLDI geometry fetch (up to 500
     requests) -> LineString midpoints, (5) JOIN the streamflow dict to the
     NLDI-discovered geometry by feature_id -> point FGB. No router mode fetches a
     whole-file-into-a-lookup-dict AND spatially-samples a SECOND API AND joins them.
     FEEDS `sfincs_forcing_autowire` (direct import at autowire.py:1013) -- since it
     is NOT folded and its seam is UNTOUCHED, no flood canary was required for it.
   - **fetch_cama_flood_discharge -- STOP / HELD.** Whole-object netCDF -> COG, but:
     (a) the live source is DEAD -- the U.Tokyo Hydra legacy URL family returns an
     HTML migration page (Yamazaki Lab moved; distribution now Google-Form +
     Dropbox-password gated), so live byte-parity cannot be proven against a dead
     source (the ADR-0068 fetch_noaa_sst HELD/BLOCKED-ON-UPSTREAM precedent) and the
     twin's own primary path raises CAMA_FLOOD_UNREACHABLE without a configured
     mirror; (b) a candidate-filename fallback loop (probe ~6 filename patterns,
     magic-sniff netCDF/HDF5-vs-HTML, first-200-netCDF wins) is unbuilt machinery;
     (c) the netCDF juggling (variable auto-pick by name/long_name, time-range
     select + non-spatial mean, coord-standardize, lon 0-360 -> -180-180 normalize,
     lat-sort, clip_box, north-up re-sort, multi-year concat) exceeds the opendap /
     gzip_object modes. HELD until a live mirror + a whole-object-netCDF mode.

3. **Metrics.** Coded fetchers -1; coded tools -1 (the MRMS twin died, the
   spec-driven surface took its name). Spec-served data sources 49 -> 50 (+1).
   Registry total UNCHANGED at 190. Twin py + test LOC removed = 819 + 712 = 1,531;
   added = the `grib_object` mode + `mrms_qpe.py` resolve hooks + `source.yaml` +
   `test_router_grib.py` (24 tests). `test_catalog_surfacing`: n_specs 49 -> 50, the
   arm2/arm3 declarable delta -48 -> -49, the stratum tool count 48 -> 49 (the
   expected metric, not a regression). Offline suite FAILED set == 9 EXACTLY (the
   pre-existing test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5; no new
   failure), run in four foreground quarters. Daemon import clean; fetch_mrms_qpe
   spec-served + registry-resolvable.

4. **HOOK-RATCHET update.** No NEW 2x+ recurring cross-source shape surfaced this
   wave. Two composition patterns were NOTED (not yet ratchet-promotable): (a) the
   whole-object-GET-then-decode raster mode (gzip_object / grib_object) -- a third
   whole-object raster mode, but each carries a source-specific decode (gunzip+window
   vs GRIB-tempfile+sentinel+reproject), so no shared decode primitive is promotable
   yet; (b) the NOAA-S3 list-latest / key-walkback resolve recurs (MRMS + the
   STOP-ruled NWM both do it) but only MRMS uses it, so it stays a per-source resolve
   hook, not a mode. All four ADR 0061 ratchets remain retired.

Non-gating divergences flagged (REPORTED, never fudged):
(a) **latest resolve strategy (MRMS).** The twin lists ALL date prefixes then the
    newest date's files; the router lists the last 2 date directories + picks the max
    key. In the normal case (Pass2 lags ~2 h, so the newest file is within today /
    yesterday UTC) the resolved key is IDENTICAL (LIVE-proven same-key). Only a
    pathological >2-day bucket gap would diverge (both still return a valid latest
    file). Same class as the ADR 0064 storm_events resolve-strategy divergence.
(b) **resolve is pre-cache-key (MRMS).** The resolved `_grib_key` enters the cache
    key (pre_resolve runs before read_through). A targeted valid_time keys
    deterministically; a `latest` call keys on the resolved key so two `latest`
    calls collapse only when they resolve the same file (value-identical). The twin
    keyed on `valid_time-or-LATEST` only. Same class as ADR 0064(e).
(c) **accumulation alias leniency (MRMS).** The router alias table accepts exactly
    the twin's set (lowercase 1h/6h/... + uppercase 01H/06H/...); an initial draft
    that also accepted 01h/03h/06h (which the twin REJECTS) was tightened to
    byte-identical rejection parity before the fold.
(d) **synthesized payload estimator (MRMS).** The twin's per-sq-deg model (196 MB /
    2450 sq-deg / 6x ratio) is reproduced as `bbox_area` (mb_per_sq_deg 0.0133), so
    per-bbox estimates match closely; only the `bbox=None` full-CONUS case differs
    (the router uses its smaller gridmet-CONUS proxy area) -- both tiny for realistic
    bboxes, no spurious warn/block. Same class as every prior fold's estimator
    divergence.
(e) **COG-vs-GTiff internal layout (MRMS).** The twin wrote a tiled GTiff
    (predictor=3, band description, TIFFTAG tags); the router's `array_to_cog_bytes`
    writes a COG (blocksize 256, no predictor/description/tags). Internal layout +
    provenance-metadata only -- the pixel values / bands / crs / transform / nodata
    read back value-identical (the ADR 0053/0054 parity standard), proven by the
    live harness. Same class as ADR 0068(e).

Consequence: the weather/GRIB family resolves to ONE genuine fold (fetch_mrms_qpe,
a new minimal `grib_object` whole-object raster access mode -- the gzip_object
precedent at GRIB scale -- + the EXISTING S3-listed-key resolve phase, proven
value-identical on live NOAA data, no-op for all priors) and FIVE honestly
STOP-ruled / HELD residues, each with a named next-mode gap (HRRR-Zarr
opaque-socket + LCC reproject + derived-variable, no-fetch WMS passthrough,
multi-source netCDF+NLDI composite, dead-source + candidate-filename + netCDF
juggling). The "coded fetcher count -> zero" endgame advances by one; the mission's
"GRIB is whole-object, the decode hook is pure" framing is vindicated for the one
true GRIB target, and the zarr / composite / passthrough shapes are named for future
waves. Supersedes nothing; extends the raster executor with one whole-object GRIB
access mode and demonstrates the resolve phase carries an S3-listed raster key.
