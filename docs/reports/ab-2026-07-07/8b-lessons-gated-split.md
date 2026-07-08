# Pass-3 failure split: retrieval vs model (qwen3:8b-16k, K=8)

Scored 183 | HIT 44 | failures split: MODEL-MISS 138

RETRIEVAL-MISS = expected tool absent from the top-K shortlist (model never saw it).
MODEL-MISS = tool was on the menu; the model chose otherwise.

| tool | outcome | class | in top-50? | model called |
|---|---|---|---|---|
| aggregate_claims_across_sources | MISS | MODEL-MISS | y | fetch_dem |
| aggregate_property_within_zone | MISS | MODEL-MISS | y | geocode_location |
| analyze_affected_fields | MISS | MODEL-MISS | y | run_model_groundwater_contamination_scenario |
| catalog_fetch | MISS | MODEL-MISS | y | fetch_dem |
| catalog_search | MISS | MODEL-MISS | y | compute_layer_bounds |
| clip_raster_to_polygon | MISS | MODEL-MISS | y | fetch_dem |
| code_exec_request | MISS | MODEL-MISS | y | geocode_location |
| compute_aspect | NO_CALL | MODEL-MISS | y |  |
| compute_blended_composite | NO_CALL | MODEL-MISS | y |  |
| compute_building_density | NO_CALL | MODEL-MISS | y |  |
| compute_canopy_height | NO_CALL | MODEL-MISS | y |  |
| compute_change_detection | NO_CALL | MODEL-MISS | y |  |
| compute_colored_relief | NO_CALL | MODEL-MISS | y |  |
| compute_contours | NO_CALL | MODEL-MISS | y |  |
| compute_cross_section | MISS | MODEL-MISS | y | geocode_location |
| compute_flood_depth_damage | MISS | MODEL-MISS | y | request_spatial_input |
| compute_hillshade | NO_CALL | MODEL-MISS | y |  |
| compute_home_range_kde | NO_CALL | MODEL-MISS | y |  |
| compute_idf_curve | NO_CALL | MODEL-MISS | y |  |
| compute_impact_envelope | NO_CALL | MODEL-MISS | y |  |
| compute_impervious_surface | NO_CALL | MODEL-MISS | y |  |
| compute_layer_bounds | NO_CALL | MODEL-MISS | y |  |
| compute_movement_trajectory | MISS | MODEL-MISS | y | publish_layer |
| compute_ndvi | MISS | MODEL-MISS | y | compute_movement_trajectory |
| compute_overtopping | MISS | MODEL-MISS | y | compute_movement_trajectory |
| compute_sediment_yield | MISS | MODEL-MISS | y | run_model_flood_scenario |
| compute_slope | NO_CALL | MODEL-MISS | y |  |
| compute_urban_heat_island | MISS | MODEL-MISS | y | geocode_location |
| compute_wave_nomograph | MISS | MODEL-MISS | y | compute_urban_heat_island |
| cut_features_with_polygon | MISS | MODEL-MISS | y | compute_zonal_statistics |
| describe_qgis_algorithm | MISS | MODEL-MISS | y | fetch_dem |
| digitize_water_body | MISS | MODEL-MISS | y | compute_zonal_statistics |
| discover_dataset | MISS | MODEL-MISS | y | compute_layer_bounds |
| enhance_satellite_image | MISS | MODEL-MISS | y | fetch_dem |
| export_case_to_qgis | MISS | MODEL-MISS | y | fetch_dem |
| extract_landcover_class | NO_CALL | MODEL-MISS | y |  |
| fetch_3dep_extra | MISS | MODEL-MISS | y | fetch_dem |
| fetch_administrative_boundaries | MISS | MODEL-MISS | y | fetch_dem |
| fetch_airnow_air_quality | MISS | MODEL-MISS | y | export_case_to_qgis |
| fetch_buildings | MISS | MODEL-MISS | y | geocode_location |
| fetch_cama_flood_discharge | MISS | MODEL-MISS | y | geocode_location |
| fetch_cdc_svi | NO_CALL | MODEL-MISS | y |  |
| fetch_census_acs | NO_CALL | MODEL-MISS | y |  |
| fetch_chirps_precipitation | NO_CALL | MODEL-MISS | y |  |
| fetch_climate_normals | NO_CALL | MODEL-MISS | y |  |
| fetch_copernicus_dem | NO_CALL | MODEL-MISS | y |  |
| fetch_dem | NO_CALL | MODEL-MISS | y |  |
| fetch_ebird_observations | NO_CALL | MODEL-MISS | y |  |
| fetch_epa_ejscreen | NO_CALL | MODEL-MISS | y |  |
| fetch_epa_frs_facilities | NO_CALL | MODEL-MISS | y |  |
| fetch_era5_reanalysis | NO_CALL | MODEL-MISS | y |  |
| fetch_esri_landcover_10m | NO_CALL | MODEL-MISS | y |  |
| fetch_fault_sources | NO_CALL | MODEL-MISS | y |  |
| fetch_fema_nfhl_zones | NO_CALL | MODEL-MISS | y |  |
| fetch_field_boundaries | MISS | MODEL-MISS | y | geocode_location |
| fetch_gbif_occurrences | NO_CALL | MODEL-MISS | y |  |
| fetch_gcn250_curve_numbers | NO_CALL | MODEL-MISS | y |  |
| fetch_ghsl_population | NO_CALL | MODEL-MISS | y |  |
| fetch_glm_lightning | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_active_fire | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_animation | NO_CALL | MODEL-MISS | y |  |
| fetch_goes_blend_animation | MISS | MODEL-MISS | y | geocode_location |
| fetch_gtsm_tide_surge | MISS | MODEL-MISS | y | fetch_goes_animation |
| fetch_hifld_critical_infrastructure | MISS | MODEL-MISS | y | fetch_dem |
| fetch_hifld_transmission_lines | MISS | MODEL-MISS | y | fetch_sentinel2_truecolor |
| fetch_hrrr_forecast | MISS | MODEL-MISS | y | fetch_dem |
| fetch_hrrr_smoke | MISS | MODEL-MISS | y | geocode_location |
| fetch_hrsl_population | MISS | MODEL-MISS | y | geocode_location |
| fetch_inaturalist_observations | MISS | MODEL-MISS | y | compute_layer_bounds |
| fetch_iucn_red_list_range | MISS | MODEL-MISS | y | fetch_population |
| fetch_landcover | NO_CALL | MODEL-MISS | y |  |
| fetch_landsat_imagery | MISS | MODEL-MISS | y | fetch_dem |
| fetch_lehd_jobs | MISS | MODEL-MISS | y | geocode_location |
| fetch_movebank_tracks | NO_CALL | MODEL-MISS | y |  |
| fetch_nexrad_reflectivity | MISS | MODEL-MISS | y | fetch_dem |
| fetch_nhdplus_nldi_navigate | MISS | MODEL-MISS | y | geocode_location |
| fetch_noaa_nwm_streamflow | MISS | MODEL-MISS | y | fetch_river_geometry |
| fetch_noaa_slr_confidence | MISS | MODEL-MISS | y | fetch_usgs_nwis_gauges |
| fetch_noaa_slr_marsh | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_slr_scenarios | NO_CALL | MODEL-MISS | y |  |
| fetch_noaa_sst | MISS | MODEL-MISS | y | publish_layer |
| fetch_nws_alerts_conus | MISS | MODEL-MISS | y | fetch_noaa_sst |
| fetch_nws_event | MISS | MODEL-MISS | y | fetch_noaa_sst |
| fetch_openfema_disasters | NO_CALL | MODEL-MISS | y |  |
| fetch_overpass_pois | NO_CALL | MODEL-MISS | y |  |
| fetch_population | NO_CALL | MODEL-MISS | y |  |
| fetch_raws_weather | MISS | MODEL-MISS | y | geocode_location |
| fetch_sentinel2_truecolor | MISS | MODEL-MISS | y | geocode_location |
| fetch_topobathy | MISS | MODEL-MISS | y | fetch_storm_events_db |
| fetch_tsunami_events | NO_CALL | MODEL-MISS | y |  |
| fetch_us_drought_monitor | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_dams | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_levees | NO_CALL | MODEL-MISS | y |  |
| fetch_usace_nsi | NO_CALL | MODEL-MISS | y |  |
| fetch_usfs_canopy_fuels | NO_CALL | MODEL-MISS | y |  |
| fetch_usgs_earthquakes | MISS | MODEL-MISS | y | geocode_location |
| fetch_usgs_groundwater_levels | MISS | MODEL-MISS | y | fetch_usgs_earthquakes |
| fetch_usgs_nwis_gauges | MISS | MODEL-MISS | y | geocode_location |
| fetch_usgs_volcano_alerts | MISS | MODEL-MISS | y | geocode_location |
| fetch_usgs_water_quality | MISS | MODEL-MISS | y | geocode_location |
| fill_gaps | MISS | MODEL-MISS | y | geocode_location |
| generate_choropleth_legend | MISS | MODEL-MISS | y | geocode_location |
| generate_damage_distribution | MISS | MODEL-MISS | y | geocode_location |
| generate_histogram | MISS | MODEL-MISS | y | fetch_dem |
| generate_time_series | MISS | MODEL-MISS | y | fetch_dem |
| geocode_location | MISS | MODEL-MISS | y | fetch_dem |
| list_run_frames | MISS | MODEL-MISS | y | geocode_location |
| list_tools_in_category | MISS | MODEL-MISS | y | geocode_location |
| run_geoclaw_inundation | MISS | MODEL-MISS | y | request_spatial_input |
| run_landlab_susceptibility | MISS | MODEL-MISS | y | geocode_location |
| run_model_asr_scenario | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_capture_zone_scenario | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_conservation_priority | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_contamination_affected_fields | MISS | MODEL-MISS | y | request_spatial_input |
| run_model_flood_habitat_scenario | MISS | MODEL-MISS | y | run_geoclaw_inundation |
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
| run_model_saltwater_intrusion_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_satellite_fire_animation | MISS | MODEL-MISS | y | geocode_location |
| run_model_sustainable_yield_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_wellhead_protection_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_model_wetland_hydroperiod_scenario | MISS | MODEL-MISS | y | geocode_location |
| run_modflow_job | MISS | MODEL-MISS | y | geocode_location |
| run_pelicun_damage_assessment | MISS | MODEL-MISS | y | run_modflow_job |
| run_pelicun_with_buildings | MISS | MODEL-MISS | y | compute_building_density |
| run_river_seepage_job | MISS | MODEL-MISS | y | run_modflow_job |
| run_swan_waves | MISS | MODEL-MISS | y | run_modflow_job |
| run_swmm_urban_flood | MISS | MODEL-MISS | y | run_modflow_job |
| web_fetch | MISS | MODEL-MISS | y | geocode_location |
