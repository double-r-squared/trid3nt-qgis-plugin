# scripts/ evaluation - what faces the public repo, what stays local, what dies

READ-ONLY evaluation of `/home/nate/Documents/trid3nt-local` at HEAD `d41822c0`
(2026-09-09). Nothing was edited, committed or deployed. LOC is PURE code
(`scripts/loc_report.py`'s `classify`: total minus blank minus comment minus
docstring lines); `.sh` files use the same subtraction without the docstring term.
Consumers were measured by grep over `Makefile`, `README.md`, `AGENTS.md`,
`tests/`, `plugin/`, `trid3nt_server/`, `contracts/`, `workers/`, `docs/` and the
other scripts. There is no `.github/` in this repo, so no CI consumer exists.

"Runs?" was measured with `venvs/agent/bin/python <script> --help` under
`MPLBACKEND=Agg`. Scripts that carry no argparse ran their own module body on
that call; the two that reached a solver were REFUSED by the input-review gate
before any solve, and three sandbox drivers failed at the first fetch because no
MinIO bucket answered. No live drive was performed and no run was produced.

## 0. Headline (MEASURED)

| fate | files | pure LOC | share |
|---|---|---|---|
| TRACKED PRODUCT | 39 | 8,312 | 74.0% |
| ATTIC | 9 | 1,242 | 11.1% |
| LOCAL-ONLY | 6 | 1,076 | 9.6% |
| DELETE | 6 | 605 | 5.4% |
| **total measured** | **60** | **11,235** | |

`scripts/` today: 60 tracked code files, 11,235 pure LOC, one flat directory plus
a `sandbox/` that holds two modules the PRODUCT imports.

`scripts/` after: **39 tracked files, 7,902 pure LOC in five directories**, once
`mesh_formats.py` (197) and `schism_gr3.py` (213) leave `scripts/` for the product
tree - they are 410 of the 8,312 TRACKED LOC and are counted out of the "after"
number because they stop being scripts. The tracked tree reads as:

| directory | files | pure LOC |
|---|---|---|
| `scripts/` (entry points named in README + docs/site + Makefile) | 8 | 284 |
| `scripts/instruments/` | 10 | 2,354 |
| `scripts/drivers/` | 12 | 2,220 |
| `scripts/packet/` | 5 | 1,910 |
| `scripts/staging/` | 2 | 1,134 |
| **tracked total** | **37** | **7,902** |
| (leaves scripts/ -> `trid3nt_server/workflows/mesh/shared/formats/`) | 2 | 410 |
| `scripts/local/` (gitignored, not tracked) | 6 | 1,076 |

The public repo stops carrying 2,923 pure LOC (ATTIC + LOCAL-ONLY + DELETE), and
605 of that is deletable today with runtime evidence.

## 1. Every file under scripts/

LOC = pure. "last" = last commit date for that path. Consumers marked NONE were
searched across Makefile, README, AGENTS.md, tests/, plugin/, trid3nt_server/,
contracts/, workers/, docs/ (excluding the generated `docs/validation/code-graph/`)
and the other scripts.

### 1a. Runtime and install entry points

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/start_agent.sh` | 54 | starts `trid3nt_server.main` from `.env.local`, writes `run/agent.pid` | `Makefile:84`; `docs/site/install.md:182`; `docs/site/troubleshooting.md:72`; `docs/site/configuration.md:3`; `trid3nt_server/main.py:179` | 2026-09-01 | not run (starts the daemon) | TRACKED PRODUCT - the `make agent` target and the documented start line |
| `scripts/start_minio.sh` | 36 | starts the local MinIO, writes `run/minio.pid` | `Makefile:72`; `docs/site/configuration.md:94`; `plugin/plugin_settings.py:18` | 2026-07-04 | not run (starts a service) | TRACKED PRODUCT - `make minio` |
| `scripts/init_minio.sh` | 21 | creates the `trid3nt-runs` + `trid3nt-cache` buckets | `Makefile:73`; `docs/site/install.md:148`; `docs/site/configuration.md:30,31` | 2026-09-04 | not run (mutates the store) | TRACKED PRODUCT - `make minio` second half |
| `scripts/fetch_binaries.sh` | 61 | idempotent downloader for mf6, minio, mc | `Makefile:68`; `docs/site/install.md:92`; `docs/site/configuration.md:50` | 2026-07-27 | not run (downloads) | TRACKED PRODUCT - `make binaries`, `make setup` |
| `scripts/install_plugin.sh` | 32 | rsyncs the plugin into the live QGIS profile | `Makefile:65`; `plugin/README.md:159,165`; `docs/site/install.md:209`; `trid3nt_server/plugin_repo.py:37,317,620` | 2026-08-16 | not run (writes the profile) | TRACKED PRODUCT - `make plugin`; the install-sync law's only fix |
| `scripts/package_plugin.sh` | 21 | builds the versioned zip + `plugins.xml` under `run/plugin-repo/` | `Makefile:80`; `trid3nt_server/plugin_repo.py:13,563,569` | 2026-08-16 | not run (writes the repo dir) | TRACKED PRODUCT - `make plugin-repo`, a prerequisite of `make agent` |
| `scripts/build_telemac_image.sh` | 8 | builds `trid3nt-local/telemac:latest` from the worker dir as context | `docs/site/install.md:127`; `workers/README.md:17`; `README.md:119` | 2026-08-16 | not run (docker build) | TRACKED PRODUCT - the worker-image-staleness law's rebuild line |
| `scripts/use_openrouter.sh` | 51 | rewrites `.env.local` to point the agent at OpenRouter or back at ollama, then restarts | `README.md:78` | 2026-07-22 | not run (edits `.env.local`) | TRACKED PRODUCT - named in the README (see Q4: it writes a secret-bearing file) |

### 1b. Instruments

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/code_graph.py` | 582 | import graph + orphan/dead-symbol report into `docs/validation/code-graph/` | `README.md:109`; `docs/DELETION_LEDGER.md:2999,3002,3307` | 2026-09-04 | yes (rc=0) | TRACKED PRODUCT - a named instrument in the README |
| `scripts/model_check.py` | 525 | checks a SysML v2 model against the tree it describes | `tests/test_model_conformance.py:21` (the suite runs it), `:138`; `docs/model/README.md:49,93` | 2026-09-02 | yes (`usage: model_check`) | TRACKED PRODUCT - the MBSE checker the offline suite executes |
| `scripts/loc_report.py` | 66 | pure-code LOC by tree and server subtree - THE standing LOC measure | `docs/IDEAS.md:4290`; this evaluation imports its `classify` | 2026-09-09 | crashes on `--help` (`loc_report.py:77` casts `sys.argv[1]` to int) | TRACKED PRODUCT - the coded-tools/LOC metric law depends on this method (see Q5) |
| `scripts/gen_tool_support_page.py` | 202 | regenerates `docs/site/tool-support.md` from the registry; `--check` mode | `docs/site/tool-support.md:13`; `docs/validation/scope-census.md:75,76,209`; `docs/DELETION_LEDGER.md:220` | 2026-09-08 | yes (`usage: ... [--check]`) | TRACKED PRODUCT - the generator of a shipped docs/site page |
| `scripts/tool_sweep.py` | 306 | sequential direct-execution sweep of every registered tool | `docs/validation/scope-census.md:73,75,76,111,209`; imports `_env_guard` at `:177` | 2026-09-08 | yes | TRACKED PRODUCT - the census instrument, maintained per tool change |
| `scripts/extract_telemac_catalog.py` | 39 | extracts each exposed TELEMAC module keyword catalog out of the image | `tests/test_telemac_catalog_drift.py:19,27,67` (imported by path); `trid3nt_server/workflows/telemac/catalog/README.md:4` | 2026-09-04 | yes (rc=0) | TRACKED PRODUCT - a test imports it |
| `scripts/harvest_living_atlas.py` | 159 | harvests the ESRI Living Atlas into two curation catalogs | `tests/test_living_atlas.py:315` (imported by path); `trid3nt_server/tools/search/living_atlas_common.py:13`; `docs/metrics.md:53` | 2026-08-04 | yes | TRACKED PRODUCT - a test imports it; it produces shipped YAML |
| `scripts/qml_preset_smoke.py` | 168 | loads every preset the family writes into installed QGIS and reads it back | `tests/test_presets.py:3` (declared the other half of that test); `trid3nt_server/emission/presets.py:25`; `docs/decisions/0326-the-preset-family.md:56` | 2026-09-03 | needs PyQGIS (`ModuleNotFoundError: qgis` at `:193`) - expected outside the QGIS python | TRACKED PRODUCT - the load-validation half of a suite test |
| `scripts/ws_smoke.py` | 193 | WS smoke test of the running daemon (`all_passed`) | `AGENTS.md:58` (the standing session gate) | 2026-08-24 | yes (rc=0 with no server contact on `--help`) | TRACKED PRODUCT - named in the workflow law |
| `scripts/replay_canary_evidence.py` | 114 | replays every committed canary and diffs metrics against the recorded ones | `docs/decisions/0319-rerun-with-overrides-and-coupled-validity.md:167`; `docs/validation/skeleton-loc-ledger.md:486`; `docs/IDEAS.md:2036` | 2026-08-26 | yes (`usage: ... [--only ...]`) | TRACKED PRODUCT - the drift instrument over committed evidence (see Q6) |

### 1c. The proof-packet renderers (the packet law's chain)

`trid3nt_server/testing/canaries.py:415` loads `scripts/assemble_proof_packet.py`
BY PATH at every canary close, and that script loads three sibling renderers by
path (`assemble_proof_packet.py:274-275`, called at `:305`, `:468`, `:479`,
`:531`). This chain is product code that happens to live in `scripts/`.

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/assemble_proof_packet.py` | 694 | the delivery checklist as a script: renders panels/canvas/charts/GIF, writes `packet.json`, refuses on a hole | `trid3nt_server/testing/canaries.py:415`; `tests/test_animation_legend_stability.py:188`; `docs/proof/templates/README.md:146`; `docs/IDEAS.md:1570` | 2026-09-05 | yes | TRACKED PRODUCT - the packet law's executable |
| `scripts/render_all_layers_proof.py` | 558 | contact sheet: every layer a run put on the canvas, in emission order | `scripts/assemble_proof_packet.py:305,468` | 2026-09-09 | yes | TRACKED PRODUCT - the packet's panel renderer |
| `scripts/render_run_chart_proof.py` | 78 | renders a run's persisted chart spec through the dock's renderer | `scripts/assemble_proof_packet.py:479` | 2026-08-26 | yes | TRACKED PRODUCT - the packet's chart renderer |
| `scripts/render_selafin_animation.py` | 517 | generic TELEMAC-family animation: frames -> GIF + the peak still | `scripts/assemble_proof_packet.py:531`; `tests/test_animation_legend_stability.py:32`; `docs/proof/templates/README.md:150` | 2026-09-06 | yes | TRACKED PRODUCT - the packet's animation renderer, pinned by a suite test |
| `scripts/sandbox/oceanmesh/merc_render.py` | 63 | shared ESRI tile fetch + Web-Mercator primitives for every proof render | `scripts/render_all_layers_proof.py:52`; `scripts/render_selafin_animation.py:58`; `scripts/proof_river_dye_frames.py:42`; `scripts/sandbox/pysheds_watershed/proof_watershed.py:170`; `scripts/sandbox/telemac/render_erodible_scour_proof.py:29`; `tests/test_proof_basemap_credit.py:27` | 2026-09-03 | yes (imports clean) | TRACKED PRODUCT - a suite test and two packet renderers import it; it must leave `sandbox/` |

### 1d. The drive lane

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/_env_guard.py` | 23 | refuses to build an S3 client without an explicit local endpoint | `scripts/drive_mesh_spotcheck.py:51`; `scripts/tool_sweep.py:177`; `scripts/stage_groundwater_recharge.py:440`; `scripts/stage_zell_sanford_groundwater.py:971`; `docs/decisions/0297-...:169` | 2026-08-21 | yes (import clean) | TRACKED PRODUCT - the no-ambient-AWS law, enforced in code |
| `scripts/drive_artemis_structure_slot.py` | 76 | the ARTEMIS `structure` slot proved in all three ways it can be filled | `trid3nt_server/testing/canaries.py:98,121` (the canary comment defers to it); `docs/DELETION_LEDGER.md:2837` | 2026-09-01 | yes | TRACKED PRODUCT - the canary names it as the home of the unfilled-slot question |
| `scripts/drive_do_sag_cards.py` | 91 | a user-gated `telemac_do_sag` answered through the CARDS, `--smoke` | `trid3nt_server/testing/canaries.py:259`; `docs/decisions/0307-swmm-campaign-wave-a.md:46`; `docs/decisions/0305-...:336` | 2026-09-06 | yes | TRACKED PRODUCT - the small variant of a declared canary |
| `scripts/drive_river_dye_cards.py` | 177 | a user-gated `telemac_river_dye` answered through the CARDS, `--coarse` | `trid3nt_server/testing/canaries.py:259`; `docs/decisions/0312-...:173`; `docs/decisions/0305-...:337,432` | 2026-09-06 | yes | TRACKED PRODUCT - same |
| `scripts/drive_keyword_floor.py` | 161 | drives the keyword surface the LLM and the human actually use | NONE | 2026-09-06 | yes | TRACKED PRODUCT - current-wave acceptance driver on `trid3nt_server.testing.run_live` (see Q2) |
| `scripts/drive_lake_domain_mesh.py` | 106 | a LAKE domain meshed from the water body's own polygon | `docs/validation/mesh-recipe-conformance.md:45`; `scripts/proof_artemis_om2d_rematch.py:24` | 2026-09-06 | yes | TRACKED PRODUCT - the conformance table cites its run |
| `scripts/drive_mesh_spotcheck.py` | 98 | the standing mesh spot-check lane | `docs/DELETION_LEDGER.md:1313,1387` | 2026-09-01 | yes | TRACKED PRODUCT - a standing lane by its own declaration |
| `scripts/drive_module_surface_flip.py` | 134 | every question the module surface flipped, end to end | NONE | 2026-09-05 | yes | TRACKED PRODUCT - current-wave acceptance driver (see Q2) |
| `scripts/drive_open_water_domains.py` | 147 | the two open-water questions on the domains they declare | NONE | 2026-09-06 | yes | TRACKED PRODUCT - current-wave acceptance driver (see Q2) |
| `scripts/proof_artemis_om2d_rematch.py` | 228 | THE FLAGSHIP: an authored OceanMesh2D domain fed into ARTEMIS | `docs/validation/skeleton-loc-ledger.md:882` | 2026-09-01 | yes | TRACKED PRODUCT - the ONE flagship canary law names this rematch |
| `scripts/proof_artemis_real_breakwater_v2.py` | 229 | mesh-faithful proof render of the real Cinder Pond ARTEMIS pair | `docs/decisions/0237-artemis.md:359`; `docs/DELETION_LEDGER.md:3108` (its `_v1` was chopped for this) | 2026-09-06 | yes (rc=0, no argparse) | TRACKED PRODUCT - the render the artemis packet carries (see Q7) |
| `scripts/seed_showcase_cases.py` | 750 | seeds inspectable showcase Cases through the product WS path | `docs/DELETION_LEDGER.md:323,329,1386,2784` | 2026-09-01 | yes | TRACKED PRODUCT - the showcase keep-list law's seeder (see Q3: 750 LOC with a ledger-QUEUED duplicate WS client) |

### 1e. Dataset staging

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/stage_groundwater_recharge.py` | 388 | stages the published CONUS recharge grids as COGs in object storage | `tests/test_router_groundwater_recharge.py:5`; `trid3nt_server/tools/fetchers/hydrology/fetch_groundwater_recharge/source.yaml:2`; `docs/decisions/0297-...:22,165` | 2026-08-24 | yes | TRACKED PRODUCT - the provenance of a shipped fetcher's source (see Q1) |
| `scripts/stage_zell_sanford_groundwater.py` | 746 | stages the Zell and Sanford CONUS surficial-groundwater grids as COGs | `tests/test_router_zell_sanford_groundwater.py:5`; `fetch_water_table_depth/source.yaml:3`; `fetch_aquifer_thickness/source.yaml:4`; `fetch_aquifer_transmissivity/source.yaml:3` | 2026-08-24 | yes | TRACKED PRODUCT - same, for three fetchers (see Q1) |

### 1f. Product modules stranded under sandbox/

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/sandbox/oceanmesh/mesh_formats.py` | 197 | coastal-TIN to solver-mesh writers and the repo's ONE topology pass | `trid3nt_server/workflows/mesh/shared/nodes.py:39,48` (the PRODUCT `sys.path`-inserts `scripts/sandbox/oceanmesh` and imports it); reached from `trid3nt_server/workflows/mesh/meshers/om2d.py:613,707` and `shared/primitives.py:139`; `tests/test_mesh_om2d.py:665,675,690` | 2026-09-03 | yes | TRACKED PRODUCT - must MOVE into the product tree; a shipped mesher imports it out of a directory named sandbox |
| `scripts/sandbox/oceanmesh/schism_gr3.py` | 213 | the coastal-TIN to SCHISM `hgrid.gr3` bridge and its boundary helpers | `scripts/sandbox/oceanmesh/mesh_formats.py:28` (which the product imports) | 2026-08-30 | yes | TRACKED PRODUCT - same; `docs/DELETION_LEDGER.md:1840` records its move here from `workers/schism/`, which no longer exists |

### 1g. LOCAL-ONLY

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/tool_routing_bench.py` | 574 | 15-prompt tool-routing breadth benchmark against the local LLM over WS | `scripts/tool_usability_sweep.py:41`; `scripts/tool_routing_sweep.py:39`; `docs/site/models.md:119`; `plugin/net/trid3nt_client.py:16` (protocol reference) | 2026-08-24 | `--help` has no argparse: it began a live bench and was killed at 90s (rc=124) | LOCAL-ONLY - a NATE-machine local-model harness; needs a running ollama + daemon (see Q8) |
| `scripts/telemac_routing_probe.py` | 163 | fast first-tool routing probe for the TELEMAC family | `docs/DELETION_LEDGER.md:2890,3149` | 2026-09-01 | yes | LOCAL-ONLY - same class: local-model routing measurement |
| `scripts/routing_failure_split.py` | 62 | splits pass-3 failures into RETRIEVAL vs MODEL | `docs/site/models.md:121` | 2026-08-24 | yes (rc=0) | LOCAL-ONLY - reads a local sweep log (see Q8) |
| `scripts/backfill_run_journal.py` | 118 | one-shot idempotent seed of the run journal from surviving run prefixes | `docs/decisions/0314-the-static-plan-and-the-style-contract.md:101` | 2026-09-03 | yes (`--dry-run`) | LOCAL-ONLY - a migration against THIS machine's object store; the journal it seeds is local state |
| `scripts/run_do_sag_direct.py` | 84 | direct `telemac_do_sag` call - the DO-sag reference run | `docs/decisions/0307-swmm-campaign-wave-a.md:29,462`; `docs/site/engines.md:16` (as `scripts/run_*_direct.py`) | 2026-09-01 | yes | LOCAL-ONLY - the cards driver is the live lane; the direct call is a bench convenience (see Q9) |
| `scripts/run_river_dye_direct.py` | 75 | direct `telemac_river_dye` call - the dye-plume reference run | `docs/decisions/0305-...:338,403` | 2026-08-30 | yes | LOCAL-ONLY - same (see Q9) |

### 1h. ATTIC

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/proof_auto_emit_seam.py` | 93 | one-shot live proof that a processing raster reaches the map with no publish tool | `docs/decisions/0313-emission-is-automatic-publish-layer-dies.md:86`; `docs/validation/emission-fold-store-conformance.md:101` | 2026-09-04 | yes | ATTIC - its ADR landed; the seam is now asserted by the suite, nobody calls this |
| `scripts/proof_declared_style_live.py` | 127 | one-shot live proof that a declared style row reaches the canvas as a `.qml` | `docs/validation/emission-fold-presets-conformance.md:33,115` | 2026-09-03 | needs PyQGIS (`ModuleNotFoundError: qgis` at `:125`) | ATTIC - superseded as a standing check by `qml_preset_smoke.py`, which the suite pins |
| `scripts/proof_rerun_with_overrides.py` | 231 | one-shot live proof of the rerun-with-overrides primitive on the do_sag canary | `docs/decisions/0319-...:167`; `docs/DELETION_LEDGER.md:2635`; `docs/validation/skeleton-loc-ledger.md:485` | 2026-09-03 | yes | ATTIC - the primitive landed and `replay_canary_evidence.py` is the standing instrument |
| `scripts/proof_river_dye_frames.py` | 165 | animated plume render for a `telemac_river_dye` run | `docs/decisions/0305-...:339,466`; `docs/DELETION_LEDGER.md:356` | 2026-09-06 | yes | ATTIC - superseded by `render_selafin_animation.py`, the generic TELEMAC-family animator the packet calls; one template's private copy of it |
| `scripts/sandbox/pysheds_watershed/proof_watershed.py` | 219 | standalone pysheds watershed-coverage proof | NONE | 2026-08-24 | fetches on import; failed at the first S3 call (`NoSuchBucket`) | ATTIC - a spike whose finding is recorded; no consumer |
| `scripts/sandbox/replication/ballcreek_delineate_explore.py` | 106 | Ball Creek fork identification through the DEM flow network | NONE | 2026-08-24 | fetches on import; failed at the first S3 call (`NoSuchBucket`) | ATTIC - replication exploration, superseded by the rain-on-grid front |
| `scripts/sandbox/replication/edi_coweeta_coverage.py` | 118 | EDI/Coweeta streamflow coverage probe | NONE | 2026-08-16 | yes (rc=0) | ATTIC - a one-shot coverage question, answered |
| `scripts/sandbox/telemac/render_erodible_scour_proof.py` | 168 | GAIA erodible-bed scour proof render | `docs/validation/lean-sweep-inventory.md:91` flags its hardcoded scratch path at `:32` | 2026-08-16 | no - `FileNotFoundError` on the hardcoded `/tmp/.../metrics_new.json` | ATTIC - the GAIA proof landed; the script cannot re-run without a machine-specific temp file |
| `scripts/sandbox/telemac/run_erodible_scour_direct.py` | 15 | direct-call driver for the GAIA v2 erodible-bed path | NONE | 2026-09-04 | begins a live call; degraded at the first fetch (no bucket) | ATTIC - the render half is atticked and nothing else calls it |

### 1i. DELETE

| path | LOC | what it does | consumers | last | runs? | fate |
|---|---|---|---|---|---|---|
| `scripts/tool_routing_sweep.py` | 168 | per-tool routing sweep, resumable | `scripts/tool_usability_sweep.py:48`; `docs/site/models.md:120` | 2026-08-24 | NO - `AttributeError: module 'tool_routing_bench' has no attribute 'new_id'` at `:175` | DELETE - it calls four bench symbols that no longer exist; `tool_routing_bench.py:66` moved `handshake`/`create_case`/`delete_case` to `trid3nt_server.testing.ws_client` and `new_id` to `new_ulid` |
| `scripts/tool_usability_sweep.py` | 183 | "usable coverage": is every tool reachable in one turn | `docs/DELETION_LEDGER.md:3148` | 2026-08-22 | NO - same dead symbols at `:154` (`bench.new_id`), `:160` (`bench.do_handshake`), `:172` (`bench.create_case`), `:211` (`bench.delete_case`), and it imports the dead sweep at `:48` | DELETE - dead against the current bench in five places |
| `scripts/proof_artemis_composer_live.py` | 26 | live smoke of the wired ARTEMIS composer by direct call | `docs/validation/telemac-family-migration-inventory.md:24` (a table that also names four scripts that no longer exist) | 2026-08-24 | NO - refused: `PHYSICS_INPUT_REQUIRED ... open_depth_threshold_m` (auto mode, law 9) | DELETE - superseded by the `artemis_harbor_agitation` LiveRun canary at `trid3nt_server/testing/canaries.py:122`, and its auto-mode call is refused by the input-review gate |
| `scripts/proof_wave_bed_input_live.py` | 63 | live smoke that the in-worker lake-datum bed surfaces as a Case INPUT layer | NONE | 2026-09-03 | NO - same `PHYSICS_INPUT_REQUIRED` refusal, then `SMOKE FAILED` | DELETE - refused by the current gate, zero consumers |
| `scripts/proof_wave_bed_input_render.py` | 29 | QGIS-true render of that same INPUT layer | NONE | 2026-08-31 | NO - `AssertionError` in `render_fidelity_proof_generic.download_s3` | DELETE - the render half of a dead pair |
| `scripts/render_fidelity_proof_generic.py` | 136 | generic ESRI-basemap raster proof renderer | `scripts/proof_wave_bed_input_render.py:19`; `scripts/proof_wave_bed_input_live.py:25` - both DELETE | 2026-08-27 | yes (rc=0) | DELETE - its only two importers die with it; the packet renderers use `merc_render` instead |

## 2. Proposed structure for the TRACKED PRODUCT set

Five directories by role. Every path reference that must move with each file is
named; there are no other references (measured by grep for the basename over
Makefile, README, AGENTS.md, tests/, plugin/, trid3nt_server/, contracts/,
workers/, docs/ and scripts/).

### `scripts/` - user-facing entry points (8 files, 284 LOC)

`start_agent.sh`, `start_minio.sh`, `init_minio.sh`, `fetch_binaries.sh`,
`install_plugin.sh`, `package_plugin.sh`, `build_telemac_image.sh`,
`use_openrouter.sh`.

These stay exactly where they are. They are the only paths a clone-and-run user
types, and every reference to them is already correct: `Makefile:65,68,72,73,80,84`,
`README.md:78,108,119`, `docs/site/install.md:92,127,148,176,182,209`,
`docs/site/configuration.md:3,27,30,31,50,94,115`, `docs/site/troubleshooting.md:72`,
`docs/site/engines.md:56`, `plugin/README.md:159,165`, `workers/README.md:17`.
Moving anything else OUT of the top level is what makes this set legible.

### `scripts/instruments/` (10 files, 2,354 LOC)

`code_graph.py`, `model_check.py`, `loc_report.py`, `gen_tool_support_page.py`,
`tool_sweep.py`, `extract_telemac_catalog.py`, `harvest_living_atlas.py`,
`qml_preset_smoke.py`, `ws_smoke.py`, `replay_canary_evidence.py`.

Must move with them:
- `tests/test_model_conformance.py:21` (`CHECKER = REPO_ROOT / "scripts" / "model_check.py"`) and its message at `:138`
- `tests/test_telemac_catalog_drift.py:19` and its message at `:67`
- `tests/test_living_atlas.py:315`
- `tests/test_presets.py:3` and `trid3nt_server/emission/presets.py:25` (prose)
- `README.md:109` (the `scripts/code_graph.py` line)
- `AGENTS.md:58` (the `scripts/ws_smoke.py` gate line)
- `docs/site/tool-support.md:13` (the generator credit)
- `trid3nt_server/workflows/telemac/catalog/README.md:4`,
  `trid3nt_server/tools/search/living_atlas_common.py:13` (prose)
- `scripts/tool_sweep.py:177` imports `_env_guard`, which moves to `drivers/`:
  either keep `_env_guard.py` at the `scripts/` root or add the sibling path insert.

### `scripts/packet/` (5 files, 1,910 LOC)

`assemble_proof_packet.py`, `render_all_layers_proof.py`,
`render_run_chart_proof.py`, `render_selafin_animation.py`, and `merc_render.py`
lifted out of `sandbox/oceanmesh/`.

This is the packet law's chain, and it is load-bearing product behavior: a canary
that cannot assemble a packet exits non-zero.

Must move with them:
- `trid3nt_server/testing/canaries.py:415` - the hardcoded
  `.../scripts/assemble_proof_packet.py` path
- `scripts/assemble_proof_packet.py:275` - `REPO / "scripts" / f"{name}.py"`
  becomes `REPO / "scripts" / "packet" / ...`
- `scripts/render_all_layers_proof.py:50`, `render_selafin_animation.py:56` -
  the `sys.path.insert(... "scripts"/"sandbox"/"oceanmesh")` lines become the
  packet dir (or plain sibling imports once `merc_render` sits beside them)
- `tests/test_animation_legend_stability.py:32,188`
- `tests/test_proof_basemap_credit.py:27`
- `docs/proof/templates/README.md:146,150`; `docs/IDEAS.md:1570`
- two ATTIC-bound scripts also insert that oceanmesh path
  (`proof_river_dye_frames.py:40`, `sandbox/telemac/render_erodible_scour_proof.py:28`)
  - if they go to the attic the references leave with them

### `scripts/drivers/` (12 files, 2,220 LOC)

`_env_guard.py`, the seven `drive_*.py`, `proof_artemis_om2d_rematch.py`,
`proof_artemis_real_breakwater_v2.py`, `seed_showcase_cases.py`.

This is the live-drive lane: each one imports
`trid3nt_server.testing.{GateAnswers, LiveRun, run_live}` or the tool registry
directly and writes into the proof tree through
`trid3nt_server/testing/proof_paths.py`, the one path builder.

Must move with them:
- `trid3nt_server/testing/canaries.py:98,121` (the artemis structure-slot comment)
  and `:259` (the do_sag / river_dye small-variant comment)
- `docs/validation/mesh-recipe-conformance.md:45`; `docs/decisions/0237-artemis.md:359`;
  `docs/decisions/0305-...:336,337`; `docs/decisions/0307-swmm-campaign-wave-a.md:46`
- `scripts/tool_sweep.py:177`, `stage_*.py:440,971`, `drive_mesh_spotcheck.py:51`
  all `from _env_guard import ...` - whichever directory it lands in, those four
  sibling imports must agree

### `scripts/staging/` (2 files, 1,134 LOC)

`stage_groundwater_recharge.py`, `stage_zell_sanford_groundwater.py`.

Must move with them: `tests/test_router_groundwater_recharge.py:5`,
`tests/test_router_zell_sanford_groundwater.py:5`, and the four fetcher
`source.yaml` provenance comments
(`fetch_groundwater_recharge/source.yaml:2`, `fetch_water_table_depth/source.yaml:3`,
`fetch_aquifer_thickness/source.yaml:4`, `fetch_aquifer_transmissivity/source.yaml:3`).
The `.gitignore` line `scratchpad/staging/` already covers their work dirs.

### Out of scripts/ entirely: the two mesh-format modules

`mesh_formats.py` (197) and `schism_gr3.py` (213) are imported by the PRODUCT:
`trid3nt_server/workflows/mesh/shared/nodes.py:39` names the directory as a
STRING (`_TIN_FORMATS = "scripts/sandbox/oceanmesh"`), inserts it on `sys.path`
at `:45-47` and imports `mesh_formats` at `:48`; `om2d.py:613,707` and
`shared/primitives.py:139` reach it from there, and `tests/test_mesh_om2d.py:665,675,690`
exercise it. A shipped mesher reaching into a directory named `sandbox` through a
path string is the seam to close: move both into
`trid3nt_server/workflows/mesh/shared/formats/` and delete the `sys.path` hack.

Must move with them: `nodes.py:36-50` (the constant, the insert and the import),
`docs/DELETION_LEDGER.md:73,1801,1840,1847`,
`docs/validation/mesh-wave-conformance.md:97,526`,
`docs/validation/skeleton-loc-ledger.md:638`, `docs/design/mesh-wave-kickoff.md:12`.
This is a code change, not a scripts reshuffle - see Q10.

## 3. LOCAL-ONLY set and its .gitignore rule

Proposed rule, appended to `.gitignore` beside the existing sandbox carve-outs:

```
# Machine-shaped harnesses: local-model benches, one-shot store migrations and
# direct-call bench conveniences. Git will not protect these - they live only on
# the machine that runs them.
scripts/local/
```

Contents (6 files, 1,076 pure LOC): `tool_routing_bench.py` (574),
`telemac_routing_probe.py` (163), `routing_failure_split.py` (62),
`backfill_run_journal.py` (118), `run_do_sag_direct.py` (84),
`run_river_dye_direct.py` (75).

Standing laws that still need them:
- The model-tier and local-model laws: `tool_routing_bench.py` +
  `routing_failure_split.py` are how a model swap gets numbers rather than vibes;
  `docs/site/models.md:119,121` currently instructs a user to run them, which is
  the conflict in Q8 - a LOCAL-ONLY script cannot be named in a shipped docs page.
- The offline-suite baseline and the drive-lane pre-flight need NOTHING here: the
  pre-flight lives in `trid3nt_server/testing/live_run.py` (the drivers read
  `ev.preflight_note` at `drive_open_water_domains.py:156`,
  `drive_module_surface_flip.py:153`, `drive_keyword_floor.py:133`), and those
  drivers are TRACKED. No law points at `scripts/local/` for a gate.
- The packet law's renderer is NOT here - it is `scripts/packet/`, TRACKED,
  because `canaries.py:415` executes it.
- The run-journal law (`docs/decisions/0314-...:101`) needs
  `backfill_run_journal.py` only for a store that already exists on this machine;
  a fresh clone has no runs to backfill.
- `.env.local` handling: `use_openrouter.sh` REWRITES the secret-bearing
  `.env.local` (which `.gitignore` already excludes). It is TRACKED today because
  `README.md:78` names it - see Q4.

## 4. DELETE and ATTIC, with evidence

### DELETE (6 files, 605 pure LOC) - runtime evidence, each verified this session

1. `scripts/tool_routing_sweep.py` (168) - `AttributeError: module
   'tool_routing_bench' has no attribute 'new_id'` at `:175`. Also calls
   `bench.do_handshake`, `bench.create_case`, `bench.delete_case`; none exist -
   `tool_routing_bench.py:66` now imports `create_case, delete_case, handshake, mk`
   from `trid3nt_server.testing.ws_client` and uses `new_ulid` at `:669`.
   Removing it costs `docs/site/models.md:120` one line.
2. `scripts/tool_usability_sweep.py` (183) - the same four dead symbols at
   `:154,:160,:172,:211`, plus it loads the dead sweep at `:48`.
3. `scripts/proof_artemis_composer_live.py` (26) - superseded by the
   `artemis_harbor_agitation` LiveRun at `trid3nt_server/testing/canaries.py:122`,
   and its direct auto-mode call is now REFUSED: `PHYSICS_INPUT_REQUIRED ...
   open_depth_threshold_m ... re-run in user_gated mode`. A smoke that the product
   refuses to run is not a smoke.
4. `scripts/proof_wave_bed_input_live.py` (63) - identical refusal, then prints
   `SMOKE FAILED`. Zero consumers anywhere.
5. `scripts/proof_wave_bed_input_render.py` (29) - `AssertionError` at
   `render_fidelity_proof_generic.py:70`; zero consumers.
6. `scripts/render_fidelity_proof_generic.py` (136) - importers are exactly items
   4 and 5 (`proof_wave_bed_input_live.py:25`, `proof_wave_bed_input_render.py:19`).

### ATTIC (9 files, 1,242 pure LOC)

Capabilities nobody calls, whose finding is already recorded in an ADR or a
validation page. Evidence per file is in section 1h; the pattern is: a one-shot
live proof whose ADR landed (`proof_auto_emit_seam`, `proof_rerun_with_overrides`,
`proof_declared_style_live`), a template-private copy of a capability the generic
renderer now owns (`proof_river_dye_frames` vs `render_selafin_animation`), or a
sandbox spike with zero importers (`proof_watershed`, `ballcreek_delineate_explore`,
`edi_coweeta_coverage`, the two `sandbox/telemac/` erodible-scour halves).

### Stale references found while measuring (docs, not scripts)

These name scripts that do not exist at HEAD; they should be corrected in the same
pass so the reshuffle does not inherit them:
- `AGENTS.md:59` - `scripts/run_sfincs_direct.py` (MISSING)
- `tests/test_telemac_rain_on_grid_template.py:6,8` -
  `scripts/sandbox/telemac/rog_coweeta_live.py`, `rog_offline_smoke.py` (both MISSING)
- `docs/validation/telemac-family-migration-inventory.md:24` -
  `scripts/proof_coastal_tidal_surge.py`, `..._registered.py`,
  `sandbox/telemac/rog_render_proofs.py`, `sandbox/replication/rog_ballcreek*.py` (MISSING)
- `docs/DELETION_LEDGER.md:580` - a QUEUED section for
  `scripts/run_l2_validation_harness.py` (MISSING; chopped per `:1114`)

Also present on disk but untracked and already gitignored:
`scripts/sandbox/oceanmesh/shoreline/GSHHS_i_L1.*` (4 files) and every
`__pycache__/` - no action, listed for completeness.

## 5. Questions for NATE

Each is a place where two fates are defensible. Recommendation given.

1. **The two `stage_*` scripts (1,134 LOC) - TRACKED PRODUCT or LOCAL-ONLY?**
   They are one-shot multi-GB stagers that write into THIS machine's MinIO and
   refuse without a local endpoint (`_env_guard.require_local_endpoint`). But four
   shipped fetcher `source.yaml` files and two suite tests name them as the
   provenance of the data they serve.
   *Recommendation: TRACKED in `scripts/staging/`.* The correct-data-class law
   makes provenance part of the product; a user who cannot see how a served grid
   was built cannot audit it.

2. **The three consumer-less current-wave drivers (`drive_keyword_floor` 161,
   `drive_module_surface_flip` 134, `drive_open_water_domains` 147) - TRACKED or
   LOCAL-ONLY?** Zero references anywhere, all touched 2026-09-05/06.
   *Recommendation: TRACKED in `scripts/drivers/`.* They are the wave's acceptance
   evidence and run on the same `run_live` pre-flight as the canary drivers; the
   fix for "no consumer" is a line in the canary that names them, exactly as
   `canaries.py:259` does for the do_sag and river_dye cards.

3. **`seed_showcase_cases.py` (750 LOC) - TRACKED as-is, or TRACKED after the
   ledger's QUEUED cut?** `docs/DELETION_LEDGER.md:323` already flags its private
   copies of the WS protocol primitives (`mk`, `_handshake`, `_create_case`,
   `delete_case`, `_auto_approve_request`) against `trid3nt_server/testing/ws_client.py`.
   *Recommendation: TRACKED, and land the ledger cut in the same wave* - it is the
   single largest driver and most of the excess is a duplicated client.

4. **`use_openrouter.sh` (51) - TRACKED or LOCAL-ONLY?** It edits `.env.local` in
   place (a gitignored, key-bearing file) and restarts the agent, which is a
   machine-shaped act; `README.md:78` names it as the way to point at OpenRouter.
   *Recommendation: TRACKED.* Setting a provider key is a first-run step for every
   user, not a NATE-machine act; the secret stays in the ignored file it writes.

5. **`loc_report.py --help` crashes (`:77` casts `sys.argv[1]` to int) - fix now or
   leave?** It is the standing LOC measure and the metric law depends on its method.
   *Recommendation: leave for the reshuffle wave and fix as a one-line argparse
   guard there.* Reporting it here rather than fixing it keeps this pass read-only.

6. **`replay_canary_evidence.py` (114) - instrument or attic?** No code consumer;
   three docs cite it as the standing drift check over committed evidence.
   *Recommendation: TRACKED in `scripts/instruments/`* - it is the only thing that
   would catch silent drift in the committed canary metrics.

7. **`proof_artemis_real_breakwater_v2.py` (229) - `drivers/` or `packet/`?** It is
   a bespoke render for one template, but `docs/decisions/0237-artemis.md:359` and
   `DELETION_LEDGER.md:3108` make it the render the artemis packet carries.
   *Recommendation: `scripts/drivers/`,* with the ARTEMIS animation exemption
   already declared in the assembler; if the assembler ever renders it, move it.

8. **The local-model harness set vs `docs/site/models.md:119-121`.** The page tells
   a user to run `tool_routing_bench.py`, `tool_routing_sweep.py` and
   `routing_failure_split.py`. The sweep is DEAD (Q section 4, item 1); the other
   two are NATE-machine harnesses needing a local ollama.
   *Recommendation: delete the sweep, move bench + failure-split to
   `scripts/local/`, and rewrite `models.md:119-121` to describe the measurement a
   user should make rather than naming scripts the repo no longer ships.*

9. **`run_do_sag_direct.py` (84) + `run_river_dye_direct.py` (75) - LOCAL-ONLY or
   ATTIC?** The cards drivers are the live lane; `docs/site/engines.md:16` still
   describes a `scripts/run_*_direct.py` harness.
   *Recommendation: LOCAL-ONLY.* The direct call is the fastest way to re-solve one
   template while iterating, which is a bench act; `engines.md:16` should describe
   the cards lane instead.

10. **Moving `mesh_formats.py` + `schism_gr3.py` (410 LOC) into
    `trid3nt_server/workflows/mesh/shared/formats/` - this wave or its own?** It
    deletes a `sys.path` hack in shipped code (`nodes.py:39-48`) and touches
    `om2d.py`, `primitives.py` and `tests/test_mesh_om2d.py`.
    *Recommendation: its own small job, ahead of the reshuffle,* so the scripts
    move is pure file relocation and this one carries its own green suite.
