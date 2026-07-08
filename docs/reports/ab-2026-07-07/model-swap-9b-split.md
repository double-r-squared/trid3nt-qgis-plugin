# Pass-3 failure split: retrieval vs model (qwen3:8b-16k, K=8)

Scored 183 | HIT 42 | failures split: MODEL-MISS 141

RETRIEVAL-MISS = expected tool absent from the top-K shortlist (model never saw it).
MODEL-MISS = tool was on the menu; the model chose otherwise.

| tool | outcome | class | in top-50? | model called |
|---|---|---|---|---|
| aggregate_claims_across_sources | MISS | MODEL-MISS | y | geocode_location |
| aggregate_property_within_zone | NO_CALL | MODEL-MISS | y |  |
| analyze_affected_fields | MISS | MODEL-MISS | y | geocode_location |
| catalog_fetch | NO_CALL | MODEL-MISS | y |  |
| catalog_search | MISS | MODEL-MISS | y | run_model_news_event_ingest |
| clip_raster_to_bbox | NO_CALL | MODEL-MISS | y |  |
| clip_raster_to_polygon | NO_CALL | MODEL-MISS | y |  |
| code_exec_request | MISS | MODEL-MISS | y | geocode_location |
| compute_aspect | MISS | MODEL-MISS | y | geocode_location |
| compute_blended_composite | NO_CALL | MODEL-MISS | y |  |
| compute_building_density | NO_CALL | MODEL-MISS | y |  |
| compute_canopy_height | NO_CALL | MODEL-MISS | y |  |
| compute_change_detection | NO_CALL | MODEL-MISS | y |  |
| compute_colored_relief | NO_CALL | MODEL-MISS | y |  |
| compute_contours | NO_CALL | MODEL-MISS | y |  |
| compute_cross_section | MISS | MODEL-MISS | y | geocode_location |
| compute_flood_depth_damage | MISS | MODEL-MISS | y | geocode_location |
| compute_hillshade | MISS | MODEL-MISS | y | geocode_location |
| compute_home_range_kde | MISS | MODEL-MISS | y | compute_layer_bounds |
| compute_idf_curve | MISS | MODEL-MISS | y | geocode_location |
| compute_impact_envelope | NO_CALL | MODEL-MISS | y |  |
| compute_impervious_surface | NO_CALL | MODEL-MISS | y |  |
| compute_layer_bounds | NO_CALL | MODEL-MISS | y |  |
| compute_movement_trajectory | NO_CALL | MODEL-MISS | y |  |
| compute_overtopping | NO_CALL | MODEL-MISS | y |  |
| compute_sediment_yield | NO_CALL | MODEL-MISS | y |  |
| compute_terrain_profile | NO_CALL | MODEL-MISS | y |  |
| compute_urban_heat_island | NO_CALL | MODEL-MISS | y |  |
| compute_wave_nomograph | NO_CALL | MODEL-MISS | y |  |
| compute_zonal_statistics | MISS | MODEL-MISS | y | geocode_location |
| count_features_above_threshold | MISS | MODEL-MISS | y | geocode_location |
| cut_features_with_polygon | NO_CALL | MODEL-MISS | y |  |
| describe_qgis_algorithm | MISS | MODEL-MISS | y | compute_zonal_statistics |
| digitize_water_body | NO_CALL | MODEL-MISS | y |  |
| discover_dataset | NO_CALL | MODEL-MISS | y |  |
| enhance_satellite_image | NO_CALL | MODEL-MISS | y |  |
| extract_landcover_class | MISS | MODEL-MISS | y | geocode_location |
| fetch_3dep_extra | MISS | MODEL-MISS | y | geocode_location |
| fetch_airnow_air_quality | MISS | MODEL-MISS | y | geocode_location |
| fetch_asos_metar | MISS | MODEL-MISS | y | geocode_location |
| fetch_buildings | MISS | MODEL-MISS | y | geocode_location |
| fetch_cdc_svi | MISS | MODEL-MISS | y | geocode_location |
| fetch_chirps_precipitation | NO_CALL | MODEL-MISS | y |  |
| fetch_climate_normals | MISS | MODEL-MISS | y | fetch_administrative_boundaries |
| fetch_copernicus_dem | MISS | MODEL-MISS | y | geocode_location |
| fetch_dem | MISS | MODEL-MISS | y | geocode_location |
| fetch_ebird_observations | MISS | MODEL-MISS | y | list_categories |
| fetch_epa_ejscreen | MISS | MODEL-MISS | y | geocode_location |
| fetch_esri_landcover_10m | NO_CALL | MODEL-MISS | y |  |
| fetch_fault_sources | NO_CALL | MODEL-MISS | y |  |
| fetch_fema_nfhl_zones | NO_CALL | MODEL-MISS | y |  |
| fetch_field_boundaries | NO_CALL | MODEL-MISS | y |  |
| fetch_firms_active_fire | NO_CALL | MODEL-MISS | y |  |
| fetch_gcn250_curve_numbers | NO_CALL | MODEL-MISS | y |  |
| fetch_glm_lightning | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_active_fire | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_archive_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_blend_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_gtsm_tide_surge | NO_CALL | MODEL-MISS | y |  |
| fetch_hifld_critical_infrastructure | NO_CALL | MODEL-MISS | y |  |
| fetch_hifld_transmission_lines | NO_CALL | MODEL-MISS | y |  |
| fetch_hrrr_forecast | NO_CALL | MODEL-MISS | y |  |
| fetch_hrrr_smoke | NO_CALL | MODEL-MISS | y |  |
| fetch_hrsl_population | NO_CALL | MODEL-MISS | y |  |
| fetch_jrc_global_surface_water | NO_CALL | MODEL-MISS | y |  |
| fetch_landcover | NO_CALL | MODEL-MISS | y |  |
| fetch_landfire_fuels | NO_CALL | MODEL-MISS | y |  |
| fetch_landsat_imagery | NO_CALL | MODEL-MISS | y |  |
| fetch_lehd_jobs | NO_CALL | MODEL-MISS | y |  |
| fetch_mobi | NO_CALL | MODEL-MISS | y |  |
| fetch_movebank_tracks | MISS | MODEL-MISS | y | geocode_location |
| fetch_mrms_qpe | MISS | MODEL-MISS | y | list_categories |
| fetch_mtbs_burn_severity | NO_CALL | MODEL-MISS | y |  |
| fetch_naip | NO_CALL | MODEL-MISS | y |  |
| fetch_nexrad_reflectivity | NO_CALL | MODEL-MISS | y |  |
| fetch_nhdplus_nldi_navigate | NO_CALL | MODEL-MISS | y |  |
| fetch_nifc_fire_perimeters | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_slr_confidence | MISS | MODEL-MISS | y | geocode_location |
| fetch_noaa_slr_marsh | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_slr_scenarios | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_sst | NO_CALL | MODEL-MISS | y |  |
| fetch_nws_alerts_conus | NO_CALL | MODEL-MISS | y |  |
| fetch_openaq_measurements | MISS | MODEL-MISS | y | geocode_location |
| fetch_openfema_disasters | MISS | MODEL-MISS | y | list_categories |
| fetch_overpass_pois | MISS | MODEL-MISS | y | geocode_location |
| fetch_population | MISS | MODEL-MISS | y | geocode_location |
| fetch_raws_weather | MISS | MODEL-MISS | y | list_tools_in_category |
| fetch_river_geometry | MISS | MODEL-MISS | y | geocode_location |
| fetch_snotel_snow | NO_CALL | MODEL-MISS | y |  |
| fetch_soilgrids | MISS | MODEL-MISS | y | geocode_location |
| fetch_statsgo_soils | MISS | MODEL-MISS | y | geocode_location |
| fetch_storm_events_db | MISS | MODEL-MISS | y | list_categories |
| fetch_topobathy | NO_CALL | MODEL-MISS | y |  |
| fetch_usfs_canopy_fuels | MISS | MODEL-MISS | y | geocode_location |
| fetch_usgs_earthquakes | NO_CALL | MODEL-MISS | y |  |
| fetch_usgs_groundwater_levels | MISS | MODEL-MISS | y | geocode_location |
| fetch_usgs_water_quality | MISS | MODEL-MISS | y | geocode_location |
| fetch_viirs_day_fire | MISS | MODEL-MISS | y | geocode_location |
| fetch_wdpa_protected_areas | MISS | MODEL-MISS | y | geocode_location |
| fetch_wfigs_incident | MISS | MODEL-MISS | y | geocode_location |
| fill_gaps | MISS | MODEL-MISS | y | geocode_location |
| generate_choropleth_legend | MISS | MODEL-MISS | y | geocode_location |
| generate_damage_distribution | MISS | MODEL-MISS | y | geocode_location |
| generate_time_series | MISS | MODEL-MISS | y | generate_histogram |
| list_categories | NO_CALL | MODEL-MISS | y |  |
| list_qgis_algorithms | NO_CALL | MODEL-MISS | y |  |
| list_run_frames | NO_CALL | MODEL-MISS | y |  |
| list_tools_in_category | NO_CALL | MODEL-MISS | y |  |
| merge_features | MISS | MODEL-MISS | y | geocode_location |
| model_debris_flow | MISS | MODEL-MISS | y | geocode_location |
| postprocess_pelicun | NO_CALL | MODEL-MISS | y |  |
| publish_layer | NO_CALL | MODEL-MISS | y |  |
| qgis_process | NO_CALL | MODEL-MISS | y |  |
| request_spatial_input | NO_CALL | MODEL-MISS | y |  |
| run_geoclaw_inundation | NO_CALL | MODEL-MISS | y |  |
| run_model_conservation_priority | MISS | MODEL-MISS | y | geocode_location |
| run_model_contamination_affected_fields | MISS | MODEL-MISS | y | geocode_location |
| run_model_flood_habitat_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_flood_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_glm_lightning_animation | MISS | MODEL-MISS | y | geocode_location |
| run_model_groundwater_contamination_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_mar_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_mine_dewatering_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_multi_species_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_news_event_ingest | MISS | MODEL-MISS | y | geocode_location |
| run_model_nws_flood_event_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_regional_water_budget_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_satellite_fire_animation | MISS | MODEL-MISS | y | geocode_location |
| run_model_sustainable_yield_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_wellhead_protection_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_wetland_hydroperiod_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_modflow_job | MISS | MODEL-MISS | y | geocode_location |
| run_pelicun_damage_assessment | MISS | MODEL-MISS | y | geocode_location |
| run_pelicun_with_buildings | MISS | MODEL-MISS | y | geocode_location |
| run_river_seepage_job | NO_CALL | MODEL-MISS | y |  |
| run_seismic_hazard_psha | NO_CALL | MODEL-MISS | y |  |
| run_swan_waves | NO_CALL | MODEL-MISS | y |  |
| run_swmm_urban_flood | NO_CALL | MODEL-MISS | y |  |
| summarize_layer_statistics | NO_CALL | MODEL-MISS | y |  |
| web_fetch | NO_CALL | MODEL-MISS | y |  |
