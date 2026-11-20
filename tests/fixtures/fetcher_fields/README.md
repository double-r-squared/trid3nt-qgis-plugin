# Recorded source answers, read by the field map and the vector writer

Recorded 2026-11-18, verbatim, from the public services:

- `usgs_field_measurements_des_moines.json` - the USGS Water Data OGC API `field-measurements`
  collection's own GeoJSON answer for wells around Des Moines, Iowa: 185 records, each with the
  collection's 21 published fields (`observing_procedure`, `qualifier` as a list, `value` as text).
- `nwis_iv_06610670.rdb` - the NWIS instantaneous-values RDB for USGS 06610670, Missouri River at
  Hwy 370 at Bellevue, Nebr., gage height (00065), retrieved 2026-11-18 19:09 -05:00.
- `wqp_result_header.csv` - the header row of a Water Quality Portal `resultPhysChem` CSV answer:
  the 81 field names the portal publishes, `/` included (`DataQuality/PrecisionValue`).
- `usgs_monitoring_locations_des_moines.json` - the same API's `monitoring-locations` collection (site_type_code GW)
  answer for the box around those wells, recorded 2026-11-19, trimmed to the 98 wells the 185 records above
  name: each with the collection's published fields (`well_constructed_depth`, `altitude_accuracy`, ...).
- `nwis_site_expanded.rdb` - the NWIS Site-service RDB (siteOutput=expanded) for USGS 06610000 (Missouri River at
  Omaha) and 06610670, retrieved 2026-11-18 19:01 -05:00: 42 site fields each, `alt_acy_va` included.
- `asos_kpdx_kvuo_20241001.csv` - the IEM ASOS service's `data=all` comma CSV for KPDX and KVUO, 2024-10-01 00:00-03:00 UTC
  (33 columns, `metar` text included), recorded 2026-11-20.
- `raws_network_anec1.json` and `raws_obhistory_anec1_20241001.json` - the IEM network GeoJSON feature for the RAWS station
  ANEC1 (Sweetwater, CA_DCP) and its `obhistory.json` day 2024-10-01 (25 rows: the SHEF elements `XRIRGZZ`, `UDIRGZZ`,
  `URHRGZZ`, `VBIRGZZ`, ... beside `tmpf`, `dwpf`, `sknt`, `drct`), recorded 2026-11-20.
- `snotel_stations_or_20240301.json` and `snotel_data_20240301_20240304.json` - three Oregon SNTL records of the NRCS AWDB
  `/stations` catalog and the AWDB `/data` answer for two of them (WTEQ and SNWD, DAILY, 2024-03-01 to 03-04), recorded 2026-11-20.
- `normals_annual_USW00024229.csv` and `normals_inventory_excerpt.txt` - the NCEI 1991-2020 annual access CSV for Portland
  Intl AP (one row, about 700 columns) and its line of `inventory_30yr.txt`, recorded 2026-11-20.
- `storm_events_oregon_2023.csv.gz` - 27 Oregon rows (25 with a begin point, 2 without) of NCEI's
  `StormEvents_details-ftp_v1.0_d2023_c20261119.csv.gz`, all 51 columns, recorded 2026-11-20.
- OpenAQ and AirNow are keyed and no key is stored on this host, so no answer of either is recorded: their tests state the
  documented v3 and `aq/data` shapes by hand and say so.
- `gem_faults_excerpt.geojson` - four features of the GEM Global Active Faults harmonized GeoJSON
  (three UCERF3 faults of the Bay Area, one Australian fault with no `average_rake`), traces cut to six vertices,
  recorded 2026-11-20.
- `ncei_tsunami_events_2011.json` and `ncei_tsunami_runups_2011.json` - NCEI hazel `tsunamis/events` (the Tohoku year,
  two items) and `tsunamis/runups` (two items) answers, recorded 2026-11-20.
- `hans_monitored_volcanoes.json` and `hans_us_volcanoes.json` - the HANS `getMonitoredVolcanoes` and `getUSVolcanoes`
  lists cut to three vnums (311320, 312090, 311160), recorded 2026-11-20.
- `ibtracs_idalia_2023_excerpt.csv` - the IBTrACS v04r01 last3years CSV: header, units row and nine fixes of IDALIA
  (SID 2023239N21274) either side of its landfall record, recorded 2026-11-20.
- `wfigs_year_to_date_features.json` - two features of WFIGS_Incident_Locations_YearToDate (the ten fields the fetch
  asks for, `f=json`), recorded 2026-11-20.
- `ehydro_index_feature.json` - one feature of the eHydro_Survey_Data index (the fields the fetch asks for), footprint
  cut to 12 vertices, recorded 2026-11-20.
- `nhd_waterbody_hr.json` and `nhd_waterbody_medium.json` - the same waterbody (NHD permanent identifier
  {1D017476-8ADD-42BA-832D-B4FB83B615D2}) from the NHDPlus HR MapServer layer 9 and the NHD MapServer layer 12,
  `outFields=*`, `maxAllowableOffset=0.0005`, recorded 2026-11-20; the mirrors spell the names in different case.
- `nldi_flowlines_comid13294314.json` - the NLDI `comid/13294314/navigation/DM/flowlines` answer (3 km), recorded 2026-11-20.
- `osm_buildings.parquet`, `osm_roads.parquet`, `osm_waterway.parquet`, `osm_coast.parquet` - the frames `osmnx.features_from_bbox`
  returned for building (Council Bluffs box), highway (primary, secondary, residential), waterway (river, stream, canal)
  and natural=coastline (Casco Bay box), index reset, recorded 2026-11-20; read with `geopandas.read_parquet` and
  `set_index(["element", "id"])`. The Overpass man_made and amenity requests timed out, so those two classes have no frame.
- `fiboa_us_cropland_omaha.parquet` - three rows of the fiboa US USDA cropland GeoParquet (the file's own Albers CRS and
  columns), read with a bbox over the Omaha area, recorded 2026-11-20.
- `usseabed_chesapeake_ext_excerpt.zip` - the usSEABED packager's `us9_ext.csv` for a box off the Virginia shore, header
  and 12 rows, the `geom` and every sentinel as the service writes them, recorded 2026-11-20.
- `wqp_bed_material_sites.csv` and `wqp_bed_material_results.csv` - the WQP Station row and the 18 `resultPhysChem`
  rows of one bed-sediment sample (Stockton area, NWIS provider, 83 columns) as `dataretrieval.wqp` returned them,
  recorded 2026-11-20.
- `comcat_event_us6000qw60.json` - the full ComCat event GeoJSON for us6000qw60 (Kamchatka 2025, every product),
  recorded 2026-11-20.
- `openfema_declarations_ri.json` - six rows of the OpenFEMA DisasterDeclarationsSummaries answer for Rhode Island,
  recorded 2026-11-20.
- EDI's PASTA and portal answer no public request from this host, so no LTER entity is recorded: its test states the
  documented delimited shape by hand and says so. NHC's CurrentStorms.json held no active storm on the recording day,
  so the active-storm record is stated by hand in its test.
- `usgs_earthquakes_nc_excerpt.json` - four events of the USGS FDSN GeoJSON answer for a box off Cape Mendocino
  (2024-12-05, magnitude 3 and up), every property the service sends, recorded 2026-11-20.
- `nws_alerts_fl_excerpt.json` - two polygon alerts and one zone-only alert of the api.weather.gov active-alerts answer
  for Florida, every property, recorded 2026-11-20.
- `nsi_structures_omaha_excerpt.json` - four structures of the National Structure Inventory answer for a block in
  Omaha, every field, recorded 2026-11-20.
- `nhd_area_hr_all_fields.json`, `nhd_flowline_hr_all_fields.json`, `nhd_waterbody_hr_all_fields.json` - NHDPlus HR
  NHDArea, NetworkNHDFlowline and NHDWaterbody features asked with `outFields=*` (geometry simplified by
  `maxAllowableOffset`), recorded 2026-11-20.
- `nwi_wetlands_omaha_esrijson.json` - two features of the USFWS wetlands map service as ESRI JSON with `outFields=*`,
  recorded 2026-11-20 (the service names its fields ATTRIBUTE, WETLAND_TYPE, QAQC_CODE, ACRES, GlobalID).
- `tigerweb_county_all_fields.json`, `tigerweb_state_all_fields.json` - one TIGERweb county and one state feature with
  `outFields=*` (geometry simplified), recorded 2026-11-20.
- FIRMS answers nothing without a MAP_KEY, so no FIRMS answer is recorded: its test states the documented VIIRS header by
  hand and says so.
