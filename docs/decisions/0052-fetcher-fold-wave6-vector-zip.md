# 0052 - fetcher fold wave-6 VECTOR/ZIP family: 3 vector folds + ZIP-family defer

Context: Phase-2 wave-6 opened the VECTOR/ZIP family. Two scopes: (1) fold the
vector twins wave-2 deferred for want of declarative machinery (fetch_epa_ejscreen
esri-json, fetch_noaa_slr_scenarios fan-out, fetch_usace_levees sub-layer routing);
(2) the ZIP-member family (fetch_administrative_boundaries, fetch_storm_tracks,
fetch_river_geometry, fetch_ghsl_population) via a NEW transport zip-member mode
(CoalescedRangeFile + python zipfile central-directory range reads, no /vsizip/).
All seven twins were read IN FULL (the standing rule: the audit label is a prior,
not a verdict). Every fold closes byte-identical vs the live twin (offline harness
gate) before its twin is cut, per the cull doctrine.

Decision (2026-07-30):

1. THREE VECTOR TWINS FOLDED (scope 1, LANDED), each byte-identical against the
   live twin over the real endpoints (drivers_wave6.py; read_through stubbed):
   - **fetch_noaa_slr_scenarios** (PASS 27/27): the NEW declarative **fan-out**
     transform (`ingest.fan_out`) + a `float_list` param type. One vector query
     PER `scenario_ft` level against a per-level MapServer (`url_template` +
     `value_map` -> service name), each feature stamped `slr_ft` + `scenario_label`
     + `dissolve`, dissolved polygons merged in sorted-level order. Live: n / geom
     / crs / column-set / slr_ft-set identical for single + default-3 scenarios;
     honest-empty header (inland bbox) identical; forced upstream + bad-bbox +
     bad-scenario error frames identical.
   - **fetch_usace_levees** (PASS 26/26): `endpoint_by_param` (the `layer` enum ->
     NLD FeatureServer sub-layer 16/14/10) + `properties_by_param` (per-layer kept
     column set synthesized into a passthrough column_map) + the existing
     `json_coerce_nested` (STATES/COUNTIES list fields -> JSON strings). Live:
     leveed_areas + system_routes n / geom / crs / column-set / SYSTEM_ID-set
     identical; ocean-bbox honest-empty header identical; error frames identical.
   - **fetch_epa_ejscreen** (PASS 30/30): the NEW **esri_json** ingest mode
     (`f=json` + JSON-envelope geometry + in-process esri-ring decode; the audit's
     "GDAL-as-parser" is done in-process to keep the ring convention byte-identical
     to the twin) + three declarative column kinds (`percentile` / `fraction` /
     `raw`, the twin's `_normalize_*` sentinel semantics) + `from_param` value-field
     routing (the `indicator` param selects the source percentile field) + a
     `kind: param` echo column + a case-insensitive (`lowercase`) enum. Live: pm25 +
     diesel n / geom / crs / column-order / bg_id-set / value-column / indicator-echo
     identical; ocean-bbox honest-empty 21-column header identical; error frames
     identical.

   All new machinery is STRICTLY NO-OP for prior specs: each is gated on an
   `ingest.*` key, a new param `type`, or a new column `kind` that no prior spec
   sets. The 695 contract tests + the 14 previously-promoted specs' drivers +
   the offline suite are the no-op proof (every prior spec byte-for-byte
   unchanged). The three twins' docstrings are carried VERBATIM
   (`inspect.getdoc`), so the promoted tools' `FunctionDeclaration` description +
   the BM25/dense retrieval index do not shift: all three corpus phrasing sets
   rank their tool in the model-free top-8 (7/7, 7/7, 8/8). No new dependencies.

2. THE ZIP-MEMBER FAMILY DEFERRED WITH EVIDENCE (scope 2). All four twins were
   read in full; NONE closes byte-identical via a zip-member range-read transport,
   and the transport's premise (central-directory range reads to enable WINDOWED
   member access) does not hold for any of them (STOP RULE: defer with evidence,
   never force a non-byte-identical substitution):
   - **fetch_ghsl_population**: the only single-raster-member candidate. LIVE probe
     of a real GHSL tile zip (R5_C19, 174 MB) parsed the central directory over a
     range GET (the mechanism works) and found the inner `.tif` member is
     **DEFLATE-compressed** (method=8, csize 172,963,838 / usize 178,592,532), NOT
     stored. A windowed COG read inside a DEFLATE member is impossible without
     decompressing the ENTIRE 178 MB member, so the zip-member transport gives NO
     byte-range advantage over the twin's `/vsizip//vsicurl/`; folding it needs a
     `fixed_tile_grid` mode PLUS full-member-decompress-then-window, not a no-op
     extension. (ADR-0047 scoped `fixed_tile_grid` + the zip-member mode as a
     future enabler; this wave's live evidence shows the zip-member half does not
     unlock a windowed raster fold.)
   - **fetch_administrative_boundaries**: a MULTI-FILE shapefile (.shp + .dbf +
     .shx + .prj) in a WHOLE-downloaded TIGER zip (ZCTA ~504 MB), read via
     `zipfile.extractall` + `geopandas.read_file`, then client-side clip. A
     single-member range read yields no byte-identical path -- pyogrio needs every
     sidecar member (i.e. the whole object), and the `place` level adds a bespoke
     state-FIPS-envelope routing table + per-state download-and-merge. Not a no-op
     mode.
   - **fetch_storm_tracks** (BESPOKE): IBTrACS points-CSV parsing (custom column
     map, Saffir label, season/name filter, per-basin file selection) + NHC
     current-storms JSON + a forecast-track *zipped shapefile* (whole download via
     `zipfile.ZipFile(BytesIO)`) + storm-grouping LineString assembly. The
     irreducible parsers, not the zip access, are the body.
   - **fetch_river_geometry** (BESPOKE; nested in `flood`): Overpass QL waterway
     query (primary) + NHDPlus HR HUC4 FileGDB fallback (a heuristic bbox->HUC4
     table, whole `.GDB.zip` download + OpenFileGDB read + clip). A GDB is a
     multi-file directory; a single-member range read cannot serve it. A
     do-not-regress flood leg -- extra reason not to force.

   No zip-member transport module was built: it has no byte-identical consumer
   this wave (all four candidates fail the range-read premise), and building it
   speculatively would violate the no-speculative-infra norm -- mirroring wave-5's
   honest raster fold-none-defer-many precedent (ADR-0047).

Registry accounting: registry total stays 186 (three twins died, three
spec-driven surfaces took their names under `_router._promoted`). CODED tools
176 -> 173 (-3); coded fetchers 85 -> 82 (-3); spec-served data sources 14 -> 17
(+3). Retrieval index unshifted. Offline suite FAILED set unchanged at the
baseline 9 (test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5 --
both PRE-EXISTING, unrelated to this wave). Daemon import clean.

Consequence: the router's vector surface now carries three general declarative
capabilities -- fan-out (multi-query-per-value + merge), per-enum sub-layer
routing + per-enum projection, and esri-json ingest with sentinel-normalizing
column kinds -- each proven byte-identical on live data and each unlocking a
family beyond its first source (fan-out for any multi-scenario source; esri_json
for any `f=json`-only ArcGIS layer). The ZIP family's fold is empirically shown
NOT to reduce to a zip-member range-read transport; it needs, in rough order,
full-member decompression + `fixed_tile_grid` (ghsl), multi-file-member assembly
(admin shapefile, river GDB), and the irreducible bespoke parsers (storm_tracks).
Each is its own scoped, NATE-methodology-signed job. Supersedes nothing; extends
the wave-2 ArcGIS-family declarative surface (ADR-0039) and the wave-4/5
fold-some-defer-rest precedent (ADR-0045/0047).
