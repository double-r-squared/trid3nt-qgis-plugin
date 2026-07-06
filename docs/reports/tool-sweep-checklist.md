# TRID3NT Local tool sweep -- direct-execution checklist

Updated: 2026-07-06T16:54:01  
Total 176 | PASS 137 | KEY 5 | FAIL 19 | TIMEOUT 3 | SKIP-ARGS 12

AOI: ~3km downtown Tampa. KEY = needs an API key, earmarked for later.

| tool | status | time | note |
|---|---|---|---|
| aggregate_claims_across_sources | SKIP-ARGS | 0s | required params not fabricatable: ['sources', 'claim_targets'] |
| aggregate_property_within_zone | SKIP-ARGS | 0s | required params not fabricatable: ['value_layer_uri'] |
| analyze_affected_fields | SKIP-ARGS | 0s | required params not fabricatable: ['plume_layer_uri'] |
| catalog_fetch | FAIL | 8s | OGCAdapterError: OGC ARCGIS_REST GET failed for url=https://hazards.fema.gov/arcgis/rest/s |
| catalog_search | PASS | 0s |  |
| clip_raster_to_bbox | PASS | 1s |  |
| clip_raster_to_polygon | PASS | 0s |  |
| clip_vector_to_polygon | PASS | 0s |  |
| code_exec_request | FAIL | 0s | CodeExecConfirmationRequired: code_exec_request requires user confirmation before running: |
| compute_aspect | PASS | 0s |  |
| compute_blended_composite | PASS | 0s |  |
| compute_building_density | PASS | 66s |  |
| compute_canopy_height | PASS | 40s |  |
| compute_colored_relief | PASS | 0s |  |
| compute_contours | PASS | 13s |  |
| compute_cross_section | PASS | 0s |  |
| compute_hillshade | PASS | 0s |  |
| compute_home_range_kde | PASS | 1s |  |
| compute_impact_envelope | SKIP-ARGS | 0s | required params not fabricatable: ['flood_layer_uri'] |
| compute_impervious_surface | PASS | 0s |  |
| compute_layer_bounds | PASS | 0s |  |
| compute_movement_trajectory | FAIL | 0s | MovementTrajectoryError: points layer 's3://trid3nt-cache/cache/dynamic-1h/usgs_earthquake |
| compute_ndvi | PASS | 8s |  |
| compute_overtopping | PASS | 0s |  |
| compute_slope | PASS | 0s |  |
| compute_terrain_profile | PASS | 0s |  |
| compute_wave_nomograph | PASS | 0s |  |
| compute_zonal_statistics | PASS | 0s |  |
| count_features_above_threshold | PASS | 0s |  |
| cut_features_with_polygon | FAIL | 0s | CutFeaturesError: the cutter polygon fully consumed every target feature and delete_emptie |
| describe_qgis_algorithm | FAIL | 0s | RuntimeError: QGIS discovery tool invoked but worker submitter is not bound; agent service |
| digitize_water_body | PASS | 13s |  |
| discover_dataset | PASS | 2s |  |
| enhance_satellite_image | FAIL | 0s | EnhanceSatelliteImageError: input has 1 band(s); enhance_satellite_image polishes 3(+)-ban |
| extract_landcover_class | PASS | 0s |  |
| fetch_3dep_extra | PASS | 9s |  |
| fetch_administrative_boundaries | PASS | 36s |  |
| fetch_airnow_air_quality | KEY | 0s | AirNowMissingKeyError: no AirNow API key available: pass api_key=..., secret_ref=..., or s |
| fetch_asos_metar | PASS | 2s |  |
| fetch_buildings | PASS | 7s |  |
| fetch_cama_flood_discharge | FAIL | 3s | CaMaFloodUnreachableError: CaMa-Flood data source migrated: the kickoff-named U.Tokyo Hydr |
| fetch_cdc_svi | PASS | 1s |  |
| fetch_census_acs | PASS | 3s |  |
| fetch_chirps_precipitation | PASS | 5s |  |
| fetch_climate_normals | TIMEOUT | 420s | exceeded 420s (thread abandoned) |
| fetch_copernicus_dem | PASS | 6s |  |
| fetch_dem | PASS | 0s |  |
| fetch_ebird_observations | KEY | 0s | EBirdMissingKeyError: no eBird API key available: pass api_key=..., secret_ref=..., or set |
| fetch_epa_ejscreen | PASS | 1s |  |
| fetch_epa_frs_facilities | PASS | 5s |  |
| fetch_era5_reanalysis | KEY | 0s | ERA5MissingKeyError: No Copernicus CDS API key is configured (cdsapi: Missing/incomplete c |
| fetch_esri_landcover_10m | PASS | 6s |  |
| fetch_fault_sources | PASS | 1s |  |
| fetch_fema_nfhl_zones | PASS | 4s |  |
| fetch_field_boundaries | PASS | 34s |  |
| fetch_firms_active_fire | FAIL | 1s | FirmsAuthError: FIRMS rejected the MAP_KEY. Set GRACE2_FIRMS_MAP_KEY to a valid key from h |
| fetch_gbif_occurrences | SKIP-ARGS | 0s | required params not fabricatable: ['species_key'] |
| fetch_gcn250_curve_numbers | PASS | 11s |  |
| fetch_ghsl_population | PASS | 13s |  |
| fetch_glm_lightning | PASS | 31s |  |
| fetch_goes_active_fire | PASS | 56s |  |
| fetch_goes_animation | PASS | 118s |  |
| fetch_goes_archive_animation | PASS | 107s |  |
| fetch_goes_blend_animation | FAIL | 1s | GOESAnimEmptyError: no SLIDER geocolor frames for goes-18/conus in window 2026-07-05T18:00 |
| fetch_goes_satellite | PASS | 45s |  |
| fetch_gridmet | PASS | 2s |  |
| fetch_gtsm_tide_surge | FAIL | 0s | GTSMUpstreamError: CDS retrieve failed: Missing/incomplete configuration file: /home/nate/ |
| fetch_hifld_critical_infrastructure | PASS | 1s |  |
| fetch_hifld_transmission_lines | PASS | 2s |  |
| fetch_hrrr_forecast | PASS | 25s |  |
| fetch_hrrr_smoke | PASS | 54s |  |
| fetch_hrsl_population | PASS | 13s |  |
| fetch_inaturalist_observations | PASS | 1s |  |
| fetch_iucn_red_list_range | KEY | 0s | IUCNAuthError: no IUCN Red List API key resolved; pass api_key=, secret_ref=, or set $GRAC |
| fetch_jrc_global_surface_water | PASS | 16s |  |
| fetch_landcover | PASS | 1s |  |
| fetch_landfire_fuels | PASS | 24s |  |
| fetch_landsat_imagery | PASS | 9s |  |
| fetch_lehd_jobs | PASS | 20s |  |
| fetch_mobi | PASS | 9s |  |
| fetch_modis_lst | PASS | 5s |  |
| fetch_movebank_tracks | SKIP-ARGS | 0s | required params not fabricatable: ['study_id'] |
| fetch_mrms_qpe | PASS | 18s |  |
| fetch_mtbs_burn_severity | PASS | 3s |  |
| fetch_naip | PASS | 0s |  |
| fetch_nexrad_reflectivity | PASS | 0s |  |
| fetch_nhdplus_nldi_navigate | PASS | 1s |  |
| fetch_nifc_fire_perimeters | PASS | 6s |  |
| fetch_noaa_coops_currents | PASS | 3s |  |
| fetch_noaa_coops_tides | PASS | 4s |  |
| fetch_noaa_nwm_streamflow | PASS | 44s |  |
| fetch_noaa_slr_confidence | PASS | 1s |  |
| fetch_noaa_slr_marsh | PASS | 2s |  |
| fetch_noaa_slr_scenarios | PASS | 23s |  |
| fetch_noaa_sst | PASS | 92s |  |
| fetch_nws_alerts_conus | PASS | 1s |  |
| fetch_nws_event | FAIL | 1s | NWSUpstreamError: FlatGeobuf write failed for 3 features: Could not add feature to layer a |
| fetch_nws_river_forecast | PASS | 1s |  |
| fetch_openaq_measurements | KEY | 0s | OpenAQMissingKeyError: no OpenAQ API key available: pass api_key=..., secret_ref=..., or s |
| fetch_openfema_disasters | PASS | 74s |  |
| fetch_overpass_pois | PASS | 2s |  |
| fetch_population | PASS | 0s |  |
| fetch_raws_weather | PASS | 26s |  |
| fetch_river_geometry | PASS | 144s |  |
| fetch_roads_osm | PASS | 3s |  |
| fetch_sentinel1_sar | PASS | 7s |  |
| fetch_sentinel2_truecolor | PASS | 8s |  |
| fetch_snotel_snow | PASS | 12s |  |
| fetch_soilgrids | PASS | 38s |  |
| fetch_statsgo_soils | PASS | 5s |  |
| fetch_storm_events_db | TIMEOUT | 420s | exceeded 420s (thread abandoned) |
| fetch_topobathy | PASS | 170s |  |
| fetch_tsunami_events | PASS | 1s |  |
| fetch_us_drought_monitor | PASS | 1s |  |
| fetch_usace_dams | PASS | 1s |  |
| fetch_usace_levees | PASS | 1s |  |
| fetch_usace_nsi | PASS | 4s |  |
| fetch_usfs_canopy_fuels | PASS | 23s |  |
| fetch_usgs_earthquakes | PASS | 0s |  |
| fetch_usgs_groundwater_levels | PASS | 9s |  |
| fetch_usgs_nwis_gauges | PASS | 0s |  |
| fetch_usgs_volcano_alerts | PASS | 64s |  |
| fetch_usgs_water_quality | PASS | 8s |  |
| fetch_viirs_day_fire | PASS | 114s |  |
| fetch_wdpa_protected_areas | PASS | 1s |  |
| fetch_wfigs_incident | PASS | 1s |  |
| fill_gaps | FAIL | 0s | FillGapsError: no enclosed gaps (interior rings) were found in the union of the supplied p |
| generate_choropleth_legend | PASS | 0s |  |
| generate_damage_distribution | SKIP-ARGS | 0s | required params not fabricatable: ['damage_layer_uri'] |
| generate_histogram | PASS | 0s |  |
| generate_time_series | FAIL | 0s | ChartToolError: This raster has no time dimension (single band or no time tags). Use gener |
| geocode_location | PASS | 0s |  |
| list_categories | PASS | 0s |  |
| list_qgis_algorithms | FAIL | 0s | RuntimeError: QGIS discovery tool invoked but worker submitter is not bound; agent service |
| list_run_frames | PASS | 0s |  |
| list_tools_in_category | PASS | 0s |  |
| lookup_precip_return_period | PASS | 0s |  |
| merge_features | PASS | 0s |  |
| postprocess_pelicun | SKIP-ARGS | 0s | required params not fabricatable: ['damage_layer_uri'] |
| publish_layer | PASS | 1s |  |
| qgis_process | PASS | 0s |  |
| request_spatial_input | PASS | 0s |  |
| run_geoclaw_inundation | PASS | 40s |  |
| run_landlab_susceptibility | PASS | 42s |  |
| run_model_asr_scenario | PASS | 0s |  |
| run_model_capture_zone_scenario | PASS | 0s |  |
| run_model_conservation_priority | PASS | 2s |  |
| run_model_contamination_affected_fields | FAIL | 0s | ContaminationAffectedFieldsConfirmationDeniedError: MODFLOW run not started: the parameter |
| run_model_flood_habitat_scenario | PASS | 50s |  |
| run_model_flood_scenario | PASS | 42s |  |
| run_model_glm_lightning_animation | PASS | 702s |  |
| run_model_goes_fire_animation | FAIL | 191s | GOESFireAnimEmptyError: GOES SLIDER frames were available for goes-18/conus over 2026-07-0 |
| run_model_groundwater_contamination_scenario | FAIL | 0s | ParameterExtractionError: could not extract a release duration (hours / days) from the art |
| run_model_mar_scenario | PASS | 0s |  |
| run_model_mine_dewatering_scenario | PASS | 0s |  |
| run_model_multi_species_scenario | PASS | 0s |  |
| run_model_news_event_ingest | SKIP-ARGS | 0s | required params not fabricatable: ['sources'] |
| run_model_nws_flood_event_scenario | PASS | 1s |  |
| run_model_regional_water_budget_scenario | PASS | 2s |  |
| run_model_river_seepage_scenario | PASS | 3s |  |
| run_model_saltwater_intrusion_scenario | PASS | 0s |  |
| run_model_satellite_fire_animation | PASS | 3s |  |
| run_model_sustainable_yield_scenario | PASS | 0s |  |
| run_model_wellhead_protection_scenario | PASS | 0s |  |
| run_model_wetland_hydroperiod_scenario | PASS | 0s |  |
| run_modflow_job | PASS | 0s |  |
| run_pelicun_damage_assessment | FAIL | 33s | PelicunNoAssetsError: No assets intersect the hazard raster footprint. Check that the asse |
| run_pelicun_with_buildings | FAIL | 0s | PelicunWithBuildingsError: pelicun_damage_with_buildings: density→points conversion failed |
| run_river_seepage_job | PASS | 0s |  |
| run_seismic_hazard_psha | PASS | 41s |  |
| run_solver | SKIP-ARGS | 0s | required params not fabricatable: ['solver', 'model_setup_uri'] |
| run_swan_waves | PASS | 41s |  |
| run_swmm_urban_flood | TIMEOUT | 900s | exceeded 900s (thread abandoned) |
| summarize_layer_statistics | PASS | 1s |  |
| wait_for_completion | SKIP-ARGS | 0s | required params not fabricatable: ['handle'] |
| web_fetch | SKIP-ARGS | 0s | required params not fabricatable: ['url'] |
