# Repo metrics ledger

Progress tracking toward web-orphan removal + modular architecture (NATE
2026-07-27). Append a row per milestone; regenerate counts with the command
below. LOC = python lines incl. comments/blank (consistent measure - trend
matters, not the absolute).

    for d in server/src/trid3nt_server server/tests contracts/src \
      contracts/tests services/workers qgis-plugin/trid3nt scripts; do \
      find $d -name "*.py" -not -path "*__pycache__*" | xargs wc -l \
      | tail -1; done

| date | server pkg | server tests | contracts | workers | plugin | scripts | registry | server.py | notes |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-27 | 224,142 (563f) | 179,409 (423f) | 11,675+9,483 | 47,897 | 14,300 | 6,255 | 211 | 15,441 | post engine-door rollout, pre cull-pass-2 landing; server.py monolith flagged (cards extraction queued); Mexico Beach scripts + hygiene sweep pending |
| 2026-07-27b | 219,681 (552f) | 176,598 (414f) | - | - | - | - | 202 | 15,441 | post cull (9 tools, replication-proven) + structural batch (agent/ umbrella, search/, 35 dead files); -4,461 pkg lines, -11 files, -9 registered tools vs morning row; suite true baseline = 10 |
| 2026-07-28 | 219757 (558f) | - | - | - | - | - | 200 | 13,910 | post hygiene sweep: server.py -1,529 (cards extracted), comment archaeology -1,785 across 264 files (comment lines 25,317 -> 25301), meta renames, ADR offload notes; suite baseline 10 |
| 2026-07-28b | 218614 | - | - | - | - | - | 200 | 13,228 | recall pass: narrative blocks 216->16, war-stories 0, -1143 lines, comment-lines -> 24502; 9 genuine-architecture ADRs |
| 2026-07-28c | 217252 | - | 11381 | - | - | - | 200 | 13,072 | dynamic hot-set + mongo_collections cut: Mongo stratum fully closed; lessons/vertex-log/deadname-default/plugin-button batch landed

## Folder-level view (added 2026-07-28, regenerate per milestone)

    cd server/src/trid3nt_server && for d in */ agent/*/; do \
      find "$d" -name "*.py" -not -path "*__pycache__*" | xargs wc -l | tail -1; done

| date | agent/tools | agent/workflows | agent/other | AGENT total | root files | emission | sandbox | credentials | PLATFORM total |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-28 | 119,548 | 57,056 | 12,406 | 189,010 (87%) | 20,785 | 4,190 | 2,081 | 1,665 | 28,721 (13%) |
| 2026-07-28d | 216078 | - | - | 32878 | - | - | 200 | 13,116 | SFINCS remediation: flood.py 5637->2405 (conformance: run_sfincs + forcing_autowire), deckbuilder worker gone, quadtree/waves arm deleted, shared hoist; TRUE BASELINE NOW 9 (coastal fixed) | 
| 2026-07-29 | 214738 | - | - | - | - | - | 200 | 13,116 | fetcher-fold wave 1: 5 twins deleted (-5,735), spec-served surfaces live, experiment SUPPORTED byte-identical |
| 2026-07-29b | 211629 | - | - | - | - | - | 200 | 13,116 | fetcher-fold wave 2 (ArcGIS vector family): 6 twins deleted (net -5,306 incl. tests), router gains declarative where/column_map/fallback-chain/endpoint_select; 3 deferred by stop-rule (ejscreen, slr, levees) |
| 2026-07-29c | ~210060 | - | - | - | - | - | 200 | 13,116 | fetcher-fold wave 3 (USGS via dataretrieval): wqp+nldi folded (-1,570 py), gw-levels+nwis-gauges deferred by stop-rule |
| 2026-07-29d | ~206500 | - | - | - | - | - | 198 | 13,116 | cull phase A: news+conservation+goes wrappers out (live-gated), fetch_slider_timestamps in; satellite deferred on FIRMS key |
| 2026-07-29e | ~206850 | - | - | - | - | - | 197 | 13,116 | cull phase B: glm wrapper out (P2 dropped, moving-base proven live); shared/ model_* residuals now 1 (satellite, FIRMS-key gated) |
| 2026-07-29f | ~204600 | - | - | - | - | - | 191 | 13,116 | processing wave: generate_chart generic primitive in, 4 fixed-shape charts + clip_bbox + zonal + aggregate tools out (all live-gated); folder at redundancy floor |
| 2026-07-29g | ~202700 | - | - | - | - | - | 191 | 13,116 | fold wave 4 (stations): coops_currents folded (net -1,590), 5 deferred by stop-rule; twin-comparison experiment machinery retired |
| 2026-07-29h | ~204000 | - | - | - | - | - | 191 | 13,116 | ingest transport (ADR 0044): httpx opener owns remote-file sockets, direct_window off vsicurl (parity sha-identical), 404->EMPTY restored; STAC-tile seam residual |
| 2026-07-29i | 204730 | - | - | - | - | - | 190 | 13,116 | satellite preemptive cull (NATE waiver, ADR 0046): last shared/ model_* wrapper out; corpus re-homed w/ retrieval proof |
| 2026-07-29j | ~204790 | - | - | - | - | - | 190 | 13,116 | fold wave 5 (raster): STAC-tile seam onto transport (byte-identical live); 11 raster twins deferred by stop-rule, 6 mode-enablers scoped in ADR 0047 |
| 2026-07-29k | ~204510 | - | - | - | - | - | 190 | 13,116 | processing de-cloud (ADR 0048): _gdal_runner shared module, in-process COG encoder, grace2 fallback deleted, golden parity 4/4; flood canary PASSED (depth COG + overviews via in-process encoder) |

## CANONICAL METRIC (NATE 2026-07-30): CODED tools, rolling deltas

The registry total counts REGISTRATIONS incl. spec-synthesized data-tools
and is NOT the reduction metric. The metric is HAND-ROLLED PYTHON TOOLS
(coded = registered minus _router._promoted) and their LOC, reported as
rolling n-1 -> n deltas per landing, never arc-start comparisons.

| date | coded tools | coded fetchers | spec-served (data) | bespoke fetcher LOC | debug surface note |
|---|---|---|---|---|---|
| 2026-07-30 (current) | 176 | 85 | 14 | 62,665 (of 67,056 fetchers-pkg; router+transport engine = 4,391) | 14 sources debug through ONE 4,391-line engine instead of ~14k lines of bespoke files |
| 2026-07-30 | 176 | 85 | 14 | 62,665 | observability batch (ADR 0051): +902 LOC of NEW capability (rotation, retention, actionability, classifier); coded counts unchanged - infra landing, not a fold |
| 2026-07-30 | 173 | 82 | 17 | ~60,470 | fold wave 6 VECTOR family (ADR 0052): levees + slr_scenarios + ejscreen folded (-3 coded, -2,196 twin py), router gains declarative fan-out + float_list, endpoint_by_param/properties_by_param sub-layer routing, esri_json ingest + percentile/fraction/raw kinds + from_param + lowercase-enum; all 3 byte-identical live; ZIP family (ghsl/admin/storm/river) deferred by stop-rule (ghsl inner tif is DEFLATE not stored -> no windowed zip-member read; others multi-file shapefile/GDB or bespoke parsers) |
| 2026-07-30b | 173 | 82 | 17 | ~60,500 | fold wave 6: -3 coded (slr, levees, ejscreen; net -2,000 LOC); fan-out + per-enum routing + esri-json modes; zip family deferred w/ evidence (DEFLATE member, sidecars, bespoke parsers) |
| 2026-07-30c | 170 | 79 | 20 | ~58,786 (router+transport engine ~4,765) | fold wave 7 raster enablers (ADR 0053): landfire + usfs (imageserver_export exportImage mode + all-nodata gate) + modis_lst (stac_float continuous-float: latest-item + DN scale/offset + two-tier PC sign) folded (-3 coded, -1,684 twin py); router gains 2 raster ingestion modes + transport get_bytes + param-keyed style/units + payload ceil; all 3 byte-identical live (lf 32/32, us 32/32, modis 34/34); copernicus_dem deferred (5-consumer _copernicus_dem_impl coupling) + enablers 3-6 (redirect/gzip, VRT-fanout, griddap, colormap-DSL) not attempted -- fewer-fully doctrine |
| 2026-07-30c | 170 | 79 | 20 | ~58,900 | fold wave 7: -3 coded (landfire, usfs, modis via imageserver_export + stac_float); copernicus deferred (5-leg delegate re-point = scoped job); enablers 3-6 to wave 8 |
| 2026-07-30d | ~204600 | - | - | - | - | - | 190 | 13,116 | fold wave 8 (ADR 0054): copernicus_dem (stac_float + serialize-nodata; 5-leg re-point onto registry seam) + gcn250_curve_numbers (direct_window: transport skip-HEAD recovery + redirect-follow, enum-URL, integer-pixel window rounding, all-nodata gate, int16 serialize) folded (-2 coded, -1,053 twin py); both value-identical live (cop 13/13, gcn250 22/22 across 3 AMC); coded 170->168, fetchers 79->77, spec-served 20->22; chirps(gzip-object)/hrsl+soilgrids(VRT-fanout)/noaa_sst(griddap; ERDDAP unreachable in-env)/jrc(colormap-DSL) deferred by stop-rule; FLOOD CANARY PASSED |
| 2026-07-31 | 168 | 77 | 22 | ~57,900 | fold wave 8: -2 coded (copernicus via stac_float + 5-leg re-point, gcn250 via skip-HEAD/redirect); flood canary green; chirps/VRT/griddap/jrc stop-ruled |
| 2026-07-31b | 166 | 75 | 24 | ~57,170 (raster_cog +331 NEW capability) | fold wave 9 (ADR 0055): hrsl_population (multi_url VRT fan-out: whole-object .vrt parse -> intersecting-member windowed reads through the transport -> mosaic; all-nodata EMPTY, member-failure UPSTREAM) + chirps_precipitation (gzip_object: whole-object GET + gunzip + in-memory window; date-templated monthly/daily URL, sentinel-collapse, 404 NOT_AVAILABLE, bbox=None global) folded (-2 coded, -1,065 twin py); both value-identical live (hrsl 17/18 maxabsdiff=0.0, chirps 36/36 incl. global grid); coded 168->166, fetchers 77->75, spec-served 22->24; soilgrids stopped (projected-VRT Homolosine reproject + per-property scale-by-param does NOT stack on serialize/scale directives -> scoped job); griddap HELD (ERDDAP unreachable in-env), jrc DEAD; no flood coupling (grep-verified, canary not run) |
