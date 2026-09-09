# Fetcher fold, the raster half - the STAC executor and its eight consumers

Charter: `docs/validation/fetcher-fold-census.md` (F1 + the shared raster spine
+ `_pc_stac`). Rulings: `docs/IDEAS.md` "STAC FOLD", "FETCHER FOLD RULED",
"FOLD STAGE 0 RULINGS" (2026-09-09). Stage 0's measurements:
`docs/validation/fetcher-fold-stage0.md`.

Every number here is measured on this machine in `venvs/agent`. Parity artifacts
and probe scripts live under the wave's evidence directory; each pre-fold COG was
captured from the CURRENT HEAD before its spec was touched.

## 1. What landed

One executor, `_router/executors/stac_raster.py`, serving one access mode
(`ingest.access: stac`) and three renders:

| render | what it produces | consumers |
|---|---|---|
| `float` | a physical-scalar float32 grid (DN scale/offset, dB, positive-only) | copernicus_dem, modis_lst, sentinel1_sar |
| `mosaic` | a uint8 first-valid mosaic with a palette baked into band 1 | esri_landcover_10m, jrc_global_surface_water, opera_dswx |
| `rgb` | a 3-band photometric-RGB uint8 composite | naip, sentinel2_truecolor, landsat_imagery |

The library owns search, signing, the destination grid and the pixel fuse
(`pystac_client` + `planetary_computer.sign_inplace` + `odc.stac.load` +
`odc.geo.GeoBox`). What survives is the spec vocabulary - which collection,
which asset, which scene - and the band math no catalog does.

`raster_cog.py` keeps every transport-bound mode. Measured: the VRT parser and
the multi-URL mosaic STAY - `access: multi_url` (1 spec) and
`projected_vrt_window` (1 spec) are F6 rows, and F1 used neither.

## 2. LOC, measured

| file | before | after | delta |
|---|---:|---:|---:|
| `_router/executors/raster_cog.py` | 2,846 | 1,820 | **-1,026** |
| `_router/executors/stac_raster.py` | 0 | 875 | +875 |
| `fetchers/imagery/_pc_stac.py` | 262 | 0 (deleted) | **-262** |
| `tools/processing/_pc_search.py` | 0 | 100 | +100 |
| `fetchers/_fetch_common.py` (`bbox_pixel_dims`) | - | +25 | +25 |
| the eight `source.yaml` rows | - | - | +48 / -20 |
| **product total** | | | **-260** |

The 1,026 lifted out of `raster_cog.py` is 985 of STAC modes plus 31 of
`execute` branches plus the dispatch and docstring lines they carried. The
census projected -425 for F1 plus -25 for the spine plus -133 of `_pc_stac`;
the measured product delta is **-260**, because the new executor is 875 rather
than the census's ~560 estimate: the band math it lifts (the RGB transforms,
the QA/SCL masks, the joint stretch, the inferno ramp, the select ladders)
measures larger than the estimate, and the read path it adds carries its own
constraints in comments.

Registered tool count: **161 -> 162**, and the +1 is `fetch_opera_dswx`, a NEW
tool, not a fold. Every fold commit leaves the count unchanged.

## 3. Parity, per spec

Bar: pixel-identical against a PRE-FOLD fetch of the same request, or the
measured difference stated. Request bbox `[-82.55, 27.85, -82.45, 27.95]`
(Tampa Bay, 0.01 deg^2) for every row; date windows fixed so the pre and post
fetches see the same catalog.

| spec | request | grid | palette | pixels |
|---|---|---|---|---|
| `fetch_copernicus_dem` | metric grid | identical (369x328 f32) | n/a | **IDENTICAL** 0/121,032 |
| `fetch_copernicus_dem` | `px_per_deg=3600` | identical (361x361 f32) | n/a | **IDENTICAL** 0/130,321 |
| `fetch_esri_landcover_10m` | `year=2023` | identical (1108x985 u8) | identical | **IDENTICAL** 0/1,091,380 |
| `fetch_modis_lst` | `11A2`/`day`, Jun 2026 | identical (16x16 f32) | n/a | **IDENTICAL** 0/256 |
| `fetch_jrc_global_surface_water` | `band=occurrence` | identical (369x328 u8) | identical | **IDENTICAL** 0/121,032 |
| `fetch_jrc_global_surface_water` | `band=change` | identical (369x328 u8) | identical | **IDENTICAL** 0/121,032 |
| `fetch_sentinel1_sar` | `vv`/rtc, Apr-Jun 2026 | identical (1108x985 f32) | n/a | **IDENTICAL** 0/1,091,380 |
| `fetch_landsat_imagery` | `true_color`, H1 2026 | identical (369x328x3 u8) | n/a | **IDENTICAL** 0/363,096 |
| `fetch_landsat_imagery` | `thermal`, H1 2026 | identical (369x328x3 u8) | n/a | **IDENTICAL** 0/363,096 |
| `fetch_sentinel2_truecolor` | Apr-Jun 2026, cloud<30 | identical (1108x985x3 u8) | n/a | **DIFFERS** - see below |
| `fetch_naip` | default | identical (8192x8192x3 u8) | n/a | **DIFFERS** 551,435/201,326,592, max 7 DN - see below |

### 3.1 Wall time, same request, same machine

Single samples, so read them as an order of magnitude and not a benchmark. The
fold is FASTER on eight of nine rows: GDAL's own HTTP with multirange and merged
consecutive ranges beats the per-block coalescing opener on these reads.

| request | pre-fold | folded |
|---|---:|---:|
| copernicus_dem, metric | 8.1 s | 5.0 s |
| copernicus_dem, px_per_deg=3600 | 11.4 s | 5.8 s |
| esri_landcover_10m | 9.4 s | 6.9 s |
| modis_lst | 5.1 s | 5.3 s |
| jrc, occurrence | 9.3 s | 4.4 s |
| jrc, change | 8.0 s | 4.5 s |
| sentinel1_sar | 40.5 s | 13.0 s |
| landsat, true_color | 62.8 s | 8.6 s |
| landsat, thermal | 39.2 s | 6.3 s |
| sentinel2_truecolor | 82.1 s | 11.1 s |
| **naip** | **430.9 s** | **1,603.1 s** |

`fetch_naip` is the exception, measured at **3.7x** (7.2 min -> 26.7 min), and
the mechanism is named: its render reads three bands of ONE multi-band asset,
and the loader opens the source once PER BAND (each band is its own
`RasterSource`). The read it replaces opened once and read three bands from that
handle, sharing GDAL's block cache. On a pixel-interleaved uint8 source that is
three times the bytes over the wire, on the request that was already the slowest
raster row on the board. The fix, if it is worth spending: declare the asset's
bands as one extra dimension in `stac_cfg` so the loader treats it as a single
3-D band and opens once. NOT taken here - it is a change to how the fold uses the
library, past what the census recommended, and it wants a ruling rather than a
guess.

### 3.2 The two facts parity turned on

**Source nodata.** The pre-fold reads passed `src_nodata = src.nodata if
src.nodata is not None else <the spec's sentinel>` into
`rasterio.warp.reproject`, keeping the sentinel out of the resampling kernel.
`odc.stac.load` resolves source nodata from the asset header ALONE and has no
parameter for a declared fallback (`RasterLoadParams.src_nodata_fallback`
exists; `resolve_load_cfg` never sets it and `load()` cannot reach it).

Measured consequence, before the fix: JRC's COGs publish no header nodata, so
`occurrence` differed on 3,232 of 121,032 pixels (max 63) and `change` on
**72,865 of 121,032** (max 183) - a fill value blended into a class ramp.
`stac_raster._driver_with_src_nodata` carries the declared sentinel in on the
reader (a `RioDriver` subclass, 20 LOC, `dataclasses.replace(cfg,
src_nodata_fallback=...)`), and both bands are pixel-identical.

**Overviews.** `odc.stac.load` reads from an overview level when the
destination is coarser than the source; the pre-fold reproject always read the
full-resolution band. `use_overviews=False` is passed for every load, which is
what makes the resampled rows reproduce.

### 3.3 `fetch_sentinel2_truecolor` - stated, not identical

Measured: 336,575 of 3,274,140 band-pixels differ, max 202 DN. All of it is one
mechanism.

The selected scene (`S2B_MSIL2A_20260507T155819_R097_T17RLM`, 0.53% cloud)
covers only the TOP 21% of the AOI - rows 0-237 of 1108 hold every data pixel,
and everything below is nodata in both builds. Because the scene covers part of
the destination, `odc` reprojects into the OVERLAPPING SUB-WINDOW
(`compute_reproject_roi` -> `dst_geobox[roi_dst]`) while the pre-fold read
reprojected into the whole array. GDAL builds its approximate coordinate
transformer per warp call at a 0.125-pixel tolerance, so two warps of the same
UTM source into two different destination extents differ sub-pixel. Isolated on
one band (B04, no render): median relative difference **0.32%**, p95 **2.1%**,
and it is not a whole-pixel shift (rolling the reference by one pixel in any
direction makes the disagreement 16x worse).

On the DELIVERED 8-bit render that becomes: essentially every data pixel moves
by +-1 DN (the joint 2/98 stretch shifts with its inputs), and **217 of
1,091,380 pixels** (0.02%) cross the `all_bands_zero` nodata rule at the scene
edge, which is where the max-202 values live.

Why the other UTM rows are identical: `fetch_landsat_imagery`'s scene covers
the whole 369x328 destination, so `roi_dst` IS the full geobox and both builds
warp over the same extent. The difference appears only when a scene covers part
of the request.

This is the parity law's "a nodata mask / a resampling kernel" clause, stated
with its measurement rather than waved through. It is a property of GDAL's warp
approximation, not of the fold's arithmetic, and it is bounded by the 0.125-pixel
tolerance both builds use.

### 3.4 `fetch_naip` - the same mechanism, an order of magnitude smaller

551,435 of 201,326,592 band-pixels differ (0.27%), max 7 DN, on an 8192x8192x3
uint8 aerial image. Same cause as sentinel2: the NAIP quad covers part of the
0.1-degree destination, so the two builds warp over different destination
extents. There is no stretch on this render to amplify it, which is why the
difference stays at a few DN rather than reaching the scene-edge flips
sentinel2 shows.

## 4. The read path, and the norm clauses traded

Assets read through GDAL's HTTP client, configured once by
`odc.loader.configure_rio(cloud_defaults=True, GDAL_HTTP_MAX_RETRY=5,
GDAL_HTTP_RETRY_DELAY=1, GDAL_HTTP_RETRY_CODES="429,500,502,503,504")` - odc
opens its own rasterio env per read from a module global, so an enclosing
`rasterio.Env` does not reach it (Stage 0, A2).

Met: the upstream STATUS is verbatim on the exception, and retries fire and
recover on 429/503.
Traded, and ledgered at `docs/REANALYZE_LEDGER.md` with the measurement: the S3
XML `<Code>` body is lost, and `Retry-After` is not honored.

## 5. OPERA DSWx - the proving consumer, and why it refuses

`fetchers/hydrology/fetch_opera_dswx/` is a new zero-hook spec over the NASA
Earthdata STAC catalog (`https://cmr.earthdata.nasa.gov/stac/POCLOUD`), both
collections declared (`OPERA_L3_DSWX-HLS_V1_1.0`, `OPERA_L3_DSWX-S1_V1_1.0`),
the water band selected by the `_B01_WTR` suffix the two products share (their
asset keys are numbered differently: `1_B01_WTR` vs `0_B01_WTR`), 30 m stated
on the row, `vertical_datum: n/a`, `style: {kind: classed}`.

The V1 assets are EDL-gated (Stage 0, Probe D: a read is 401 to
`urs.earthdata.nasa.gov/oauth/authorize`; anonymous S3 is 403). Per the ruling
there is no live DSWx fetch this wave: `stac.sign: netrc:urs.earthdata.nasa.gov`
makes the spec REFUSE BY NAME, naming the file (`~/.netrc`), the host and the
`GDAL_HTTP_COOKIEFILE`/`COOKIEJAR` requirement, before any network call.
`tests/test_router_opera_dswx.py::test_live_dswx_fetch_over_a_us_basin` is the
Stage 0 cell and SKIPS with that reason until the credential exists.

New-tool law, met before acceptance: `corpus.yaml` carries 8 phrasings, and the
model-free retrieval check (BM25 + local dense + name-substring RRF, no LLM)
ranks `fetch_opera_dswx` **8/8 in the top ten and 7/8 in the top three**; the
one at rank 5 ("satellite observed inundation to compare against my model")
loses to `fetch_flood_extent_observation` and `extract_model_at_observations`,
both of which are genuinely closer to that phrasing.

## 6. `_pc_stac.py` - the debt paid, not declared

The census measured two consumers; there are five.
`compute_ndvi`, `digitize_water_body` and `compute_change_detection` used
`search_least_cloudy_item` + `sas_sign_href` + `bbox_pixel_dims` +
`VSICURL_ENV_KW`; `tools/search/ogc_adapter.py` and
`gates/cards/solver_confirm.py` used `bbox_pixel_dims` alone.

- The SAS half (`_request_sas_token` + `sas_sign_href` + the token cache, ~90
  LOC) DIES: `planetary_computer.sign_inplace` on the catalog client means the
  three processing tools receive already-signed hrefs, and all eight
  `sas_sign_href` call sites are gone.
- `bbox_pixel_dims` moves to `fetchers/_fetch_common.py`, beside the other pure
  bbox helpers, and the five consumers import it there.
- The search the three processing tools share moves to
  `tools/processing/_pc_search.py` (100 LOC) - beside its only consumers,
  rather than under `fetchers/` where nothing fetched it.
- `fetchers/imagery/_pc_stac.py` is DELETED.

## 7. Suite

Five slices from the repo root with `venvs/agent`, globs unquoted,
`env -u TRID3NT_CACHE_BUCKET`, `-p no:cacheprovider --timeout=300 -q`, each its
own invocation.

| slice | result |
|---|---|
| `tests/test_[a-e]*.py` | 1571 passed, 5 skipped (297.32s) |
| `tests/test_[f-o]*.py` | 4250 passed, 1 xfailed (154.68s) |
| `tests/test_[p-r]*.py` | 1682 passed, 2 skipped (160.54s) |
| `tests/test_[s-z]*.py` | 1469 passed, 5 skipped (510.86s) |
| `contracts/tests` | 425 passed (3.39s) |
| `plugin/tests` | 386 passed, 2 failed - the documented pre-existing Qt pair |
| `scripts/model_check.py --model docs/model/data-seam.sysml` | 0 findings |

ONE flake, isolated and cleared: `test_run_river_dye_scenario.py::
test_a_derived_release_sits_on_the_DECLARED_centerline` failed once in a `[p-r]`
run taken while the 27-minute NAIP parity fetch was saturating the connection.
Three isolation reruns pass (14 s, 26 s, and the whole file at 85 s), and a clean
`[p-r]` slice with nothing else on the wire is 1682 passed / 2 skipped.

## 8. Retrieval, unchanged

Corpus files and docstrings are byte-unchanged by the fold, so the only shift the
index can see is the +1 tool. Measured after, model-free (BM25 + local dense +
name-substring RRF): `fetch_esri_landcover_10m` 8/8, `fetch_modis_lst` 11/11,
`fetch_jrc_global_surface_water` 8/8, `fetch_naip` 8/8,
`fetch_sentinel2_truecolor` 8/8, `fetch_sentinel1_sar` 8/8 all in the top three;
`fetch_landsat_imagery` 8/8 in the top ten and 7/8 in the top three.

## 9. Not run here

The daemon restart, `scripts/ws_smoke.py` and the flood canary are live gates on
a running server with the MinIO environment; they are NATE's step, not an
agent's, and this wave did not touch a solver path.
