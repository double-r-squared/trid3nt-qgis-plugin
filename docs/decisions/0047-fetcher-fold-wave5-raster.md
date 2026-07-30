# 0047 - fetcher fold wave-5 raster: STAC-tile transport migration + raster-family defer

Context: Phase-2 wave-5 opened the RASTER-COG families on the new httpx transport
(ADR-0044: `_router/transport/` owns every remote-file socket, GDAL parses only).
Two scopes: (1) fold the raster twins whose parity closes via the existing router
modes (`opendap` / transport-backed `direct_window` / `stac_search`) or a small
declarative extension strictly no-op for prior specs; (2) migrate the
`stac_search` tile read off the last `/vsicurl/` residual onto the transport.
Eleven fold candidates were read IN FULL (wave-4's binding lesson: the audit label
is a prior, not a verdict): chirps, jrc_global_surface_water, modis_lst,
copernicus_dem, ghsl_population, hrsl_population, noaa_sst, gcn250_curve_numbers,
landfire_fuels, usfs_canopy_fuels, soilgrids.

Decision (2026-07-29):

1. STAC-TILE SEAM MIGRATED (scope 2, LANDED). `raster_cog._read_tile_window` now
   reads a remote https tile href through `transport.open_windowed_cog` instead of
   GDAL `/vsicurl/`; local paths (cached/test COGs) still open directly. The
   reproject body is unchanged, so output is byte-identical; a transport error maps
   to the typed router upstream frame. Proof: offline unit tests (local-vs-transport
   byte-identical array + colormap; forced 404 -> typed `RouterUpstreamError`);
   LIVE byte-identical pixels + identical 256-entry colormap on a real Planetary
   Computer `io-lulc-annual-v02` tile (the exact collection `fetch_esri_landcover_10m`
   consumes); LIVE end-to-end `fetch_esri_landcover_10m` route emits a valid uint8
   categorical COG (classes 1-11, EPSG:4326, nodata 0). This closes the ADR-0044
   `/vsicurl/` residual for the only promoted `stac_search` source.

2. ALL 11 RASTER FOLD CANDIDATES DEFERRED with evidence (scope 1). The full reads
   plus a live transport probe show NONE closes byte-identical via the existing
   modes or a small no-op extension; each needs distinct NEW ingestion machinery,
   which is a genuine parity gap (STOP RULE: defer with evidence, never force). The
   binding constraint is that the transport opener serves a SINGLE URL, and every
   `direct_window` raster candidate is a multi-tile VRT, a gzipped object, or a
   redirecting host -- none a plain single remote COG:
   - hrsl_population: multi-tile `.vrt`. LIVE probe: the single-URL opener reads the
     VRT grid but returns ALL-NaN (it cannot fan out to the VRT's sub-tile sources,
     which the opener re-serves as the VRT bytes); `/vsicurl/` returns real values.
     Needs a multi-URL / VRT-fan-out transport opener.
   - soilgrids: multi-tile `.vrt` PLUS Homolosine->EPSG:4326 `rasterio.warp.reproject`
     (bilinear, densified edges) + per-property scale divisor + nodata remap. Needs
     VRT fan-out AND a reproject/scale `direct_window` branch.
   - gcn250_curve_numbers: single figshare `.tif`, but the ndownloader URL 302-redirects
     and is not openable via `/vsicurl/` at all (LIVE probe: RasterioIOError). Also
     int16 GTiff-tiled output + enum->URL selection + default-nodata-255 -- not
     byte-identical to a float32-COG `direct_window`. Needs redirect-follow +
     dtype/nodata/driver directives + enum-keyed endpoint selection. (SFINCS consumer:
     `sfincs_forcing_autowire` -- a do-not-regress leg; extra reason not to force.)
   - chirps_precipitation: date-templated `.tif.gz` (two path patterns by period enum)
     + mandatory gzip-decompress + value-threshold nodata collapse (<= -9000 -> -9999)
     + supports_global_query bbox=None. Needs URL templating + gzip + threshold-collapse
     steps.
   - jrc_global_surface_water: uint8-mosaic shape (near esri) but bespoke COMPUTED
     per-band colormaps (blue ramp / 12-step seasonality / diverging change) + asset-key
     selected by the `band` param + a custom sign endpoint with 4 retries. Needs a
     declarative colormap-ramp language + asset-by-param + sign-strategy knob.
   - modis_lst: STAC continuous-FLOAT output (DN*0.02-273.15 degC, NaN nodata, no baked
     palette) + a most-recent-single-item selection mode (not the intersect-all mosaic)
     + two-tier per-href sign. Needs a continuous-float STAC branch AND a latest-item
     selector.
   - copernicus_dem: STAC continuous-FLOAT mosaic (meters, nodata sentinel -9999) via the
     existing intersect-all loop but the uint8/palette write is wrong for it. Needs the
     continuous-float STAC branch. (Also a deprecated delegate; canonical path is
     `fetch_dem(source="copernicus")`.)
   - landfire_fuels / usfs_canopy_fuels: ArcGIS ImageServer `exportImage` REST (not a
     static COG) + an all-nodata coverage gate. Needs a new `imageserver_export` access
     mode (shared by both) + a declarative nodata-coverage-gate flag. (landfire is a
     `run_elmfire` consumer.)
   - ghsl_population: fixed 10-deg global tile grid opened via `/vsizip//vsicurl/`
     (zip-member read, EXPLICITLY the next wave's mode) + GDAL-side networking (against
     ADR-0044) + per-tile pixel gate + an unprefixed `BBOX_REQUIRED` error code the spec
     schema cannot emit. Needs a new `fixed_tile_grid` mode + the zip-member transport
     mode.
   - noaa_sst: NOAA CoastWatch ERDDAP `griddap` bracket-selector REST GET (single
     timestep, no time-average) + 404-body-substring EMPTY/UPSTREAM disambiguation. NOT
     the `opendap` (pydap) mode. Needs a new `griddap` access mode.

No twin was deleted, no `source.yaml` added, no registration changed: registry stays
190, retrieval index unshifted (no docstring/corpus moved), every prior spec byte-for-
byte unchanged. Two schema nits surfaced for a future spec-author pass (recorded, not
acted): `PayloadEstimateSpec` has no ceiling field (usfs clips [0.05, 50]); no escape
hatch for an unprefixed error_code (ghsl/hrsl/gcn `BBOX_REQUIRED`).

Consequence: the wave delivers the STAC-tile transport migration (scope 2, byte-identical
proven) and an empirically-grounded raster fold roadmap. The raster family's fold does
NOT reduce to the existing modes; it needs, in rough leverage order, (a) a VRT-fan-out /
multi-URL transport opener (unlocks hrsl, soilgrids, and any VRT COG source -- the
highest-leverage single enabler), (b) an `imageserver_export` mode (landfire + usfs,
shared), (c) a continuous-float STAC branch (modis, copernicus_dem), (d) a `griddap`
mode (noaa_sst), (e) redirect-follow + dtype/nodata directives for single-COG hosts
(gcn250, chirps gzip), (f) a declarative colormap-ramp language (jrc). Each is its own
scoped, NATE-methodology-signed job; none is a "small no-op extension" foldable this
wave without forcing a non-byte-identical substitution on live data (several of them
SFINCS/ELMFIRE do-not-regress legs). Supersedes nothing; extends ADR-0044 (transport)
and the wave-4 fold-1-defer-many precedent (ADR-0045).
