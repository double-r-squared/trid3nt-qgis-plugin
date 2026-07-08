# Pass-3 failure split: retrieval vs model (qwen3:8b-16k, K=8)

Scored 172 | HIT 45 | failures split: MODEL-MISS 127

RETRIEVAL-MISS = expected tool absent from the top-K shortlist (model never saw it).
MODEL-MISS = tool was on the menu; the model chose otherwise.

| tool | outcome | class | in top-50? | model called |
|---|---|---|---|---|
| aggregate_claims_across_sources | MISS | MODEL-MISS | y | geocode_location |
| aggregate_property_within_zone | NO_CALL | MODEL-MISS | y |  |
| analyze_affected_fields | MISS | MODEL-MISS | y | aggregate_claims_across_sources |
| catalog_fetch | MISS | MODEL-MISS | y | fetch_fema_nfhl_zones |
| catalog_search | NO_CALL | MODEL-MISS | y |  |
| clip_raster_to_bbox | NO_CALL | MODEL-MISS | y |  |
| clip_raster_to_polygon | MISS | MODEL-MISS | y | clip_raster_to_bbox |
| clip_vector_to_polygon | MISS | MODEL-MISS | y | geocode_location |
| code_exec_request | NO_CALL | MODEL-MISS | y |  |
| compute_aspect | NO_CALL | MODEL-MISS | y |  |
| compute_blended_composite | MISS | MODEL-MISS | y | geocode_location |
| compute_building_density | NO_CALL | MODEL-MISS | y |  |
| compute_canopy_height | MISS | MODEL-MISS | y | geocode_location |
| compute_cross_section | MISS | MODEL-MISS | y | compute_terrain_profile |
| compute_home_range_kde | MISS | MODEL-MISS | y | compute_hillshade |
| compute_impact_envelope | MISS | MODEL-MISS | y | compute_colored_relief |
| compute_impervious_surface | NO_CALL | MODEL-MISS | y |  |
| compute_layer_bounds | NO_CALL | MODEL-MISS | y |  |
| compute_movement_trajectory | MISS | MODEL-MISS | y | fetch_dem |
| compute_ndvi | MISS | MODEL-MISS | y | compute_movement_trajectory |
| compute_overtopping | MISS | MODEL-MISS | y | compute_movement_trajectory |
| compute_slope | NO_CALL | MODEL-MISS | y |  |
| compute_terrain_profile | NO_CALL | MODEL-MISS | y |  |
| compute_wave_nomograph | NO_CALL | MODEL-MISS | y |  |
| compute_zonal_statistics | NO_CALL | MODEL-MISS | y |  |
| count_features_above_threshold | NO_CALL | MODEL-MISS | y |  |
| digitize_water_body | MISS | MODEL-MISS | y | geocode_location |
| enhance_satellite_image | MISS | MODEL-MISS | y | qgis_process |
| extract_landcover_class | MISS | MODEL-MISS | y | cut_features_with_polygon |
| fetch_3dep_extra | NO_CALL | MODEL-MISS | y |  |
| fetch_administrative_boundaries | MISS | MODEL-MISS | y | fetch_wdpa_protected_areas |
| fetch_airnow_air_quality | MISS | MODEL-MISS | y | geocode_location |
| fetch_asos_metar | NO_CALL | MODEL-MISS | y |  |
| fetch_buildings | MISS | MODEL-MISS | y | fetch_asos_metar |
| fetch_cama_flood_discharge | MISS | MODEL-MISS | y | fetch_dem |
| fetch_cdc_svi | MISS | MODEL-MISS | y | geocode_location |
| fetch_census_acs | NO_CALL | MODEL-MISS | y |  |
| fetch_chirps_precipitation | NO_CALL | MODEL-MISS | y |  |
| fetch_climate_normals | NO_CALL | MODEL-MISS | y |  |
| fetch_copernicus_dem | MISS | MODEL-MISS | y | geocode_location |
| fetch_dem | NO_CALL | MODEL-MISS | y |  |
| fetch_ebird_observations | NO_CALL | MODEL-MISS | y |  |
| fetch_epa_ejscreen | MISS | MODEL-MISS | y | fetch_sentinel2_truecolor |
| fetch_epa_frs_facilities | NO_CALL | MODEL-MISS | y |  |
| fetch_era5_reanalysis | MISS | MODEL-MISS | y | fetch_dem |
| fetch_esri_landcover_10m | MISS | MODEL-MISS | y | compute_layer_bounds |
| fetch_fault_sources | MISS | MODEL-MISS | y | run_seismic_hazard_psha |
| fetch_fema_nfhl_zones | NO_CALL | MODEL-MISS | y |  |
| fetch_field_boundaries | MISS | MODEL-MISS | y | fetch_fema_nfhl_zones |
| fetch_firms_active_fire | MISS | MODEL-MISS | y | fetch_nifc_fire_perimeters |
| fetch_gcn250_curve_numbers | MISS | MODEL-MISS | y | fetch_dem |
| fetch_ghsl_population | MISS | MODEL-MISS | y | fetch_population |
| fetch_glm_lightning | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_active_fire | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_animation | MISS | MODEL-MISS | y | geocode_location |
| fetch_goes_satellite | NO_CALL | MODEL-MISS | y |  |
| fetch_gridmet | NO_CALL | MODEL-MISS | y |  |
| fetch_gtsm_tide_surge | NO_CALL | MODEL-MISS | y |  |
| fetch_hifld_critical_infrastructure | NO_CALL | MODEL-MISS | y |  |
| fetch_hifld_transmission_lines | NO_CALL | MODEL-MISS | y |  |
| fetch_hrrr_forecast | MISS | MODEL-MISS | y | geocode_location |
| fetch_hrrr_smoke | MISS | MODEL-MISS | y | geocode_location |
| fetch_inaturalist_observations | MISS | MODEL-MISS | y | geocode_location |
| fetch_iucn_red_list_range | NO_CALL | MODEL-MISS | y |  |
| fetch_jrc_global_surface_water | NO_CALL | MODEL-MISS | y |  |
| fetch_landcover | MISS | MODEL-MISS | y | fetch_dem |
| fetch_landfire_fuels | NO_CALL | MODEL-MISS | y |  |
| fetch_landsat_imagery | MISS | MODEL-MISS | y | geocode_location |
| fetch_lehd_jobs | MISS | MODEL-MISS | y | run_model_flood_scenario |
| fetch_mobi | NO_CALL | MODEL-MISS | y |  |
| fetch_modis_lst | NO_CALL | MODEL-MISS | y |  |
| fetch_movebank_tracks | NO_CALL | MODEL-MISS | y |  |
| fetch_mrms_qpe | NO_CALL | MODEL-MISS | y |  |
| fetch_mtbs_burn_severity | NO_CALL | MODEL-MISS | y |  |
| fetch_naip | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_coops_currents | MISS | MODEL-MISS | y | fetch_noaa_coops_tides |
| fetch_noaa_nwm_streamflow | MISS | MODEL-MISS | y | fetch_usgs_nwis_gauges |
| fetch_noaa_slr_confidence | MISS | MODEL-MISS | y | fetch_us_drought_monitor |
| fetch_noaa_slr_marsh | MISS | MODEL-MISS | y | fetch_openaq_measurements |
| fetch_noaa_slr_scenarios | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_sst | MISS | MODEL-MISS | y | fetch_noaa_slr_scenarios |
| fetch_nws_alerts_conus | NO_CALL | MODEL-MISS | y |  |
| fetch_nws_river_forecast | MISS | MODEL-MISS | y | geocode_location |
| fetch_openfema_disasters | NO_CALL | MODEL-MISS | y |  |
| fetch_overpass_pois | NO_CALL | MODEL-MISS | y |  |
| fetch_population | NO_CALL | MODEL-MISS | y |  |
| fetch_raws_weather | NO_CALL | MODEL-MISS | y |  |
| fetch_river_geometry | NO_CALL | MODEL-MISS | y |  |
| fetch_roads_osm | NO_CALL | MODEL-MISS | y |  |
| fetch_sentinel1_sar | MISS | MODEL-MISS | y | geocode_location |
| fetch_statsgo_soils | MISS | MODEL-MISS | y | geocode_location |
| fetch_tsunami_events | MISS | MODEL-MISS | y | fetch_statsgo_soils |
| fetch_us_drought_monitor | MISS | MODEL-MISS | y | fetch_storm_events_db |
| fetch_usace_dams | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_levees | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_nsi | NO_CALL | MODEL-MISS | y |  |
| fetch_usgs_groundwater_levels | MISS | MODEL-MISS | y | geocode_location |
| fetch_wfigs_incident | MISS | MODEL-MISS | y | geocode_location |
| generate_damage_distribution | MISS | MODEL-MISS | y | fill_gaps |
| list_qgis_algorithms | MISS | MODEL-MISS | y | geocode_location |
| list_run_frames | MISS | MODEL-MISS | y | list_qgis_algorithms |
| request_spatial_input | MISS | MODEL-MISS | y | publish_layer |
| run_geoclaw_inundation | MISS | MODEL-MISS | y | request_spatial_input |
| run_landlab_susceptibility | NO_CALL | MODEL-MISS | y |  |
| run_model_asr_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_capture_zone_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_conservation_priority | NO_CALL | MODEL-MISS | y |  |
| run_model_contamination_affected_fields | NO_CALL | MODEL-MISS | y |  |
| run_model_flood_habitat_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_flood_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_glm_lightning_animation | MISS | MODEL-MISS | y | geocode_location |
| run_model_goes_fire_animation | MISS | MODEL-MISS | y | geocode_location |
| run_model_groundwater_contamination_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_mar_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_mine_dewatering_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_multi_species_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_news_event_ingest | MISS | MODEL-MISS | y | geocode_location |
| run_model_nws_flood_event_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_regional_water_budget_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_river_seepage_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_saltwater_intrusion_scenario | MISS | MODEL-MISS | y | fetch_dem |
| run_model_satellite_fire_animation | MISS | MODEL-MISS | y | run_model_flood_scenario |
| run_model_wellhead_protection_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_wetland_hydroperiod_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_modflow_job | MISS | MODEL-MISS | y | geocode_location |
| run_pelicun_damage_assessment | MISS | MODEL-MISS | y | geocode_location |
| run_swmm_urban_flood | MISS | MODEL-MISS | y | compute_building_density |
