# Orphans -- unreachable from every declared root

Roots: `trid3nt_server.main`, `trid3nt_server.__main__`, `trid3nt_server.tools`, `workers.telemac.entrypoint`, `plugin`

Bucket precedence is roots > tests > scripts, so a module reachable
from both a test and a script is reported as test-only.

Unreachable, not imported by any test, not imported by any script.
These are the corpses with nothing holding them up.

Excluded: 104 unreachable docstring-only `__init__.py` package
markers (directories the runtime walks for data, not modules to import).

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.tools.fetchers._router.hooks.cds` | 652 | trid3nt_server/tools/fetchers/_router/hooks/cds.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.hooks` | 361 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nws_river_forecast.hooks` | 334 | trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_raws_weather.hooks` | 288 | trid3nt_server/tools/fetchers/weather/fetch_raws_weather/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_asos_metar.hooks` | 280 | trid3nt_server/tools/fetchers/weather/fetch_asos_metar/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_usgs_earthquakes.hooks` | 247 | trid3nt_server/tools/fetchers/hazard/fetch_usgs_earthquakes/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels.hooks` | 237 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.hooks` | 236 | trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nws_alerts_conus.hooks` | 227 | trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_tsunami_events.hooks` | 218 | trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_usgs_volcano_alerts.hooks` | 202 | trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.soil.fetch_snotel_snow.hooks` | 201 | trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers._router.hooks.pfdf_raster` | 176 | trid3nt_server/tools/fetchers/_router/hooks/pfdf_raster.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_airnow_air_quality.hooks` | 165 | trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_usace_nsi.hooks` | 142 | trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nws_event.hooks` | 125 | trid3nt_server/tools/fetchers/weather/fetch_nws_event/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_osm_breakwaters.hooks` | 98 | trid3nt_server/tools/fetchers/ocean/fetch_osm_breakwaters/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_osm_coastline.hooks` | 59 | trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/hooks.py | no importer in any scanned tree |

## Test-only-reachable -- the anchor class

Reachable from `tests/` but from no root. The test is the only
thing keeping the module alive; deleting both is one move.

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.tools.fetchers._router.hooks.topobathy` | 1830 | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py | imported only by tests.fetchers.test_bathymetry_data_seam, tests.fetchers.test_fallback_ladder, tests.fetchers.test_fallback_sweep_guard, tests.fetchers.test_router_topobathy ... |
| `trid3nt_server.tools.fetchers.imagery._goes_archive_core` | 1167 | trid3nt_server/tools/fetchers/imagery/_goes_archive_core.py | imported only by tests.fetchers.test_router_glm, tests.fetchers.test_router_goes_archive, trid3nt_server.tools.fetchers._router.hooks.goes_archive, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_tracks.hooks` | 971 | trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/hooks.py | imported only by tests.fetchers.test_router_storm_tracks |
| `trid3nt_server.tools.fetchers.imagery._satellite_slider` | 736 | trid3nt_server/tools/fetchers/imagery/_satellite_slider.py | imported only by tests.fetchers.test_router_goes_animation, tests.fetchers.test_router_viirs_day_fire, tests.fetchers.test_satellite_slider, trid3nt_server.tools.fetchers._router.hooks.goes_animation ... |
| `scripts.model_check` | 732 | scripts/model_check.py | imported only by tests.model.test_model_conformance |
| `trid3nt_server.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow.hooks` | 652 | trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/hooks.py | imported only by tests.fetchers.test_router_nwm_streamflow |
| `trid3nt_server.tools.fetchers.terrain.fetch_dem.hooks` | 508 | trid3nt_server/tools/fetchers/terrain/fetch_dem/hooks.py | imported only by tests.fetchers.test_aoi_pin_lane_c, tests.fetchers.test_router_dem |
| `trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks` | 451 | trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/hooks.py | imported only by tests.fetchers.test_router_glm |
| `trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters.hooks` | 444 | trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/hooks.py | imported only by tests.fetchers.test_router_chained |
| `trid3nt_server.tools.fetchers.imagery.fetch_goes_satellite.hooks` | 441 | trid3nt_server/tools/fetchers/imagery/fetch_goes_satellite/hooks.py | imported only by tests.fetchers.test_router_goes_satellite |
| `trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks` | 425 | trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/hooks.py | imported only by tests.fetchers.test_bathymetry_data_seam |
| `trid3nt_server.tools.fetchers._router.hooks.goes_animation` | 350 | trid3nt_server/tools/fetchers/_router/hooks/goes_animation.py | imported only by tests.fetchers.test_router_goes_animation |
| `trid3nt_server.tools.fetchers.hydrology.fetch_lter_records.hooks` | 345 | trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/hooks.py | imported only by tests.fetchers.test_router_lter_records |
| `trid3nt_server.tools.fetchers._router.hooks.topobathy_class` | 327 | trid3nt_server/tools/fetchers/_router/hooks/topobathy_class.py | imported only by tests.fetchers.test_bathymetry_data_seam, trid3nt_server.tools.fetchers._router.hooks.topobathy |
| `trid3nt_server.tools.fetchers._router.hooks.hrrr` | 320 | trid3nt_server/tools/fetchers/_router/hooks/hrrr.py | imported only by tests.fetchers.test_router_hrrr |
| `trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks.hooks` | 319 | trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/hooks.py | imported only by tests.fetchers.test_router_envelope |
| `trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.hooks` | 276 | trid3nt_server/tools/fetchers/hazard/fetch_fema_nfhl_zones/hooks.py | imported only by tests.fetchers.test_router_arcgis_odd |
| `trid3nt_server.tools.fetchers.hazard.fetch_fault_sources.hooks` | 271 | trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/hooks.py | imported only by tests.fetchers.test_router_fault_sources |
| `trid3nt_server.tools.fetchers.imagery.fetch_viirs_day_fire.hooks` | 256 | trid3nt_server/tools/fetchers/imagery/fetch_viirs_day_fire/hooks.py | imported only by tests.fetchers.test_router_viirs_day_fire |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.hooks` | 256 | trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/hooks.py | imported only by tests.fetchers.test_router_chained |
| `trid3nt_server.tools.fetchers._router.hooks.goes_archive` | 254 | trid3nt_server/tools/fetchers/_router/hooks/goes_archive.py | imported only by tests.fetchers.test_router_goes_archive |
| `trid3nt_server.tools.fetchers.imagery._goes_common` | 254 | trid3nt_server/tools/fetchers/imagery/_goes_common.py | imported only by tests.fetchers.test_router_goes_animation, tests.fetchers.test_router_goes_archive, tests.fetchers.test_router_goes_satellite, trid3nt_server.tools.fetchers._router.hooks.goes_animation ... |
| `trid3nt_server.tools.fetchers.hazard.fetch_usace_dams.hooks` | 240 | trid3nt_server/tools/fetchers/hazard/fetch_usace_dams/hooks.py | imported only by tests.fetchers.test_router_arcgis_odd |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_population.hooks` | 223 | trid3nt_server/tools/fetchers/socioeconomic/fetch_population/hooks.py | imported only by tests.fetchers.test_router_population |
| `trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident.hooks` | 220 | trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/hooks.py | imported only by tests.fetchers.test_router_wfigs_incident |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries.hooks` | 209 | trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/hooks.py | imported only by tests.fetchers.test_aoi_pin_lane_c, tests.fetchers.test_router_field_boundaries |
| `trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities.hooks` | 200 | trid3nt_server/tools/fetchers/hazard/fetch_epa_frs_facilities/hooks.py | imported only by tests.fetchers.test_router_arcgis_odd |
| `trid3nt_server.tools.fetchers.weather.fetch_aorc_precip.hooks` | 186 | trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/hooks.py | imported only by tests.fetchers.test_router_aorc_precip |
| `trid3nt_server.tools.fetchers.hydrology.fetch_flood_extent_observation.hooks` | 183 | trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/hooks.py | imported only by tests.fetchers.test_router_flood_extent_observation |
| `trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.hooks` | 165 | trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/hooks.py | imported only by tests.fetchers.test_router_firms |
| `trid3nt_server.workflows.mesh.meshers.drivers.telemac_dico_driver` | 161 | trid3nt_server/workflows/mesh/meshers/drivers/telemac_dico_driver.py | imported only by tests.mesh.test_mesh_om2d |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_overpass_pois.hooks` | 160 | trid3nt_server/tools/fetchers/socioeconomic/fetch_overpass_pois/hooks.py | imported only by tests.fetchers.test_router_overpass |
| `trid3nt_server.tools.fetchers._router.hooks.osm` | 159 | trid3nt_server/tools/fetchers/_router/hooks/osm.py | imported only by tests.fetchers.test_router_overpass, trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry.hooks, trid3nt_server.tools.fetchers.ocean.fetch_osm_breakwaters.hooks, trid3nt_server.tools.fetchers.ocean.fetch_osm_coastline.hooks ... |
| `trid3nt_server.tools.fetchers._router.hooks.hyriver` | 151 | trid3nt_server/tools/fetchers/_router/hooks/hyriver.py | imported only by tests.fetchers.test_router_hyriver, trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.hooks, trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks.hooks, trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing.hooks |
| `trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing.hooks` | 151 | trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/hooks.py | imported only by tests.fetchers.test_router_nldas2 |
| `trid3nt_server.tools.fetchers.weather.fetch_mrms_qpe.hooks` | 149 | trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/hooks.py | imported only by tests.fetchers.test_router_grib |
| `trid3nt_server.tools.payload_sampling` | 142 | trid3nt_server/tools/payload_sampling.py | imported only by tests.tools.test_resolution_doctrine_0224, trid3nt_server.tools.fetchers._router.hooks.topobathy |
| `trid3nt_server.tools.fetchers.terrain.fetch_landcover.hooks` | 124 | trid3nt_server/tools/fetchers/terrain/fetch_landcover/hooks.py | imported only by tests.fetchers.test_router_landcover |
| `trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry.hooks` | 120 | trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/hooks.py | imported only by tests.fetchers.test_router_river |
| `trid3nt_contracts.export_schemas` | 97 | contracts/trid3nt_contracts/export_schemas.py | imported only by contracts.tests.test_catalog, contracts.tests.test_export_schemas, contracts.tests.test_schema_drift |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_administrative_boundaries.hooks` | 94 | trid3nt_server/tools/fetchers/socioeconomic/fetch_administrative_boundaries/hooks.py | imported only by tests.fetchers.test_router_zip_multifile |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_roads_osm.hooks` | 91 | trid3nt_server/tools/fetchers/socioeconomic/fetch_roads_osm/hooks.py | imported only by tests.fetchers.test_router_overpass |
| `trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water.hooks` | 83 | trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/hooks.py | imported only by tests.fetchers.test_router_jrc |
| `trid3nt_server.tools.fetchers.imagery.fetch_slider_timestamps.hooks` | 83 | trid3nt_server/tools/fetchers/imagery/fetch_slider_timestamps/hooks.py | imported only by tests.fetchers.test_router_slider_timestamps |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings.hooks` | 58 | trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/hooks.py | imported only by tests.fetchers.test_router_buildings |
| `trid3nt_server.tools.fetchers._public_s3` | 37 | trid3nt_server/tools/fetchers/_public_s3.py | imported only by trid3nt_server.tools.fetchers._router.hooks.hrrr, trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks, trid3nt_server.tools.fetchers.weather.fetch_aorc_precip.hooks, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
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

None.

## scripts/ entry modules with no importer

Standalone drivers are entry points by design; listed for staleness
review (a driver for a deleted seam is dead), not as a defect.

None.
