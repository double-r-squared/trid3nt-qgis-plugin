# TRID3NT Local tool-routing sweep (pass 3) -- qwen3:8b-16k

Updated: 2026-07-06T19:50:04  
Scored 77/174 | ERROR 1 | HIT 10 | MISS 34 | NO_CALL 32

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
| compute_canopy_height | ERROR | ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout; no clo | 4 |
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
