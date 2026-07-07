# TRID3NT Local tool-routing sweep (pass 3) -- qwen3:8b-16k

Updated: 2026-07-06T22:26:23  
Scored 174/174 | ERROR 1 | HIT 45 | MISS 73 | NO_CALL 55

| tool | outcome | first_call | seconds |
|---|---|---|---|
| aggregate_claims_across_sources | MISS | geocode_location | 100 |
| aggregate_property_within_zone | NO_CALL |  | 30 |
| analyze_affected_fields | MISS | aggregate_claims_across_sources | 66 |
| catalog_fetch | MISS | fetch_fema_nfhl_zones | 16 |
| catalog_search | NO_CALL |  | 26 |
| clip_raster_to_bbox | NO_CALL |  | 33 |
| clip_raster_to_polygon | MISS | clip_raster_to_bbox | 68 |
| clip_vector_to_polygon | MISS | geocode_location | 122 |
| code_exec_request | NO_CALL |  | 21 |
| compute_aspect | NO_CALL |  | 40 |
| compute_blended_composite | MISS | geocode_location | 110 |
| compute_building_density | NO_CALL |  | 15 |
| compute_canopy_height | MISS | geocode_location | 216 |
| compute_colored_relief | HIT | compute_colored_relief | 64 |
| compute_contours | HIT | compute_contours | 83 |
| compute_cross_section | MISS | compute_terrain_profile | 75 |
| compute_hillshade | HIT | compute_hillshade | 85 |
| compute_home_range_kde | MISS | compute_hillshade | 89 |
| compute_impact_envelope | MISS | compute_colored_relief | 240 |
| compute_impervious_surface | NO_CALL |  | 0 |
| compute_layer_bounds | NO_CALL |  | 1 |
| compute_movement_trajectory | MISS | fetch_dem | 74 |
| compute_ndvi | MISS | compute_movement_trajectory | 70 |
| compute_overtopping | MISS | compute_movement_trajectory | 240 |
| compute_slope | NO_CALL |  | 0 |
| compute_terrain_profile | NO_CALL |  | 0 |
| compute_wave_nomograph | NO_CALL |  | 0 |
| compute_zonal_statistics | NO_CALL |  | 0 |
| count_features_above_threshold | NO_CALL |  | 0 |
| cut_features_with_polygon | HIT | geocode_location | 79 |
| describe_qgis_algorithm | HIT | geocode_location | 69 |
| digitize_water_body | MISS | geocode_location | 145 |
| discover_dataset | HIT | fetch_dem | 213 |
| enhance_satellite_image | MISS | qgis_process | 74 |
| extract_landcover_class | MISS | cut_features_with_polygon | 98 |
| fetch_3dep_extra | NO_CALL |  | 42 |
| fetch_administrative_boundaries | MISS | fetch_wdpa_protected_areas | 87 |
| fetch_airnow_air_quality | MISS | geocode_location | 124 |
| fetch_asos_metar | NO_CALL |  | 91 |
| fetch_buildings | MISS | fetch_asos_metar | 75 |
| fetch_cama_flood_discharge | MISS | fetch_dem | 127 |
| fetch_cdc_svi | MISS | geocode_location | 240 |
| fetch_census_acs | NO_CALL |  | 0 |
| fetch_chirps_precipitation | NO_CALL |  | 1 |
| fetch_climate_normals | NO_CALL |  | 0 |
| fetch_copernicus_dem | MISS | geocode_location | 100 |
| fetch_dem | NO_CALL |  | 30 |
| fetch_ebird_observations | NO_CALL |  | 34 |
| fetch_epa_ejscreen | MISS | fetch_sentinel2_truecolor | 135 |
| fetch_epa_frs_facilities | NO_CALL |  | 34 |
| fetch_era5_reanalysis | MISS | fetch_dem | 87 |
| fetch_esri_landcover_10m | MISS | compute_layer_bounds | 168 |
| fetch_fault_sources | MISS | run_seismic_hazard_psha | 56 |
| fetch_fema_nfhl_zones | NO_CALL |  | 30 |
| fetch_field_boundaries | MISS | fetch_fema_nfhl_zones | 126 |
| fetch_firms_active_fire | MISS | fetch_nifc_fire_perimeters | 102 |
| fetch_gbif_occurrences | HIT | fetch_gbif_occurrences | 66 |
| fetch_gcn250_curve_numbers | MISS | fetch_dem | 108 |
| fetch_ghsl_population | MISS | fetch_population | 229 |
| fetch_glm_lightning | NO_CALL |  | 40 |
| fetch_goes_active_fire | NO_CALL |  | 31 |
| fetch_goes_animation | MISS | geocode_location | 127 |
| fetch_goes_archive_animation | HIT | fetch_goes_archive_animation | 67 |
| fetch_goes_blend_animation | HIT | fetch_goes_animation | 240 |
| fetch_goes_satellite | NO_CALL |  | 0 |
| fetch_gridmet | NO_CALL |  | 1 |
| fetch_gtsm_tide_surge | NO_CALL |  | 1 |
| fetch_hifld_critical_infrastructure | NO_CALL |  | 0 |
| fetch_hifld_transmission_lines | NO_CALL |  | 0 |
| fetch_hrrr_forecast | MISS | geocode_location | 72 |
| fetch_hrrr_smoke | MISS | geocode_location | 119 |
| fetch_hrsl_population | HIT | fetch_hrsl_population | 57 |
| fetch_inaturalist_observations | MISS | geocode_location | 235 |
| fetch_iucn_red_list_range | NO_CALL |  | 33 |
| fetch_jrc_global_surface_water | NO_CALL |  | 24 |
| fetch_landcover | MISS | fetch_dem | 91 |
| fetch_landfire_fuels | NO_CALL |  | 27 |
| fetch_landsat_imagery | MISS | geocode_location | 240 |
| fetch_lehd_jobs | MISS | run_model_flood_scenario | 2 |
| fetch_mobi | NO_CALL |  | 1 |
| fetch_modis_lst | NO_CALL |  | 0 |
| fetch_movebank_tracks | NO_CALL |  | 0 |
| fetch_mrms_qpe | NO_CALL |  | 0 |
| fetch_mtbs_burn_severity | NO_CALL |  | 0 |
| fetch_naip | NO_CALL |  | 0 |
| fetch_nexrad_reflectivity | HIT | fetch_nexrad_reflectivity | 73 |
| fetch_nhdplus_nldi_navigate | HIT | publish_layer | 90 |
| fetch_nifc_fire_perimeters | HIT | fetch_nifc_fire_perimeters | 110 |
| fetch_noaa_coops_currents | MISS | fetch_noaa_coops_tides | 69 |
| fetch_noaa_coops_tides | HIT | fetch_river_geometry | 90 |
| fetch_noaa_nwm_streamflow | MISS | fetch_usgs_nwis_gauges | 66 |
| fetch_noaa_slr_confidence | MISS | fetch_us_drought_monitor | 103 |
| fetch_noaa_slr_marsh | MISS | fetch_openaq_measurements | 240 |
| fetch_noaa_slr_scenarios | NO_CALL |  | 27 |
| fetch_noaa_sst | MISS | fetch_noaa_slr_scenarios | 73 |
| fetch_nws_alerts_conus | NO_CALL |  | 15 |
| fetch_nws_event | HIT | fetch_dem | 190 |
| fetch_nws_river_forecast | MISS | geocode_location | 104 |
| fetch_openaq_measurements | HIT | fetch_openaq_measurements | 240 |
| fetch_openfema_disasters | NO_CALL |  | 1 |
| fetch_overpass_pois | NO_CALL |  | 0 |
| fetch_population | NO_CALL |  | 0 |
| fetch_raws_weather | NO_CALL |  | 0 |
| fetch_river_geometry | NO_CALL |  | 0 |
| fetch_roads_osm | NO_CALL |  | 0 |
| fetch_sentinel1_sar | MISS | geocode_location | 153 |
| fetch_sentinel2_truecolor | HIT | fetch_sentinel1_sar | 95 |
| fetch_snotel_snow | HIT | fetch_sentinel1_sar | 76 |
| fetch_soilgrids | ERROR | ConnectionClosedError: no close frame received or sent | 83 |
| fetch_statsgo_soils | MISS | geocode_location | 113 |
| fetch_storm_events_db | HIT | fetch_statsgo_soils | 66 |
| fetch_topobathy | HIT | fetch_topobathy | 66 |
| fetch_tsunami_events | MISS | fetch_statsgo_soils | 240 |
| fetch_us_drought_monitor | MISS | fetch_storm_events_db | 1 |
| fetch_usace_dams | NO_CALL |  | 1 |
| fetch_usace_levees | NO_CALL |  | 0 |
| fetch_usace_nsi | NO_CALL |  | 0 |
| fetch_usfs_canopy_fuels | HIT | fetch_usfs_canopy_fuels | 153 |
| fetch_usgs_earthquakes | HIT | fetch_usgs_earthquakes | 84 |
| fetch_usgs_groundwater_levels | MISS | geocode_location | 76 |
| fetch_usgs_nwis_gauges | HIT | fetch_usgs_earthquakes | 72 |
| fetch_usgs_volcano_alerts | HIT | fetch_usgs_earthquakes | 189 |
| fetch_usgs_water_quality | HIT | fetch_usgs_volcano_alerts | 129 |
| fetch_viirs_day_fire | HIT | fetch_viirs_day_fire | 64 |
| fetch_wdpa_protected_areas | HIT | fetch_viirs_day_fire | 129 |
| fetch_wfigs_incident | MISS | geocode_location | 190 |
| fill_gaps | HIT | fill_gaps | 80 |
| generate_choropleth_legend | HIT | fetch_wfigs_incident | 86 |
| generate_damage_distribution | MISS | fill_gaps | 73 |
| generate_histogram | HIT | fetch_wfigs_incident | 72 |
| generate_time_series | HIT | generate_histogram | 78 |
| geocode_location | HIT | fetch_wfigs_incident | 159 |
| list_categories | HIT | list_categories | 64 |
| list_qgis_algorithms | MISS | geocode_location | 120 |
| list_run_frames | MISS | list_qgis_algorithms | 92 |
| list_tools_in_category | HIT | list_tools_in_category | 65 |
| lookup_precip_return_period | HIT | list_qgis_algorithms | 78 |
| merge_features | HIT | fetch_dem | 73 |
| postprocess_pelicun | HIT | fetch_dem | 151 |
| publish_layer | HIT | publish_layer | 85 |
| qgis_process | HIT | fetch_dem | 90 |
| request_spatial_input | MISS | publish_layer | 57 |
| run_geoclaw_inundation | MISS | request_spatial_input | 240 |
| run_landlab_susceptibility | NO_CALL |  | 0 |
| run_model_asr_scenario | NO_CALL |  | 1 |
| run_model_capture_zone_scenario | NO_CALL |  | 0 |
| run_model_conservation_priority | NO_CALL |  | 0 |
| run_model_contamination_affected_fields | NO_CALL |  | 0 |
| run_model_flood_habitat_scenario | NO_CALL |  | 0 |
| run_model_flood_scenario | MISS | geocode_location | 78 |
| run_model_glm_lightning_animation | MISS | geocode_location | 78 |
| run_model_goes_fire_animation | MISS | geocode_location | 57 |
| run_model_groundwater_contamination_scenario | MISS | geocode_location | 46 |
| run_model_mar_scenario | MISS | geocode_location | 67 |
| run_model_mine_dewatering_scenario | MISS | geocode_location | 49 |
| run_model_multi_species_scenario | MISS | geocode_location | 42 |
| run_model_news_event_ingest | MISS | geocode_location | 46 |
| run_model_nws_flood_event_scenario | MISS | geocode_location | 94 |
| run_model_regional_water_budget_scenario | MISS | geocode_location | 68 |
| run_model_river_seepage_scenario | MISS | geocode_location | 93 |
| run_model_saltwater_intrusion_scenario | MISS | fetch_dem | 35 |
| run_model_satellite_fire_animation | MISS | run_model_flood_scenario | 77 |
| run_model_sustainable_yield_scenario | HIT | run_model_sustainable_yield_scenario | 42 |
| run_model_wellhead_protection_scenario | MISS | geocode_location | 74 |
| run_model_wetland_hydroperiod_scenario | MISS | geocode_location | 75 |
| run_modflow_job | MISS | geocode_location | 101 |
| run_pelicun_damage_assessment | MISS | geocode_location | 146 |
| run_pelicun_with_buildings | HIT | run_pelicun_with_buildings | 112 |
| run_river_seepage_job | HIT | run_river_seepage_job | 72 |
| run_seismic_hazard_psha | HIT | run_pelicun_with_buildings | 89 |
| run_swan_waves | MISS | geocode_location | 171 |
| run_swmm_urban_flood | HIT | run_swan_waves | 40 |
| summarize_layer_statistics | HIT | summarize_layer_statistics | 91 |
| web_fetch | MISS | fetch_dem | 240 |
