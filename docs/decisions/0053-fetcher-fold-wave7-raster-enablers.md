# 0053 - fetcher fold wave-7 RASTER ENABLERS: imageserver_export + continuous-float STAC

Context: Phase-2 wave-7 opened the RASTER ENABLERS ADR-0047 enumerated (six new
ingestion modes, in leverage order, each its own scoped NATE-methodology job).
ADR-0047 deferred all eleven raster twins for want of distinct new machinery; this
wave BUILDS two of those enablers and folds the three twins they unlock, each
proven byte-identical against the LIVE twin (drivers_wave7.py; read_through
stubbed) before its twin is cut, per the cull doctrine. All new machinery is
STRICTLY NO-OP for the prior 17 specs -- gated on a new `ingest.access` value, a
new `ingest.*` sub-block, or a new optional spec field no prior spec sets.

Decision (2026-07-30):

1. `imageserver_export` MODE BUILT + TWO TWINS FOLDED (LANDED). A new
   `raster_cog` access mode fetches an ArcGIS ImageServer `exportImage` REST
   response through the transport (ADR-0044: our httpx opener owns the socket) and
   returns the server's ready GeoTIFF body UNCHANGED (the twins did no
   reserialization -- the exportImage response IS the cached artifact), so output
   is value-identical. Declarative knobs: `service_by_param` (layer enum -> LF2022
   ImageServer service), the m/deg pixel-size formula (`native_cell_m`/`px_min`/
   `px_max`), `export_query` (bboxSR/format/pixelType/imageSR/f), and an all-nodata
   coverage gate (`nodata_sentinel` + `zero_is_nodata`). Two transport/schema
   additions ride along, both no-op for priors: transport `get_bytes` (the
   whole-object GET counterpart to `range_get`, retry-authority-backed); and
   param-keyed `output.style_preset_by_param` + `normalize.units_by_param` +
   `payload_estimate.ceil_mb` (the landfire/usfs per-layer categorical-vs-scaled
   style/units split + the usfs [0.05, 50] payload clip ADR-0047 flagged).
   - **fetch_landfire_fuels** (PASS 32/32): 6-layer LF2022 CONUS fuels/vegetation.
   - **fetch_usfs_canopy_fuels** (PASS 32/32): LF2022 CBH/CBD canopy fuels.
   Both: live day-values (band/dtype/crs/nodata/bounds/min/max/mean) + layer
   (type/style_preset/role/units/bbox-absent) identical per layer; forced-upstream
   (twin `requests` + router httpx both patched) identical; bad-bbox + bad-layer
   identical; the all-nodata EMPTY gate proven byte-identical by feeding BOTH sides
   the same synthetic S16 all-(-32768) TIFF (the live ImageServer resamples data
   even over open water, so the EMPTY path never fires live -- not forced).
   ELMFIRE consumer seam re-pointed: `run_elmfire._fetch_deck_inputs` now resolves
   `fetch_landfire_fuels` through `TOOL_REGISTRY[...].fn` (was a direct twin
   import). Fire-spread canary: run_elmfire imports clean; the registry seam
   resolves the promoted tool; a live fbfm40 fetch over a small CONUS AOI returns a
   valid raster LayerURI (role=primary, categorical_landcover, .tif).

2. `stac_float` CONTINUOUS-FLOAT STAC MODE BUILT + ONE TWIN FOLDED (LANDED). A new
   `raster_cog` access mode does a PC-STAC search -> item select -> two-tier
   per-href sign (per-href `/api/sas/v1/sign` PRIMARY, per-collection token
   FALLBACK -- the `modiseuwest` account the token cannot authorize) -> windowed
   bilinear reproject through the transport -> DN scale/offset transform -> float32
   with NaN fill. Declarative knobs: `select: latest` (most-recent single item;
   the alternative `intersect_all` first-valid mosaic is coded for a future DEM
   consumer), `datetime_window` (both bounds -> that window, else a trailing
   N-day default), `collection_by_param` + `asset_by_params` with product/daynight
   ALIAS normalization + membership validation (`_normalize_via_aliases`
   reproduces the twin `_normalize_product`/`_normalize_daynight` typed
   PARAM_INVALID contract), and `transform` (scale/offset/fill_dn/src_nodata).
   - **fetch_modis_lst** (PASS 34/34): MODIS 8-day 1 km LST scaled to deg C. Live
     day + night values (min/max/mean rounded 4dp) identical; layer identical;
     the ocean all-fill NO_DATA path identical live; forced-upstream (pystac
     `Client.open` patched both sides) identical; bad-bbox / over-6deg^2-area /
     bad-product / bad-daynight all identical.

3. **fetch_copernicus_dem DEFERRED WITH EVIDENCE.** The `stac_float` mode closes
   the copernicus RASTER parity (continuous-float mosaic, meters, -9999 nodata),
   but the twin is a DEPRECATED delegate over a shared `_copernicus_dem_impl` that
   FIVE canonical legs import directly -- `fetch_dem(source="copernicus")` (the
   canonical surface + a fallback), `model_debris_flow`, `compute_sediment_yield`,
   and `_hydrology_common`. Folding it means either duplicating the impl (violates
   clean-as-you-go) or re-pointing five do-not-regress internal consumers off an
   internal function -- a regression surface disproportionate to a deprecated
   delegate. STOP RULE: defer; re-homing `_copernicus_dem_impl` (or promoting
   `fetch_dem` itself) is its own scoped job. The `stac_float` mode is ready for it
   the day that lands.

4. **ENABLERS 3-6 NOT ATTEMPTED THIS WAVE** (redirect-follow + gzip for
   gcn250/chirps; VRT-fanout multi-URL opener for hrsl/soilgrids; `griddap` for
   noaa_sst; declarative colormap-ramp for jrc). The wave prioritized completing
   FEWER enablers FULLY (built + tested + sources folded + twins deleted + live-
   proven) over many partial; each remains its own scoped job per the ADR-0047
   leverage roster. gcn250 (SFINCS) and hrsl/soilgrids VRT-fanout (ADR-0047's
   highest-leverage single enabler) are the recommended next cuts.

Registry accounting: registry total stays 190 (three twins died, three
spec-driven surfaces took their names under `_router._promoted`). CODED tools
173 -> 170 (-3); coded fetchers 82 -> 79 (-3); spec-served data sources 17 -> 20
(+3). Retrieval index unshifted: all three docstrings carried VERBATIM
(`inspect.getdoc`), all three corpus phrasing sets rank their tool in the
model-free top-8 (7/7, 6/6, 8/8). Coverage migrated: the three twin test files
(1,882 lines) deleted; imageserver_export + stac_float unit coverage added to
`test_router_executors.py` (passthrough-bytes-unchanged, JSON/non-TIFF ->
upstream, all-nodata/all-zero -> empty, pixel-size clamp, param-keyed style/units,
payload ceil, alias-normalize + PARAM_INVALID, dispatch wiring). Twin py removed:
1,684 lines; router+transport+schema engine grew ~374 lines of NEW capability.
Daemon import clean. Offline suite FAILED set unchanged at the baseline 9
(test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5).

Consequence: the router's raster surface now carries two general new ingestion
modes -- ArcGIS ImageServer exportImage (any `exportImage` REST raster with an
all-nodata coverage gate) and continuous-float STAC (any DN-scaled single-item or
first-valid-mosaic STAC float source with two-tier PC signing) -- each proven
byte-identical on live data and each unlocking a family beyond its first source.
Supersedes nothing; executes two of the six enablers ADR-0047 scoped and extends
the wave-4/5/6 fold-some-defer-rest precedent (ADR-0045/0047/0052).
