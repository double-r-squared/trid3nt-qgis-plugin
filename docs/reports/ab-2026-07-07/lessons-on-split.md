# Pass-3 failure split: retrieval vs model (qwen3:8b-16k, K=8)

Scored 176 | HIT 53 | failures split: MODEL-MISS 123

RETRIEVAL-MISS = expected tool absent from the top-K shortlist (model never saw it).
MODEL-MISS = tool was on the menu; the model chose otherwise.

| tool | outcome | class | in top-50? | model called |
|---|---|---|---|---|
| aggregate_claims_across_sources | MISS | MODEL-MISS | y | geocode_location |
| aggregate_property_within_zone | MISS | MODEL-MISS | y | summarize_layer_statistics |
| analyze_affected_fields | MISS | MODEL-MISS | y | summarize_layer_statistics |
| catalog_fetch | MISS | MODEL-MISS | y | compute_layer_bounds |
| catalog_search | MISS | MODEL-MISS | y | summarize_layer_statistics |
| clip_raster_to_polygon | MISS | MODEL-MISS | y | aggregate_property_within_zone |
| clip_vector_to_polygon | MISS | MODEL-MISS | y | catalog_search |
| code_exec_request | MISS | MODEL-MISS | y | geocode_location |
| compute_aspect | NO_CALL | MODEL-MISS | y |  |
| compute_blended_composite | NO_CALL | MODEL-MISS | y |  |
| compute_building_density | NO_CALL | MODEL-MISS | y |  |
| compute_canopy_height | NO_CALL | MODEL-MISS | y |  |
| compute_colored_relief | NO_CALL | MODEL-MISS | y |  |
| compute_contours | NO_CALL | MODEL-MISS | y |  |
| compute_cross_section | NO_CALL | MODEL-MISS | y |  |
| compute_hillshade | MISS | MODEL-MISS | y | geocode_location |
| compute_home_range_kde | MISS | MODEL-MISS | y | compute_hillshade |
| compute_impact_envelope | MISS | MODEL-MISS | y | compute_hillshade |
| compute_impervious_surface | NO_CALL | MODEL-MISS | y |  |
| compute_movement_trajectory | MISS | MODEL-MISS | y | fetch_dem |
| compute_ndvi | MISS | MODEL-MISS | y | compute_layer_bounds |
| compute_overtopping | MISS | MODEL-MISS | y | compute_hillshade |
| compute_slope | MISS | MODEL-MISS | y | geocode_location |
| compute_terrain_profile | MISS | MODEL-MISS | y | geocode_location |
| compute_wave_nomograph | MISS | MODEL-MISS | y | compute_slope |
| compute_zonal_statistics | NO_CALL | MODEL-MISS | y |  |
| count_features_above_threshold | NO_CALL | MODEL-MISS | y |  |
| cut_features_with_polygon | NO_CALL | MODEL-MISS | y |  |
| describe_qgis_algorithm | NO_CALL | MODEL-MISS | y |  |
| digitize_water_body | NO_CALL | MODEL-MISS | y |  |
| enhance_satellite_image | MISS | MODEL-MISS | y | geocode_location |
| export_case_to_qgis | MISS | MODEL-MISS | y | fetch_dem |
| extract_landcover_class | MISS | MODEL-MISS | y | export_case_to_qgis |
| fetch_3dep_extra | MISS | MODEL-MISS | y | fetch_topobathy |
| fetch_administrative_boundaries | MISS | MODEL-MISS | y | geocode_location |
| fetch_airnow_air_quality | MISS | MODEL-MISS | y | publish_layer |
| fetch_asos_metar | MISS | MODEL-MISS | y | fetch_openaq_measurements |
| fetch_buildings | NO_CALL | MODEL-MISS | y |  |
| fetch_cama_flood_discharge | NO_CALL | MODEL-MISS | y |  |
| fetch_cdc_svi | NO_CALL | MODEL-MISS | y |  |
| fetch_census_acs | NO_CALL | MODEL-MISS | y |  |
| fetch_chirps_precipitation | NO_CALL | MODEL-MISS | y |  |
| fetch_climate_normals | MISS | MODEL-MISS | y | geocode_location |
| fetch_dem | NO_CALL | MODEL-MISS | y |  |
| fetch_ebird_observations | NO_CALL | MODEL-MISS | y |  |
| fetch_epa_ejscreen | NO_CALL | MODEL-MISS | y |  |
| fetch_epa_frs_facilities | MISS | MODEL-MISS | y | fetch_sentinel2_truecolor |
| fetch_era5_reanalysis | NO_CALL | MODEL-MISS | y |  |
| fetch_esri_landcover_10m | MISS | MODEL-MISS | y | fetch_sentinel2_truecolor |
| fetch_fault_sources | MISS | MODEL-MISS | y | geocode_location |
| fetch_fema_nfhl_zones | MISS | MODEL-MISS | y | fetch_fault_sources |
| fetch_field_boundaries | NO_CALL | MODEL-MISS | y |  |
| fetch_firms_active_fire | MISS | MODEL-MISS | y | compute_layer_bounds |
| fetch_gbif_occurrences | MISS | MODEL-MISS | y | fetch_field_boundaries |
| fetch_gcn250_curve_numbers | MISS | MODEL-MISS | y | fetch_firms_active_fire |
| fetch_ghsl_population | NO_CALL | MODEL-MISS | y |  |
| fetch_glm_lightning | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_archive_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_blend_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_satellite | NO_CALL | MODEL-MISS | y |  |
| fetch_gridmet | NO_CALL | MODEL-MISS | y |  |
| fetch_gtsm_tide_surge | NO_CALL | MODEL-MISS | y |  |
| fetch_hifld_critical_infrastructure | NO_CALL | MODEL-MISS | y |  |
| fetch_hifld_transmission_lines | NO_CALL | MODEL-MISS | y |  |
| fetch_hrrr_forecast | MISS | MODEL-MISS | y | publish_layer |
| fetch_hrsl_population | MISS | MODEL-MISS | y | geocode_location |
| fetch_inaturalist_observations | MISS | MODEL-MISS | y | fetch_dem |
| fetch_iucn_red_list_range | MISS | MODEL-MISS | y | fetch_hifld_critical_infrastructure |
| fetch_jrc_global_surface_water | MISS | MODEL-MISS | y | fetch_population |
| fetch_landcover | NO_CALL | MODEL-MISS | y |  |
| fetch_landfire_fuels | MISS | MODEL-MISS | y | geocode_location |
| fetch_landsat_imagery | MISS | MODEL-MISS | y | fetch_landfire_fuels |
| fetch_mobi | MISS | MODEL-MISS | y | fetch_population |
| fetch_modis_lst | MISS | MODEL-MISS | y | fetch_sentinel2_truecolor |
| fetch_movebank_tracks | MISS | MODEL-MISS | y | fetch_sentinel2_truecolor |
| fetch_mrms_qpe | MISS | MODEL-MISS | y | fetch_dem |
| fetch_mtbs_burn_severity | MISS | MODEL-MISS | y | fetch_dem |
| fetch_nexrad_reflectivity | MISS | MODEL-MISS | y | fetch_naip |
| fetch_nhdplus_nldi_navigate | NO_CALL | MODEL-MISS | y |  |
| fetch_nifc_fire_perimeters | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_coops_currents | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_coops_tides | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_nwm_streamflow | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_slr_confidence | NO_CALL | MODEL-MISS | y |  |
| fetch_nws_alerts_conus | NO_CALL | MODEL-MISS | y |  |
| fetch_nws_event | NO_CALL | MODEL-MISS | y |  |
| fetch_nws_river_forecast | NO_CALL | MODEL-MISS | y |  |
| fetch_openaq_measurements | NO_CALL | MODEL-MISS | y |  |
| fetch_openfema_disasters | NO_CALL | MODEL-MISS | y |  |
| fetch_overpass_pois | MISS | MODEL-MISS | y | geocode_location |
| fetch_river_geometry | MISS | MODEL-MISS | y | geocode_location |
| fetch_sentinel2_truecolor | MISS | MODEL-MISS | y | fetch_dem |
| fetch_soilgrids | NO_CALL | MODEL-MISS | y |  |
| fetch_statsgo_soils | MISS | MODEL-MISS | y | geocode_location |
| fetch_topobathy | MISS | MODEL-MISS | y | fetch_storm_events_db |
| fetch_tsunami_events | NO_CALL | MODEL-MISS | y |  |
| fetch_us_drought_monitor | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_dams | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_levees | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_nsi | NO_CALL | MODEL-MISS | y |  |
| fetch_usgs_nwis_gauges | MISS | MODEL-MISS | y | fetch_usgs_earthquakes |
| fetch_wdpa_protected_areas | MISS | MODEL-MISS | y | fetch_dem |
| fetch_wfigs_incident | MISS | MODEL-MISS | y | publish_layer |
| postprocess_pelicun | NO_CALL | MODEL-MISS | y |  |
| publish_layer | NO_CALL | MODEL-MISS | y |  |
| qgis_process | NO_CALL | MODEL-MISS | y |  |
| run_landlab_susceptibility | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_asr_scenario | MISS | MODEL-MISS | y | qgis_process |
| run_model_capture_zone_scenario | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_conservation_priority | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_groundwater_contamination_scenario | NO_CALL | MODEL-MISS | y |  |
| run_model_mar_scenario | MISS | MODEL-MISS | y | run_model_groundwater_contamination_scenario |
| run_model_multi_species_scenario | MISS | MODEL-MISS | y | fetch_dem |
| run_model_saltwater_intrusion_scenario | MISS | MODEL-MISS | y | run_model_river_seepage_scenario |
| run_model_wellhead_protection_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_wetland_hydroperiod_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_modflow_job | MISS | MODEL-MISS | y | geocode_location |
| run_pelicun_with_buildings | MISS | MODEL-MISS | y | geocode_location |
| run_swan_waves | MISS | MODEL-MISS | y | run_pelicun_damage_assessment |
| run_swmm_urban_flood | MISS | MODEL-MISS | y | run_pelicun_damage_assessment |
| summarize_layer_statistics | MISS | MODEL-MISS | y | geocode_location |
| web_fetch | MISS | MODEL-MISS | y | fetch_dem |
