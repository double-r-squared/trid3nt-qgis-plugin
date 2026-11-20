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
