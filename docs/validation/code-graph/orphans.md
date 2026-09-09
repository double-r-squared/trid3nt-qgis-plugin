# Orphans -- unreachable from every declared root

Roots: `trid3nt_server.main`, `trid3nt_server.__main__`, `trid3nt_server.tools`, `workers.telemac.entrypoint`, `plugin`

Bucket precedence is roots > tests > scripts, so a module reachable
from both a test and a script is reported as test-only.

Unreachable, not imported by any test, not imported by any script.
These are the corpses with nothing holding them up.

Excluded: 107 unreachable docstring-only `__init__.py` package
markers (directories the runtime walks for data, not modules to import).

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.tools.fetchers._router.hooks.cds` | 680 | trid3nt_server/tools/fetchers/_router/hooks/cds.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.hooks` | 386 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nws_river_forecast.hooks` | 340 | trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_raws_weather.hooks` | 292 | trid3nt_server/tools/fetchers/weather/fetch_raws_weather/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_asos_metar.hooks` | 288 | trid3nt_server/tools/fetchers/weather/fetch_asos_metar/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_usgs_earthquakes.hooks` | 257 | trid3nt_server/tools/fetchers/hazard/fetch_usgs_earthquakes/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.hooks` | 246 | trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels.hooks` | 244 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nws_alerts_conus.hooks` | 231 | trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_tsunami_events.hooks` | 221 | trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.soil.fetch_snotel_snow.hooks` | 207 | trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_usgs_volcano_alerts.hooks` | 205 | trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers._router.hooks.pfdf_raster` | 196 | trid3nt_server/tools/fetchers/_router/hooks/pfdf_raster.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_airnow_air_quality.hooks` | 173 | trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_usace_nsi.hooks` | 146 | trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nws_event.hooks` | 135 | trid3nt_server/tools/fetchers/weather/fetch_nws_event/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_osm_breakwaters.hooks` | 102 | trid3nt_server/tools/fetchers/ocean/fetch_osm_breakwaters/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_osm_coastline.hooks` | 66 | trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/hooks.py | no importer in any scanned tree |

## Test-only-reachable -- the anchor class

Reachable from `tests/` but from no root. The test is the only
thing keeping the module alive; deleting both is one move.

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.tools.fetchers._router.hooks.topobathy` | 1933 | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py | imported only by tests.test_bathymetry_data_seam, tests.test_fallback_ladder, tests.test_fallback_sweep_guard, tests.test_resolution_doctrine_0224 ... |
| `trid3nt_server.tools.fetchers.imagery._goes_archive_core` | 1324 | trid3nt_server/tools/fetchers/imagery/_goes_archive_core.py | imported only by tests.test_router_glm, tests.test_router_goes_archive, trid3nt_server.tools.fetchers._router.hooks.goes_archive, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
| `scripts.assemble_proof_packet` | 1046 | scripts/assemble_proof_packet.py | imported only by tests.test_animation_legend_stability |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_tracks.hooks` | 991 | trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/hooks.py | imported only by tests.test_router_storm_tracks |
| `scripts.render_selafin_animation` | 913 | scripts/render_selafin_animation.py | imported only by tests.test_animation_legend_stability |
| `trid3nt_server.tools.fetchers.imagery._satellite_slider` | 818 | trid3nt_server/tools/fetchers/imagery/_satellite_slider.py | imported only by tests.test_router_goes_animation, tests.test_router_viirs_day_fire, tests.test_satellite_slider, trid3nt_server.tools.fetchers._router.hooks.goes_animation ... |
| `scripts.model_check` | 788 | scripts/model_check.py | imported only by tests.test_model_conformance |
| `trid3nt_server.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow.hooks` | 692 | trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/hooks.py | imported only by tests.test_router_nwm_streamflow |
| `trid3nt_server.tools.fetchers.terrain.fetch_dem.hooks` | 565 | trid3nt_server/tools/fetchers/terrain/fetch_dem/hooks.py | imported only by tests.test_aoi_pin_lane_c, tests.test_router_dem |
| `trid3nt_server.testing.live_run` | 522 | trid3nt_server/testing/live_run.py | imported only by scripts.proof_artemis_om2d_rematch, tests.test_live_run_harness, trid3nt_server.testing, trid3nt_server.testing.canaries |
| `trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks` | 472 | trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/hooks.py | imported only by tests.test_router_glm |
| `trid3nt_server.tools.fetchers.imagery.fetch_goes_satellite.hooks` | 463 | trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/hooks.py | imported only by tests.test_router_goes_satellite |
| `trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters.hooks` | 457 | trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/hooks.py | imported only by tests.test_router_chained |
| `trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks` | 450 | trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/hooks.py | imported only by tests.test_bathymetry_data_seam |
| `trid3nt_server.tools.fetchers._router.hooks.goes_animation` | 377 | trid3nt_server/tools/fetchers/_router/hooks/goes_animation.py | imported only by tests.test_router_goes_animation |
| `trid3nt_server.testing.proof_animations` | 372 | trid3nt_server/testing/proof_animations.py | imported only by scripts.assemble_proof_packet, tests.test_animation_legend_stability, trid3nt_server.testing.canaries |
| `trid3nt_server.tools.fetchers.hydrology.fetch_lter_records.hooks` | 372 | trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/hooks.py | imported only by tests.test_router_lter_records |
| `trid3nt_server.tools.fetchers._router.hooks.topobathy_class` | 356 | trid3nt_server/tools/fetchers/_router/hooks/topobathy_class.py | imported only by tests.test_bathymetry_data_seam, trid3nt_server.tools.fetchers._router.hooks.topobathy |
| `trid3nt_server.tools.fetchers._router.hooks.hrrr` | 346 | trid3nt_server/tools/fetchers/_router/hooks/hrrr.py | imported only by tests.test_router_hrrr |
| `trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks.hooks` | 340 | trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/hooks.py | imported only by tests.test_router_envelope |
| `trid3nt_server.tools.fetchers.imagery._goes_common` | 294 | trid3nt_server/tools/fetchers/imagery/_goes_common.py | imported only by tests.test_router_goes_animation, tests.test_router_goes_archive, tests.test_router_goes_satellite, trid3nt_server.tools.fetchers._router.hooks.goes_animation ... |
| `trid3nt_server.tools.fetchers.hazard.fetch_fault_sources.hooks` | 283 | trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/hooks.py | imported only by tests.test_router_fault_sources |
| `trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.hooks` | 279 | trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/hooks.py | imported only by tests.test_router_arcgis_odd |
| `trid3nt_server.tools.fetchers._router.hooks.goes_archive` | 276 | trid3nt_server/tools/fetchers/_router/hooks/goes_archive.py | imported only by tests.test_router_goes_archive |
| `trid3nt_server.tools.fetchers.imagery.fetch_viirs_day_fire.hooks` | 265 | trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/hooks.py | imported only by tests.test_router_viirs_day_fire |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.hooks` | 261 | trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/hooks.py | imported only by tests.test_router_chained |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_population.hooks` | 250 | trid3nt_server/tools/fetchers/socioeconomic/fetch_population/hooks.py | imported only by tests.test_router_population |
| `trid3nt_server.tools.fetchers.hazard.fetch_usace_dams.hooks` | 245 | trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/hooks.py | imported only by tests.test_router_arcgis_odd |
| `trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident.hooks` | 229 | trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/hooks.py | imported only by tests.test_router_wfigs_incident |
| `scripts.harvest_living_atlas` | 224 | scripts/harvest_living_atlas.py | imported only by tests.test_living_atlas |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries.hooks` | 223 | trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/hooks.py | imported only by tests.test_aoi_pin_lane_c, tests.test_router_field_boundaries |
| `trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities.hooks` | 205 | trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/hooks.py | imported only by tests.test_router_arcgis_odd |
| `trid3nt_server.tools.fetchers.weather.fetch_aorc_precip.hooks` | 205 | trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/hooks.py | imported only by tests.test_router_aorc_precip |
| `trid3nt_server.tools.fetchers.hydrology.fetch_flood_extent_observation.hooks` | 195 | trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/hooks.py | imported only by tests.test_router_flood_extent_observation |
| `trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.hooks` | 178 | trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/hooks.py | imported only by tests.test_router_firms |
| `trid3nt_server.workflows.mesh.meshers.drivers.telemac_dico_driver` | 177 | trid3nt_server/workflows/mesh/meshers/drivers/telemac_dico_driver.py | imported only by scripts.extract_telemac_catalog, tests.test_mesh_om2d |
| `trid3nt_server.tools.fetchers._router.hooks.osm` | 174 | trid3nt_server/tools/fetchers/_router/hooks/osm.py | imported only by tests.test_router_overpass, trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry.hooks, trid3nt_server.tools.fetchers.ocean.fetch_osm_breakwaters.hooks, trid3nt_server.tools.fetchers.ocean.fetch_osm_coastline.hooks ... |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.hooks` | 168 | trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/hooks.py | imported only by tests.test_router_overpass |
| `trid3nt_server.tools.payload_sampling` | 168 | trid3nt_server/tools/payload_sampling.py | imported only by tests.test_resolution_doctrine_0224, trid3nt_server.tools.fetchers._router.hooks.topobathy |
| `trid3nt_server.tools.fetchers._router.hooks.hyriver` | 160 | trid3nt_server/tools/fetchers/_router/hooks/hyriver.py | imported only by tests.test_router_hyriver, trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.hooks, trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks.hooks, trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing.hooks |
| `trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing.hooks` | 156 | trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/hooks.py | imported only by tests.test_router_nldas2 |
| `trid3nt_server.tools.fetchers.weather.fetch_mrms_qpe.hooks` | 154 | trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/hooks.py | imported only by tests.test_router_grib |
| `scripts.sandbox.oceanmesh.merc_render` | 141 | scripts/sandbox/oceanmesh/merc_render.py | imported only by tests.test_proof_basemap_credit |
| `trid3nt_server.tools.fetchers.terrain.fetch_landcover.hooks` | 135 | trid3nt_server/tools/fetchers/terrain/fetch_landcover/hooks.py | imported only by tests.test_router_landcover |
| `trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry.hooks` | 129 | trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/hooks.py | imported only by tests.test_router_river |
| `trid3nt_server.testing.ws_client` | 112 | trid3nt_server/testing/ws_client.py | imported only by scripts.seed_showcase_cases, scripts.tool_routing_bench, scripts.ws_smoke, trid3nt_server.testing ... |
| `trid3nt_contracts.export_schemas` | 111 | contracts/trid3nt_contracts/export_schemas.py | imported only by contracts.tests.test_catalog, contracts.tests.test_export_schemas, contracts.tests.test_schema_drift |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_administrative_boundaries.hooks` | 98 | trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/hooks.py | imported only by tests.test_router_zip_multifile |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_roads_osm.hooks` | 95 | trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/hooks.py | imported only by tests.test_router_overpass |
| `trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water.hooks` | 92 | trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/hooks.py | imported only by tests.test_router_jrc |
| `trid3nt_server.tools.fetchers.imagery.fetch_slider_timestamps.hooks` | 92 | trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/hooks.py | imported only by tests.test_router_slider_timestamps |
| `trid3nt_server.testing.proof_paths` | 76 | trid3nt_server/testing/proof_paths.py | imported only by scripts.assemble_proof_packet, scripts.drive_artemis_structure_slot, scripts.drive_do_sag_cards, scripts.drive_river_dye_cards ... |
| `scripts.extract_telemac_catalog` | 73 | scripts/extract_telemac_catalog.py | imported only by tests.test_telemac_catalog_drift |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings.hooks` | 66 | trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/hooks.py | imported only by tests.test_router_buildings |
| `trid3nt_server.tools.fetchers._public_s3` | 48 | trid3nt_server/tools/fetchers/_public_s3.py | imported only by trid3nt_server.tools.fetchers._router.hooks.hrrr, trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks, trid3nt_server.tools.fetchers.weather.fetch_aorc_precip.hooks, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
| `trid3nt_server.testing` | 27 | trid3nt_server/testing/__init__.py | imported only by scripts.drive_artemis_structure_slot, scripts.drive_do_sag_cards, scripts.drive_keyword_floor, scripts.drive_module_surface_flip ... |
| `trid3nt_server.tools.fetchers.hazard` | 2 | trid3nt_server/tools/fetchers/hazard/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology` | 1 | trid3nt_server/tools/fetchers/hydrology/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery` | 1 | trid3nt_server/tools/fetchers/imagery/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean` | 1 | trid3nt_server/tools/fetchers/ocean/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.terrain` | 1 | trid3nt_server/tools/fetchers/terrain/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather` | 1 | trid3nt_server/tools/fetchers/weather/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_fault_sources` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/__init__.py | imported only by trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.hooks |
| `trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_usace_dams` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_flood_extent_observation` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_lter_records` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery.fetch_goes_satellite` | 0 | trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery.fetch_slider_timestamps` | 0 | trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery.fetch_viirs_day_fire` | 0 | trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_bluetopo` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_administrative_boundaries` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_population` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_population/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_roads_osm` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.terrain.fetch_dem` | 0 | trid3nt_server/tools/fetchers/terrain/fetch_dem/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.terrain.fetch_landcover` | 0 | trid3nt_server/tools/fetchers/terrain/fetch_landcover/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_aorc_precip` | 0 | trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_glm_lightning` | 0 | trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_mrms_qpe` | 0 | trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing` | 0 | trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_events_db` | 0 | trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_tracks` | 0 | trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/__init__.py | no importer in any scanned tree |

## Script-only-reachable

Reachable from `scripts/` but from no root and no test -- product
code that survives only because a proof driver imports it.

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.testing.canaries` | 482 | trid3nt_server/testing/canaries.py | imported only by scripts.proof_artemis_om2d_rematch |

## scripts/ entry modules with no importer

Standalone drivers are entry points by design; listed for staleness
review (a driver for a deleted seam is dead), not as a defect.

| module | loc | path |
|---|---|---|
| `scripts.stage_zell_sanford_groundwater` | 1151 | scripts/stage_zell_sanford_groundwater.py |
| `scripts.seed_showcase_cases` | 921 | scripts/seed_showcase_cases.py |
| `scripts.render_all_layers_proof` | 896 | scripts/render_all_layers_proof.py |
| `scripts.code_graph` | 798 | scripts/code_graph.py |
| `scripts.tool_routing_bench` | 741 | scripts/tool_routing_bench.py |
| `scripts.stage_groundwater_recharge` | 552 | scripts/stage_groundwater_recharge.py |
| `scripts.tool_sweep` | 388 | scripts/tool_sweep.py |
| `scripts.proof_artemis_om2d_rematch` | 383 | scripts/proof_artemis_om2d_rematch.py |
| `scripts.sandbox.oceanmesh.schism_gr3` | 335 | scripts/sandbox/oceanmesh/schism_gr3.py |
| `scripts.proof_rerun_with_overrides` | 327 | scripts/proof_rerun_with_overrides.py |
| `scripts.sandbox.pysheds_watershed.proof_watershed` | 323 | scripts/sandbox/pysheds_watershed/proof_watershed.py |
| `scripts.proof_artemis_real_breakwater_v2` | 308 | scripts/proof_artemis_real_breakwater_v2.py |
| `scripts.sandbox.oceanmesh.mesh_formats` | 287 | scripts/sandbox/oceanmesh/mesh_formats.py |
| `scripts.ws_smoke` | 283 | scripts/ws_smoke.py |
| `scripts.drive_river_dye_cards` | 271 | scripts/drive_river_dye_cards.py |
| `scripts.gen_tool_support_page` | 255 | scripts/gen_tool_support_page.py |
| `scripts.proof_river_dye_frames` | 245 | scripts/proof_river_dye_frames.py |
| `scripts.tool_usability_sweep` | 237 | scripts/tool_usability_sweep.py |
| `scripts.tool_routing_sweep` | 231 | scripts/tool_routing_sweep.py |
| `scripts.drive_keyword_floor` | 230 | scripts/drive_keyword_floor.py |
| `scripts.telemac_routing_probe` | 224 | scripts/telemac_routing_probe.py |
| `scripts.drive_open_water_domains` | 219 | scripts/drive_open_water_domains.py |
| `scripts.qml_preset_smoke` | 217 | scripts/qml_preset_smoke.py |
| `scripts.sandbox.telemac.render_erodible_scour_proof` | 217 | scripts/sandbox/telemac/render_erodible_scour_proof.py |
| `scripts.proof_declared_style_live` | 199 | scripts/proof_declared_style_live.py |
| `scripts.drive_module_surface_flip` | 197 | scripts/drive_module_surface_flip.py |
| `scripts.sandbox.replication.edi_coweeta_coverage` | 187 | scripts/sandbox/replication/edi_coweeta_coverage.py |
| `scripts.replay_canary_evidence` | 182 | scripts/replay_canary_evidence.py |
| `scripts.drive_mesh_spotcheck` | 171 | scripts/drive_mesh_spotcheck.py |
| `scripts.backfill_run_journal` | 166 | scripts/backfill_run_journal.py |
| `scripts.drive_lake_domain_mesh` | 163 | scripts/drive_lake_domain_mesh.py |
| `scripts.render_fidelity_proof_generic` | 163 | scripts/render_fidelity_proof_generic.py |
| `scripts.drive_do_sag_cards` | 161 | scripts/drive_do_sag_cards.py |
| `scripts.proof_auto_emit_seam` | 153 | scripts/proof_auto_emit_seam.py |
| `scripts.sandbox.replication.ballcreek_delineate_explore` | 152 | scripts/sandbox/replication/ballcreek_delineate_explore.py |
| `scripts.render_run_chart_proof` | 147 | scripts/render_run_chart_proof.py |
| `scripts.drive_artemis_structure_slot` | 136 | scripts/drive_artemis_structure_slot.py |
| `scripts.run_do_sag_direct` | 129 | scripts/run_do_sag_direct.py |
| `scripts.run_river_dye_direct` | 115 | scripts/run_river_dye_direct.py |
| `scripts.routing_failure_split` | 95 | scripts/routing_failure_split.py |
| `scripts.proof_wave_bed_input_live` | 94 | scripts/proof_wave_bed_input_live.py |
| `scripts.loc_report` | 83 | scripts/loc_report.py |
| `scripts._env_guard` | 55 | scripts/_env_guard.py |
| `scripts.proof_wave_bed_input_render` | 50 | scripts/proof_wave_bed_input_render.py |
| `scripts.proof_artemis_composer_live` | 42 | scripts/proof_artemis_composer_live.py |
| `scripts.sandbox.telemac.run_erodible_scour_direct` | 34 | scripts/sandbox/telemac/run_erodible_scour_direct.py |
