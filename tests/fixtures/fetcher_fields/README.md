# Recorded source answers, read by the field map and the vector writer

Recorded 2026-11-18, verbatim, from the public services:

- `usgs_field_measurements_des_moines.json` - the USGS Water Data OGC API `field-measurements`
  collection's own GeoJSON answer for wells around Des Moines, Iowa: 185 records, each with the
  collection's 21 published fields (`observing_procedure`, `qualifier` as a list, `value` as text).
- `nwis_iv_06610670.rdb` - the NWIS instantaneous-values RDB for USGS 06610670, Missouri River at
  Hwy 370 at Bellevue, Nebr., gage height (00065), retrieved 2026-11-18 19:09 -05:00.
- `wqp_result_header.csv` - the header row of a Water Quality Portal `resultPhysChem` CSV answer:
  the 81 field names the portal publishes, `/` included (`DataQuality/PrecisionValue`).
