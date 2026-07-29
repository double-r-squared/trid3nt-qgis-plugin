# 0037 - fetcher-fold replication-parity closure (5/5 pilots)

Context: the replication-parity panel (contract `docs/specs/router-pilot-contract.md`
sec 4.2) refuted the B1 router core (0036) with NAMED gaps: only 2/5 pilots
(gridmet, census_acs) reached PASS. The three router-executor / router-contract
gaps were genuine (not spec-side): an error-prefix conflation, a vector column
gap, a station timestamp/date-format gap, and an esri STAC stub. This note pins
the closure decisions. Supersedes the 0036 claim that errors are stamped
`<SOURCE_CLASS>_<SUFFIX>` -- that is true only when the twin's A.6 token equals its
cache `source_class`; three twins differ (see decision 1).

Decision (parity closure; no twin touched; fold arm off = strict no-op):

1. Error-prefix as data, not source_class. `source_class` doubles as the cache
   prefix and MUST equal the twin's, but three twins stamp A.6 from a DIFFERENT
   token (COOPS_TIDES vs noaa_coops_tides, ESRI_LANDCOVER vs esri_landcover_10m,
   HIFLD_INFRA vs hifld_critical_infrastructure). Added `SourceSpec.error_prefix`
   (optional; default `source_class.upper()`) + an `error_code_prefix` property;
   `_router/errors.py` stamps from it and every router raise site passes
   `spec.error_code_prefix` (the cache prefix stays `spec.source_class`). One
   field can now satisfy BOTH the cache side and the error-frame side.

2. Vector ingest transforms are declarative `ingest.*`, never source hardcodes.
   `vector_fgb` gained `geometry_filter` (geom-type allowlist + finite-coord drop),
   `json_coerce_nested` (dict/list props -> JSON strings), and `derived_columns`
   ({source: const|param|routing}), reproducing the hifld twin's
   facility_type/facility_label + Point/finite filter without a source-specific
   branch. Derived column names ride into the honest-empty header-only schema too.

3. Station timestamp + date format. `station_timeseries` honors
   `ingest.per_station.time_normalize: iso8601z` (twin-exact `" "->"T"` + `Z`) and
   coerces the router-validated ISO date to a `date` object so a `{start:%Y%m%d}`
   template strftimes to the CO-OPS datagetter's required YYYYMMDD (a raw str
   raised on `%Y` -- a live call would have failed). Proven with a live CO-OPS
   request (real Fort Myers data, ISO+Z timestamps).

4. ESRI STAC is no longer a stub -- it reuses `_pc_stac` verbatim. The
   `raster_cog` `stac_search` sub-mode (`stac_to_mosaic`) does SAS-sign
   (`sas_sign_href`) + `bbox_pixel_dims` + reproject-to-EPSG:4326 nearest
   categorical read + first-non-nodata multi-item mosaic + uint8 + baked palette
   (`array_to_cog_bytes(colormap=...)`, ColorInterp.palette). The tiled-mosaic
   transform inherits parity: single-tile fast path calls the executor; the
   multi-tile path uses `stac_to_mosaic` + uint8 tiles + palette-preserving merge.
   Proven with a live PC-STAC request (uint8 EPSG:4326 palette COG, real classes).

5. Harness grading tightened per contract sec 4.2: the property schema (column
   set) and the timestamp value spot-check are VALUES-gate fields (not advisory
   INFO), so a column-set / timestamp mismatch now FAILS the source. Re-graded
   5/5 PASS.

Consequence: all 5 pilots reach replication parity (5/5), the A.6 error frame is
byte-identical for every pilot, and the fold gate now correctly BLOCKS a source
until BOTH routing and replication parity hold. The router remains
indistinguishable from a hand-written twin on the graded surface; the two
remaining live-mode notes carried forward (hifld facility_type -> per-service URL
routing; census JOIN speaks api.census.gov not the twin's data.census.gov backend)
are unrelated to the pilots' graded requests and tracked for their own lanes.
