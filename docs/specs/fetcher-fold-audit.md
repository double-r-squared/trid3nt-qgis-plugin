# Fetcher fold - phase-1 audit

Companion to `docs/specs/data-router-fold.md`. Classifies every fetcher under
`server/src/trid3nt_server/agent/tools/fetchers/` for the generic data-router
fold: SHAPE (what it returns + how), FOLDABILITY (spec-expressible / bespoke /
hybrid), USAGE (telemetry + nested consumers), FAMILY grouping, and the 5 pilot
picks.

Read-only audit. Branch `refactor/engine-doors`. No code or infra changed.

## Governing policy (NATE, 2026-07-28)

- **No endpoint is ever cut.** Usage/telemetry is NOT a cut criterion - "we
  need to retain all working endpoints, they drive functionality." Every
  fetcher's endpoint survives the fold in one of three forms: spec-driven
  (YAML + router), hybrid (spec + a named transform hook), or retained code.
- There is **no "cut-without-replacement" class**. The only verdicts are
  SPEC-EXPRESSIBLE / BESPOKE / HYBRID.
- Telemetry counts appear in the table **only** as migration-ordering and
  pilot-selection information.
- Design constraint carried into every verdict: hand-rolled spec-driven
  sources must be **indistinguishable from catalog-native handling** - one
  uniform pipeline and one surface regardless of source origin.

## Scope

- **97 fetcher tool modules** (dir==stem convention: `fetch_X/fetch_X.py`),
  totalling **71,836 lines** across the whole `fetchers/` tree (38% of the
  agent surface, matches the data-router-fold spec's figure).
- **6 shared helpers** (not tools, already partial folds): `_fetch_common.py`
  (bbox validate + resolution quantize + typed error base), `_public_s3.py`
  (anon S3), `us_states.py` (state-code resolver), `imagery/_pc_stac.py`
  (Planetary Computer STAC search + SAS sign), `imagery/_satellite_slider.py`
  (frame->animation assembly), `ocean/_noaa_slr_raster.py` (SLR raster shim).
  These are the seeds of the router's shared modes.

## The core finding: the canonical fetcher anatomy

Reading two pilots in full (`fetch_gridmet`, `fetch_noaa_coops_tides`) plus
per-cluster reads of the rest shows every fetcher is the same skeleton:

**~75-85% is boilerplate the router provides ONCE:**
- typed error subclasses (`FooError` / `FooInputError` / `FooUpstreamError` /
  `FooEmptyError` carrying `error_code` + `retryable`)
- `_build_metadata()` + `AtomicToolMetadata` construction
- `estimate_payload_mb()` heuristic (bbox-area or station-count formula)
- `_validate_bbox` / `_validate_date_range` / `_validate_<enum>` /
  `_round_bbox_to_6dp` (bbox validate + quantize already live in
  `_fetch_common.py`)
- `read_through(metadata, params, ext, fetch_fn)` cache wiring
- `LayerURI(...)` construction at the end

**~15-25% is source-specific** and becomes the YAML spec:
- the endpoint URL(s) + how params are templated onto them
- the response -> COG or FlatGeobuf ingestion (keyed on declared shape)
- normalization directives (CRS, units, datum, quantity stamp)

So a ~650-line spec-expressible fetcher collapses to a ~40-60 line YAML spec
plus a shared router pipeline; roughly 85% of its Python is deleted. The fold
is real and large.

## SHAPE taxonomy used

- **raster-COG** - gridded data -> GeoTIFF/COG. Sub-modes the router needs:
  `STAC-search` (Planetary Computer / earth-search, + SAS/asset signing),
  `OPeNDAP-xarray` (THREDDS/CDS netCDF subset), `direct-COG-window`
  (rasterio windowed read of a known COG / ImageServer exportImage / gz `.tif`),
  `WMS-WCS`.
- **vector-API-FGB** - JSON / GeoJSON / ArcGIS FeatureServer-MapServer /
  WFS / FDSN query -> FlatGeobuf of features. The dominant shape.
- **station-timeseries-FGB** - discover stations, loop per-station data
  requests, point-FGB with inline `time_series_csv` attribute.
- **tiled-imagery-assembly** - multi-timestep/scene frames -> mosaic or
  animation (gif/mp4). The bespoke hotspot.
- **other** - point lookups returning a dict/table not a LayerURI, or
  named-entity resolvers returning a point+bbox.

## Foldable-lines heuristic

Per-module `foldable_lines` estimate = lines of Python removed and replaced by
YAML + shared router: SPEC-EXPRESSIBLE ~= 85% of module lines; HYBRID ~= 65%
(core folds, named transform hook stays); BESPOKE ~= 30% (only the shared
boilerplate folds; the irreducible logic stays as code).

## Telemetry (migration-ordering + pilot-selection only)

Source: `data/telemetry/tool_calls.jsonl`, full file = 3,826 records. Two
record types: 1,254 `tool_retrieval_shadow` (visibility shadow), 2,572 dispatch
records (`tool_name` + `success`). **Window is narrow: 2026-07-24 -> 2026-07-29
(~5 days)** - bench runs plus a handful of live sessions, NOT long-run
production history. Only 7 fetchers fired in-window; a 0 here means "not
exercised in this thin window," never "dead" (and per policy, never a cut
signal). Many 0-count fetchers are dispatched indirectly through workflows
(see nested consumers) which does not always emit a separate fetcher record.

Fired fetchers (dispatch count / successful):

| fetcher | dispatches | ok | note |
|---|---|---|---|
| fetch_dem | 1570 | 1040 | dominant - terrain is the workhorse; also nested in 7 workflows |
| geocode_location | 280 | 280 | every place-name turn; nested in 4 consumers |
| fetch_storm_events_db | 158 | 20 | high failure rate in-window (bench stress) |
| fetch_nws_alerts_conus | 36 | 36 | live NWS flood-event scenarios |
| fetch_wdpa_protected_areas | 20 | 20 | conservation bench cases |
| fetch_esri_landcover_10m | 17 | 17 | landcover bench + nested in 2 processing tools |

(Two 0-ok dispatch names in telemetry - `fetch_volcano_lava_flow`,
`compute_terrain_relief_v2` - map to NO module; they are model-hallucinated
tool names, not fetchers, and are excluded.)

All other 90 fetchers: 0 dispatches in-window. Migration ordering should be
driven by family cohesion + nested-consumer criticality, NOT by these counts.

## Nested consumers (the compatibility surface)

Fetchers imported directly by non-registry code (workflows / processing tools /
gates). The router must expose the same callable seam per source so these
migrate mechanically. This is the real "do not break" set:

| fetcher | nested consumers |
|---|---|
| fetch_dem | compute_contours, extract_model_at_observations, flood, model_dambreak_geoclaw_scenario, model_landslide_scenario, model_urban_flood_swmm, run_elmfire |
| geocode_location | compute_impact_envelope, flood, query_point_hazard, run_sfincs |
| fetch_topobathy | flood, model_dambreak_geoclaw_scenario, model_wave_scenario |
| fetch_buildings | compute_exposure_summary, model_urban_flood_swmm, sfincs_forcing_autowire |
| fetch_copernicus_dem | _hydrology_common, compute_sediment_yield, model_debris_flow |
| lookup_precip_return_period | compute_idf_curve, flood, model_urban_flood_swmm |
| fetch_statsgo_soils | compute_sediment_yield, model_debris_flow |
| fetch_esri_landcover_10m | compute_sediment_yield, compute_urban_heat_island |
| fetch_3dep_extra | model_landslide_scenario, model_urban_flood_swmm |
| fetch_goes_animation | model_goes_fire_animation, model_satellite_fire_animation |
| _satellite_slider (helper) | model_goes_fire_animation, model_satellite_fire_animation |
| fetch_goes_archive_animation | model_glm_lightning_animation |
| fetch_goes_satellite | model_glm_lightning_animation |
| fetch_glm_lightning | model_glm_lightning_animation |
| fetch_mtbs_burn_severity | model_debris_flow |
| fetch_naip | compute_canopy_height |
| fetch_modis_lst | compute_urban_heat_island |
| fetch_population | compute_exposure_summary |
| fetch_usace_nsi | compute_flood_depth_damage |
| fetch_usgs_groundwater_levels | compute_model_residuals |
| fetch_administrative_boundaries | region_choice (gate) |
| fetch_fault_sources | model_seismic_hazard_scenario |
| fetch_landfire_fuels | run_elmfire |
| fetch_landcover | flood |
| fetch_river_geometry | flood |
| fetch_gcn250_curve_numbers | sfincs_forcing_autowire |
| fetch_gtsm_tide_surge | sfincs_forcing_autowire |
| fetch_noaa_coops_tides | sfincs_forcing_autowire |
| fetch_noaa_nwm_streamflow | sfincs_forcing_autowire |
| fetch_usgs_nwis_gauges | sfincs_forcing_autowire |
| fetch_storm_tracks | sfincs_forcing_autowire |
| fetch_mrms_qpe | model_nws_flood_event_scenario |
| fetch_nws_alerts_conus | model_nws_flood_event_scenario |
| fetch_viirs_day_fire | model_satellite_fire_animation |
| _pc_stac (helper) | solver_confirm (gate), compute_change_detection, compute_ndvi, digitize_water_body, ogc_adapter |

The `sfincs_forcing_autowire` module is the single densest consumer (6 fetchers:
gcn250, gtsm, coops_tides, nwm_streamflow, nwis_gauges, storm_tracks) - the
SFINCS forcing stack. `flood` (5) and `model_urban_flood_swmm` (4) are next.
These flood workflows anchor the "do not regress" migration risk.

## Per-fetcher classification tables

Columns: fetcher | shape | verdict | foldable_lines | bespoke citation | family | notes.
Telemetry count column omitted where 0 (see telemetry section); the 7 fired
fetchers are noted inline.

<!-- FAMILY-TABLES-START -->

### biodiversity (7)

Family quirks: mixed auth (eBird key, IUCN token, Movebank Basic Auth via
vault; GBIF/iNat keyless); each "simple" occurrence API carries a
name->taxon-ID resolution sub-call; several APIs are radius-only or
geometry-less so a bbox/polygon has to be synthesized.

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_ebird_observations | vector-API-FGB (radius-only API) | HYBRID | 547 | `_bbox_to_tile_centers` L200-264, `_fetch_all_tiles` dedup L488-551 | api.ebird.org; keyed (3-path waterfall); bbox synthesized via overlapping-circle tile cover |
| fetch_gbif_occurrences | vector-API-FGB (paginated) | HYBRID | 441 | `_resolve_species_name_to_taxon_key` L161-267 | keyless offset pagination; hook = name->taxonKey with strict EXACT gate |
| fetch_inaturalist_observations | vector-API-FGB (paginated) | HYBRID | 420 | `_resolve_taxon_id`/`_coerce_taxon_id` L137-215 | keyless page pagination; hook = taxon name/id resolution via /v1/taxa |
| fetch_iucn_red_list_range | vector-API-FGB (record, no geometry) | HYBRID | 502 | placeholder-polygon + row synth L473-611 | keyed; API returns no geometry - fabricates placeholder square + found/not-found sentinel |
| fetch_mobi | raster-COG (STAC-search) | SPEC-EXPRESSIBLE | 356 | - | PC STAC + SAS sign + windowed reproject; layer param is a static asset-key table |
| fetch_movebank_tracks | vector-FGB (trajectory assembly) | BESPOKE | 264 | `_records_to_flatgeobuf_bytes` group/sort/LineString L475-625, `_resolve_credentials` dual-format vault L173-289 | Basic Auth (no keyless); per-individual LineString assembly with whole-track-in-bbox semantics |
| fetch_wdpa_protected_areas | vector-API-FGB (paginated ArcGIS) | HYBRID | 471 | `_normalize_designation_filter` curated vocab L117-281 | telemetry: 20 fired; ArcGIS envelope query; hook = 32-entry designation alias table |

### climate (7)

Family quirks: heavy netCDF/OPeNDAP (THREDDS gridMET, CDS ERA5), auth split
(CDS `~/.cdsapirc`, keyless CHIRPS/NCEI); "climate" reads are the raster-netCDF
proving ground for the router's OPeNDAP mode.

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_gridmet | raster-COG (OPeNDAP-xarray) | SPEC-EXPRESSIBLE | 590 | - | **PILOT (read in full).** THREDDS DAP agg URL template + time-subset + time-mean collapse -> COG; the raster-netCDF reference |
| fetch_chirps_precipitation | raster-COG (direct-COG-window, gz) | HYBRID | 374 | gzip decompress L266-289 + nodata collapse L344-345 | date-templated .tif.gz URL, keyless, array-slice window |
| fetch_climate_normals | station-timeseries-FGB (single value) | HYBRID | 408 | `_parse_inventory` fixed-width slicing L218-252 | NCEI inventory + per-station CSV; hook = fixed-width byte-offset parse |
| fetch_era5_reanalysis | raster-COG (OPeNDAP/CDS) | BESPOKE | 317 | `_cds_retrieve_with_timeout` L456-549, `_netcdf_to_da` L557-678, `_combine_wind_components` two-request stitch L774-818 | **awkward.** CDS async queue+poll, no native timeout, derived wind = 2 retrieves combined |
| fetch_modis_lst | raster-COG (STAC-search) | HYBRID | 406 | `_sign_href` per-href SAS fallback L261-285 | PC STAC + DN->degC scale; hook = MODIS Azure account needs per-href sign |
| fetch_us_drought_monitor | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 538 | - | Living Atlas query; current-vs-archive is a period-is-None switch |
| lookup_precip_return_period | other (point lookup -> dict/table) | BESPOKE | 200 | Atlas-2 synthesis `_depth_at` L326-424, two-tier fallback L552-665 | nested in compute_idf_curve, flood, swmm; NOT a LayerURI; Atlas-14 parse + offline Atlas-2 fallback synthesis |

### hazard (17)

Family quirks: dominated by ArcGIS FeatureServer/MapServer `/query` (resultOffset
or exceededTransferLimit pagination, `where=` clauses) - the single most
repeated pattern in the whole tree and the best SPEC evidence. Bespoke offenders
cluster around two-source stitch+join (openfema, volcano_alerts) and
named-entity resolvers (wfigs); FIRMS adds a MAP_KEY auth cascade.

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_epa_frs_facilities | vector-API-FGB (multi-layer union) | HYBRID | 514 | `_normalize_superfund_feature` L502-546 + 5-layer union L639-671 | program->layer dict; "frs" sentinel unions 5 layers |
| fetch_fault_sources | vector-API-FGB (static whole-file) | BESPOKE | 189 | `_parse_fault_feature` L268-313 + `_filter_faults_to_bbox` L316-328 | nested in model_seismic_hazard; one fixed 10.6MB GEM GeoJSON, all filtering client-side |
| fetch_fema_nfhl_zones | vector-API-FGB (ArcGIS) | HYBRID | 504 | `_fetch_nfhl_features` OBJECTID-cursor L381-490 | nonstandard OBJECTID-watermark pagination; partial-result-on-500 |
| fetch_firms_active_fire | vector-API-FGB (path-templated CSV) | HYBRID | 503 | `_resolve_map_key` L280-327 + `_fetch_firms_csv` auth-sniff L523-582 | NASA FIRMS MAP_KEY cascade; auth failure via response body text |
| fetch_hifld_critical_infrastructure | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 528 | - | facility_type->service routing dict, resultOffset pagination |
| fetch_hifld_transmission_lines | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 501 | - | single FeatureServer, optional VOLTAGE where-clause |
| fetch_landfire_fuels | raster-COG (ImageServer exportImage) | HYBRID | 349 | `_is_all_nodata` pixel-read L360-404 | nested in run_elmfire; layer->service dict is spec; hook = client-side nodata detection |
| fetch_mtbs_burn_severity | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 450 | - | nested in model_debris_flow; single FeatureServer, YEAR where-clause |
| fetch_nifc_fire_perimeters | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 436 | - | unpaginated single-shot; bbox optional |
| fetch_openfema_disasters | vector-API-FGB (2-source stitch+aggregate) | BESPOKE | 260 | stitch L626-669 + `_aggregate_by_county` L442-491 + `_resolve_states` L304-334 | OpenFEMA OData + TIGERweb county polys joined on FIPS |
| fetch_tsunami_events | vector-API-FGB (dual-mode JSON) | HYBRID | 588 | `_parse_items` events-vs-runups remap L443-518 | NCEI Hazel; hook = field-name remap by observation_type |
| fetch_usace_dams | vector-API-FGB (dual-endpoint token auth) | BESPOKE | 375 | `_fetch_nid_bytes` L967-1028 + `_resolve_nid_token` L644-679 + `_build_where_clause` L496-530 | largest (1249 L); authoritative-token vs public-mirror; ESRI 498/499 credential signaling |
| fetch_usace_levees | vector-API-FGB (ArcGIS sub-layer routing) | SPEC-EXPRESSIBLE | 592 | - | layer->sub-layer-id dict, exceededTransferLimit pagination |
| fetch_usfs_canopy_fuels | raster-COG (ImageServer exportImage) | HYBRID | 340 | `_is_all_nodata` pixel-read L340-378 | near-duplicate of landfire_fuels; same nodata hook |
| fetch_usgs_earthquakes | vector-API-FGB (FDSN JSON) | SPEC-EXPRESSIBLE | 795 | - | single FDSN query; bbox/time/magnitude params; no auth |
| fetch_usgs_volcano_alerts | vector-API-FGB (2-source stitch+join) | BESPOKE | 238 | `_join_alerts_to_coords` L379-434 + 2-endpoint fetch L548-600 | getMonitoredVolcanoes + getUSVolcanoes joined on vnum |
| fetch_wfigs_incident | other (named-incident resolver) | BESPOKE | 181 | `_build_wfigs_params` token-OR LIKE L196-245 + `_select_best_feature` L291-322 + `_resolve_incident` fallback L424-486 | returns a dict (point+bbox), not a LayerURI |

### hydrology (13)

Family quirks: USGS OGC/NWIS/WQP (some return RDB or CSV not JSON), NLDI graph
navigation (mostly server-side), and a cluster of two-source geometry+values
joins. Bespoke offenders are format-parser + multi-hop cases; Overpass appears
here too (river_geometry).

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_cama_flood_discharge | raster-COG (netCDF download) | BESPOKE | 257 | `_fetch_cama_nc_to_tempfile` L360-417 + `_netcdf_to_cog_bytes` L424-625 | probes 6 filenames, HTML-migration sentinel sniff, var discovery + multi-year concat |
| fetch_flood_extent_observation | tiled-imagery-assembly (MODIS mosaic) | BESPOKE | 193 | `_tiles_for_bbox` L315-324 + mosaic L380-457 | fixed 10-deg MCDWD tile grid, per-tile GET+reproject, first-valid-wins mosaic |
| fetch_high_water_marks | vector-API-FGB (event/state scoped) | HYBRID | 456 | `_resolve_event_id` L294-321 + `_states_overlapping_bbox` L324-333 | 2 STN JSON endpoints; event-name resolution + state-bbox fallback |
| fetch_jrc_global_surface_water | raster-COG (STAC via _pc_stac) | HYBRID | 436 | `_band_colormap` L204-211 + `_sign_href` L323-356 | reuses shared PC-STAC; hooks = per-band palette + /sign-href auth |
| fetch_nhdplus_nldi_navigate | vector-API-FGB (NLDI server-side walk) | HYBRID | 366 | `_snap_point_to_comid` L203-237 | one navigate call; only hook = optional point->COMID snap |
| fetch_nhd_waterbodies | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 454 | - | HR primary / medium-res fallback, identical schema, resultOffset |
| fetch_noaa_nwm_streamflow | station-timeseries-FGB (S3 netCDF + NLDI) | BESPOKE | 263 | `_resolve_nwm_key` L317-390 + `_discover_comids_in_bbox` L450-472 + join L597-685 | nested in sfincs_forcing_autowire; S3 XML discovery + 5x5 NLDI grid-sample + per-COMID join |
| fetch_nwi_wetlands | vector-API-FGB (ArcGIS, esri-json fallback) | HYBRID | 379 | `_esri_json_to_features`/`_rings_to_geojson` L219-256 | WAF needs browser-spoofed headers; esri-json rings->GeoJSON hook |
| fetch_nws_river_forecast | vector-API-FGB (per-gauge fan-out) | HYBRID | 626 | `_enrich_thresholds` L627-650 + `_enrich_series` L652-692 | bbox gauge-list is spec; 2 opt-in per-gauge fan-outs |
| fetch_river_geometry | vector-API-FGB (Overpass + GDB fallback) | BESPOKE | 246 | `_build_overpass_waterway_ql` L269-286 + `_fetch_nhdplushr_geometry_bytes` L535-669 | nested in flood; hand-built Overpass QL + HUC4 GDB zip download/extract/clip |
| fetch_usgs_groundwater_levels | vector-API-FGB (OGC join) | SPEC-EXPRESSIBLE | 741 | - | nested in compute_model_residuals; 2 OGC endpoints left-joined by location id |
| fetch_usgs_nwis_gauges | vector-API-FGB dual-mode (+windowed series) | BESPOKE | 366 | `_parse_site_rdb` L652-708 + `_parse_iv_json_window` L527-649 + `_build_window_flatgeobuf` L765-820 | nested in sfincs_forcing_autowire; RDB tab-delimited fallback parser + distinct windowed-series schema (inline time_series_csv) |
| fetch_usgs_water_quality | vector-API-FGB (station+result CSV join) | HYBRID | 541 | `_parse_result_csv` L405-453 + `_join_sites` L456-484 | Result service is CSV; latest-by-date dedup + left-join hook |

### ocean (8)

Family quirks: NOAA CO-OPS (station catalog + datagetter), Copernicus CDS
(GTSM, same auth wrinkle as ERA5), ArcGIS SLR services, and a heavy
multi-source DEM/bathy stitch (topobathy). The SLR trio share
`_noaa_slr_raster.py` (MapServer export -> georeferenced RGBA COG).

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_noaa_coops_tides | station-timeseries-FGB | SPEC-EXPRESSIBLE | 685 | - | **PILOT (read in full).** station catalog discover + per-station datagetter loop + inline time_series_csv -> FGB; the station-timeseries reference. Nested in sfincs_forcing_autowire |
| fetch_noaa_coops_currents | station-timeseries-FGB (snapshot) | HYBRID | 497 | `_parse_predictions` L439-495 | sibling of tides; hook = flood/ebb/slack direction from velocity sign |
| fetch_noaa_slr_confidence | raster-COG (WMS/export via helper) | SPEC-EXPRESSIBLE | 110 | - | MapServer export PNG -> RGBA COG via `_noaa_slr_raster`; slr_ft enum->service lookup |
| fetch_noaa_slr_marsh | raster-COG (WMS/export via helper) | SPEC-EXPRESSIBLE | 111 | - | same shared helper; slr_ft (0.5-ft step) enum->service lookup |
| fetch_noaa_slr_scenarios | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 603 | - | FeatureServer query per scenario_ft, looped + merged with scenario columns |
| fetch_noaa_sst | raster-COG (OPeNDAP/ERDDAP griddap) | HYBRID | 363 | `_fetch_griddap_nc` L265-297 | ERDDAP griddap selector (lat-descending slice) + 404-body no-data disambiguation |
| fetch_topobathy | raster-COG (multi-source stitch) | BESPOKE | 481 | `_build_merged_topobathy` L751-899 + `_merge_sources_rasterio` L1064-1163 | nested in flood/dambreak/wave; CUDEM manifest + ETOPO + NCEI-STAC + fetch_dem, NAVD88 gate, heterogeneous-CRS composite |
| fetch_gtsm_tide_surge | station-timeseries-FGB (CDS netCDF/ZIP) | BESPOKE | 323 | `_cds_retrieve_with_timeout` L425-489 + `_netcdf_to_gauge_records` L531-708 | nested in sfincs_forcing_autowire; CDS auth (4-path), ZIP of monthly netCDFs, station axis by exclusion |

### soil (4)

Family quirks: mostly global single-COG/VRT windowed reads (SoilGrids Homolosine
reproject, GCN250, HRSL-style); one vendor-SDK path (statsgo via pfdf), one
station-timeseries (SNOTEL).

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_gcn250_curve_numbers | raster-COG (direct-COG-window) | SPEC-EXPRESSIBLE | 460 | - | nested in sfincs_forcing_autowire; single global GeoTIFF per AMC enum, vsicurl window, already EPSG:4326 |
| fetch_snotel_snow | station-timeseries-FGB (latest snapshot) | SPEC-EXPRESSIBLE | 659 | - | NRCS AWDB stations (client-side bbox filter) + one batched multi-station call |
| fetch_soilgrids | raster-COG (direct-COG-window + reproject) | SPEC-EXPRESSIBLE | 512 | - | ISRIC VRT per property/depth via vsicurl, Homolosine->EPSG:4326, per-property scale divisor |
| fetch_statsgo_soils | other (vendor-SDK call) | HYBRID | 315 | `_fetch_statsgo_field_bytes` L218-320 | nested in compute_sediment_yield, model_debris_flow; wraps pfdf.data.usgs.statsgo library (no URL/params), result re-serialized to COG |

### socioeconomic (14)

Family quirks: the OSM/Overpass QL cluster (buildings, roads, pois - all
BESPOKE), Census two-endpoint geometry+values joins (TIGERweb + ACS/LODES),
static TIGER ZIP downloads with per-region routing tables, and the geocoder.

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_administrative_boundaries | vector-API-FGB (static TIGER ZIP + clip) | HYBRID | 449 | `_state_fips_for_bbox` L192-216 + place-level merge L491-580 | nested in region_choice gate; state/county/zcta = nationwide ZIP; place = per-state ZIPs merged via bbox->FIPS table |
| fetch_buildings | vector-API-FGB (Overpass + PC fallback) | BESPOKE | 225 | `_build_overpass_buildings_ql` L100-120 + `_fetch_msft_buildings_bytes` L449-540 | nested in exposure/swmm/sfincs; dual-source osm<->msft; msft path often degrades to placeholder |
| fetch_cdc_svi | vector-API-FGB (ArcGIS) | SPEC-EXPRESSIBLE | 499 | - | paginated ArcGIS query, -999 null-sentinel normalize |
| fetch_census_acs | vector-API-FGB (2-endpoint join) | HYBRID | 523 | `_fetch_acs_bytes` join L662-674 + `_compute_value` L550-568 | TIGERweb + census.gov joined on GEOID; optional CENSUS_API_KEY with keyless fallback |
| fetch_epa_ejscreen | vector-API-FGB (ArcGIS esri-json) | SPEC-EXPRESSIBLE | 672 | - | f=json + rings->GeoJSON quirk; indicator->field alias table |
| fetch_field_boundaries | vector-API-FGB (GeoParquet range-read) | HYBRID | 369 | `_select_dataset` L210-246 + `_read_fields_gdf` L296-408 | 3-dataset registry (US/Japan/Denmark); CRS-aware bbox pushdown; honest no-coverage |
| fetch_ghsl_population | raster-COG (tiled window + mosaic) | SPEC-EXPRESSIBLE | 463 | - | 10-deg tile grid math (param-expressible) + tile-boundary merge via /vsizip//vsicurl/ |
| fetch_hrsl_population | raster-COG (direct-COG-window) | SPEC-EXPRESSIBLE | 416 | - | single global VRT via vsicurl, window read + COG rewrite |
| fetch_lehd_jobs | vector-API-FGB (2-endpoint join) | HYBRID | 462 | `_aggregate_wac_to_tract`/`_parse_wac_csv` L372-468 | TIGERweb + LODES WAC csv.gz per state; block->tract aggregation join |
| fetch_overpass_pois | vector-API-FGB (generic Overpass) | BESPOKE | 227 | `_build_overpass_ql` L329-349 + `_resolve_tag` L261-321 | generic key=value tags, 3-mirror fallback in `_post_overpass` L352-406 |
| fetch_population | raster-COG + vector dual-source router | HYBRID | 376 | dispatch L520-578 + `_iso3_for_lonlat`/`_worldpop_year` L97-149 | nested in compute_exposure_summary; WorldPop raster primary (full-country DL, no range) + ACS points opt-in |
| fetch_roads_osm | vector-API-FGB (Overpass) | BESPOKE | 205 | `_build_overpass_ql` L210-229 | Overpass QL + bbox-clip-to-AOI on LineStrings L446-454 |
| fetch_usace_nsi | vector-API-FGB (REST POST body) | SPEC-EXPRESSIBLE | 536 | - | nested in compute_flood_depth_damage; POST FeatureCollection body; Pelicun column aliasing |
| geocode_location | other (Nominatim fuzzy geocoder) | BESPOKE | 267 | `_extract_us_state` L105-241 + area-intent/AOI-floor L525-655 | telemetry: 280 fired; nested in 4 consumers; state-snap + POI-vs-place reorder + min-AOI-floor |

### weather (13)

Family quirks: station-timeseries via IEM (ASOS/RAWS discover-then-bulk), air
quality APIs (AirNow/OpenAQ, 3-path key auth), NOAA S3 gridded (HRRR zarr, MRMS
GRIB - shared cycle/hour-walkback hook), NWS API GeoJSON (alerts/event), and two
bulk-file BESPOKE parsers (storm_events CSV, storm_tracks HURDAT/IBTrACS).

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_airnow_air_quality | vector-API-FGB | SPEC-EXPRESSIBLE | 774 | - | single bbox+window GET, 3-path key auth, parameter alias + dedup-latest |
| fetch_asos_metar | station-timeseries-FGB | SPEC-EXPRESSIBLE | 734 | - | per-state IEM GeoJSON discover + ONE bulk multi-station CSV; canonical station mode |
| fetch_glm_lightning | raster-COG (netCDF binning + animation) | BESPOKE | 187 | `_bin_ged` L258-321 + `_ged_to_purple_rgba` L338-358 + animation fan-out L582-624 | nested in model_glm_lightning_animation; point->grid energy binning + colorizer + multi-frame |
| fetch_hrrr_forecast | raster-COG (zarr-S3) | HYBRID | 571 | `_resolve_cycle` L320-356 | zarr open + LCC reproject is spec; hook = cycle walkback + derived 10m wind (2 components) |
| fetch_hrrr_smoke | raster-COG (zarr-S3) | HYBRID | 514 | `_resolve_cycle` L320-356 | same zarr+reproject as forecast; cycle walkback hook |
| fetch_mrms_qpe | raster-COG (GRIB) | HYBRID | 532 | `_resolve_qpe_key` L350-411 | nested in model_nws_flood_event_scenario; S3 hourly-key walkback + GDAL-GRIB decode |
| fetch_nexrad_reflectivity | raster-COG (WMS passthrough) | SPEC-EXPRESSIBLE | 241 | - | SURPRISE: pure WMS GetMap URL template, cacheable=False, never fetches pixels |
| fetch_nws_alerts_conus | vector-API-FGB | HYBRID | 540 | `_resolve_zone_geometries` L443-544 | telemetry: 36 fired; nested in nws_flood_event; hook = zone-reference resolution for null-geometry alerts |
| fetch_nws_event | vector-API-FGB | SPEC-EXPRESSIBLE | 502 | - | state/FIPS/bbox canonicalization + GeoJSON->FGB; drops null-geom (no zone resolution) |
| fetch_openaq_measurements | vector-API-FGB (list + latest join) | SPEC-EXPRESSIBLE | 860 | - | paginated locations + per-location latest + sensor->parameter join, 3-path key |
| fetch_raws_weather | station-timeseries-FGB | SPEC-EXPRESSIBLE | 731 | - | per-state IEM DCP GeoJSON discover + per-station-per-day loop + SHEF rename |
| fetch_storm_events_db | vector-API-FGB (bulk CSV) | BESPOKE | 262 | `_resolve_csv_url` L322-364 + `_parse_filter_and_serialize` L394-605 | telemetry: 158 fired (20 ok); directory-index scrape for current date suffix + heavy pandas filter |
| fetch_storm_tracks | vector-API-FGB (dual-format tracks) | BESPOKE | 375 | `_parse_ibtracs_csv` L441-553 + `_fetch_forecast_track_points` L668-767 + `_build_line_flatgeobuf` L827-882 | nested in sfincs_forcing_autowire; IBTrACS CSV + NHC JSON/shapefile, basin selection, storm-grouping line assembly |

### imagery (9)

Family quirks: the BESPOKE hotspot. Two shared helpers seed router modes but
only cover half the work: `_pc_stac.py` (STAC search + SAS sign + pixel sizing)
and `_satellite_slider.py` (PNG-tile frame stitch). The animation cluster needs
**two distinct assembly modes** (SLIDER-PNG-stitch vs raw-netCDF-band-math), and
3 of 6 STAC fetchers deviate from the helper's own primitives (hand-rolled
`_select_scene`, nonstandard per-href sign). GOES archive hides an
engine-grade fire-detection algorithm.

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_goes_animation | tiled-imagery-assembly (SLIDER) | BESPOKE | 253 | `_build_frame_list` L243-261 + `_blend_animation_impl` L611-791 | nested in goes/satellite fire animations; SLIDER PNG stitch; band="blend" reroutes to dual-product composite |
| fetch_goes_archive_animation | tiled-imagery-assembly (raw-netCDF band-math) | BESPOKE | 493 | `_warp_band_to_physical` L869-975; fire-detection/RGB bakers L572-805; `_list_archive_keys_in_window` L453-531 | largest file (1644 L); nested in glm_lightning_animation; Matson-Dozier fire mask; does NOT use the slider stitch |
| fetch_goes_active_fire | tiled-imagery-assembly (delegates) | HYBRID | 165 | fetch_fn lambda L205-207 | delegates all band math to archive sibling; only wires params/loop/LayerURI |
| fetch_goes_satellite | raster-COG (raw ABI netCDF, single ts) | BESPOKE | 256 | `_reproject_and_clip` L508-666 + `_list_recent_keys` L368-462 | nested in glm_lightning_animation; hand-rolled S3 REST-XML listing (not boto3) |
| fetch_viirs_day_fire | tiled-imagery-assembly (SLIDER polar-pass) | HYBRID | 325 | `_local_solar_hour`/`_is_daytime_pass` L172-191 | nested in satellite_fire_animation; per-frame = 1 stitch + 1 mosaic; sole hook = daytime-pass astronomical filter |
| fetch_landsat_imagery | raster-COG (STAC-search) | BESPOKE | 230 | `_select_scene` L280-342; QA/stretch/thermal band ops L397-494; branch L560-599 | 3 divergent band_combo paths, QA-bitmask cloud mask, DN->reflectance/Kelvin scaling |
| fetch_naip | raster-COG (STAC-search) | SPEC-EXPRESSIBLE | 315 | - | cleanest use of shared `_pc_stac` primitives; plain 3-band vsicurl window read |
| fetch_sentinel1_sar | raster-COG (STAC-search) | BESPOKE | 180 | `_select_scene` L254-317 + `_power_to_db` L376-392 | custom coverage-then-recency scene ranking (bypasses helper) + SAR power->dB |
| fetch_sentinel2_truecolor | raster-COG (STAC-search) | HYBRID | 336 | `_truecolor_from_bands` L255-288 | uses shared search; single isolable SCL-mask + percentile-stretch hook |

### terrain (5)

Family quirks: raster-COG DEMs and landcover; the two BESPOKE ones are the
highest-value-in-use. `fetch_dem` (1570 telemetry dispatches, nested in 7
workflows) is a canonical multi-endpoint fallback stitch; `fetch_landcover` is a
hand-built GDAL COG-translate pipeline with legend remap.

| fetcher | shape | verdict | foldable | bespoke citation | notes |
|---|---|---|---|---|---|
| fetch_3dep_extra | raster-COG (pfdf-wrapped TNM) | SPEC-EXPRESSIBLE | 440 | - | nested in landslide/swmm; single pfdf.dem.read() + standard COG write |
| fetch_copernicus_dem | raster-COG (STAC-search) | HYBRID | 333 | `_sign_href` L186-214 | nested in sediment/debris/hydrology; single-collection search + tile mosaic; hook = per-href PC sign |
| fetch_dem | raster-COG (3DEP direct + Copernicus STAC fallback) | BESPOKE | 188 | `fetch_dem` L531-590 + `_fetch_3dep_dem_bytes_bounded` L325-372 + auto-coarsen L89-133 | telemetry: **1570 dispatches (top fetcher)**, nested in 7 workflows; multi-endpoint fallback stitch w/ bounded-timeout thread + honest fallback_note |
| fetch_esri_landcover_10m | raster-COG (STAC-search, tiled mosaic) | HYBRID | 525 | `_plan_tile_grid` L172-213 + multi-tile branch L555-688 | telemetry: 17 fired; nested in sediment/UHI; hook = auto-tiling AOIs >0.5 deg^2 + rasterio.merge |
| fetch_landcover | raster-COG (WMS-WCS NLCD; ESA STAC stub) | BESPOKE | 252 | `_fix_nlcd_background_transparency` L393-457 + GDAL COG pipeline L163-378 + auto-coarsen L700-748 | nested in flood; pixel-value legend remap + hand-built GDAL-CLI->rasterio COG-translate-with-overviews |

<!-- FAMILY-TABLES-END -->

## Verdict summary

<!-- SUMMARY-START -->

<!-- SUMMARY-END -->
