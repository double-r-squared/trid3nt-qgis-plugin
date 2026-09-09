# Fetcher fold - conformance table

An adversarial, fresh-eyes pass over the wave `2aa60191..b2daea6e`, checked
against its two authorities read verbatim first: the charter
`docs/validation/fetcher-fold-census.md` and the rulings in `docs/IDEAS.md`
("STAC FOLD", "FETCHER FOLD, SECOND HALF", "FETCHER FOLD RULED", "FOLD STAGE 0
RULINGS", "FOLD VECTOR STAGE", "FOLD HYDRO STAGE + RULINGS").

Every number is measured on this machine in `venvs/agent` at HEAD `b2daea6e`.
Deviations are REPORTED, never fixed here. Evidence:
`/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/fetcher-fold/verify/`.

Method for the fate column: every in-scope spec's executor and access mode
resolved live through `router.select_executor`, and its co-located `hooks.py`
counted by `wc -l` at both revisions.

---

## 1. Every family in the census

Key: **FOLDED** - the family moved to the library the census named.
**RESIDUE(<n>)** - it moved and `<n>` measured lines of source vocabulary
survive. **STAYED(<reason>)** - it did not move, on a ruled reason.

| # | family | specs | census verdict | measured fate | evidence |
|---|---|---:|---|---|---|
| F1 | STAC (Planetary Computer) | 8 (+1 new) | fold to odc-stac | **FOLDED, RESIDUE(92)** | 6 rows on `stac_raster`, 3 on `tiled_mosaic` over the same executor; `raster_cog` 2,846 -> 1,820; the JRC colour table (92) survives whole as the census predicted |
| F2 | Zarr / OPeNDAP | 4 | STAY (already xarray) | **STAYED(already migrated in-hook)** | `hrrr` 346, `aorc_precip`/`gridmet` unchanged; byte-identical to pre-wave |
| F3 | ERDDAP griddap | 1 | STAY | **STAYED(url builder + no-data markers are source vocabulary)** | `fetch_noaa_sst` on `raster_cog:griddap`, unchanged |
| F4 | ArcGIS image services | 5 | STAY | **STAYED(no driver speaks exportImage)** | 5 rows on `imageserver_export` / `mapserver_export`, unchanged |
| F5 | OGC WCS 1.0.0 | 1 | STAY | **STAYED(hook is pure compute, no I/O to fold)** | `fetch_landcover` hook 135, unchanged |
| F6 | bulk object / VRT / tile grid | 12 | STAY | **STAYED(folding costs the typed transport)** | all 12 still on `transport/range_file.py`; `transport/` 820 unchanged |
| F7 | library-delegate rasters | 4 | STAY | **STAYED(already the owner)** | `fetch_dem`, `3dep_extra`, `statsgo`, `era5` byte-identical |
| F8 | bespoke S3 composites | 3 | STAY, one deferred residue | **STAYED(ruled closed)** | `topobathy.py` 1,933 + `topobathy_class.py` 356 + `bluetopo` byte-identical |
| F9 | animation frames | 6 | STAY | **STAYED(pre-rendered tiles / point binning)** | `goes_animation` 377, `goes_archive` 276, `glm` 472 unchanged |
| G1 | ESRI `/query` | 15 | fold to the ESRIJSON driver | **FOLDED 12 of 15, RESIDUE(958)** | 11 rows on `vector_ogr`; `fetch_fema_nfhl_zones` folded to pygeohydro instead (hydro stage); 3 STAYED - see the deviations |
| G2 | OGC API Features | 1 | **FC** on the OAPIF driver | **STAYED(the driver cannot express the declared `state_code` selector)** | `fetch_usgs_groundwater_levels` still `chained_resolution`, hook 244 unchanged; ledger row with CONDITION |
| G3 | Overpass | 6 | fold to OSMnx | **FOLDED, RESIDUE(800)** | shared `overpass.py` 733 DELETED, `hooks/osm.py` 174 born, the six rows carry 626 of their own vocabulary; family 962 -> 800 |
| G4 | bespoke JSON / CSV | 18 | NO FOLD except storm-tracks | **STAYED, one leg FOLDED(-13)** | 16 of 18 byte-identical; `storm_tracks` 1,004 -> 991 (the ruled zip leg); `openfema` + `firms` differ by comment-only lines from the scope-move residue commit |
| G5 | `dataretrieval` delegates | 2 | fold the NLDI half to pynhd | **STAYED(pynhd's NLDI is broken at the pin pygeohydro caps)** | `dataretrieval_delegate.py` 431 unchanged; ledger row with CONDITION |
| G6 | US hydro, hooked | 2 | fold both to pygeohydro | **1 FOLDED RESIDUE(340), 1 HELD** | `high_water_marks` 354 -> 340 on `STNFloodEventData`; `usgs_nwis_gauges` byte-identical (held for calibration, as ruled) |
| G7 | library delegates, non-raster | 3 | fold NWM's NLDI half | **STAYED(same pynhd finding as G5)** | `noaa_nwm_streamflow` 694 -> 692; `gtsm`, `field_boundaries` byte-identical |
| G8 | NOAA CO-OPS | 3 | STAY | **STAYED(no ruled library reaches CO-OPS)** | `station_timeseries.py` 552 byte-identical |
| G9 | record (LTER, SLIDER) | 2 | STAY | **STAYED(EML entity resolve; a live index)** | `lter_records` 372, `slider_timestamps` 92 unchanged |
| G10 | TIGER zipped shapefile | 1 | fold to `/vsizip//vsicurl/` | **FOLDED, RESIDUE(98)** | `zip_vector.py` 187 DELETED; the URL planner survives at 98 as predicted |
| - | `_pc_stac.py` | - | pay the debt | **PAID** | module DELETED; `bbox_pixel_dims` -> `_fetch_common`, the shared search -> `processing/_pc_search.py` (100) beside its consumers |
| - | `transforms/join.py` | - | travels with the attic | **DELETED** | 342 lines, zero remaining consumers |

---

## 2. Every ruling in "FETCHER FOLD RULED"

| # | ruling | verdict | evidence |
|---|---|---|---|
| 1 | HyRiver ADOPTED, pinned `pygeohydro==0.19.4` + `pynhd==0.19.3` | **DEVIATES (superseded)** | `pygeohydro==0.19.4` declared; `pynhd` NOT declared - dropped as a direct dep because nothing imports it after FOLD STAGE 0 (b) put the NLDI halves back on dataretrieval. `pynldas2==0.19.3` declared in its place. The deviation is from the ruling's text and is authorized by NATE's own later Stage 0 ruling |
| 1a | its response cache given an explicit expiry | **CONFORMS** | `hooks/hyriver.py` `_CACHE_EXPIRE_S = 3600`, the shortest router TTL window, under `$TRID3NT_RUNS_DIR/hyriver-cache` |
| 1b | `status_to_retry=(429,500,502,503,504)` at every call site | **DEVIATES (measured, better)** | the census's cell assumed `pygeoogc.RetrySession`; Stage 0 measured that pygeohydro's HTTP layer has no retry at all and returns a JSON-bodied 4xx as data. One shared shim (`hyriver_call`, 160 lines) carries the same status set plus a verbatim raise. Every one of the 3 call sites imports it - verified: `fetch_nldas2_forcing`, `fetch_fema_nfhl_zones`, `fetch_high_water_marks` |
| 1c | the deciding reason: NID's tables and STN's query set | **HALF FALSIFIED, reported** | the wave measured that we never hand-carried NID's tables, so `fetch_usace_dams` was REJECTED with that measurement. The STN half held |
| 2a | STAC rasters through GDAL's HTTP gated on Probe A | **CONFORMS, with the trade ledgered** | probe re-run here: `RasterioIOError: HTTP response code: 404 / 409` verbatim; the S3 `<Code>` body lost, `Retry-After` ignored. Both clauses ledgered with measurements at `docs/REANALYZE_LEDGER.md` |
| 2b | ESRI on the ESRIJSON driver gated on Probe B | **CONFORMS** | 11 rows on `vector_ogr`; `pyogrio.set_gdal_config_options` used, not `rasterio.Env` (the two-GDAL finding honored) |
| 2c | Overpass on OSMnx, 55 s documented, mirror wrapper, verbatim-error hook | **CONFORMS, plus one refusal** | `hooks/osm.py` 174: the 3-mirror chain under a lock, the raise-on-status wrapper, and a `_MAX_THROTTLED = 3` ceiling the ruling did not ask for, because the library's 429 retry is an unbounded recursion. The extra refusal is ledgered |
| 3 | `fetch_usgs_nwis_gauges` HELD; `high_water_marks` folds; CO-OPS does not | **CONFORMS** | gauges + all three CO-OPS rows byte-identical to pre-wave; HWM on pygeohydro |
| 4a | `fetch_dem` OUT of both folds for good | **CONFORMS** | byte-identical; no STAC substitution |
| 4b | no source whose bytes would change | **CONFORMS** | GOES ABI, NLCD landcover, LANDFIRE, MODIS mirrors all untouched at their own sources |
| 4c | the bespoke JSON family does NOT fold except the storm-tracks zip leg | **CONFORMS** | 16 of 18 byte-identical; storm_tracks -13 |
| 4d | no `noaa-coops` dependency | **CONFORMS** | absent from `pyproject.toml` |
| 4e | topobathy's warp-merge core stays closed | **CONFORMS** | byte-identical |
| 4f | the fold PAYS `_pc_stac.py`'s debt | **CONFORMS, and larger than ruled** | the census counted two consumers; five were found and all five moved; module deleted |
| 4g | the join transform travels with the scope attic | **CONFORMS** | deleted at `3368d71b` |
| - | provenance rows stay on the source row | **CONFORMS** | `normalize`, `style`, `ttl_class`, `vertical_datum`, `resolution` untouched across every folded spec |
| - | registered tool count unchanged by every fold commit | **CONFORMS** | see section 4 |

---

## 3. Parity, re-run live by the verifier against the PRE-FOLD captures

Two raster and three vector rows, re-fetched at HEAD and compared against the
artifact frozen before the hook was deleted.

| spec | family | grid / schema | result |
|---|---|---|---|
| `fetch_copernicus_dem` | F1 STAC float | identical 369x328 f32, nodata -9999 both | **PIXEL IDENTICAL** 0/121,032 |
| `fetch_esri_landcover_10m` | F1 STAC mosaic | identical 1108x985 u8, colormap identical | **PIXEL IDENTICAL** 0/1,091,380 |
| `fetch_administrative_boundaries` | G10 `/vsizip//vsicurl/` | 2/2 rows, 18/18 columns, dtypes equal | **GEOMETRY EXACT** 2/2 at 1e-9 |
| `fetch_osm_coastline` | G3 OSMnx | 78/78 rows, 3/3 columns equal | **GEOMETRY EXACT** 78/78 at 1e-9 |
| `fetch_nwi_wetlands` | G1 ESRIJSON | 2,683/2,683 rows, 3/3 columns equal, union bounds equal to the last digit | **DIFFERS as ledgered** - union symmetric difference 1.85e-08 of area, interior-ring count 1,926 both sides, all 2,683 exterior windings flipped |

The NWI difference is exactly the one the wave stated: `f=json` carries full
float64 where the GeoJSON writer rounded, and the driver rewinds exteriors. Two
naive comparators disagree about it and both are wrong - a sort-by-area pairing
mis-pairs features whose area moved by 1e-11, and `equals_exact` reads a winding
flip as a difference. The pairing-free measure is the union symmetric
difference, and it is 1.85e-08.

## 3a. Retrieval, re-measured model-free

Every one of the 31 folded or new rows was re-ranked against its OWN
`corpus.yaml` phrasings with the model-free retriever (BM25 + local dense +
name-substring RRF). 29 of 31 reach the top ten on every one of their prompts;
`fetch_buildings` is 10/10 in the top ten and 8/10 at rank 1. The two exceptions
are in section 9 and neither is the fold's: the fold moved `ingest.*` blocks
only, and no corpus file or tool description in the wave's diff changed.

## 4. Counts

| thing | wave start | HEAD | source |
|---|---:|---:|---|
| registered tools, as the DAEMON loads them | 161 | **163** | `logs/agent.log` "tool registry loaded" at 2026-09-08 21:18 and at 2026-09-09 09:16 |
| registered tools, as `import trid3nt_server.tools` loads them | 159 | **161** | `len(TOOL_REGISTRY)`; this denominator structurally omits `search_data_catalog` and `fetch_from_catalog`, which register only via `main.py`'s startup path |
| `source.yaml` specs | 97 | **99** | `git ls-tree` at both revisions |
| `@register_tool` decorator sites | 60 | 60 | `git grep -c` at both revisions |

The +2 is `fetch_opera_dswx` (`df6e92f1`) and `fetch_nldas2_forcing`
(`f80753b7`), both NEW tools the rulings called for, neither a fold. No fold
commit added or dropped a tool: the wave diff contains no added or removed
`register_tool` / `AtomicToolMetadata` line, and exactly two `source.yaml` files
were added and none deleted.

## 5. Hooks remaining, and each one's owner

`_router/hooks/` after the fold, 4,720 lines including the 222-line loader:

| module | LOC | owner | family |
|---|---:|---|---|
| `topobathy.py` | 1,933 | `fetch_topobathy` (+ `fetch_bluetopo` imports it) | F8, ruled closed |
| `cds.py` | 680 | `fetch_era5_reanalysis` (425) + `fetch_gtsm_tide_surge` (255) | F7 / G7, STAYED |
| `goes_animation.py` | 377 | `fetch_goes_animation`, `fetch_goes_blend_animation` | F9, STAYED |
| `topobathy_class.py` | 356 | the `water_body_class` feed | F8, STAYED |
| `hrrr.py` | 346 | `fetch_hrrr_forecast`, `fetch_hrrr_smoke` | F2, STAYED |
| `goes_archive.py` | 276 | `fetch_goes_active_fire`, `fetch_goes_archive_animation` | F9, STAYED |
| `__init__.py` | 222 | the tree-walking loader | infrastructure |
| `pfdf_raster.py` | 196 | `fetch_3dep_extra`, `fetch_statsgo_soils` | F7, STAYED |
| `osm.py` | **174** | the six OSM rows | G3, NEW - the fold's mirror chain + error hook |
| `hyriver.py` | **160** | `fetch_high_water_marks`, `fetch_fema_nfhl_zones`, `fetch_nldas2_forcing` | G6, NEW - the one shim |

Executors: `zip_vector.py` DELETED (187); `vector_ogr.py` NEW (316);
`stac_raster.py` NEW (875); `vector_fgb.py` 722 -> 501 (the serializer half);
`raster_cog.py` 2,846 -> 1,820.

## 6. LOC, measured, never estimated

`wc -l` over `git archive` of both revisions:

| tree | wave start | HEAD | delta |
|---|---:|---:|---:|
| `trid3nt_server/**/*.py` | 135,416 | 134,778 | **-638** |
| `trid3nt_server/tools/fetchers/**/*.py` | 31,160 | 30,417 | **-743** |
| co-located `hooks.py` | 12,053 | 12,532 | +479 |
| `_router/hooks/*.py` | 5,119 | 4,720 | -399 |
| `_router/executors/*.py` | 5,652 | 5,392 | -260 |
| `tests/**/*.py` | 103,943 | 104,956 | +1,013 |
| `trid3nt_server/**/*.yaml` | 14,925 | 15,244 | +319 |

Against the census's projected **-3,490**, the honest product number is
**-638**, of which +301 is `fetch_nldas2_forcing`, a new capability rather than
a fold; the fold-only figure is about **-939**, 27 percent of the projection.
The wave states this itself, per stage: raster -260, vector -402, hydro +120.
The gap is not a measurement dispute; it is named in the three stage records.

## 7. Deviations, reported

1. **`pynhd` is not a declared dependency**, though "FETCHER FOLD RULED" pins
   it. Authorized by "FOLD STAGE 0 RULINGS" (b). The census's G5/G7 deltas
   (-130, -180) were therefore not taken.
2. **G2 did not fold.** `fetch_usgs_groundwater_levels` was **FC** in the
   census; the OAPIF driver cannot express the declared `state_code` selector,
   so the hook stays and the census's -174 was not taken. Ledgered with a
   CONDITION.
3. **Three G1 rows did not fold** (`fetch_usace_dams`,
   `fetch_epa_frs_facilities`, `fetch_wfigs_incident`; 679 of hook LOC). The
   census called the first two **RES** and the third **S**; the wave states the
   contract extensions the first two would need rather than building them.
4. **`fetch_fema_nfhl_zones` grew**, 192 -> 279 (+87). The census called it
   **FC**. The service degrades under a wide read (9,894 polygons: the old
   cursor returned 6,000 and called it complete), so the row now tiles and
   reports what it loses. That is a correctness gain bought with LOC.
5. **`fetch_high_water_marks`'s residue is 340, not the census's ~120.** Stated
   in the hydro record as -14.
6. **The G3 family measures -162, not the census's -872.** `overpass.py`'s 733
   died, but the six rows' own vocabulary (626) is not boilerplate. Stated.
7. **Two upstream-error clauses are traded on both GDAL paths** - the S3
   `<Code>` body and `Retry-After`. Ledgered with source-level confirmation.
8. **A hard 500 on an ESRI row costs two retry ladders.** Measured here: the
   driver's own five attempts (1.0, 2.4, 5.3, 12.7, 30.5 s) exhaust, and then
   `_verbatim_upstream` re-reads the same URL through `transport/client.py`,
   which runs its own five. About 52 s plus the transport's ladder before the
   caller sees a typed error. Correct, and not stated anywhere.
9. **A count claim in the wave's own summary inverts a correct record.** The
   hydro-stage report's 163 is right; the later claim that `len(TOOL_REGISTRY)`
   "measures 161 both before and after" compares a different denominator (the
   in-process import, which omits the two startup-registered catalog tools)
   against it and calls the report the error.

## 8. The live gates

The daemon was restarted at HEAD (the running one had booted on 2026-09-08 code),
`scripts/ws_smoke.py` reports `all_passed=True`, and both canaries were driven
through it to a verified packet:

| canary | verdict | deliverables | pin |
|---|---|---:|---|
| `telemac_rain_on_grid` (coarse, 60 mm/h design storm) | **PASS** | 13, none missing | **27 of 29 metric keys byte-identical** to the 2026-09-03 pin; the two that move are per-run URIs |
| `telemac_river_dye_refined` (Eel River near Scotia) | **PASS** | 13, none missing | the 2026-08-26 pin no longer matches the declaration - see below |

Both packets carry a folded vector row and a folded raster row under them:
`fetch_river_geometry` (OSMnx) and `fetch_nhd_area_water` (ESRIJSON) as input
panels, `fetch_copernicus_dem` (odc-stac) as the river bed. Interrogated before
delivery: every panel's scalar agrees with the run's own answer
(`dye_cmax_mgl` 97.4317 = the panel's 0-97.43 ramp; `max_depth_peak_m` 9.9493 =
the depth panel's 0-9.949; `mesh_node_count` 6,615 / 12,974 = the mesh panel),
the DEM's dark valley registers with the NLDI centreline and the mapped water
polygon, the mesh is drawn as a wireframe, each panel declares its own framing
against the canvas extent, and the dye plume discriminates rather than painting
a uniform field.

## 9. Out of scope, found while driving the live gates

Reported, not fixed. None is inside `git diff 2aa60191..HEAD`.

1. **A categorical land-cover layer now reaches the canvas with no legend.** The
   SAME cached object
   (`s3://trid3nt-cache/cache/static-30d/landcover/c0ae94543d133fd61aecf0a444e335f5.tif`)
   published a `categorical` legend of 175 classes in the 2026-09-03 pin and
   publishes `legend: null` today, so the proof panel paints NLCD class codes as
   a continuous grey ramp labelled 30-80 while its own caption says "already
   painted (the file carries its colours)". `fetch_landcover` is byte-identical
   to the wave's start; the emission/legend seam last moved on 2026-09-04
   (`2ad07c18`, `9a37c40c`). This is the honesty floor's own case - a classed
   raster rendered as a continuous one - and it is live now.
2. **A declared mesh lever is not honoured.**
   `canaries.py:311` declares `mesh_resolution_m: 10.0` for
   `telemac_river_dye_refined`; the run answers at `mesh_size_m` 7.763 and
   reports `mesh_resolution_m: null`. Its metrics therefore cannot be compared
   against the frozen 2026-08-26 pin (`dye_cmax_mgl` 41.4 -> 97.4,
   `plume_reach_m` 645.1 -> 56.7), so that lane is not a silent pin until it is
   re-pinned. Nothing in this wave touches mesh code.
3. **Every TELEMAC case run mints its Dispatch/Sim observability cards labelled
   `telemac_river_dye`**, because `TELEMAC_SOLVER_NAME` is that string
   (`workflows/telemac/solving/run_telemac.py:33`) and `solve.py:199-204` passes
   it as the solver identity for every template. A `telemac_rain_on_grid` run
   shows "Dispatch telemac_river_dye solve". Predates this wave (`b006cf3a`,
   2026-09-06).
4. **Two retrieval gaps, neither moved by the fold.**
   `fetch_overpass_pois` loses 3 of its own 8 corpus phrasings out of the top
   ten ("show me the hospitals in this area", "where are the fire stations near
   this flood zone"); its corpus and description are byte-unchanged by the wave,
   so this is the corpus, not the fold. And `fetch_copernicus_dem` is the only
   one of the 99 specs with no `corpus.yaml` at all - also true at the wave's
   start.
