# Orphans -- unreachable from every declared root

Roots: `trid3nt_server.main`, `trid3nt_server.__main__`, `trid3nt_server.tools`, `workers.telemac.entrypoint`, `workers.mesh.entrypoint`, `plugin`

Bucket precedence is roots > tests > scripts, so a module reachable
from both a test and a script is reported as test-only.

Unreachable, not imported by any test, not imported by any script.
These are the corpses with nothing holding them up.

Excluded: 116 unreachable docstring-only `__init__.py` package
markers (directories the runtime walks for data, not modules to import).

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.workflows.lib._setter_envelope` | 440 | trid3nt_server/workflows/lib/_setter_envelope.py | no importer in any scanned tree |
| `trid3nt_server.credentials.secrets_handler` | 42 | trid3nt_server/credentials/secrets_handler.py | no importer in any scanned tree |

## Test-only-reachable -- the anchor class

Reachable from `tests/` but from no root. The test is the only
thing keeping the module alive; deleting both is one move.

| module | loc | path | evidence |
|---|---|---|---|
| `scripts.render_selafin_animation` | 900 | scripts/render_selafin_animation.py | imported only by tests.test_animation_legend_stability |
| `scripts.model_check` | 788 | scripts/model_check.py | imported only by tests.test_model_conformance |
| `trid3nt_server.testing.live_run` | 526 | trid3nt_server/testing/live_run.py | imported only by scripts.proof_artemis_om2d_rematch, tests.test_live_run_harness, trid3nt_server.testing, trid3nt_server.testing.canaries |
| `trid3nt_contracts.output_quantities` | 458 | contracts/trid3nt_contracts/output_quantities.py | imported only by contracts.tests.test_engine_run_args_mixin |
| `trid3nt_server.tools.fetchers._router.stratified` | 328 | trid3nt_server/tools/fetchers/_router/stratified.py | imported only by tests.test_catalog_surfacing, tests.test_fallback_sweep_guard |
| `trid3nt_server.workflows.shared.tide_series` | 276 | trid3nt_server/workflows/shared/tide_series.py | imported only by tests.test_tide_series_datum |
| `scripts.harvest_living_atlas` | 224 | scripts/harvest_living_atlas.py | imported only by tests.test_living_atlas |
| `scripts.sandbox.oceanmesh.merc_render` | 141 | scripts/sandbox/oceanmesh/merc_render.py | imported only by tests.test_proof_basemap_credit |
| `trid3nt_contracts.export_schemas` | 116 | contracts/trid3nt_contracts/export_schemas.py | imported only by contracts.tests.test_catalog, contracts.tests.test_export_schemas, contracts.tests.test_schema_drift |
| `trid3nt_server.testing.ws_client` | 111 | trid3nt_server/testing/ws_client.py | imported only by scripts.seed_showcase_cases, scripts.tool_routing_bench, scripts.ws_smoke, trid3nt_server.testing ... |
| `trid3nt_server.testing.proof_paths` | 76 | trid3nt_server/testing/proof_paths.py | imported only by scripts.assemble_proof_packet, scripts.drive_artemis_structure_slot, scripts.drive_do_sag_cards, scripts.drive_river_dye_cards ... |
| `trid3nt_server.testing` | 27 | trid3nt_server/testing/__init__.py | imported only by scripts.drive_artemis_structure_slot, scripts.drive_do_sag_cards, scripts.drive_river_dye_cards |

## Script-only-reachable

Reachable from `scripts/` but from no root and no test -- product
code that survives only because a proof driver imports it.

| module | loc | path | evidence |
|---|---|---|---|
| `trid3nt_server.testing.canaries` | 491 | trid3nt_server/testing/canaries.py | imported only by scripts.proof_artemis_om2d_rematch |
| `trid3nt_server.testing.proof_animations` | 334 | trid3nt_server/testing/proof_animations.py | imported only by scripts.assemble_proof_packet, trid3nt_server.testing.canaries |

## scripts/ entry modules with no importer

Standalone drivers are entry points by design; listed for staleness
review (a driver for a deleted seam is dead), not as a defect.

| module | loc | path |
|---|---|---|
| `scripts.stage_zell_sanford_groundwater` | 1151 | scripts/stage_zell_sanford_groundwater.py |
| `scripts.assemble_proof_packet` | 1025 | scripts/assemble_proof_packet.py |
| `scripts.seed_showcase_cases` | 921 | scripts/seed_showcase_cases.py |
| `scripts.render_all_layers_proof` | 861 | scripts/render_all_layers_proof.py |
| `scripts.code_graph` | 799 | scripts/code_graph.py |
| `scripts.tool_routing_bench` | 741 | scripts/tool_routing_bench.py |
| `scripts.stage_groundwater_recharge` | 552 | scripts/stage_groundwater_recharge.py |
| `scripts.tool_sweep` | 391 | scripts/tool_sweep.py |
| `scripts.proof_artemis_om2d_rematch` | 383 | scripts/proof_artemis_om2d_rematch.py |
| `scripts.sandbox.oceanmesh.schism_gr3` | 335 | scripts/sandbox/oceanmesh/schism_gr3.py |
| `scripts.proof_rerun_with_overrides` | 327 | scripts/proof_rerun_with_overrides.py |
| `scripts.sandbox.pysheds_watershed.proof_watershed` | 323 | scripts/sandbox/pysheds_watershed/proof_watershed.py |
| `scripts.proof_artemis_real_breakwater_v2` | 308 | scripts/proof_artemis_real_breakwater_v2.py |
| `scripts.sandbox.oceanmesh.mesh_formats` | 287 | scripts/sandbox/oceanmesh/mesh_formats.py |
| `scripts.ws_smoke` | 283 | scripts/ws_smoke.py |
| `scripts.drive_river_dye_cards` | 271 | scripts/drive_river_dye_cards.py |
| `scripts.gen_tool_support_page` | 263 | scripts/gen_tool_support_page.py |
| `scripts.proof_river_dye_frames` | 247 | scripts/proof_river_dye_frames.py |
| `scripts.tool_usability_sweep` | 237 | scripts/tool_usability_sweep.py |
| `scripts.tool_routing_sweep` | 231 | scripts/tool_routing_sweep.py |
| `scripts.telemac_routing_probe` | 224 | scripts/telemac_routing_probe.py |
| `scripts.sandbox.telemac.render_erodible_scour_proof` | 217 | scripts/sandbox/telemac/render_erodible_scour_proof.py |
| `scripts.sandbox.replication.edi_coweeta_coverage` | 187 | scripts/sandbox/replication/edi_coweeta_coverage.py |
| `scripts.replay_canary_evidence` | 182 | scripts/replay_canary_evidence.py |
| `scripts.drive_mesh_spotcheck` | 171 | scripts/drive_mesh_spotcheck.py |
| `scripts.backfill_run_journal` | 166 | scripts/backfill_run_journal.py |
| `scripts.drive_lake_domain_mesh` | 163 | scripts/drive_lake_domain_mesh.py |
| `scripts.render_fidelity_proof_generic` | 163 | scripts/render_fidelity_proof_generic.py |
| `scripts.drive_do_sag_cards` | 162 | scripts/drive_do_sag_cards.py |
| `scripts.proof_auto_emit_seam` | 154 | scripts/proof_auto_emit_seam.py |
| `scripts.sandbox.replication.ballcreek_delineate_explore` | 152 | scripts/sandbox/replication/ballcreek_delineate_explore.py |
| `scripts.render_run_chart_proof` | 147 | scripts/render_run_chart_proof.py |
| `scripts.drive_artemis_structure_slot` | 136 | scripts/drive_artemis_structure_slot.py |
| `scripts.run_do_sag_direct` | 129 | scripts/run_do_sag_direct.py |
| `scripts.run_river_dye_direct` | 115 | scripts/run_river_dye_direct.py |
| `scripts.routing_failure_split` | 95 | scripts/routing_failure_split.py |
| `scripts.proof_wave_bed_input_live` | 94 | scripts/proof_wave_bed_input_live.py |
| `scripts._env_guard` | 55 | scripts/_env_guard.py |
| `scripts.proof_wave_bed_input_render` | 50 | scripts/proof_wave_bed_input_render.py |
| `scripts.proof_artemis_composer_live` | 42 | scripts/proof_artemis_composer_live.py |
| `scripts.sandbox.telemac.run_erodible_scour_direct` | 34 | scripts/sandbox/telemac/run_erodible_scour_direct.py |
