# TRID3NT Local tool-routing sweep (pass 3) -- qwen3:8b-16k

Updated: 2026-07-07T18:09:39  
Scored 183/183 | ERROR 1 | HIT 44 | MISS 91 | NO_CALL 47

| tool | outcome | first_call | seconds |
|---|---|---|---|
| aggregate_claims_across_sources | MISS | fetch_dem | 57 |
| aggregate_property_within_zone | MISS | geocode_location | 105 |
| analyze_affected_fields | MISS | run_model_groundwater_contamination_scenario | 58 |
| catalog_fetch | MISS | fetch_dem | 49 |
| catalog_search | MISS | compute_layer_bounds | 54 |
| clip_raster_to_bbox | HIT | aggregate_property_within_zone | 73 |
| clip_raster_to_polygon | MISS | fetch_dem | 77 |
| clip_vector_to_polygon | HIT | fetch_dem | 136 |
| code_exec_request | MISS | geocode_location | 240 |
| compute_aspect | NO_CALL |  | 1 |
| compute_blended_composite | NO_CALL |  | 1 |
| compute_building_density | NO_CALL |  | 0 |
| compute_canopy_height | NO_CALL |  | 0 |
| compute_change_detection | NO_CALL |  | 0 |
| compute_colored_relief | NO_CALL |  | 0 |
| compute_contours | NO_CALL |  | 1 |
| compute_cross_section | MISS | geocode_location | 171 |
| compute_flood_depth_damage | MISS | request_spatial_input | 240 |
| compute_hillshade | NO_CALL |  | 1 |
| compute_home_range_kde | NO_CALL |  | 2 |
| compute_idf_curve | NO_CALL |  | 0 |
| compute_impact_envelope | NO_CALL |  | 0 |
| compute_impervious_surface | NO_CALL |  | 0 |
| compute_layer_bounds | NO_CALL |  | 0 |
| compute_movement_trajectory | MISS | publish_layer | 60 |
| compute_ndvi | MISS | compute_movement_trajectory | 93 |
| compute_overtopping | MISS | compute_movement_trajectory | 112 |
| compute_sediment_yield | MISS | run_model_flood_scenario | 77 |
| compute_slope | NO_CALL |  | 15 |
| compute_terrain_profile | ERROR | ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no clo | 2 |
| compute_urban_heat_island | MISS | geocode_location | 152 |
| compute_wave_nomograph | MISS | compute_urban_heat_island | 109 |
| compute_zonal_statistics | HIT | fetch_modis_lst | 95 |
| count_features_above_threshold | HIT | fetch_modis_lst | 156 |
| cut_features_with_polygon | MISS | compute_zonal_statistics | 91 |
| delineate_watershed | HIT | compute_zonal_statistics | 116 |
| describe_qgis_algorithm | MISS | fetch_dem | 104 |
| digitize_water_body | MISS | compute_zonal_statistics | 240 |
| discover_dataset | MISS | compute_layer_bounds | 84 |
| enhance_satellite_image | MISS | fetch_dem | 156 |
| export_case_to_qgis | MISS | fetch_dem | 59 |
| extract_landcover_class | NO_CALL |  | 56 |
| extract_stream_network | HIT | extract_stream_network | 77 |
| fetch_3dep_extra | MISS | fetch_dem | 138 |
| fetch_administrative_boundaries | MISS | fetch_dem | 191 |
| fetch_airnow_air_quality | MISS | export_case_to_qgis | 75 |
| fetch_asos_metar | HIT | geocode_location | 103 |
| fetch_buildings | MISS | geocode_location | 97 |
| fetch_cama_flood_discharge | MISS | geocode_location | 240 |
| fetch_cdc_svi | NO_CALL |  | 1 |
| fetch_census_acs | NO_CALL |  | 2 |
| fetch_chirps_precipitation | NO_CALL |  | 0 |
| fetch_climate_normals | NO_CALL |  | 0 |
| fetch_copernicus_dem | NO_CALL |  | 0 |
| fetch_dem | NO_CALL |  | 1 |
| fetch_ebird_observations | NO_CALL |  | 2 |
| fetch_epa_ejscreen | NO_CALL |  | 0 |
| fetch_epa_frs_facilities | NO_CALL |  | 0 |
| fetch_era5_reanalysis | NO_CALL |  | 0 |
| fetch_esri_landcover_10m | NO_CALL |  | 0 |
| fetch_fault_sources | NO_CALL |  | 0 |
| fetch_fema_nfhl_zones | NO_CALL |  | 0 |
| fetch_field_boundaries | MISS | geocode_location | 93 |
| fetch_firms_active_fire | HIT | geocode_location | 240 |
| fetch_gbif_occurrences | NO_CALL |  | 1 |
| fetch_gcn250_curve_numbers | NO_CALL |  | 1 |
| fetch_ghsl_population | NO_CALL |  | 0 |
| fetch_glm_lightning | NO_CALL |  | 0 |
| fetch_goes_active_fire | NO_CALL |  | 0 |
| fetch_goes_animation | NO_CALL |  | 0 |
| fetch_goes_archive_animation | HIT | geocode_location | 115 |
| fetch_goes_blend_animation | MISS | geocode_location | 165 |
| fetch_goes_satellite | HIT | fetch_goes_archive_animation | 132 |
| fetch_gridmet | HIT | fetch_viirs_day_fire | 111 |
| fetch_gtsm_tide_surge | MISS | fetch_goes_animation | 138 |
| fetch_hifld_critical_infrastructure | MISS | fetch_dem | 103 |
| fetch_hifld_transmission_lines | MISS | fetch_sentinel2_truecolor | 114 |
| fetch_hrrr_forecast | MISS | fetch_dem | 120 |
| fetch_hrrr_smoke | MISS | geocode_location | 63 |
| fetch_hrsl_population | MISS | geocode_location | 109 |
| fetch_inaturalist_observations | MISS | compute_layer_bounds | 57 |
| fetch_iucn_red_list_range | MISS | fetch_population | 74 |
| fetch_jrc_global_surface_water | HIT | compute_layer_bounds | 85 |
| fetch_landcover | NO_CALL |  | 30 |
| fetch_landfire_fuels | HIT | fetch_hrrr_smoke | 142 |
| fetch_landsat_imagery | MISS | fetch_dem | 73 |
| fetch_lehd_jobs | MISS | geocode_location | 121 |
| fetch_mobi | HIT | fetch_lehd_jobs | 68 |
| fetch_modis_lst | HIT | fetch_modis_lst | 71 |
| fetch_movebank_tracks | NO_CALL |  | 42 |
| fetch_mrms_qpe | HIT | fetch_population | 201 |
| fetch_mtbs_burn_severity | HIT | fetch_population | 141 |
| fetch_naip | HIT | fetch_population | 101 |
| fetch_nexrad_reflectivity | MISS | fetch_dem | 95 |
| fetch_nhdplus_nldi_navigate | MISS | geocode_location | 178 |
| fetch_nifc_fire_perimeters | HIT | fetch_nhdplus_nldi_navigate | 100 |
| fetch_noaa_coops_currents | HIT | fetch_noaa_coops_currents | 62 |
| fetch_noaa_coops_tides | HIT | fetch_river_geometry | 104 |
| fetch_noaa_nwm_streamflow | MISS | fetch_river_geometry | 92 |
| fetch_noaa_slr_confidence | MISS | fetch_usgs_nwis_gauges | 240 |
| fetch_noaa_slr_marsh | NO_CALL |  | 2 |
| fetch_noaa_slr_scenarios | NO_CALL |  | 1 |
| fetch_noaa_sst | MISS | publish_layer | 50 |
| fetch_nws_alerts_conus | MISS | fetch_noaa_sst | 148 |
| fetch_nws_event | MISS | fetch_noaa_sst | 199 |
| fetch_nws_river_forecast | HIT | geocode_location | 156 |
| fetch_openaq_measurements | HIT | fetch_openaq_measurements | 240 |
| fetch_openfema_disasters | NO_CALL |  | 2 |
| fetch_overpass_pois | NO_CALL |  | 1 |
| fetch_population | NO_CALL |  | 1 |
| fetch_raws_weather | MISS | geocode_location | 81 |
| fetch_river_geometry | HIT | geocode_location | 92 |
| fetch_roads_osm | HIT | fetch_dem | 103 |
| fetch_sentinel1_sar | HIT | fetch_dem | 86 |
| fetch_sentinel2_truecolor | MISS | geocode_location | 132 |
| fetch_snotel_snow | HIT | fetch_raws_weather | 127 |
| fetch_soilgrids | HIT | fetch_soilgrids | 96 |
| fetch_statsgo_soils | HIT | fetch_dem | 131 |
| fetch_storm_events_db | HIT | geocode_location | 240 |
| fetch_topobathy | MISS | fetch_storm_events_db | 2 |
| fetch_tsunami_events | NO_CALL |  | 1 |
| fetch_us_drought_monitor | NO_CALL |  | 0 |
| fetch_usace_dams | NO_CALL |  | 0 |
| fetch_usace_levees | NO_CALL |  | 0 |
| fetch_usace_nsi | NO_CALL |  | 0 |
| fetch_usfs_canopy_fuels | NO_CALL |  | 0 |
| fetch_usgs_earthquakes | MISS | geocode_location | 171 |
| fetch_usgs_groundwater_levels | MISS | fetch_usgs_earthquakes | 79 |
| fetch_usgs_nwis_gauges | MISS | geocode_location | 82 |
| fetch_usgs_volcano_alerts | MISS | geocode_location | 75 |
| fetch_usgs_water_quality | MISS | geocode_location | 140 |
| fetch_viirs_day_fire | HIT | fetch_dem | 179 |
| fetch_wdpa_protected_areas | HIT | fetch_viirs_day_fire | 105 |
| fetch_wfigs_incident | HIT | fetch_usgs_earthquakes | 188 |
| fill_gaps | MISS | geocode_location | 66 |
| generate_choropleth_legend | MISS | geocode_location | 74 |
| generate_damage_distribution | MISS | geocode_location | 88 |
| generate_histogram | MISS | fetch_dem | 100 |
| generate_time_series | MISS | fetch_dem | 90 |
| geocode_location | MISS | fetch_dem | 72 |
| list_categories | HIT | geocode_location | 109 |
| list_qgis_algorithms | HIT | fetch_dem | 65 |
| list_run_frames | MISS | geocode_location | 66 |
| list_tools_in_category | MISS | geocode_location | 92 |
| lookup_precip_return_period | HIT | fetch_dem | 136 |
| merge_features | HIT | geocode_location | 101 |
| model_debris_flow | HIT | geocode_location | 104 |
| postprocess_pelicun | HIT | geocode_location | 96 |
| publish_layer | HIT | geocode_location | 90 |
| qgis_process | HIT | fetch_dem | 96 |
| request_spatial_input | HIT | request_spatial_input | 45 |
| run_geoclaw_inundation | MISS | request_spatial_input | 85 |
| run_landlab_susceptibility | MISS | geocode_location | 123 |
| run_model_asr_scenario | MISS | request_spatial_input | 77 |
| run_model_capture_zone_scenario | MISS | request_spatial_input | 53 |
| run_model_conservation_priority | MISS | request_spatial_input | 166 |
| run_model_contamination_affected_fields | MISS | request_spatial_input | 73 |
| run_model_flood_habitat_scenario | MISS | run_geoclaw_inundation | 61 |
| run_model_flood_scenario | MISS | geocode_location | 91 |
| run_model_glm_lightning_animation | MISS | geocode_location | 63 |
| run_model_goes_fire_animation | MISS | geocode_location | 51 |
| run_model_groundwater_contamination_scenario | MISS | geocode_location | 59 |
| run_model_mar_scenario | MISS | geocode_location | 70 |
| run_model_mine_dewatering_scenario | MISS | geocode_location | 55 |
| run_model_multi_species_scenario | MISS | geocode_location | 80 |
| run_model_news_event_ingest | MISS | geocode_location | 65 |
| run_model_nws_flood_event_scenario | MISS | geocode_location | 61 |
| run_model_regional_water_budget_scenario | MISS | geocode_location | 62 |
| run_model_river_seepage_scenario | MISS | geocode_location | 79 |
| run_model_saltwater_intrusion_scenario | MISS | geocode_location | 63 |
| run_model_satellite_fire_animation | MISS | geocode_location | 36 |
| run_model_sustainable_yield_scenario | MISS | geocode_location | 66 |
| run_model_wellhead_protection_scenario | MISS | geocode_location | 41 |
| run_model_wetland_hydroperiod_scenario | MISS | geocode_location | 53 |
| run_modflow_job | MISS | geocode_location | 138 |
| run_pelicun_damage_assessment | MISS | run_modflow_job | 93 |
| run_pelicun_with_buildings | MISS | compute_building_density | 154 |
| run_river_seepage_job | MISS | run_modflow_job | 125 |
| run_seismic_hazard_psha | HIT | run_modflow_job | 78 |
| run_swan_waves | MISS | run_modflow_job | 96 |
| run_swmm_urban_flood | MISS | run_modflow_job | 77 |
| summarize_layer_statistics | HIT | summarize_layer_statistics | 54 |
| web_fetch | MISS | geocode_location | 93 |
