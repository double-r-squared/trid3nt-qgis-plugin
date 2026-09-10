# Fetcher fold census - the two-lens result, by protocol family

> Frozen record. Its section 6 acceptance list names `scripts/ws_smoke.py` and
> `scripts/run_sfincs_direct.py`: the first is now `scripts/instruments/ws_smoke.py`
> and the second left the tree with the SFINCS purge. The finding below is left
> verbatim; the live gate list is AGENTS.md law 1.

Read-only census over the fetcher tree at `/home/nate/Documents/trid3nt-local`,
HEAD `d243c0d5` ("docs: the lean sweep closed"). The two lens passes were taken at
`e492c740`; `git diff --stat e492c740 HEAD -- trid3nt_server/tools/fetchers/` is
empty, so every LOC below is current. Nothing edited in the tree but this file;
no commit, no checkout, no daemon.

Rulings this answers, read verbatim first: `docs/IDEAS.md:3965` STAC FOLD,
`:3984` FETCHER FOLD SECOND HALF, `:4030` SCOPE CENSUS RULED, `:4054` RESEQUENCED.

**Scope.** 108 `source.yaml` specs in the tree. The 13 scope-attic packages are
excluded: the 7 biodiversity fetchers (one of which, `biodiversity/fetch_mobi`,
is a raster-cog STAC spec that WOULD fold clean) and the 4 demographic specs
(`fetch_cdc_svi`, `fetch_census_acs`, `fetch_epa_ejscreen`, `fetch_lehd_jobs`);
the 2 movement-ecology tools live under `tools/processing/`, not here, though
their hook `_router/hooks/movebank_tracks.py` (278 LOC) travels with them.
**97 specs in scope**, every one placed in exactly one family below.

**Measurement basis.** `wc -l`, `__pycache__` excluded. Hook LOC is charged to the
family that owns the hook file; `cds.py` (680) is the one file split across two
families (the ERA5 leg 425 to F7, the GTSM leg 255 to G7). Executor LOC is charged
once, to each executor's dominant family, with two shared rows broken out so the
5,654 executor total reconciles exactly. "LOC after" is the measured surviving
residue, not an estimate of effort: for every folding family it is the sum of the
named blocks the lens passes proved survive, plus any new code the fold adds.

---

## 1. Headline table, by protocol family

| # | family | specs | hook LOC today | executor LOC today | the library that owns it | LOC after (measured residue) | delta |
|---|---|---:|---:|---:|---|---:|---:|
| F1 | STAC (Planetary Computer STAC v1) | 8 | 92 | 985 | **odc-stac + odc-geo + planetary-computer** | 652 | **-425** |
| F2 | Zarr / OPeNDAP gridded | 4 | 553 | 71 | xarray + rioxarray (ALREADY adopted in-hook) | 624 | 0 |
| F3 | ERDDAP griddap | 1 | 0 | 160 | xarray (already opens the body); erddapy not ruled | 160 | 0 |
| F4 | ArcGIS ImageServer / MapServer image export | 5 | 0 | 239 | none - no OGR/GDAL driver speaks `exportImage` | 239 | 0 |
| F5 | OGC WCS 1.0.0 | 1 | 135 | 99 | none in the ruled set (OWSLib unruled) | 234 | 0 |
| F6 | Bulk object / VRT / tile-grid raster over our transport | 12 | 601 | 1,018 | none - folding costs the typed transport | 1,619 | 0 |
| F7 | Library-delegate rasters (py3dep / pfdf / cdsapi) | 4 | 1,188 | 165 | py3dep, pfdf, cdsapi - ALREADY the owner | 1,353 | 0 |
| F8 | Bespoke S3 composites (urllist / GPKG tile scheme / CF netCDF) | 3 | 3,206 | 0 | odc-geo reaches ~212 LOC of topobathy only | 3,206 | 0 (-152 available) |
| F9 | Animation frames (SLIDER pyramid, GOES archive, GLM) | 6 | 1,392 | 118 | none - pre-rendered tiles and point binning | 1,510 | 0 |
| - | *shared raster spine* (`array_to_cog_bytes`, dispatch, `execute`) | - | - | 274 | rasterio (stays; `to_raster` cannot bake a colormap) | 249 | -25 |
| G1 | ESRI FeatureServer / MapServer `/query` | 15 | 1,007 | 722 | **GDAL `ESRIJSON` driver via pyogrio** | 732 | **-997** |
| G2 | OGC API Features | 1 | 244 | 0 | **GDAL `OAPIF` driver via pyogrio** | 70 | **-174** |
| G3 | Overpass (OSM) | 6 | 962 | 108 | **OSMnx** | 198 | **-872** |
| G4 | Bespoke JSON / CSV HTTP APIs | 18 | 5,114 | 246 | none - the code is source vocabulary | 5,360 | 0 (-460 available) |
| G5 | `dataretrieval` delegates | 2 | 0 | 431 | **pynhd** (NLDI half); dataretrieval keeps WQP | 301 | **-130** |
| G6 | US hydro, hooked (NWIS, STN HWM) | 2 | 740 | 0 | **pygeohydro** (`NWIS`, `STNFloodEventData`) | 240 | **-500** |
| G7 | Library delegates, non-raster | 3 | 1,174 | 0 | pynhd (NWM's NLDI half); cdsapi, geopandas stay | 994 | **-180** |
| G8 | NOAA CO-OPS station timeseries | 3 | 0 | 552 | **none in the ruled set** (`noaa-coops` unruled) | 552 | 0 |
| G9 | Record (LTER, SLIDER index) | 2 | 466 | 74 | none | 540 | 0 |
| G10 | TIGER zipped shapefile | 1 | 98 | 187 | **GDAL `/vsizip//vsicurl/` via pyogrio** | 98 | **-187** |
| - | *shared* `chained_resolution` (~12 specs) | - | - | 205 | n/a | 205 | 0 |
| | **TOTALS** | **97** | **16,972** | **5,654** | | **19,136** | **-3,490** |

Reconciliation: hooks 16,972 + excluded-package hooks 1,356 + the loader
`_router/hooks/__init__.py` 392 = 18,720, the measured `hooks/*.py` total.
Executors 5,654 is the measured `executors/*.py` total, charged once.

**Not in the totals, both real:**

- `imagery/_pc_stac.py` 262 - `sas_sign_href:144` + `_request_sas_token:109` +
  `search_least_cloudy_item:163` (~133 LOC) die with F1, but the module CANNOT be
  deleted: `tools/processing/compute_ndvi/compute_ndvi.py:54` and
  `tools/processing/digitize_water_body/digitize_water_body.py:72,312,358,382`
  import it. **-133 of product LOC, a half-dead module left behind** - a
  clean-as-you-go debt the fold wave must either pay (migrate both processing
  tools to `planetary_computer.sign`) or declare.
- `_router/transforms/join.py` 342 - its ONLY consumers are `fetch_census_acs` and
  `fetch_lehd_jobs`, both scope-attic bound. **It travels with the attic.** Not a
  fold, and it must not be counted as one.

**Deferred but measured**, if NATE rules F8 and G4 in: a further **-612**, total
**-4,102**.

**Against the rulings' own estimates.** STAC FOLD projected 4,500-6,000 LOC for
the raster half (`docs/IDEAS.md:3977`); the measured raster half is **-450**
(F1 -425 plus the spine's -25), plus `_pc_stac`'s -133. The gap is not a
measurement dispute: of the ten sources the ruling names, HRRR, AORC and gridMET
are **already** `xr.open_zarr` / `xr.open_dataset` + rioxarray in-hook
(`hooks/hrrr.py:248-283`, `hooks/aorc_precip.py:52-92`,
`raster_cog.py:181-251`) with nothing left to fold; landcover is WCS, topobathy
is a urllist composite, BlueTopo is a GeoPackage tile join, CHIRPS and MRMS are
whole-object gzip. Only Copernicus and JRC are STAC. **The raster fold's real
prize is the four specs the ruling did not name** - the three RGB imagery specs
and `fetch_esri_landcover_10m`, 539 LOC of executor between them.

The vector half is where the LOC is: **-3,040** across G1, G2, G3, G5, G6, G7,
G10, seven-tenths of the whole fold.

### The projected fetcher tree

```
trid3nt_server/tools/fetchers/
  <group>/<spec>/                         source.yaml + corpus.yaml + hooks.py
                                          (hooks CO-LOCATE per SCOPE CENSUS RULED;
                                           the loader tree-walks them)
  _router/
    executors/
      raster_cog.py            ~1,836     was 2,846; the 4 STAC modes lifted out,
                                          3 execute branches gone. Keeps the 7
                                          transport-bound readers, WCS, the two
                                          ArcGIS image-service readers, griddap,
                                          opendap, array_to_cog_bytes
      stac_raster.py            ~560  NEW ~200 new (odc.stac.load -> xarray ->
                                          array_to_cog_bytes) + ~360 lifted residue
                                          (collection/asset resolve, the select
                                          ladders, DN math, the QA/SCL bitmasks,
                                          the joint stretch, the inferno ramp)
      vector_fgb.py              ~472     the SERIALIZER half only: the honest-empty
                                          header-only FGB (:378-469), the declared/
                                          derived schema, keep_null_geometry, the
                                          SPATIAL_INDEX=NO null-geometry guard,
                                          apply_column_map/apply_ingest_transforms
                                          (now the frame-normalizer for every driver
                                          read), build_where as a `where=` builder
      station_timeseries.py       552     unchanged
      http_json.py                246     unchanged (its _fetch_endpoint_fallback:70
                                          is also the Overpass mirror chain)
      dataretrieval_delegate.py  ~301     wqp_features:183 + _latest_results_by_site
      chained_resolution.py       205     unchanged
      library_delegate.py         165     unchanged
      animation_frames.py         118     unchanged
      overpass_sidecar.py         108     unchanged (the one sanctioned side write)
      record.py                    74     unchanged
      zip_vector.py            DELETED    187, one consumer, folds to /vsizip//vsicurl/
    transport/                    820     UNCHANGED and load-bearing: client.py:5-8
                                          is the sole retry authority, opener.py
                                          recovers the verbatim S3 <Code>
    transforms/
      fan_out.py                  115     one consumer (slr_scenarios)
      tiled_mosaic.py             197
      join.py                  ATTIC      342, travels with census_acs + lehd_jobs
    hooks/
      __init__.py                ~392     becomes the tree-walking loader; the 57
                                          hook modules have moved beside their specs
  imagery/
    _goes_archive_core.py       1,324     stays (fire-temp / true-color composites)
    _satellite_slider.py          818     stays (SLIDER stitch + honest approx georef)
    _goes_common.py               294     stays
    _pc_stac.py                  ~129     bbox_pixel_dims survives; the SAS half dies
                                          but the module lives for two processing tools
```

---

## 2. Per family: every spec, its fate, and why

Key: **FC** = FOLDS CLEAN (the library call plus spec data replaces the hook, no
source-specific python left). **RES** = FOLDS WITH A RESIDUE (a named piece of pure
compute survives - a mask, a datum, a gate, a palette). **S** = STAYS.

### F1 - STAC, Planetary Computer STAC v1 (8 specs, 92 hook + 985 executor)

Four executor sub-modes: `stac_float` (`raster_cog.py:1924-2116`),
`stac_multi_asset_rgb` (`:2131-2479`), `stac_search` (`:2480-2669`),
`stac_continuous_mosaic` (`:2670-2765`). Collection ids are already declared in
`ingest.stac.collection` - exactly the argument `odc.stac.load` wants.

| spec | fate | reason |
|---|---|---|
| `terrain/fetch_copernicus_dem` | **FC** | zero hook; `odc.stac.load` with the default first-valid fuser IS the whole body |
| `climate/fetch_modis_lst` | **FC** | zero hook; scale/offset becomes a 3-line xarray expression |
| `terrain/fetch_esri_landcover_10m` | **FC** | zero hook; a categorical first-valid mosaic is odc's default fuser at `resampling="nearest"`; ~150 of 190 LOC dies |
| `imagery/fetch_naip` | **FC** | passthrough RGB - `odc.stac.load(bands=["image"])`, no render math |
| `imagery/fetch_sentinel2_truecolor` | **FC** | one joint 2/98 stretch survives; the per-asset read loop dies |
| `hydrology/fetch_jrc_global_surface_water` | **RES** | the load folds; `hooks/jrc_global_surface_water.py` 92 is a PURE per-band GDAL colour table and survives whole - the correct place for it |
| `imagery/fetch_landsat_imagery` | **RES** | the load folds; the `qa_pixel` bitmask, the reflectance/LST transforms, the joint stretch and the inferno ramp are band math odc does not do (~175 LOC) |
| `imagery/fetch_sentinel1_sar` | **RES** | the load folds; the coverage-fraction scene rank and the `10*log10` dB conversion survive |

Dies: `_pc_sign_two_tier:1781-1806` (26, replaced by
`Client.open(modifier=planetary_computer.sign_inplace)`), the native-lattice snap in
`_stac_float_grid:1899-1921` (~55, replaced by
`GeoBox.from_bbox(..., anchor=xy_(fx,fy))` at `odc/geo/geobox.py:527`), the windowed
`rasterio.warp.reproject` loop `:2027-2050` (~50), the RGB per-asset read loop (~174),
the `stac_search` hand-rolled tile window (~150), the `stac_continuous` mosaic (~90).

Excluded footnote: `biodiversity/fetch_mobi` would be a 9th, **FC**, 0 hook LOC.

### F2 - Zarr / OPeNDAP gridded (4 specs, 553 hook + 71 executor) - ALL STAY

| spec | fate | reason |
|---|---|---|
| `weather/fetch_hrrr_forecast` | **S** | `hooks/hrrr.py:248-283` is ALREADY `xr.open_zarr` x2 + merge + `rio.write_crs(LCC)` + `rio.reproject` + `rio.clip_box` - the fold's target state, reached |
| `weather/fetch_hrrr_smoke` | **S** | same hook module, nothing left |
| `weather/fetch_aorc_precip` | **S** | `hooks/aorc_precip.py:52-92` is ALREADY `s3fs.S3Map` + `xr.open_zarr(consolidated=True)` + `.sel` + area mean |
| `climate/fetch_gridmet` | **S** | `raster_cog.py:181-251` is ALREADY `xr.open_dataset` + `.sel` + time mean |

**This family is the ruling's own estimate error, isolated.** Four of the ten
sources STAC FOLD named for migration are already migrated.

### F3 - ERDDAP griddap (1 spec, 160 executor) - STAYS

`ocean/fetch_noaa_sst` - **S**. `_griddap_to_array:999-1147` already opens the
response body with xarray in memory; what remains is the bracket-selector URL
builder and the ERDDAP no-data body markers, both source vocabulary.

### F4 - ArcGIS image services (5 specs, 239 executor) - ALL STAY

`hazard/fetch_landfire_fuels`, `hazard/fetch_usfs_canopy_fuels`,
`ocean/fetch_greatlakes_bathymetry` (**S**, `_imageserver_export_bytes:1542-1674`):
the server's GeoTIFF response IS the artifact; `mosaicRule` is a server-side mosaic.
`ocean/fetch_noaa_slr_confidence`, `ocean/fetch_noaa_slr_marsh` (**S**,
`_mapserver_export_rgba_bytes:1687-1780`): a server-symbolized PNG32 overlay,
georeferenced client-side. No GDAL driver and no library speaks `exportImage`.

### F5 - OGC WCS 1.0.0 (1 spec, 135 hook + 99 executor) - STAYS

`terrain/fetch_landcover` - **S**. MRLC GeoServer WCS is not STAC.
`hooks/landcover.py` 135 is PURE - NLCD alias/vintage parse, the 4000 px
auto-coarsen that re-quantizes the bbox INTO the cache key, and the Manning's
sidecar. No I/O to fold. (131 consumers - the second-widest blast radius in the
tree.)

### F6 - Bulk object / VRT / tile-grid rasters (12 specs, 601 hook + 1,018 executor) - ALL STAY

All twelve read through `transport/range_file.py` with `rasterio.open(opener=)`.
GDAL never networks; folding them onto odc-stac or rioxarray's default reader
hands the socket to CPL HTTP and re-opens the exact regression
`raster_cog.py:258-261` names: "This is where the old `/vsicurl/` path lost the
404->EMPTY split (GDAL discarded the status, so every failure read as
UPSTREAM_ERROR)."

| spec | fate | reason |
|---|---|---|
| `soil/fetch_gcn250_curve_numbers` | **S** | one known COG, byte-range window (`_direct_window_to_array:252-382`) |
| `hydrology/fetch_aquifer_thickness` | **S** | staged `s3://trid3nt-cache/` object, byte-range window |
| `hydrology/fetch_aquifer_transmissivity` | **S** | same |
| `hydrology/fetch_groundwater_recharge` | **S** | same, two sources |
| `hydrology/fetch_water_table_depth` | **S** | same |
| `socioeconomic/fetch_hrsl_population` | **S** | `.vrt` mosaic hand-parsed (`:383-566`) precisely so member reads go through our transport |
| `soil/fetch_soilgrids` | **S** | projected `.vrt` window in Goode Homolosine, densified bounds, Int16 scale (`:580-707`) |
| `socioeconomic/fetch_ghsl_population` | **S** | a ZIP-of-DEFLATE-TIF member is not byte-windowable; whole object per tile |
| `climate/fetch_chirps_precipitation` | **S** | a gzip stream has no windowable layout; xarray adds nothing |
| `weather/fetch_mrms_qpe` | **S** | GRIB decode needs a real path, `cfgrib` is not installed, and the S3-list resolve phase is bespoke |
| `hydrology/fetch_flood_extent_observation` | **S** | LANCE NRT is an HTTP archive dir-walk, not a catalog |
| `socioeconomic/fetch_population` | **S** | WorldPop answers range requests with 200, not 206 (`hooks/worldpop.py:4-6`) - whole-object download is the only correct read |

### F7 - Library-delegate rasters (4 specs, 1,188 hook + 165 executor) - ALL STAY

| spec | fate | reason |
|---|---|---|
| `terrain/fetch_dem` | **S** | ALREADY a py3dep delegate (`terrain/fetch_dem/source.yaml:38-47`). See Q1 - claimed by both lenses, and it is the board's highest-traffic tool (1,591 calls) |
| `terrain/fetch_3dep_extra` | **S** | pfdf owns TNM discovery (`hooks/pfdf_raster.py` 196) |
| `soil/fetch_statsgo_soils` | **S** | same module, ScienceBase COGs |
| `climate/fetch_era5_reanalysis` | **S** | cdsapi request-poll-download under a wall-clock watchdog - a job queue, not a catalog |

### F8 - Bespoke S3 composites (3 specs, 3,206 hook) - STAY, with one deferred residue

| spec | fate | reason |
|---|---|---|
| `ocean/fetch_topobathy` | **RES (deferred)** | of `hooks/topobathy.py` 1,933, only `_compute_target_grid` + `_decimated_source_read` + `_composite_sources_to_array` (~212 LOC, `:881-1104`) is a reproject-onto-a-shared-grid that `odc.geo` GeoBox + `rioxarray.reproject_match` express. The other ~1,720 is CUDEM urllist discovery, ETOPO/NCEI fallback tiles, the per-tile NAVD88 datum gate, the coverage ladder and the fallback-warning composition - no library equivalent. **Do not open it in this wave** (Q9) |
| `ocean/fetch_bluetopo` | **S** | coverage truth is a GeoPackage polygon join with a per-tile datum assert (`hooks/bluetopo.py:211-313`); there is no catalog to load |
| `imagery/fetch_goes_satellite` | **S** | S3 key listing + CF netCDF + geostationary reproject; no STAC, no Zarr |

`hooks/topobathy_class.py` 356 (the `water_body_class` feed) is charged here and stays.

### F9 - Animation frames (6 specs, 1,392 hook + 118 executor) - ALL STAY

| spec | fate | reason |
|---|---|---|
| `imagery/fetch_goes_animation` | **S** | SLIDER is a pre-rendered RGB tile pyramid - no catalog, no grid metadata at source |
| `imagery/fetch_goes_blend_animation` | **S** | same hook pair; GeoColor + FireTemperature blend |
| `imagery/fetch_viirs_day_fire` | **S** | irregular polar passes with a local-solar-time day filter |
| `imagery/fetch_goes_archive_animation` | **S** | per-frame CF netCDF composite |
| `imagery/fetch_goes_active_fire` | **S** | same hook pair, `mode: hotspots` |
| `weather/fetch_glm_lightning` | **S** | point binning with `numpy.add.at`; `hooks/glm.py:20-22` states parallax forbids warping the result |

Shared substrate `_goes_archive_core.py` 1,324 + `_satellite_slider.py` 818 +
`_goes_common.py` 294 all stay - none of it is STAC or Zarr.

### G1 - ESRI FeatureServer / MapServer `/query` (15 specs, 1,007 hook + 722 executor)

**Proven live in `venvs/agent`** (pyogrio 0.13.0 / GDAL 3.12.4, read-only):
`read_dataframe('ESRIJSON:<NWI MapServer>/query?...&geometry=...')` returned
**87 rows** with columns `Wetlands.OBJECTID, Wetlands.ATTRIBUTE, ...` - the exact
prefixing `hooks/nwi_wetlands.py` exists to strip. And with
`&resultRecordCount=100` over a 391-feature bbox it returned **391 rows**: the
driver followed `exceededTransferLimit` across 4 pages by itself. That is the whole
of `vector_fgb._fetch_from_endpoint:656-687`.

Zero-hook today (10) - they fold by swapping the executor's page loop and esri-json
decoder for one driver read; every `ingest.*` directive stays as spec data:

| spec | fate | reason |
|---|---|---|
| `climate/fetch_us_drought_monitor` | **FC** | endpoint-select-by-date and the D0-D4 lookup are spec data |
| `hazard/fetch_hifld_critical_infrastructure` | **FC** | the routing table, derived_columns and the finite-Point filter are spec data |
| `hazard/fetch_hifld_transmission_lines` | **FC** | the min-voltage clause becomes a `where=` in the URL |
| `hazard/fetch_mtbs_burn_severity` | **FC** | year-range where clause |
| `hazard/fetch_nifc_fire_perimeters` | **FC** | the global sweep is just the URL without a geometry param |
| `hazard/fetch_usace_levees` | **FC** | `pygeohydro.NLD(layer=...).bygeom` (`levee.py:18,97`) already names `leveed_areas`/`system_routes`/`embankments` - exactly its 3 sub-layers |
| `hydrology/fetch_nhd_area_water` | **FC** | `pynhd.NHDPlusHR` or plain ESRIJSON; the `maxAllowableOffset` generalization tolerance is a real cartographic choice and must survive as a spec field |
| `hydrology/fetch_nhdplus_hr_flowlines` | **FC** | same, plus a gnis_name where clause |
| `hydrology/fetch_nhd_waterbodies` | **RES** | the HR -> medium-res fallback ordering is a same-dataset silent hop the library does not express; the case-insensitive column map survives |
| `ocean/fetch_noaa_slr_scenarios` | **RES** | `transforms/fan_out.py` 115 (fan out over `scenario_ft`, merge) is ours |

Hooked today (5):

| spec | hook LOC | fate | reason |
|---|---:|---|---|
| `hydrology/fetch_nwi_wetlands` | 134 | **FC** | the driver reproduces the read exactly; the prefix strip becomes a 3-row `column_map` |
| `hazard/fetch_fema_nfhl_zones` | 192 | **FC** | `pygeohydro.NFHL('NFHL','flood hazard zones')` (`nfhl.py:19`) or plain ESRIJSON; the SFHA/zone-code vocabulary is spec data |
| `hazard/fetch_usace_dams` | 245 | **RES** | `pygeohydro.NID().get_bygeom` (`nid.py:389`) ships the dam_type/dam_purpose code tables we hand-carry, but NID's own client is KEYLESS - our credential-shaped missing-key error and the USPS state normalization survive |
| `hazard/fetch_epa_frs_facilities` | 205 | **RES** | 5 driver reads plus concat; the layer set per `facility_program` and the LAT/LON -> Point synthesis (that layer has no geometry) survive |
| `hazard/fetch_wfigs_incident` | 231 | **S** | the ESRI read folds, but this is a discovery RECORD, not a layer: the Current -> YearToDate ordering, the best-feature-by-size rule and the AOI pad are the tool |

### G2 - OGC API Features (1 spec, 244 hook)

`hydrology/fetch_usgs_groundwater_levels` - **FC**. **Proven live:**
`OAPIF:https://api.waterdata.usgs.gov/ogcapi/v0`, layer
`latest-field-measurements`, `bbox=(-81.6,26.0,-81.3,26.3)` -> 50 rows with
`monitoring_location_id` present; `bbox=` pushdown works, 36 collections listed.
Two driver reads joined on `monitoring_location_id` as a pandas merge replace
`build_request`/`parse_response`/`enrich_plan`/`enrich_merge`. Survives (~70 LOC):
the state-XOR-bbox selector gate, the typed `USGS_GROUNDWATER_NO_WELLS` on a
primary miss (never an empty-success layer), and the best-effort degrade that keeps
wells with blank aquifer fields.

### G3 - Overpass (6 specs, 962 hook + 108 executor)

`hooks/overpass.py` 733 registers 10 hooks for 5 specs
(`:156,210,345,355,508,525,627,637,696,704`); `hooks/buildings.py` 229 is the
sixth. This is the ruling's "733-line hook + buildings", confirmed exactly.

| spec | fate | reason |
|---|---|---|
| `socioeconomic/fetch_roads_osm` | **FC** | `osmnx.features_from_bbox(bbox, {'highway': [...]})` (`features.py:93`); the road-class enum is spec data |
| `socioeconomic/fetch_overpass_pois` | **FC** | same call; the 5-param tag/amenity/category alias surface is the tool's UX and stays spec data |
| `hydrology/fetch_river_geometry` | **FC** | same call on `waterway` |
| `ocean/fetch_osm_breakwaters` | **FC** | same call on `man_made` |
| `ocean/fetch_osm_coastline` | **FC** | same call on `natural=coastline` |
| `socioeconomic/fetch_buildings` | **RES** | OSMnx returns the **full tag bag as frame columns**, so the `.tags.json` sidecar payload comes free and the multipolygon relation assembly dies - but `overpass_sidecar.py` 108 (the one sanctioned side write, recomputing the `.fgb` cache key at `:41`) stays, and so does the slim-FGB / fat-sidecar split |

**The honest price, named.** `overpass.py`'s real value is the 3-mirror chain
(`overpass-api.de` -> `overpass.kumi.systems` -> `overpass.private.coffee`,
declared per spec as `ingest.http_source.endpoint_fallback` and driven by
`http_json._fetch_endpoint_fallback:70`). OSMnx has ONE `settings.overpass_url`
(`settings.py:163`). A ~30 LOC wrapper (set, call, catch, reset, next mirror) buys
the other ~900 back. OSMnx also rewraps upstream errors in its own
`InsufficientResponseError` / `ResponseStatusCodeError` (`osmnx/_errors.py`) - the
verbatim-error norm needs a re-proof here, though `osmnx/_http.py:316` does log the
full `response.text` on non-OK.

### G4 - Bespoke JSON / CSV HTTP APIs (18 specs, 5,114 hook + 246 executor) - RECOMMEND NO FOLD

No driver and no ruled library reaches these. Their content is not fetch
boilerplate - it is source vocabulary.

| spec | hook LOC | fate | reason |
|---|---:|---|---|
| `weather/fetch_storm_tracks` | 1,006 | **RES** | the largest hook in the tree; only the active-mode zip-shapefile leg folds to `/vsizip//vsicurl/` (~-200), killing an extract-to-tmpdir. The IBTrACS/NHC mode split, the season resolve and the storm-name canonicalization stay |
| `hazard/fetch_openfema_disasters` | 457 | **RES** | the per-state TIGERweb county leg is an ESRIJSON read (~-60); the OData `$skip/$top` paging and the GEOID left-join are the source |
| `hydrology/fetch_nws_river_forecast` | 340 | **S** | NWPS is not in HyRiver; two bounded best-effort per-gauge chases (flood-category thresholds, `/stageflow` + crest) |
| `weather/fetch_raws_weather` | 292 | **S** | per-state DCP discovery, RAWS-named stations only, per-station-per-day obhistory enrichment |
| `weather/fetch_asos_metar` | 288 | **RES** | GDAL CSV could read the ONE bulk IEM CGI file (~-40); the per-state ASOS network discovery is the source |
| `hazard/fetch_fault_sources` | 283 | **RES** | `/vsicurl/` reads the 10.6 MB GEM GeoJSON (**proven: 13,696 features, LineString Z**) and GDAL's bbox filter replaces the client-side clip (~-60); the `(best,min,max)` kinematic triple and the GEM depth/dip/rake defaults are irreducible physics vocabulary |
| `weather/fetch_storm_events_db` | 261 | **RES** | `/vsicurl/vsigzip/` reads a resolved CSV (~-40); the regex scrape of the NCEI HTML directory index for the window's years and newest processed-date file IS the source. 158 live calls - second-most-exercised fetcher on the board |
| `hazard/fetch_usgs_earthquakes` | 257 | **S** | the reader is trivial; the decode is the tool - id at feature top level, **depth lifted from the geometry Z**, epoch-ms times, the `metadata.count` cap gate |
| `weather/fetch_openaq_measurements` | 246 | **S** | paged `/v3/locations` to a 2000-station cap, then per-location `/latest` fan-out joined via the sensor map |
| `weather/fetch_nws_alerts_conus` | 231 | **RES** | `/vsicurl/` reads the primary (~-30); the per-alert zone-URL chase, so zone/county watches with NULL inline geometry still draw, is ours. 50 consumers, 36 live calls |
| `hazard/fetch_tsunami_events` | 221 | **S** | NCEI hazel, mode-selected endpoint, per-mode (`events` vs `runups`) item decode |
| `soil/fetch_snotel_snow` | 207 | **S** | NRCS AWDB is in no ruled HyRiver package |
| `hazard/fetch_usgs_volcano_alerts` | 205 | **S** | two static HANS endpoints inner-joined on `vnum` with a client-side bbox filter - HANS has no spatial query |
| `climate/fetch_climate_normals` | 188 | **RES** | GDAL CSV reads the per-station files (~-30); the **fixed-width** NCEI station inventory slice does not fold |
| `hazard/fetch_firms_active_fire` | 178 | **S** | **MUST NOT FOLD.** The key is in the URL path and a bad/rate-limited key returns **200 with an error body**; the auth split lives in both `parse_response` and `classify_status`. A driver read swallows it - a fold here breaks the honesty floor |
| `weather/fetch_airnow_air_quality` | 173 | **S** | keyed box GET, latest row per (lat,lon,parameter), derived AQI column |
| `socioeconomic/fetch_usace_nsi` | 146 | **S** | the `structures` query is a **POST whose body is a FeatureCollection wrapping the bbox** - no OGR driver POSTs a JSON body. The two Pelicun columns (`component_type` <- occtype, `replacement_value` <- val_struct) are the contract |
| `weather/fetch_nws_event` | 135 | **RES** | same endpoint as alerts; the `area` canonicalization to a state code or 5-digit county FIPS stays |

Total available if NATE rules the residues in: **~-460 of 5,114 (9 percent)**,
spread across 8 specs, each needing its own parity proof. Poor ratio.

### G5 - `dataretrieval` delegates (2 specs, 431 executor)

| spec | fate | reason |
|---|---|---|
| `hydrology/fetch_nhdplus_nldi_navigate` | **FC** | `pynhd.NLDI().comid_byloc` (`nldi.py:198`) + `.navigate_byid` (`:408`) make both the hand-written `_nldi_snap:295` and `nldi_features:320` unnecessary; `distance_km`/`direction` map 1:1 (~-130) |
| `hydrology/fetch_usgs_water_quality` | **S** | `wqp_features:183` + `_latest_results_by_site:143` already ride the agency's own client, which the ruling names "already used"; moving to `pygeohydro.WaterQuality` buys nothing. The characteristic alias table is the spec's value |

### G6 - US hydro, hooked (2 specs, 740 hook)

| spec | hook LOC | fate | reason |
|---|---:|---|---|
| `hydrology/fetch_usgs_nwis_gauges` | 386 | **RES** | `pygeohydro.NWIS.get_info` + `.get_streamflow` (`nwis.py:321,638`) own the IV/Site services and the RDB parsing (`retrieve_rdb:136`). Survives (~120): the two output schemas, the rule that mode derives the cache key, and the **IV-empty -> Site-locations degrade**, which is a data-source fallback the library does not express. 34 consumers, and it is the calibration wave's gauge row (Q7) |
| `hydrology/fetch_high_water_marks` | 354 | **RES** | `pygeohydro.STNFloodEventData` / `stn_flood_event` (`stnfloodevents.py:23,460`) ships the `hwms_query_params` set and the event/state filters and geo-references the records itself - replacing the state derivation (STN has no server-side bbox filter), the request build and the decode. Survives (~120): the WSE quantity stamp, the **datum rows** in the post-emit envelope breakdown, the typed `HWM_NO_MARKS`, and the `HighWaterMarksLayerURI` subclass - product contract, not fetch code |

### G7 - Library delegates, non-raster (3 specs, 1,174 hook)

| spec | hook LOC | fate | reason |
|---|---:|---|---|
| `hydrology/fetch_noaa_nwm_streamflow` | 694 | **RES** | the NLDI half - a 5x5 spatial sample (25 requests) plus up to 500 per-reach geometry pulls - is `pynhd.NLDI` (~-180). The NWM half is a whole-object netCDF read to an xarray dict and belongs with F2; the join stays |
| `ocean/fetch_gtsm_tide_surge` | 255 | **S** | CDS is credentialed and returns a zip-of-netCDF; no OGR path |
| `socioeconomic/fetch_field_boundaries` | 225 | **S** | fiboa GeoParquet read via `geopandas.read_parquet(fh, bbox=...)` with GeoParquet 1.1 row-group pushdown. **The ruling's "GeoParquet with bbox pushdown via pyogrio" does not hold on this machine** - see Section 5 |

### G8 - NOAA CO-OPS station timeseries (3 specs, 552 executor) - ALL STAY

`ocean/fetch_noaa_coops_tides`, `ocean/fetch_noaa_coops_currents`
(snapshot emit, `station_timeseries.py:327`), `ocean/fetch_greatlakes_water_level`
- all **S**, all fully declarative on `station_catalog` + `per_station` with zero
hook LOC. `grep -i 'tidesandcurrents|coops'` across pygeohydro and pynhd: **zero
hits**. No ruled library reaches CO-OPS.

The executor's real content is the point-FGB schema at `station_timeseries.py:70-74`
- `station_id, station_name, lon, lat, product, datum, time_start, time_end,
n_timesteps, wl_min_m, wl_max_m, wl_mean_m, time_series_csv` - the **inline series
SFINCS and TELEMAC boundary forcing consume**. That is a product contract. See Q8.

### G9 - Record, other (2 specs, 466 hook + 74 executor) - BOTH STAY

`hydrology/fetch_lter_records` (374) - **S**: EDI PASTA is 403 anonymously, so every
fetch goes through the DataONE resolve mirror, and the resolve phase lifts the
entity URL, delimiter, header rows and column units OUT of EML metadata and INTO the
cache key. GDAL CSV cannot pick the entity or read EML.
`imagery/fetch_slider_timestamps` (92) - **S**: one live-no-cache GET of SLIDER's
`latest_times.json` shaped into an availability + cadence record. Not a layer.

### G10 - TIGER zipped shapefile (1 spec, 98 hook + 187 executor)

`socioeconomic/fetch_administrative_boundaries` - **FC**.
`/vsizip//vsicurl/https://www2.census.gov/geo/tiger/TIGER2024/...zip/<member>.shp`
with `bbox=` reads a shapefile inside a remote zip by range request - no tmpdir, no
`extractall`. **`zip_vector.py` 187 dies outright**, its only consumer. Survives:
`hooks/admin_boundaries.py` 98, a pure URL planner (state-FIPS routing table plus
the antimeridian Aleutian tail). 65 consumers - the second-most-consumed spec in the
tree, so it needs the wide-blast-radius scrutiny.

---

## 3. Executor fates

| executor | LOC today | LOC after | fate |
|---|---:|---:|---|
| `raster_cog.py` | 2,846 | ~1,836 | **SPLITS.** The 4 STAC modes (985) lift out to `stac_raster.py`; `execute:2766-2846` loses 3 branches (~25); `fetch_source_array:139-180` loses 4 of 12. **SURVIVES intact:** `array_to_cog_bytes:39-138` (100 - raw `rasterio.open(driver="COG")` with a GTiff fallback plus `write_colormap` and `colorinterp` baking, neither of which `rioxarray.to_raster` exposes, and it must return **bytes**; this is NOT the L5 `workflows/shared/cog_io.py:259-517` path), `_opendap_to_array:181-251` (71), `_direct_window_to_array:252-382` (131), the VRT block `:383-566` (184), `_projected_vrt_window_to_array:580-707` (137), the gzip block `:717-841` (138), `_grib_object_to_array:855-986` (144), `_griddap_to_array:999-1147` (160), the tile-grid block `:1159-1306` (161), `_wcs_getcoverage_to_array:1320-1404` (99), the categorical tile grid `:1419-1541` (123), the ImageServer pair `:1542-1674` (145), the MapServer pair `:1687-1780` (94) |
| `stac_raster.py` | 0 | ~560 | **NEW.** ~200 new (`odc.stac.load` -> xarray -> `array_to_cog_bytes`; spec fields collection/assets/bands/resolution) + ~360 lifted residue: collection/asset/window resolve, the `latest`/`coverage`/`intersect_all` select ladder, the DN scale/offset and `positive_only`/`log10_db` math, `_rgb_rank_item`, `_rgb_apply_transform`, `_rgb_bad_mask`, `_rgb_render_joint_stretch`, `_rgb_render_colormap`, `_rgb_cog_bytes`, `_aoi_coverage`, `_normalize_via_aliases` |
| `vector_fgb.py` | 722 | ~472 | **SPLITS: the serializer half LIVES, the fetch half DIES.** Survives: `features_to_fgb_bytes:378-469` with its honest-empty header-only FGB, the declared/derived column schema, `keep_null_geometry` and the `SPATIAL_INDEX=NO` null-geometry guard - the honesty floor, called by every other executor; `apply_column_map:228` and `apply_ingest_transforms:314` survive and get MORE work as the frame normalizer for every driver read; `build_where:58` becomes a `where=` URL builder. Dies (~250): `build_query_params:544-609`, `_fetch_one_page:611-654`, `_fetch_from_endpoint:656-687` (GDAL's paging replaces it, proven live), `_esri_geometry_to_geojson:474-499`, `_esri_feature_to_geojson:501` |
| `station_timeseries.py` | 552 | 552 | **STAYS WHOLE.** 3 CO-OPS specs, no ruled library; the FGB schema at `:70-74` plus the `iso8601z` normalization `:44` and the `{start:%Y%m%d}` coercion `:56` are the product contract |
| `http_json.py` | 246 | 246 | **STAYS.** The generic plan-fetch-parse spine for the 18 bespoke APIs; `_fetch_endpoint_fallback:70` is also the Overpass mirror chain OSMnx lacks |
| `record.py` | 74 | 74 | **STAYS.** 4 record specs, all STAYS |
| `zip_vector.py` | 187 | 0 | **DIES.** One consumer, which folds to `/vsizip//vsicurl/` |
| `overpass_sidecar.py` | 108 | 108 | **STAYS.** One consumer; the `.tags.json` side write beside the `.fgb` is the one sanctioned side write, and it recomputes the `.fgb` cache key at `:41`. OSMnx does not do it |
| `animation_frames.py` | 118 | 118 | **STAYS.** No library reaches a pre-rendered tile pyramid |
| `dataretrieval_delegate.py` | 431 | ~301 | **SHRINKS ~130.** `nldi_features:320` + `_nldi_snap:295` fold to `pynhd.NLDI`; `wqp_features:183` + `_latest_results_by_site:143` stay. Its `:15-19` docstring - the library owns the socket, the router maps its typed errors - is the template for every library fold |
| `library_delegate.py` | 165 | 165 | **STAYS.** Consumers after the fold: nwm, storm_tracks, gtsm, field_boundaries, dem, 3dep_extra, statsgo, topobathy, bluetopo, goes_satellite, population, era5, hrrr x2 |
| `chained_resolution.py` | 205 | 205 | **STAYS.** ~12 specs use its resolve + enrich phases |
| *(`transforms/join.py` 342)* | 342 | 0 | **STAYED in the tree** when `fetch_census_acs` + `fetch_lehd_jobs` moved (corrected 2026-09-08 after the scope-move verify: it is ledgered QUEUED at DELETION_LEDGER.md:25 - zero `join:` blocks remain across the 97 specs - and the demographic MANIFEST lists it under Requires:, so the fold wave treats it as an ORPHAN awaiting the ledger row, not as attic content), its only consumers. Not a fold - flag it to the move wave |
| *(`transport/` 820)* | 820 | 820 | **STAYS, and it is the thing GDAL must be measured against.** `client.py:5-8`: "The retry authority lives here and nowhere else: backoff + `Retry-After` honored on 429/5xx/timeout at BLOCK granularity... **GDAL-side retries stay off everywhere - reads never touch `/vsicurl/`**." The fold inverts that sentence |

---

## 4. THE TRADE, per library

Five cells per library. **CONFIG** = met by configuration we set. **SHIM** = met by
a small named wrapper. **NOT MET** = a Stage 0 cell, a live 3-case probe (404 / 403 /
429) that must go green before any spec in that family moves.

| library | retry | Retry-After | verbatim upstream error | caching | auth |
|---|---|---|---|---|---|
| **odc-stac 0.5.3** (F1) | **NOT MET.** `odc/loader/_rio.py:601` is `rasterio.open(src.uri, "r", sharing=False)` - **no `opener=` argument, no seam**. Its retry story is `GDAL_HTTP_MAX_RETRY` / `GDAL_HTTP_RETRY_DELAY` (`_rio.py:76-79`) via `odc.loader.configure_rio`. GDAL's default is **retry OFF** (`GDAL_HTTP_MAX_RETRY` defaults to 0) | **NOT MET.** GDAL is documented to honor `Retry-After` on codes in `GDAL_HTTP_RETRY_CODES` (default `429,502,503,504` since ~3.7), but GDAL ships as a compiled `.so` inside the rasterio wheel - **documented, not source-verified**. Live probe required | **NOT MET.** `raster_cog.py:258-261` names the exact regression: `/vsicurl/` discarded the status so every failure read as UPSTREAM_ERROR, losing the 404 -> typed-EMPTY split. rasterio DOES map ~14 CPL error classes (`rasterio/_env.pyx:14-50`) to typed exceptions plus a logging record, but the S3 XML `<Code>` body that `opener.py:1-11` recovers is not in that path | **SHIM.** odc-stac has no cache; ours is the router's cache-key tier and survives unchanged | **CONFIG.** Not odc's job: `Client.open(root, modifier=planetary_computer.sign_inplace)` or `odc.stac.load(patch_url=planetary_computer.sign)`. `planetary_computer/sas.py:166` parses account+container off the blob URL - the account-aware path our two-tier signer exists for |
| **odc-geo 0.5.3** | n/a | n/a | n/a | n/a | n/a - pure geometry/CRS math, zero I/O |
| **pyogrio 0.13.0 / GDAL 3.12.4** (G1, G2, G10) | **NOT MET**, same cell, **plus a second one** - see the two-GDAL finding below | **NOT MET**, same cell | **NOT MET**, same cell | **NOT MET.** GDAL has no response cache; and the measured latency is worse: **26.5 s for 947 NWI features, 41.6 s for 391 features at page size 100.** GDAL issues its own count query plus a page per chunk with no connection reuse we control; `transport/client.py` is a pooled httpx client. If the fold ships, the cache TTL classes do more work than they were | **CONFIG.** Token as a query param the caller supplies; no built-in refresh |
| **OSMnx 2.1.1** (G3) | **CONFIG (closest of the six).** Adaptive pre-request pause sized off Overpass's own `/status`, then a recursive retry with a fixed 55 s pause on 429/504 (`osmnx/_overpass.py:478-486`) | **SHIM.** Reacts to 429 explicitly but the 55 s is hardcoded, not read from a header (Overpass rarely sends one). Acceptable as-is; document it | **SHIM.** Rewraps in `InsufficientResponseError` / `ResponseStatusCodeError` (`osmnx/_errors.py`) with `response.reason`, not the body - but `osmnx/_http.py:316` logs the full `response.text` on non-OK. A ~10 LOC error-mapping hook in `library_delegate` style restores the norm | **CONFIG.** Its own JSON-file disk cache under `settings.cache_folder`, separate from ours; disable it or let it sit under our cache tier | **CONFIG.** None needed - Overpass is open, no API key |
| | **plus a SHIM the ruling did not price:** OSMnx has ONE `settings.overpass_url` (`settings.py:163`); our 3-mirror `endpoint_fallback` chain needs a ~30 LOC set/call/catch/reset wrapper | | | | |
| **pygeohydro 0.19.4 + pynhd 0.19.x** (G1 partial, G5, G6, G7) | **SHIM.** `pygeoogc.RetrySession` (`pygeoogc/utils.py:91-160`) wraps requests with a `urllib3.Retry` adapter: `retries=3`, `backoff_factor=0.3`, `status_forcelist=(500,502,504)`. **429 and 503 are NOT in the default forcelist** - a caller must pass `status_to_retry=(429,500,502,503,504)` | **SHIM.** urllib3's `Retry` DOES honor `Retry-After` for codes in the forcelist, so once 429 is added the norm is met. The shim is the one explicit kwarg | **CONFIG - MET.** `async_retriever/exceptions.py` raises `ServiceError`/`DownloadError` whose message is literally `f"URL: {url}\nERROR: {err}\n"` - verbatim | **CONFIG.** Its own SQLite response cache (`HYRIVER_CACHE_NAME_HTTP`, `HYRIVER_CACHE_EXPIRE`; default never-expire). **Set an expiry** or it shadows our provenance/staleness rules | **CONFIG.** `NID` is keyless, which is why `fetch_usace_dams`' credential path is a residue and not a fold |
| **dataretrieval 1.2.0** (G5) | **CONFIG - MET, precedent already landed.** httpx-based; `dataretrieval_delegate.py:15-19` already documents mapping the client's typed `dataretrieval.exceptions` to the router's input/upstream/empty classes | **CONFIG** (inherits the same mapping) | **CONFIG - MET** | **CONFIG** - ours | **CONFIG** - agency client, keyless |
| **py3dep 0.19.0 / pfdf / cdsapi** (F7) | already in force, unchanged by this fold | - | - | - | - |
| **pyesridump 1.13.0** (G1 alternate) | **NOT MET.** `num_of_retry=5`, **linear** backoff (`pause_seconds*(retry+1)`, default 10 s -> 10/20/30...), triggered on ANY exception - no 429-vs-5xx distinction | **NOT MET.** No `Retry-After` read at all | **SHIM.** `dumper.py:459-460` carries the ESRI `error.message` verbatim, but a raw HTTP 429/503 becomes `EsriDownloadError("Could not connect to URL", e)` with the status lost | **NOT MET.** No cache | **CONFIG.** Caller-supplied token |
| **geopandas 1.1.4 + pyarrow 24.0.0** (G7 field_boundaries) | already in force | - | - | - | - |

### The two-GDAL finding - PROVEN LIVE, and it is a new Stage 0 cell

`venvs/agent` contains **two independently vendored GDALs**:

```
rasterio.libs/libgdal-95b3f1c5.so.38.3.12.1     rasterio 1.5.0 -> GDAL 3.12.1
pyogrio.libs/libgdal-253d08a6.so.38.3.12.4      pyogrio  0.13.0 -> GDAL 3.12.4
```

They do not share a CPL config store. Probed read-only in the venv:

```python
with rasterio.Env(GDAL_HTTP_MAX_RETRY='7'):
    rasterio._env.get_gdal_config('GDAL_HTTP_MAX_RETRY')   # -> 7
    pyogrio.get_gdal_config_option('GDAL_HTTP_MAX_RETRY')  # -> None
```

**Consequence:** the retry/Retry-After configuration is not one setting proven
once. It must be set through `rasterio.Env` for the raster lens AND through
`pyogrio.set_gdal_config_options` for the vector lens, **on two different GDAL
builds**, and the 3-case probe must go green **twice**. The lens passes assumed
"one cell, both lenses"; it is measurably two. This alone is a reason to run
Stage 0 before any family moves.

### Where the trade does NOT bite

The xarray leg has no such trade.
`rioxarray.open_rasterio(url, open_kwargs={"opener": TransportOpener()})` passes
`open_kwargs` straight through to `rasterio.open`, so an xarray-side migration keeps
our transport and our typed errors. **Only odc-stac's reader is closed.** That
asymmetry is what makes Q2 (the custom reader driver) worth asking rather than
assuming.

---

## 5. Dependencies to declare, and pin conflicts

Already declared in `pyproject.toml`: `py3dep>=0.19,<0.20` (:38),
`rioxarray>=0.18,<1` (:39), `pystac-client>=0.8,<1` (:49), `geopandas>=1.0,<2`
(:171), `pyarrow>=16` (:179), `dataretrieval==1.2.0` (:233), `shapely==2.1.2`
(:256), `pyproj==3.7.2` (:259), `xarray==2026.4.0` (:265). The lean sweep already
pinned xarray, so the raster fold's central dependency is declared.

**To declare:**

| package | pin | family | new transitive cost |
|---|---|---|---|
| `odc-stac` | `==0.5.3` | F1 | `odc-loader` only. rasterio, xarray, dask, pystac all already present |
| `odc-geo` | `==0.5.3` | F1 | none - affine, cachetools, pyproj, shapely, xarray all present |
| `planetary-computer` | latest | F1 | trivial; replaces `_pc_stac`'s hand-rolled SAS. **Its docstring at `_pc_stac.py:7-9` says the SDK "is NOT installed in the agent venv" - that is now a choice, not a fact** |
| `osmnx` | `==2.1.1` | G3 | **zero new** - geopandas, networkx, numpy, pandas, requests, shapely all present. `Requires-Python>=3.11`; venv is 3.12.13 |
| `pygeohydro` | `==0.19.4` | G1, G6 | `hydrosignatures` (hyriver, 23 KB), `defusedxml`, `h5netcdf` - all tiny |
| `pynhd` | `==0.19.x` **not 0.20.0** | G5, G6, G7 | `networkx` present, `pyarrow` present |
| `pyogrio` | `>=0.13,<1` | G1, G2, G10 | **already installed but only transitively via geopandas** - the fold makes it a direct importer, so declare it (the lean sweep's own rule for shapely/pyproj/xarray) |

**Pin conflicts and hard constraints:**

1. **`pygeohydro` 0.19.4 caps `pynhd<0.20,>=0.19.3`, but PyPI's latest `pynhd` is
   0.20.0.** A bare `pip install pygeohydro pynhd` resolves pynhd DOWN to 0.19.x
   silently. Pin the pair explicitly - `pynhd==0.19.3` (or newest `<0.20`) beside
   `pygeohydro==0.19.4` - matching the already-installed pygeoogc 0.19.4 /
   pygeoutils 0.19.5 / py3dep 0.19.0 line.
2. **No Parquet or Arrow driver in this GDAL build.** `pyogrio.list_drivers()`
   returns 64 drivers; `Parquet` and `Arrow` are **absent**. The ruling's
   "GeoParquet with bbox pushdown via pyogrio" (`docs/IDEAS.md:3989`) does not hold
   on this machine - `geopandas.read_parquet` over pyarrow stays the only path for
   `fetch_field_boundaries`. Present and confirmed: `ESRIJSON`, `OAPIF`, `WFS`,
   `GeoJSON`, `CSV`, `FlatGeobuf` (rw), `OSM`, `ESRI Shapefile`.
3. **Two vendored GDALs** (Section 4) - not a version conflict pip can see, but a
   configuration-surface conflict. Any `GDAL_HTTP_*` policy must be applied and
   proven through both APIs.
4. **`pyesridump` buys nothing measured.** No in-lens ESRI layer hit an object-id
   wall; GDAL paged every one probed. And pygeoogc's `ArcGISRESTful` does the same
   job with a stronger retry/cache story, already paid for if HyRiver lands.
   **Recommend: do not add it until a layer actually refuses to page.**
5. `pydaymet` / `pygridmet` are named in the ruling but **no in-scope spec needs
   them** - gridMET is already `xr.open_dataset` (F2) and there is no Daymet spec.
   Do not add.
6. `cfgrib` is NOT installed, which is why `fetch_mrms_qpe`'s GRIB decode stays
   whole-object. Nothing here changes that.

---

## 6. The fold wave: stages and verification

### Stages

**Stage 0 - THE TRADE (blocks everything).** No spec moves until this is green.
- Probe A, rasterio's GDAL 3.12.1: 404 / 403 / 429 against a real object, under
  `rasterio.Env(GDAL_HTTP_MAX_RETRY=..., GDAL_HTTP_RETRY_DELAY=..., GDAL_HTTP_RETRY_CODES=...)`.
  Does the S3 XML `<Code>` reach a Python exception? Is `Retry-After` honored on 429?
- Probe B, pyogrio's GDAL 3.12.4: the same three cases through
  `pyogrio.set_gdal_config_options`. **Two builds, two proofs** (Section 4).
- Probe C: HyRiver's `RetrySession` with `status_to_retry=(429,500,502,503,504)`,
  and OSMnx's 429 path.
- If A fails: F1 falls back to the ruling's "signed reads only" - odc-stac for
  search + geobox + fuse, assets read through `open_windowed_cog` - or to Q2's
  custom `ReaderDriverSpec`. If B fails: G1/G2/G10 do not move at all.
- Deliverable: a `docs/validation/` cell with the verbatim bodies, per build.

**Stage 1 - `stac_raster.py` with OPERA DSWx as its proving consumer** (per STAC
FOLD and RESEQUENCED). A new zero-hook spec over Earthdata STAC on a NEW executor:
nothing to regress, and it proves `odc.stac.load` + geobox + `array_to_cog_bytes`
end to end before any existing spec is touched. Carry the **pixel-budget clamp**
(`px_min=16, px_max=4096`; naip 8192, `_pc_stac.py:241-262`) into the executor as an
explicit pre-load geobox check - `odc.stac.load` has no cap, and a county at 10 m
allocates unboundedly (R3).

Then families in LOC order:

| stage | family | delta | why here |
|---|---|---:|---|
| 2 | **G1** ESRI -> pyogrio `ESRIJSON` | -997 | largest single delta, proven live, 10 of 15 specs already zero-hook. Do `fetch_nwi_wetlands` first (the probe already reproduced its exact output), then the 9 other declarative rows, then the 4 hooked ones. `fetch_administrative_boundaries` (G10, -187) rides in this stage: same driver family, and it kills `zip_vector.py` |
| 3 | **G3** Overpass -> OSMnx | -872 | one library, six specs, one shared hook file. Land the ~30 LOC mirror wrapper and the error-mapping hook FIRST, then the five simple specs, then `fetch_buildings` (the sidecar is the only residue) |
| 4 | **F1** the 8 existing STAC specs onto `stac_raster.py` | -425 | the executor already exists and is proven from Stage 1. Order: `fetch_copernicus_dem` (zero hook, non-overlapping tiles, no render - the cleanest parity ground), then `fetch_esri_landcover_10m` (the only STAC spec with live calls; byte-parity bar), `fetch_modis_lst`, `fetch_jrc_global_surface_water` (proves the pure-hook seam survives), `fetch_naip`, `fetch_sentinel2_truecolor`, `fetch_landsat_imagery` last (three combos, most residue), `fetch_sentinel1_sar` |
| 5 | **G6** US hydro -> pygeohydro | -500 | gated on Q7 (calibration sequencing). Land `fetch_high_water_marks` first; hold `fetch_usgs_nwis_gauges` for the ruling |
| 6 | **G7** NWM's NLDI half, **G2** OAPIF, **G5** NLDI delegate | -484 | all three are `pynhd.NLDI` / the OAPIF driver; one dependency, one proof shape |
| - | **G4** bespoke JSON/CSV | (-460) | HELD unless NATE rules Q5 in |
| - | **F8** topobathy's warp core | (-152) | HELD - Q9 |

**Stage N - close-out.** `_pc_stac.py`'s two processing-tool consumers migrated to
`planetary_computer.sign` or the debt declared in the ledger (R6); every STAYS
family written into the ledger **with its protocol named**, so no later pass
re-opens `fetch_landcover` because "it is a raster" or `fetch_hrrr_forecast`
because "it is on the ruling's list".

### Verification

1. **Byte/pixel parity per folded spec against a pre-fold fetch.** Freeze a
   before-artifact per spec at the current HEAD, then compare. Bars, by kind:
   **byte-identical** for the passthrough-palette and passthrough-geometry rows
   (`fetch_esri_landcover_10m`, `fetch_naip`, the 10 declarative ESRI specs,
   `fetch_administrative_boundaries`); **value-identical within a declared
   tolerance** for the resampled float paths (`fetch_copernicus_dem`,
   `fetch_modis_lst`, `fetch_sentinel1_sar`, `fetch_landsat_imagery`); **feature-set
   and attribute-set identical** for the Overpass and HyRiver rows.
   Two named traps: (a) **mosaic order** - `intersect_all` merges in STAC
   search-return order (`raster_cog.py:2082-2087`) while odc sorts by `(time, id)`
   within a group unless `preserve_original_order=True`; for non-overlapping tiles
   the result is identical, for anything overlapping it is a **silent pixel
   change**, so this needs a per-spec check, not a blanket flag. (b) **mask
   co-registration** - under `resampling={"SCL":"nearest","*":"bilinear"}` the mask
   is co-registered by construction rather than by two matching calls; that is a
   gain, and it must be shown as one rather than discovered as a diff.
   **42 of 44 raster specs have ZERO recorded calls** - parity cannot lean on live
   traffic, so the frozen before-artifact is the only bar available.
2. **The retrieval table.** Before/after retrieval check per touched spec - the
   corpus queries must still route to the same tool. Folding does not change a
   docstring, but co-locating hooks moves files, and the loader is new.
3. **Registered count unchanged** at the post-attic number (174 -> 161 projected by
   the scope-move wave). The fold must not add or drop a tool.
4. **Suite zero.** All five slices from repo root with `venvs/agent`, foreground,
   each summary line read: slices 1-4
   `env -u TRID3NT_CACHE_BUCKET python -m pytest tests/test_[a-e]*.py -p no:cacheprovider --timeout=300 -q`
   (then `[f-o]`, `[p-r]`, `[s-z]`), slice 5 `contracts/tests` as its OWN
   invocation. Baseline is EXACTLY ZERO failures; anything else is investigated,
   and a flake claim needs an isolation rerun.
5. **One live packet**, plus the standing gates: daemon restart, `scripts/ws_smoke.py`
   (all_passed), the flood canary `scripts/run_sfincs_direct.py` (status=ok). The
   packet should exercise a folded vector row and a folded raster row in one case so
   the two lenses are proven together, and it gets the adversarial pre-delivery
   interrogation - cross-panel coherence, discrimination, georef/framing,
   run-vs-code freshness - before it reaches NATE.
6. **The honesty floor, explicitly re-proven.** For at least one folded spec per
   family, force a 404 and a 403 and show the typed EMPTY / auth-class split still
   holds through the library. This is the fold's single highest regression risk and
   it does not show up in a happy-path parity diff.

---

## 7. DESIGN questions for NATE

**Q1 - F7 / F1: is `fetch_dem` in either fold?**
STAC FOLD names "3DEP" in the raster migration list; FETCHER FOLD SECOND HALF names
HyRiver's py3dep for 3DEP. Measured, `fetch_dem` is **already** a py3dep delegate
(`terrain/fetch_dem/source.yaml:38-47`), so the HyRiver lens has nothing to do, and
the STAC lens would have to **substitute the source** (py3dep/TNM -> a PC STAC
elevation collection). That is a cross-DATASET substitution under the
data-source-fallback norm, not a fold, on the board's highest-traffic tool (1,591
calls, 634 consumers). It would also discard `hooks/dem_3dep.py:440-476`'s bbox
re-quantization INTO the cache key and the source-conditional
`DemAutoFallbackGateError`.
**Recommendation: `fetch_dem` is explicitly OUT of both folds, written into the
ledger with the reason, so no later pass re-opens it.** The same "it is on STAC so
it folds" temptation applies to GOES ABI, NLCD landcover, LANDFIRE and MODIS, all of
which have PC STAC mirrors; folding any of them changes which bytes the user gets
and is an author decision, never a fold-wave shim.

**Q2 - F1: custom reader driver, GDAL config, or signed reads only?**
Three defensible fates for odc-stac's closed reader (`odc/loader/_rio.py:601`).
(a) Write a `ReaderDriverSpec` (`odc/loader/_driver.py:17` registers `"rio"`)
wrapping `open_windowed_cog` - keeps the transport and the typed errors, but the
fold stops being "zero hook code" and we own a driver. (b) Accept GDAL's CPL HTTP
if Stage 0 Probe A goes green - simplest, and the LOC win is real. (c) The ruling's
own fallback: odc-stac for search + geobox + fuse, assets read through our
transport - keeps the transport but gives up most of the win, since the read loop is
where the LOC is.
**Recommendation: (b) gated on Probe A, with (a) as the named fallback and (c) only
if both fail.** The deciding fact is that F1 is worth -425 while F6's twelve specs
(-0, 1,619 LOC) stay on the transport regardless, so a GDAL-side regression would be
contained to eight specs rather than spread across the raster tree.

**Q3 - G1: `ESRIJSON` driver, pygeoogc's `ArcGISRESTful`, or pyesridump?**
Three libraries do this job. The driver is proven live here (87 rows, and 391 rows
paged automatically across 4 pages) and costs no new dependency beyond declaring
pyogrio. `ArcGISRESTful` is already paid for if HyRiver lands for G6 and has the
stronger retry/cache story. pyesridump is narrow and standalone but has the weakest
retry (linear, any-exception, no status distinction) and no cache.
**Recommendation: the `ESRIJSON` driver, gated on Stage 0 Probe B; `ArcGISRESTful`
kept in reserve for any layer that refuses to page; pyesridump deferred until a
layer actually refuses.** The measured cost of the driver is latency, not
correctness: 26.5 s for 947 features against a pooled httpx client that is faster.
Is that latency acceptable given the cache TTL classes absorb it?

**Q4 - G3: does OSMnx's 55 s fixed pause satisfy the Retry-After norm?**
OSMnx is the closest of the six libraries to the norm's intent - it reacts to 429
explicitly (`osmnx/_overpass.py:478-486`) - but the 55 s is hardcoded, not
header-driven, and Overpass rarely sends `Retry-After`. Folding also loses our
3-mirror chain unless we wrap it (~30 LOC).
**Recommendation: GO, with the 55 s accepted and documented as the Overpass-specific
answer, the ~30 LOC mirror wrapper budgeted, and a ~10 LOC error-mapping hook in the
`dataretrieval_delegate.py:15-19` style restoring the verbatim body.** -872 for
~40 LOC of shim is the best ratio in the census.

**Q5 - G4: do the eight residue rows in the bespoke family fold at all?**
-460 available across 8 of 18 specs (`fault_sources` -60, `storm_tracks` -200,
`openfema_disasters` -60, `storm_events_db` -40, `asos_metar` -40, `nws_alerts_conus`
-30, `climate_normals` -30, `nws_event` residual), each needing its own parity proof
against a bespoke source, for 9 percent of the family. Against that: the family
holds the honesty floor's sharpest cases - `fetch_firms_active_fire`'s
**200-with-an-error-body** auth split, which a driver read silently swallows.
**Recommendation: NO FOLD as a family. Take ONE row opportunistically -
`fetch_storm_tracks`' active-mode zip-shapefile leg to `/vsizip//vsicurl/` (-200,
and it deletes an extract-to-tmpdir) - and leave the other seven.** The counter-case
is `fetch_storm_events_db`: 158 live calls, second-most-exercised fetcher on the
board, so if live use is the ranking lever it argues for its own -40.

**Q6 - G1/G6/G7: is HyRiver adopted as a dependency at all?** *(its own question,
as asked)*
Adopting `pygeohydro` + `pynhd` buys ~810 LOC across 10 specs
(`fetch_usace_levees`, `fetch_fema_nfhl_zones`, `fetch_usace_dams`, the three NHD
rows, `fetch_high_water_marks`, `fetch_usgs_nwis_gauges`,
`fetch_nhdplus_nldi_navigate`, `fetch_noaa_nwm_streamflow`'s NLDI half) for a small
transitive cost (hydrosignatures 23 KB, defusedxml, h5netcdf) on a stack we already
half-own (pygeoogc 0.19.4, pygeoutils 0.19.5, py3dep 0.19.0, async-retriever 0.19.3
all installed). Against it: a **pin conflict that must be managed forever**
(pygeohydro caps pynhd<0.20 while pynhd is at 0.20.0), a **second SQLite response
cache** with a never-expire default sitting under our provenance rules, a retry
default that excludes 429, and a single-maintainer upstream.
The alternative is the GDAL driver path for the ESRI rows and keeping the two
US-hydro hooks - which costs ~500 LOC of the 810 but adds zero HyRiver coupling.
**Recommendation: ADOPT, pinned as `pygeohydro==0.19.4` + `pynhd==0.19.3`, with the
HyRiver cache expiry set explicitly and `status_to_retry=(429,500,502,503,504)`
passed at every call site.** The deciding argument is not LOC - it is that
`pygeohydro.NID` ships the dam_type/dam_purpose code tables and
`STNFloodEventData` ships the `hwms_query_params` set that we currently hand-carry
and must hand-maintain; that is the domain's own knowledge, which is exactly what
the ruling said the library should hold.

**Q7 - G6: fold the two calibration observation rows before, or after, calibration?**
`fetch_usgs_nwis_gauges` (34 consumers - `compute_skill_metrics`,
`compute_idf_curve`, `extract_model_at_observations`) and `fetch_noaa_coops_tides`
are the calibration wave's observation rows. Folding them and calibrating on them in
the same window puts two moving parts under one proof.
**Recommendation: fold `fetch_high_water_marks` in Stage 5 but HOLD
`fetch_usgs_nwis_gauges` until after calibration signs off**, and note that
`fetch_noaa_coops_tides` is unaffected either way because G8 does not fold. The
counter-case is that folding it first with a live A/B is cleaner than folding it
under a calibrated baseline later - which is why this is a question and not a
finding.

**Q8 - G8: is `noaa-coops` worth a dependency?**
No ruled library covers CO-OPS (`grep -i 'tidesandcurrents|coops'` across pygeohydro
and pynhd: zero hits). A community `noaa-coops` package exists but is outside the
ruled set, and adopting it would NOT remove the 552-LOC executor's real content -
the point-FGB schema at `station_timeseries.py:70-74`, whose `time_series_csv`
column is the inline series SFINCS and TELEMAC boundary forcing consume.
**Recommendation: NO. The library would replace the HTTP call and leave the product
contract untouched** - a new dependency for near-zero LOC on a seam the calibration
wave is about to lean on hard.

**Q9 - F8: does topobathy's warp-merge core open in this wave?**
~212 LOC (`hooks/topobathy.py:881-1104`) is a reproject-onto-a-shared-grid that
`odc.geo` GeoBox + `rioxarray.reproject_match` express, sitting inside a 1,933-LOC
composite with a 4-leg source precedence, a per-tile NAVD88 datum gate and a
coverage ladder. Its consumers include the om2d mesher
(`workflows/mesh/meshers/om2d.py`) and the TELEMAC agitation template
(`workflows/telemac/templates/agitation/agitation.py`), and it is the tree's
second-most-consumed spec at 167.
**Recommendation: NO in this wave.** -152 net against a datum gate and the mesh
wave's substrate is the worst risk/reward on the board. Revisit only if the mesh
wave is already opening that file for its own reasons.

**Q10 - all families: does the fold wave pay `_pc_stac.py`'s debt, or declare it?**
F1 kills ~133 of `_pc_stac.py`'s 262 LOC, but `compute_ndvi.py:54` and
`digitize_water_body.py:72,312,358,382` import the module, so it survives half-dead
- a clean-as-you-go violation the fold creates.
**Recommendation: pay it in Stage N** - migrate both processing tools to
`planetary_computer.sign` (they are already doing PC STAC search + signed reads, so
it is the same substitution the fetchers make) and delete the module. The
alternative, a ledger line with a CONDITION, is defensible only if the processing
ruling is about to rewrite both tools anyway.
