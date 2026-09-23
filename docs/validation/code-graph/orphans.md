# Orphans -- unreachable from every declared root

Roots: `trid3nt_server.main`, `trid3nt_server.__main__`, `trid3nt_server.tools`, `workers.telemac.entrypoint`, `plugin`

Bucket precedence is roots > tests > scripts, so a module reachable
from both a test and a script is reported as test-only.

Unreachable, not imported by any test, not imported by any script.
These are the corpses with nothing holding them up.

Excluded: 108 unreachable docstring-only `__init__.py` package
markers (directories the runtime walks for data, not modules to import).

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.tools.fetchers._router.hooks.cds` | 629 | trid3nt_server/tools/fetchers/_router/hooks/cds.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels.hooks` | 237 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_groundwater_levels/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements.hooks` | 230 | trid3nt_server/tools/fetchers/weather/fetch_openaq_measurements/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_tsunami_events.hooks` | 218 | trid3nt_server/tools/fetchers/hazard/fetch_tsunami_events/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nws_alerts_conus.hooks` | 217 | trid3nt_server/tools/fetchers/weather/fetch_nws_alerts_conus/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.soil.fetch_snotel_snow.hooks` | 195 | trid3nt_server/tools/fetchers/soil/fetch_snotel_snow/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nws_river_forecast.hooks` | 194 | trid3nt_server/tools/fetchers/hydrology/fetch_nws_river_forecast/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_usgs_volcano_alerts.hooks` | 190 | trid3nt_server/tools/fetchers/hazard/fetch_usgs_volcano_alerts/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.climate.fetch_climate_normals.hooks` | 180 | trid3nt_server/tools/fetchers/climate/fetch_climate_normals/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers._router.hooks.pfdf_raster` | 170 | trid3nt_server/tools/fetchers/_router/hooks/pfdf_raster.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_airnow_air_quality.hooks` | 150 | trid3nt_server/tools/fetchers/weather/fetch_airnow_air_quality/hooks.py | no importer in any scanned tree |
| `workers.telemac.engine_patch` | 83 | workers/telemac/engine_patch.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nws_event.hooks` | 74 | trid3nt_server/tools/fetchers/weather/fetch_nws_event/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_usace_nsi.hooks` | 70 | trid3nt_server/tools/fetchers/socioeconomic/fetch_usace_nsi/hooks.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers._router.hooks.coops_stations` | 38 | trid3nt_server/tools/fetchers/_router/hooks/coops_stations.py | no importer in any scanned tree |
| `trid3nt_server.workflows.telemac.helpers.water_quality` | 21 | trid3nt_server/workflows/telemac/helpers/water_quality.py | no importer in any scanned tree |

## Test-only-reachable -- the anchor class

Reachable from `tests/` but from no root. The test is the only
thing keeping the module alive; deleting both is one move.

| module | loc | path | evidence |
|---|---|---|---|
| `workers.mesh.scripts.om2d` | 971 | workers/mesh/scripts/om2d.py | imported only by tests.mesh.test_mesh_om2d |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_tracks.hooks` | 931 | trid3nt_server/tools/fetchers/weather/fetch_storm_tracks/hooks.py | imported only by tests.fetchers.test_router_storm_tracks |
| `scripts.model_check` | 720 | scripts/model_check.py | imported only by tests.model.test_model_conformance |
| `trid3nt_server.tools.fetchers.imagery._satellite_slider` | 640 | trid3nt_server/tools/fetchers/imagery/_satellite_slider.py | imported only by tests.fetchers.test_fetch_satellite_imagery, tests.fetchers.test_satellite_slider, trid3nt_server.tools.fetchers.imagery.fetch_satellite_imagery.hooks |
| `trid3nt_server.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow.hooks` | 618 | trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/hooks.py | imported only by tests.fetchers.test_router_nwm_streamflow |
| `trid3nt_server.tools.fetchers.imagery._goes_archive_core` | 461 | trid3nt_server/tools/fetchers/imagery/_goes_archive_core.py | imported only by tests.fetchers.test_fetch_goes_abi, tests.fetchers.test_router_glm, trid3nt_server.tools.fetchers.imagery.fetch_goes_abi.hooks, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
| `trid3nt_server.tools.fetchers.terrain.fetch_dem.hooks` | 446 | trid3nt_server/tools/fetchers/terrain/fetch_dem/hooks.py | imported only by tests.fetchers.test_router_dem, tests.inputs.test_extent |
| `trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks` | 433 | trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/hooks.py | imported only by tests.fetchers.test_router_glm |
| `trid3nt_server.tools.fetchers.hydrology.fetch_ehydro_surveys.hooks` | 423 | trid3nt_server/tools/fetchers/hydrology/fetch_ehydro_surveys/hooks.py | imported only by tests.fetchers.test_router_ehydro_surveys |
| `trid3nt_server.tools.fetchers.imagery.fetch_satellite_imagery.hooks` | 399 | trid3nt_server/tools/fetchers/imagery/fetch_satellite_imagery/hooks.py | imported only by tests.fetchers.test_fetch_satellite_imagery |
| `trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks` | 394 | trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/hooks.py | imported only by tests.fetchers.test_bathymetry_data_seam |
| `trid3nt_server.tools.fetchers.hydrology.fetch_watershed.hooks` | 356 | trid3nt_server/tools/fetchers/hydrology/fetch_watershed/hooks.py | imported only by tests.fetchers.test_router_watershed, tests.fetchers.test_watershed_delineation |
| `trid3nt_server.tools.fetchers.weather.fetch_raws_weather.hooks` | 348 | trid3nt_server/tools/fetchers/weather/fetch_raws_weather/hooks.py | imported only by tests.fetchers.test_raws_search_radius, tests.fetchers.test_raws_sensor_faults |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges.hooks` | 347 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/hooks.py | imported only by tests.fetchers.test_router_nwis |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_osm_features.hooks` | 340 | trid3nt_server/tools/fetchers/socioeconomic/fetch_osm_features/hooks.py | imported only by tests.fetchers.test_router_osm_features |
| `trid3nt_server.tools.fetchers.hydrology.fetch_lter_records.hooks` | 339 | trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/hooks.py | imported only by tests.fetchers.test_router_lter_records |
| `trid3nt_server.workflows.calibration.pairing` | 314 | trid3nt_server/workflows/calibration/pairing.py | imported only by tests.calibration.test_calibration_pairing, tests.runtime.test_observed_unit, trid3nt_server.workflows.calibration.metrics |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usbr_hydromet.hooks` | 313 | trid3nt_server/tools/fetchers/hydrology/fetch_usbr_hydromet/hooks.py | imported only by tests.fetchers.test_router_usbr_hydromet |
| `trid3nt_server.tools.fetchers._router.hooks.hrrr` | 311 | trid3nt_server/tools/fetchers/_router/hooks/hrrr.py | imported only by tests.fetchers.test_router_hrrr |
| `trid3nt_server.tools.fetchers.imagery.fetch_goes_abi.hooks` | 311 | trid3nt_server/tools/fetchers/imagery/fetch_goes_abi/hooks.py | imported only by tests.fetchers.test_fetch_goes_abi |
| `trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks.hooks` | 310 | trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/hooks.py | imported only by tests.fetchers.test_router_envelope |
| `trid3nt_server.tools.fetchers.ocean.fetch_ndbc_buoys.hooks` | 283 | trid3nt_server/tools/fetchers/ocean/fetch_ndbc_buoys/hooks.py | imported only by tests.fetchers.test_router_ndbc_buoys |
| `trid3nt_server.tools.fetchers.weather.fetch_asos_metar.hooks` | 283 | trid3nt_server/tools/fetchers/weather/fetch_asos_metar/hooks.py | imported only by tests.fetchers.test_asos_discovery_radius |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nwis_bed_material.hooks` | 266 | trid3nt_server/tools/fetchers/hydrology/fetch_nwis_bed_material/hooks.py | imported only by tests.fetchers.test_router_nwis_bed_material |
| `trid3nt_server.tools.fetchers.ocean.fetch_cudem.hooks` | 263 | trid3nt_server/tools/fetchers/ocean/fetch_cudem/hooks.py | imported only by tests.fetchers.test_bathymetry_data_seam |
| `trid3nt_server.tools.fetchers._tile_mosaic` | 249 | trid3nt_server/tools/fetchers/_tile_mosaic.py | imported only by tests.fetchers.test_bathymetry_data_seam, trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks, trid3nt_server.tools.fetchers.ocean.fetch_cudem.hooks, trid3nt_server.tools.fetchers.ocean.fetch_etopo.hooks ... |
| `trid3nt_server.tools.fetchers.imagery._goes_common` | 249 | trid3nt_server/tools/fetchers/imagery/_goes_common.py | imported only by trid3nt_server.tools.fetchers.imagery._goes_archive_core, trid3nt_server.tools.fetchers.imagery.fetch_goes_abi.hooks, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
| `trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.hooks` | 247 | trid3nt_server/tools/fetchers/weather/fetch_storm_events_db/hooks.py | imported only by tests.fetchers.test_router_chained |
| `trid3nt_server.tools.fetchers.hazard.fetch_fault_sources.hooks` | 244 | trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/hooks.py | imported only by tests.fetchers.test_router_fault_sources |
| `trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters.hooks` | 221 | trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/hooks.py | imported only by tests.fetchers.test_router_chained |
| `trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident.hooks` | 220 | trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/hooks.py | imported only by tests.fetchers.test_router_wfigs_incident |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_population.hooks` | 214 | trid3nt_server/tools/fetchers/socioeconomic/fetch_population/hooks.py | imported only by tests.fetchers.test_router_population |
| `trid3nt_server.tools.fetchers.ocean.fetch_vertical_datum_offset.hooks` | 210 | trid3nt_server/tools/fetchers/ocean/fetch_vertical_datum_offset/hooks.py | imported only by tests.fetchers.test_router_vertical_datum_offset, tests.inputs.test_vertical_datum |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries.hooks` | 203 | trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/hooks.py | imported only by tests.fetchers.test_router_field_boundaries, tests.inputs.test_extent |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nhd_waterbody_at_point.hooks` | 191 | trid3nt_server/tools/fetchers/hydrology/fetch_nhd_waterbody_at_point/hooks.py | imported only by tests.fetchers.test_router_nhd_waterbody_at_point |
| `trid3nt_server.tools.fetchers.ocean.fetch_regional_coastal_dem.hooks` | 186 | trid3nt_server/tools/fetchers/ocean/fetch_regional_coastal_dem/hooks.py | imported only by tests.fetchers.test_bathymetry_data_seam |
| `trid3nt_server.tools.fetchers.weather.fetch_aorc_precip.hooks` | 186 | trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/hooks.py | imported only by tests.fetchers.test_router_aorc_precip |
| `trid3nt_server.tools.fetchers.hydrology.fetch_flood_extent_observation.hooks` | 176 | trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/hooks.py | imported only by tests.fetchers.test_router_flood_extent_observation |
| `workers.telemac.scripts.dico` | 164 | workers/telemac/scripts/dico.py | imported only by tests.mesh.test_mesh_om2d |
| `trid3nt_server.tools.fetchers.us_states` | 161 | trid3nt_server/tools/fetchers/us_states.py | imported only by tests.fetchers.test_us_states, trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters.hooks, trid3nt_server.tools.fetchers.weather.fetch_nws_alerts_conus.hooks, trid3nt_server.tools.fetchers.weather.fetch_nws_event.hooks |
| `trid3nt_server.tools.fetchers._router.hooks.osm` | 159 | trid3nt_server/tools/fetchers/_router/hooks/osm.py | imported only by trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry.hooks, trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings.hooks, trid3nt_server.tools.fetchers.socioeconomic.fetch_osm_features.hooks |
| `trid3nt_server.workflows.calibration.metrics` | 156 | trid3nt_server/workflows/calibration/metrics.py | imported only by tests.calibration.test_calibration_pairing, tests.calibration.test_hydrograph_metrics |
| `trid3nt_server.tools.fetchers._router.hooks.hyriver` | 151 | trid3nt_server/tools/fetchers/_router/hooks/hyriver.py | imported only by tests.fetchers.test_router_hyriver, trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks.hooks, trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing.hooks |
| `trid3nt_server.tools.fetchers._router.transport.ogc_adapter` | 151 | trid3nt_server/tools/fetchers/_router/transport/ogc_adapter.py | imported only by tests.fetchers.test_ogc_adapter, tests.fetchers.test_router_chs_nonna, trid3nt_server.tools.fetchers.ocean.fetch_chs_nonna.hooks |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings.hooks` | 151 | trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/hooks.py | imported only by tests.fetchers.test_router_buildings |
| `trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing.hooks` | 151 | trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/hooks.py | imported only by tests.fetchers.test_router_nldas2 |
| `trid3nt_server.tools.fetchers.weather.fetch_mrms_qpe.hooks` | 149 | trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/hooks.py | imported only by tests.fetchers.test_router_grib |
| `trid3nt_server.tools.fetchers.ocean.fetch_etopo.hooks` | 147 | trid3nt_server/tools/fetchers/ocean/fetch_etopo/hooks.py | imported only by tests.fetchers.test_bathymetry_data_seam |
| `trid3nt_server.retention` | 143 | trid3nt_server/retention.py | imported only by tests.server.test_store_retention |
| `trid3nt_server.tools.payload_sampling` | 142 | trid3nt_server/tools/payload_sampling.py | imported only by tests.tools.test_resolution_doctrine_0224 |
| `trid3nt_server.tools.fetchers.ocean.fetch_usseabed.hooks` | 133 | trid3nt_server/tools/fetchers/ocean/fetch_usseabed/hooks.py | imported only by tests.fetchers.test_router_usseabed |
| `trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry.hooks` | 120 | trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/hooks.py | imported only by tests.fetchers.test_router_river |
| `trid3nt_server.tools.fetchers.terrain.fetch_landcover.hooks` | 120 | trid3nt_server/tools/fetchers/terrain/fetch_landcover/hooks.py | imported only by tests.fetchers.test_router_landcover |
| `trid3nt_server.tools.fetchers.ocean.fetch_chs_nonna.hooks` | 110 | trid3nt_server/tools/fetchers/ocean/fetch_chs_nonna/hooks.py | imported only by tests.fetchers.test_router_chs_nonna |
| `trid3nt_contracts.export_schemas` | 100 | contracts/trid3nt_contracts/export_schemas.py | imported only by contracts.tests.test_catalog, contracts.tests.test_export_schemas, contracts.tests.test_layer_contracts, contracts.tests.test_schema_drift |
| `trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water.hooks` | 83 | trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/hooks.py | imported only by tests.fetchers.test_router_jrc |
| `trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.hooks` | 61 | trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/hooks.py | imported only by tests.fetchers.test_router_firms |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nhd_water_surface.hooks` | 48 | trid3nt_server/tools/fetchers/hydrology/fetch_nhd_water_surface/hooks.py | imported only by tests.fetchers.test_router_nhd_water_surface |
| `trid3nt_server.render.outputs_seam` | 42 | trid3nt_server/render/outputs_seam.py | imported only by tests.render.test_outputs_seam, tests.solver.test_run_frames_off_record |
| `trid3nt_server.tools.fetchers._public_s3` | 37 | trid3nt_server/tools/fetchers/_public_s3.py | imported only by trid3nt_server.tools.fetchers._router.hooks.hrrr, trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks, trid3nt_server.tools.fetchers.weather.fetch_aorc_precip.hooks, trid3nt_server.tools.fetchers.weather.fetch_glm_lightning.hooks |
| `trid3nt_server.workflows.calibration` | 26 | trid3nt_server/workflows/calibration/__init__.py | imported only by tests.calibration.test_calibration_pairing, tests.calibration.test_hydrograph_metrics, trid3nt_server.workflows.calibration.metrics, trid3nt_server.workflows.calibration.pairing |
| `trid3nt_server.tools.fetchers.hazard` | 2 | trid3nt_server/tools/fetchers/hazard/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology` | 1 | trid3nt_server/tools/fetchers/hydrology/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery` | 1 | trid3nt_server/tools/fetchers/imagery/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean` | 1 | trid3nt_server/tools/fetchers/ocean/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.terrain` | 1 | trid3nt_server/tools/fetchers/terrain/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather` | 1 | trid3nt_server/tools/fetchers/weather/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_fault_sources` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_fault_sources/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_firms_active_fire/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_openfema_disasters/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident` | 0 | trid3nt_server/tools/fetchers/hazard/fetch_wfigs_incident/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_ehydro_surveys` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_ehydro_surveys/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_flood_extent_observation` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_flood_extent_observation/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_high_water_marks/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_jrc_global_surface_water/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_lter_records` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_lter_records/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nhd_water_surface` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_nhd_water_surface/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nhd_waterbody_at_point` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_nhd_waterbody_at_point/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_noaa_nwm_streamflow/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_nwis_bed_material` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_nwis_bed_material/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_river_geometry/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usbr_hydromet` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_usbr_hydromet/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.hydrology.fetch_watershed` | 0 | trid3nt_server/tools/fetchers/hydrology/fetch_watershed/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery.fetch_goes_abi` | 0 | trid3nt_server/tools/fetchers/imagery/fetch_goes_abi/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.imagery.fetch_satellite_imagery` | 0 | trid3nt_server/tools/fetchers/imagery/fetch_satellite_imagery/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_bluetopo` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_chs_nonna` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_chs_nonna/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_cudem` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_cudem/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_etopo` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_etopo/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_ndbc_buoys` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_ndbc_buoys/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_regional_coastal_dem` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_regional_coastal_dem/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_usseabed` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_usseabed/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.ocean.fetch_vertical_datum_offset` | 0 | trid3nt_server/tools/fetchers/ocean/fetch_vertical_datum_offset/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_buildings/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_field_boundaries/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_osm_features` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_osm_features/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.socioeconomic.fetch_population` | 0 | trid3nt_server/tools/fetchers/socioeconomic/fetch_population/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.terrain.fetch_dem` | 0 | trid3nt_server/tools/fetchers/terrain/fetch_dem/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.terrain.fetch_landcover` | 0 | trid3nt_server/tools/fetchers/terrain/fetch_landcover/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_aorc_precip` | 0 | trid3nt_server/tools/fetchers/weather/fetch_aorc_precip/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_asos_metar` | 0 | trid3nt_server/tools/fetchers/weather/fetch_asos_metar/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_glm_lightning` | 0 | trid3nt_server/tools/fetchers/weather/fetch_glm_lightning/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_mrms_qpe` | 0 | trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_nldas2_forcing` | 0 | trid3nt_server/tools/fetchers/weather/fetch_nldas2_forcing/__init__.py | no importer in any scanned tree |
| `trid3nt_server.tools.fetchers.weather.fetch_raws_weather` | 0 | trid3nt_server/tools/fetchers/weather/fetch_raws_weather/__init__.py | no importer in any scanned tree |
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
