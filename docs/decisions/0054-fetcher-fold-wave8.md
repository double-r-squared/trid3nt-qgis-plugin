# 0054 - fetcher fold wave-8: copernicus re-point + gcn250 (redirect/skip-HEAD + int16 direct_window)

Context: Phase-2 wave-8 executes the copernicus re-point ADR-0053 scoped as its
own job (point 3) plus the first of the ADR-0047 raster mode-enablers in leverage
order (redirect-follow + dtype/nodata + enum-URL for the single-object hosts). Two
sources FOLD this wave, each proven value-identical against the LIVE twin before
its twin is cut; every new knob is STRICTLY NO-OP for the 20 prior specs (gated on
a new `ingest.*` sub-key no prior spec sets, or a transport status no prior host
returns). Fewer-fully doctrine: two sources FULLY (built + tested + folded + twins
deleted + tests migrated + live-proven), the remaining enablers stopped w/ evidence.

Decision (2026-07-30):

1. **fetch_copernicus_dem FOLDED via the wave-7 `stac_float` mode (LANDED).** The
   copernicus twin was the deprecated delegate over `_copernicus_dem_impl` that
   FIVE canonical legs imported directly; ADR-0053 deferred it as a re-point job.
   All five legs are now re-pointed onto the registry-callable seam
   (`TOOL_REGISTRY["fetch_copernicus_dem"].fn(bbox=...)`, the wave-7 ELMFIRE
   precedent): `fetch_dem(source="copernicus")` (canonical) + the 3DEP-fallback
   leg, `_hydrology_common`, `model_debris_flow`, `compute_sediment_yield` -- each
   consumes only `layer.uri` (stages the COG locally), so the synthesized LayerURI
   is transparent to them. One new serialize directive rides along, no-op for
   priors: `ingest.serialize` (`{nodata, dtype}`) fills NaN -> a non-NaN sentinel
   and stamps the band nodata, reproducing the twin's -9999 DEM write (modis, the
   only prior stac_float source, sets no serialize block -> NaN-nodata passthrough,
   unchanged). Spec: `stac_float` / `select: intersect_all` (first-valid mosaic) /
   `native_cell_m: 30` / `serialize.nodata: -9999` / `max_bbox_deg2: 4` / role=input
   / units=meters / continuous_dem. Live edge-matrix PASS 13/13: value-identical
   arrays (maxabsdiff=0.0 over an Alps bbox, 23,808/23,808 valid pixels identical),
   nodata=-9999 + dtype + crs + transform identical, docstring verbatim (693 chars),
   bad-bbox + over-4deg^2 both -> COPERNICUS_DEM_BBOX_INVALID on both sides.

2. **fetch_gcn250_curve_numbers FOLDED via `direct_window` + a skip-HEAD transport
   enabler (LANDED).** GCN250 (Jaafar 2019) lives behind a figshare `ndownloader`
   URL that 302-redirects to S3 AND 403s on HEAD -- the two edges ADR-0047 named
   ("redirect-follow", "single-COG host"). Machinery, all no-op for priors:
   - TRANSPORT skip-HEAD recovery (`preflight`): a 403/405 HEAD now attempts a
     range-GET size probe FIRST; a 206 there proves the object is range-readable
     and yields the size, so the read proceeds (figshare's redirected S3 object is
     range-readable while its `ndownloader` HEAD 403s). Only when the range GET
     ALSO fails does the classified auth/not-found error stand -- purely additive:
     every prior COG host answers HEAD, so the branch is never reached. (The pooled
     httpx client already followed redirects, ADR-0044; that closes the 302 edge.)
   - `direct_window` gains `url_by_param` (enum -> object URL: AMC dry/average/wet
     -> the three figshare files), `round_pixel_window` (outward floor/ceil integer
     -pixel rounding + clip-to-extent, reproducing the twin's window math to the
     pixel), and `nodata_gate` (an all-nodata window -> typed EMPTY, the honesty
     floor -- over open water / off-coverage), plus the shared `serialize`
     (`{nodata: 255, dtype: int16}`) for the int16 CN output. Live edge-matrix PASS
     22/22: value-identical arrays across ALL THREE AMC levels (maxabsdiff=0 over a
     Bangladesh-delta bbox), dtype int16 + nodata 255 + transform identical;
     all-nodata ocean window -> GCN250_EMPTY both sides; bad enum + bad bbox ->
     GCN250_INPUT_INVALID. SFINCS/HydroMT consumer re-pointed:
     `sfincs_forcing_autowire` resolves `fetch_gcn250_curve_numbers` through the
     registry seam (was a direct twin import).

3. **Remaining ADR-0047 enablers STOPPED with evidence (not attempted / not
   closable this wave).**
   - **chirps_precipitation**: date-templated `.tif.gz` (two path patterns) +
     mandatory gzip-decompress + threshold nodata collapse + bbox=None global. A
     gzipped object is NOT byte-servable as a windowed COG (the coalescing range
     opener feeds GDAL a COG; a gzip stream has no windowable COG layout), so this
     needs a distinct whole-object-GET + gunzip + in-memory-open mode -- NOT the
     skip-HEAD/redirect enabler this wave built. Deferred.
   - **hrsl_population / soilgrids (VRT-fanout, ADR-0047's highest-leverage single
     enabler)**: the transport opener still serves a SINGLE URL; a multi-tile
     `.vrt` needs the VRT-fan-out / multi-URL opener (ADR-0047 LIVE probe: the
     single-URL opener reads the VRT grid but returns ALL-NaN). soilgrids
     additionally needs a Homolosine->4326 reproject + per-property scale branch.
     NOT built this wave. Deferred (soilgrids gated on hrsl's opener per the
     kickoff).
   - **noaa_sst (griddap)**: needs the ERDDAP bracket-selector `griddap` mode +
     404-body EMPTY/UPSTREAM disambiguation. NOT built; additionally the CoastWatch
     ERDDAP host was UNREACHABLE from the build environment (HEAD timed out at 20s),
     so a live parity gate could not be run even had the mode been built. Deferred.
   - **jrc_global_surface_water (colormap-ramp DSL)**: gated on items 1-4 landing
     "with room"; a declarative colormap-ramp language for a single current
     consumer dies by stop-rule (one-consumer DSL). Deferred.

Registry accounting: registry total stays 190 (two twins died, two spec-driven
surfaces took their names under `_router._promoted`). CODED tools 170 -> 168 (-2);
coded fetchers 79 -> 77 (-2); spec-served data sources 20 -> 22 (+2). Retrieval
index unshifted: both docstrings carried VERBATIM (copernicus 693, gcn250 3,279
chars via `inspect.getdoc`); both corpus phrasing sets rank their tool in the
model-free top-8 (copernicus 8/8, gcn250 6/6). Coverage migrated: the two twin test
files (392 + 650 = 1,042 lines) deleted; serialize + direct_window (url_by_param /
round_pixel / nodata_gate) unit coverage added to `test_router_executors.py`;
`test_catalog_surfacing` spec-served count 20 -> 22 (spec-served count is the
expected metric, not a regression). Seven fetch_dem copernicus-fallback tests
(`test_data_fetch.py` x6 + `test_compute_hillshade.py` x1) that patched the deleted
`_copernicus_dem_impl` / `_write_dem_cog` migrated onto the registry seam (a frozen
-RegisteredTool swap via `monkeypatch.setitem`) + `array_to_cog_bytes`. Twin py
removed: 1,053 lines (512 + 541);
router+transport+schema engine grew ~58 lines of NEW capability. Daemon import
clean. Offline suite FAILED set unchanged at the baseline 9 (test_fetch_resolution
_gate x4 + test_run_river_dye_scenario x5). FLOOD CANARY: run_sfincs_direct PASS
(status=ok + depth COG) after the fetch_dem / sfincs_forcing_autowire re-points.

Consequence: the router's raster surface now carries a redirect + skip-HEAD single
-object read path (any 302/HEAD-403 host with a range-readable object) with
enum-URL selection, integer-pixel window rounding, an all-nodata coverage gate, and
an int16/sentinel serialize directive, plus continuous-float STAC serialize-nodata
for the DEM class -- each proven value-identical on live data. The copernicus
re-point retires the last cross-consumer coupling ADR-0053 flagged. Supersedes
nothing; executes the copernicus re-point (ADR-0053 point 3) and the first of the
ADR-0047 mode-enablers, extending the wave-4/5/6/7 fold-some-defer-rest precedent.
