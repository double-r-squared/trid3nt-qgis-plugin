# TRID3NT Local tool-usability sweep (<= 2 turns)

Updated: 2026-07-08T00:09:22  
Scored 180/183 | ERROR 1 | UNUSABLE 22 | USABLE_T1 102 | USABLE_T2 55

**Usable coverage so far: 157/180 (87%)**

| tool | outcome | t1_first_call | t2_first_call | seconds |
|---|---|---|---|---|
| aggregate_claims_across_sources | USABLE_T2 | publish_layer | aggregate_claims_across_sources | 186 |
| aggregate_property_within_zone | USABLE_T2 |  | aggregate_property_within_zone | 218 |
| analyze_affected_fields | USABLE_T2 | run_model_groundwater_contamination_scenario | analyze_affected_fields | 149 |
| catalog_fetch | UNUSABLE |  |  | 81 |
| catalog_search | USABLE_T1 | geocode_location |  | 228 |
| clip_raster_to_bbox | USABLE_T1 | aggregate_property_within_zone |  | 73 |
| clip_raster_to_polygon | USABLE_T1 | clip_raster_to_polygon |  | 168 |
| clip_vector_to_polygon | USABLE_T1 | fetch_dem |  | 136 |
| code_exec_request | USABLE_T1 | code_exec_request |  | 53 |
| compute_aspect | USABLE_T1 | compute_aspect |  | 194 |
| compute_blended_composite | USABLE_T2 |  | compute_blended_composite | 197 |
| compute_building_density | USABLE_T1 | geocode_location |  | 311 |
| compute_canopy_height | UNUSABLE |  |  | 53 |
| compute_change_detection | UNUSABLE |  |  | 81 |
| compute_colored_relief | USABLE_T1 | geocode_location |  | 241 |
| compute_contours | USABLE_T2 | geocode_location | compute_contours | 300 |
| compute_cross_section | USABLE_T2 | geocode_location | compute_cross_section | 281 |
| compute_flood_depth_damage | UNUSABLE | run_model_flood_scenario |  | 244 |
| compute_hillshade | UNUSABLE |  |  | 31 |
| compute_home_range_kde | UNUSABLE |  |  | 224 |
| compute_idf_curve | UNUSABLE |  |  | 78 |
| compute_impact_envelope | UNUSABLE | run_model_flood_scenario |  | 202 |
| compute_impervious_surface | USABLE_T2 | geocode_location | compute_impervious_surface | 387 |
| compute_layer_bounds | USABLE_T1 | compute_layer_bounds |  | 282 |
| compute_movement_trajectory | UNUSABLE | fetch_dem |  | 244 |
| compute_ndvi | UNUSABLE |  |  | 43 |
| compute_overtopping | USABLE_T1 | compute_overtopping |  | 218 |
| compute_sediment_yield | USABLE_T2 | fetch_soilgrids | compute_sediment_yield | 344 |
| compute_slope | USABLE_T1 | compute_slope |  | 233 |
| compute_terrain_profile | USABLE_T1 | geocode_location |  | 164 |
| compute_urban_heat_island | UNUSABLE | geocode_location | geocode_location | 260 |
| compute_wave_nomograph | USABLE_T2 | run_swan_waves | compute_wave_nomograph | 266 |
| compute_zonal_statistics | USABLE_T1 | fetch_modis_lst |  | 95 |
| count_features_above_threshold | USABLE_T1 | fetch_modis_lst |  | 156 |
| cut_features_with_polygon | USABLE_T2 |  | cut_features_with_polygon | 207 |
| delineate_watershed | USABLE_T1 | compute_zonal_statistics |  | 116 |
| describe_qgis_algorithm | USABLE_T2 | list_qgis_algorithms | describe_qgis_algorithm | 123 |
| digitize_water_body | USABLE_T1 | digitize_water_body |  | 99 |
| discover_dataset | USABLE_T1 | discover_dataset |  | 51 |
| enhance_satellite_image | UNUSABLE | geocode_location | publish_layer | 273 |
| export_case_to_qgis | USABLE_T1 | export_case_to_qgis |  | 64 |
| extract_landcover_class | USABLE_T2 | geocode_location | extract_landcover_class | 323 |
| extract_stream_network | USABLE_T1 | extract_stream_network |  | 77 |
| fetch_3dep_extra | USABLE_T2 | fetch_dem | fetch_3dep_extra | 172 |
| fetch_administrative_boundaries | USABLE_T2 |  | fetch_administrative_boundaries | 117 |
| fetch_airnow_air_quality | UNUSABLE | fetch_openaq_measurements |  | 243 |
| fetch_asos_metar | USABLE_T1 | geocode_location |  | 103 |
| fetch_buildings | USABLE_T1 | geocode_location |  | 240 |
| fetch_cama_flood_discharge | USABLE_T2 | compute_building_density | run_pelicun_with_buildings | 191 |
| fetch_cdc_svi | UNUSABLE |  |  | 22 |
| fetch_census_acs | USABLE_T2 | fetch_cdc_svi | publish_layer | 270 |
| fetch_chirps_precipitation | USABLE_T2 |  | fetch_chirps_precipitation | 133 |
| fetch_climate_normals | USABLE_T2 | fetch_dem | fetch_climate_normals | 454 |
| fetch_copernicus_dem | UNUSABLE | fetch_climate_normals |  | 78 |
| fetch_dem | USABLE_T1 | fetch_copernicus_dem |  | 141 |
| fetch_ebird_observations | USABLE_T1 | fetch_ebird_observations |  | 98 |
| fetch_epa_ejscreen | USABLE_T2 | geocode_location | fetch_epa_ejscreen | 172 |
| fetch_epa_frs_facilities | USABLE_T2 |  | fetch_epa_frs_facilities | 121 |
| fetch_era5_reanalysis | UNUSABLE |  | geocode_location | 157 |
| fetch_esri_landcover_10m | UNUSABLE | geocode_location | geocode_location | 228 |
| fetch_fault_sources | USABLE_T2 | geocode_location | fetch_fault_sources | 246 |
| fetch_fema_nfhl_zones | USABLE_T1 | fetch_fema_nfhl_zones |  | 54 |
| fetch_field_boundaries | USABLE_T2 |  | fetch_field_boundaries | 125 |
| fetch_firms_active_fire | USABLE_T1 | geocode_location |  | 240 |
| fetch_gbif_occurrences | USABLE_T1 | fetch_gbif_occurrences |  | 83 |
| fetch_gcn250_curve_numbers | USABLE_T1 | fetch_gcn250_curve_numbers |  | 153 |
| fetch_ghsl_population | USABLE_T2 | geocode_location | fetch_ghsl_population | 191 |
| fetch_glm_lightning | USABLE_T2 | run_model_glm_lightning_animation | fetch_glm_lightning | 234 |
| fetch_goes_active_fire | USABLE_T2 | geocode_location | fetch_goes_active_fire | 364 |
| fetch_goes_animation | USABLE_T2 | run_model_goes_fire_animation | fetch_goes_animation | 179 |
| fetch_goes_archive_animation | USABLE_T1 | geocode_location |  | 115 |
| fetch_goes_blend_animation | UNUSABLE |  |  | 25 |
| fetch_goes_satellite | USABLE_T1 | fetch_goes_archive_animation |  | 132 |
| fetch_gridmet | USABLE_T1 | fetch_viirs_day_fire |  | 111 |
| fetch_gtsm_tide_surge | USABLE_T1 | fetch_goes_blend_animation |  | 136 |
| fetch_hifld_critical_infrastructure | USABLE_T1 | geocode_location |  | 217 |
| fetch_hifld_transmission_lines | USABLE_T1 | fetch_hifld_transmission_lines |  | 95 |
| fetch_hrrr_forecast | USABLE_T1 | fetch_hrrr_forecast |  | 70 |
| fetch_hrrr_smoke | USABLE_T1 | fetch_hrrr_smoke |  | 72 |
| fetch_hrsl_population | USABLE_T2 | geocode_location | fetch_hrsl_population | 207 |
| fetch_inaturalist_observations | USABLE_T1 | fetch_inaturalist_observations |  | 107 |
| fetch_iucn_red_list_range | USABLE_T1 | fetch_iucn_red_list_range |  | 241 |
| fetch_jrc_global_surface_water | USABLE_T1 | compute_layer_bounds |  | 85 |
| fetch_landcover | USABLE_T1 | fetch_landcover |  | 112 |
| fetch_landfire_fuels | USABLE_T1 | fetch_hrrr_smoke |  | 142 |
| fetch_landsat_imagery | USABLE_T2 | geocode_location | fetch_landsat_imagery | 168 |
| fetch_lehd_jobs | UNUSABLE | geocode_location | geocode_location | 221 |
| fetch_mobi | USABLE_T1 | fetch_lehd_jobs |  | 68 |
| fetch_modis_lst | USABLE_T1 | fetch_modis_lst |  | 71 |
| fetch_movebank_tracks | USABLE_T1 | fetch_movebank_tracks |  | 74 |
| fetch_mrms_qpe | USABLE_T1 | fetch_population |  | 201 |
| fetch_mtbs_burn_severity | USABLE_T1 | fetch_population |  | 141 |
| fetch_naip | USABLE_T1 | fetch_population |  | 101 |
| fetch_nexrad_reflectivity | USABLE_T1 | fetch_nexrad_reflectivity |  | 86 |
| fetch_nhdplus_nldi_navigate | USABLE_T1 | fetch_nhdplus_nldi_navigate |  | 114 |
| fetch_nifc_fire_perimeters | USABLE_T1 | fetch_nhdplus_nldi_navigate |  | 100 |
| fetch_noaa_coops_currents | USABLE_T1 | fetch_noaa_coops_currents |  | 62 |
| fetch_noaa_coops_tides | USABLE_T1 | fetch_river_geometry |  | 104 |
| fetch_noaa_nwm_streamflow | USABLE_T1 | fetch_noaa_nwm_streamflow |  | 113 |
| fetch_noaa_slr_confidence | USABLE_T1 | fetch_noaa_slr_confidence |  | 208 |
| fetch_noaa_slr_marsh | USABLE_T2 | geocode_location | fetch_noaa_slr_marsh | 261 |
| fetch_noaa_slr_scenarios | USABLE_T1 | geocode_location |  | 64 |
| fetch_noaa_sst | USABLE_T2 | geocode_location | fetch_noaa_sst | 188 |
| fetch_nws_alerts_conus | USABLE_T2 | fetch_nws_event | fetch_nws_alerts_conus | 222 |
| fetch_nws_event | USABLE_T1 | fetch_nws_event |  | 92 |
| fetch_nws_river_forecast | USABLE_T1 | geocode_location |  | 156 |
| fetch_openaq_measurements | USABLE_T1 | fetch_openaq_measurements |  | 240 |
| fetch_openfema_disasters | USABLE_T1 | fetch_openfema_disasters |  | 55 |
| fetch_overpass_pois | USABLE_T2 | geocode_location | fetch_overpass_pois | 166 |
| fetch_population | USABLE_T2 | geocode_location | fetch_population | 249 |
| fetch_raws_weather | USABLE_T2 | geocode_location | fetch_raws_weather | 252 |
| fetch_river_geometry | USABLE_T1 | geocode_location |  | 92 |
| fetch_roads_osm | USABLE_T1 | fetch_dem |  | 103 |
| fetch_sentinel1_sar | USABLE_T1 | fetch_dem |  | 86 |
| fetch_sentinel2_truecolor | UNUSABLE | geocode_location | geocode_location | 192 |
| fetch_snotel_snow | USABLE_T1 | fetch_raws_weather |  | 127 |
| fetch_soilgrids | USABLE_T1 | fetch_soilgrids |  | 96 |
| fetch_statsgo_soils | USABLE_T1 | fetch_dem |  | 131 |
| fetch_storm_events_db | USABLE_T1 | geocode_location |  | 240 |
| fetch_topobathy | USABLE_T1 | geocode_location |  | 118 |
| fetch_tsunami_events | USABLE_T2 | geocode_location | fetch_tsunami_events | 204 |
| fetch_us_drought_monitor | USABLE_T2 | geocode_location | fetch_us_drought_monitor | 217 |
| fetch_usace_dams | USABLE_T1 | geocode_location |  | 104 |
| fetch_usace_levees | USABLE_T2 | geocode_location | fetch_usace_levees | 198 |
| fetch_usace_nsi | USABLE_T2 | geocode_location | fetch_usace_nsi | 240 |
| fetch_usfs_canopy_fuels | USABLE_T2 | geocode_location | fetch_usfs_canopy_fuels | 207 |
| fetch_usgs_earthquakes | USABLE_T2 | geocode_location | fetch_usgs_earthquakes | 144 |
| fetch_usgs_groundwater_levels | USABLE_T2 | geocode_location | fetch_usgs_groundwater_levels | 236 |
| fetch_usgs_nwis_gauges | USABLE_T1 | fetch_usgs_nwis_gauges |  | 103 |
| fetch_usgs_volcano_alerts | UNUSABLE | geocode_location | geocode_location | 251 |
| fetch_usgs_water_quality | USABLE_T1 | fetch_usgs_water_quality |  | 81 |
| fetch_viirs_day_fire | USABLE_T1 | fetch_dem |  | 179 |
| fetch_wdpa_protected_areas | USABLE_T1 | fetch_viirs_day_fire |  | 105 |
| fetch_wfigs_incident | USABLE_T1 | fetch_usgs_earthquakes |  | 188 |
| fill_gaps | USABLE_T2 | geocode_location | fill_gaps | 286 |
| generate_choropleth_legend | USABLE_T1 | generate_choropleth_legend |  | 163 |
| generate_damage_distribution | USABLE_T1 | generate_damage_distribution |  | 196 |
| generate_histogram | USABLE_T2 | geocode_location | generate_histogram | 217 |
| generate_time_series | USABLE_T1 | geocode_location |  | 196 |
| geocode_location | USABLE_T1 | geocode_location |  | 157 |
| list_categories | USABLE_T1 | geocode_location |  | 109 |
| list_qgis_algorithms | USABLE_T1 | fetch_dem |  | 65 |
| list_run_frames | USABLE_T2 |  | list_run_frames | 109 |
| list_tools_in_category | USABLE_T1 | list_tools_in_category |  | 67 |
| lookup_precip_return_period | USABLE_T1 | fetch_dem |  | 136 |
| merge_features | USABLE_T1 | geocode_location |  | 101 |
| model_debris_flow | USABLE_T1 | geocode_location |  | 104 |
| postprocess_pelicun | USABLE_T1 | geocode_location |  | 96 |
| publish_layer | USABLE_T1 | geocode_location |  | 90 |
| qgis_process | USABLE_T1 | fetch_dem |  | 96 |
| request_spatial_input | USABLE_T1 | request_spatial_input |  | 45 |
| run_geoclaw_inundation | USABLE_T2 | geocode_location | run_geoclaw_inundation | 161 |
| run_landlab_susceptibility | USABLE_T2 | geocode_location | run_landlab_susceptibility | 211 |
| run_model_asr_scenario | USABLE_T1 | run_model_asr_scenario |  | 36 |
| run_model_capture_zone_scenario | USABLE_T2 |  | run_model_capture_zone_scenario | 85 |
| run_model_conservation_priority | USABLE_T2 | geocode_location | run_model_conservation_priority | 101 |
| run_model_contamination_affected_fields | USABLE_T1 | run_model_contamination_affected_fields |  | 59 |
| run_model_flood_habitat_scenario | USABLE_T2 | geocode_location | run_model_flood_habitat_scenario | 122 |
| run_model_flood_scenario | USABLE_T2 | geocode_location | run_model_flood_scenario | 123 |
| run_model_glm_lightning_animation | USABLE_T1 | geocode_location |  | 73 |
| run_model_goes_fire_animation | USABLE_T1 | run_model_goes_fire_animation |  | 34 |
| run_model_groundwater_contamination_scenario | USABLE_T1 | run_model_groundwater_contamination_scenario |  | 56 |
| run_model_mar_scenario | USABLE_T1 | run_model_mar_scenario |  | 47 |
| run_model_mine_dewatering_scenario | USABLE_T1 | run_model_mine_dewatering_scenario |  | 62 |
| run_model_multi_species_scenario | USABLE_T2 | geocode_location | run_model_multi_species_scenario | 109 |
| run_model_news_event_ingest | USABLE_T1 | run_model_news_event_ingest |  | 53 |
| run_model_nws_flood_event_scenario | UNUSABLE |  |  | 124 |
| run_model_regional_water_budget_scenario | USABLE_T1 | run_model_regional_water_budget_scenario |  | 33 |
| run_model_river_seepage_scenario | USABLE_T2 | geocode_location | run_model_river_seepage_scenario | 107 |
| run_model_saltwater_intrusion_scenario | USABLE_T1 | run_model_saltwater_intrusion_scenario |  | 36 |
| run_model_satellite_fire_animation | USABLE_T1 | run_model_satellite_fire_animation |  | 53 |
| run_model_sustainable_yield_scenario | USABLE_T1 | run_model_sustainable_yield_scenario |  | 52 |
| run_model_wellhead_protection_scenario | USABLE_T1 | run_model_wellhead_protection_scenario |  | 65 |
| run_model_wetland_hydroperiod_scenario | USABLE_T2 | geocode_location | run_model_wetland_hydroperiod_scenario | 86 |
| run_modflow_job | USABLE_T2 | geocode_location | run_modflow_job | 133 |
| run_pelicun_damage_assessment | USABLE_T1 | run_pelicun_damage_assessment |  | 108 |
| run_pelicun_with_buildings | USABLE_T2 | geocode_location | run_pelicun_with_buildings | 247 |
| run_river_seepage_job | ERROR |  | TimeoutError:  | 21 |
| run_seismic_hazard_psha | USABLE_T1 | run_modflow_job |  | 78 |
| summarize_layer_statistics | USABLE_T1 | summarize_layer_statistics |  | 54 |
