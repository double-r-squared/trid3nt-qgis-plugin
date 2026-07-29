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
