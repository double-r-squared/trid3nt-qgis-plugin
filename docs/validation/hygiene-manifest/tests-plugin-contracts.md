# hygiene manifest - tests + plugin/tests + contracts/tests

MANIFEST LENS, READ-ONLY. Every file below was opened and read end to end before its
row was written; the greps in the `hist` column are guards run AFTER the read, never
the census. Scope is `git ls-files tests plugin/tests contracts/tests` at HEAD.

This scope is a MAP: one row per file, `file -> destination` per TESTS RULED
(`docs/IDEAS.md` 2026-09-09, ruling on `docs/validation/tests-eval.md`). No prose rows -
the docstring / comment-block / stale-claim columns are not carried for a scope whose
wave moves files rather than edits them.

Columns: `path` | `pure` (pure LOC by `scripts/loc_report.py`'s own classify: total minus
blank minus comment minus docstring; bytes for non-`.py`) | `hist` (history / spec-notation
markers matched by the standing regex - job-NNNN, task-NNN, ADR NNNN, sprint-NN, wave N,
milestone N, FR-XX-N, OQ-NN, NFR-X-N, FNN, Invariant N, Stage N; a COUNT, carried because
these files move before the docstring wave reaches them) | `fate` | `note`.

Fates in this scope: `MOVE -> <dir>` (the mirror), `MERGE -> <file>`, `SPLIT in place`,
`+ TRIM` (a named in-file cut the charter rules), `DELETE` (the 1,627-pure-LOC cull),
`KEEP` (the two separate distributions and `tests/fixtures/`). No file in this scope is an
EXEMPT candidate: the docstring limit is not what this wave applies here.

## tests/ - the mirror move  (315 files)

| path | pure | hist | fate | note |
|---|---:|---:|---|---|
| `tests/_fakes/__init__.py` | 91 | 0 | KEEP | the shared doubles, extracted at the import-mode checkpoint: `MockMCPClient` and `_fresh_case_summary` from `test_persistence.py`, `MockWebSocket` from `test_server_case_handlers.py`, all three verbatim |
| ~~`tests/__init__.py`~~ | 0 | 0 | DELETED | died with --import-mode=importlib (the FIRST checkpoint, landed) |
| `tests/audit_gemini_schema_compliance.py` | 162 | 4 | DELETE | ORPHAN + DUPLICATE of test_gemini_schema_compliance.py; zero references |
| `tests/card_client.py` | 60 | 0 | DELETE | ORPHAN: claims to be shared by every gate test; shared by zero |
| `tests/conftest.py` | 123 | 0 | KEEP tests/conftest.py + SPLIT | root keeps _default_scripted_provider / _reset_fake_llm_harness / fake_s3 / fake_llm; _offline_cas_parse + telemac_result -> tests/telemac/conftest.py; empty_registry -> tests/tools/conftest.py; make_read_through_s3_injector -> tests/_fakes/ |
| `tests/eval_routing_live.py` | 528 | 24 | DELETE | ORPHAN: live Gemini routing driver; Gemini is not a selectable provider; zero references |
| `tests/live_evidence_job_0169.py` | 155 | 3 | DELETE | ORPHAN: closed-job live-evidence harness; zero references |
| `tests/reach_chain.py` | 68 | 0 | MOVE -> tests/_fakes/reach_chain.py | shared double, not a test |
| `tests/test_active_aoi_repair_job2.py` | 200 | 2 | MOVE -> tests/runtime/ |  |
| `tests/test_adapter_strip_private_params.py` | 71 | 2 | MOVE -> tests/adapters/ |  |
| `tests/test_agent_routing.py` | 78 | 4 | MOVE -> tests/search/ |  |
| `tests/test_always_offload_heavy_tools.py` | 90 | 1 | MOVE -> tests/tools/ |  |
| `tests/test_animation_legend_stability.py` | 169 | 0 | MOVE -> tests/scripts/ |  |
| `tests/test_anon_identity_convergence.py` | 161 | 2 | MOVE -> tests/credentials/ |  |
| `tests/test_anthropic_adapter.py` | 304 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_aoi_autofill_adr0017.py` | 210 | 2 | MOVE -> tests/server/ |  |
| `tests/test_aoi_pin_fetch_bbox_durability.py` | 206 | 0 | MOVE -> tests/runtime/ |  |
| `tests/test_aoi_pin_lane_c.py` | 212 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_aoi_residual_floored_zoom_to.py` | 152 | 2 | MOVE -> tests/emission/ |  |
| `tests/test_aoi_snap_independent_of_geolocate.py` | 29 | 0 | MOVE -> tests/server/ |  |
| `tests/test_arg_normalizer_wave_4_10.py` | 401 | 2 | MOVE -> tests/tools/ |  |
| `tests/test_auth_handshake.py` | 198 | 1 | MOVE -> tests/credentials/ |  |
| `tests/test_auto_create_case_job0262.py` | 287 | 9 | MOVE -> tests/server/ |  |
| `tests/test_auto_publish_droppable_raster.py` | 134 | 3 | MOVE -> tests/emission/ |  |
| `tests/test_bathymetry_data_seam.py` | 247 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_bench_block_hook_lane_a.py` | 171 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_build_mesh_tool.py` | 445 | 0 | MOVE -> tests/mesh/ |  |
| `tests/test_building_detail_http_route.py` | 104 | 0 | MOVE -> tests/server/ |  |
| `tests/test_case_authority_resume.py` | 163 | 2 | MOVE -> tests/server/ |  |
| `tests/test_case_binding_job0268.py` | 205 | 7 | MOVE -> tests/server/ |  |
| `tests/test_case_context_reset.py` | 37 | 4 | MOVE -> tests/server/ |  |
| `tests/test_case_history_rehydrate_f17.py` | 269 | 12 | MOVE -> tests/adapters/ |  |
| `tests/test_case_layer_persistence.py` | 150 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_case_layer_write_path_job0259.py` | 198 | 7 | MOVE -> tests/server/ |  |
| `tests/test_case_list_http_route.py` | 101 | 0 | MOVE -> tests/credentials/ |  |
| `tests/test_catalog_surfacing.py` | 136 | 45 | MOVE -> tests/search/ |  |
| `tests/test_catalog_tools.py` | 501 | 5 | MOVE -> tests/search/ |  |
| `tests/test_catalog_user_overlay.py` | 101 | 0 | MOVE -> tests/search/ |  |
| `tests/test_chart_tools.py` | 389 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_circuit_breaker.py` | 182 | 3 | MOVE -> tests/gates/ |  |
| `tests/test_circuit_breaker_integration.py` | 205 | 5 | MOVE -> tests/gates/ |  |
| `tests/test_clip_raster_to_polygon.py` | 457 | 2 | MOVE -> tests/processing/ |  |
| `tests/test_code_exec_tool.py` | 248 | 7 | MOVE -> tests/sandbox/ |  |
| `tests/test_cog_io.py` | 255 | 0 | MOVE -> tests/runtime/ | straddler: HOME not split - workflows/shared/cog_io.py is the subject, cache + solver are scaffolding |
| `tests/test_compaction_card_persistence.py` | 174 | 2 | MOVE -> tests/emission/ |  |
| `tests/test_compose_case_report.py` | 218 | 0 | MOVE -> tests/tools/ |  |
| `tests/test_compute_aspect.py` | 344 | 2 | MOVE -> tests/processing/ |  |
| `tests/test_compute_blended_composite.py` | 318 | 4 | MOVE -> tests/processing/ |  |
| `tests/test_compute_building_density.py` | 418 | 8 | MOVE -> tests/processing/ |  |
| `tests/test_compute_change_detection.py` | 170 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_colored_relief.py` | 298 | 1 | MOVE -> tests/processing/ |  |
| `tests/test_compute_contours.py` | 298 | 2 | MOVE -> tests/processing/ |  |
| `tests/test_compute_cross_section.py` | 304 | 1 | MOVE -> tests/processing/ |  |
| `tests/test_compute_exposure_summary.py` | 155 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_flood_depth_damage.py` | 217 | 1 | MOVE -> tests/processing/ |  |
| `tests/test_compute_flood_extent_skill.py` | 176 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_hillshade.py` | 521 | 7 | MOVE -> tests/processing/ |  |
| `tests/test_compute_idf_curve.py` | 146 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_impervious_surface.py` | 392 | 8 | MOVE -> tests/processing/ |  |
| `tests/test_compute_layer_bounds.py` | 214 | 2 | MOVE -> tests/processing/ |  |
| `tests/test_compute_model_residuals.py` | 318 | 1 | MOVE -> tests/processing/ |  |
| `tests/test_compute_ndvi.py` | 166 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_sediment_yield.py` | 208 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_skill_metrics.py` | 188 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_compute_slope.py` | 246 | 2 | MOVE -> tests/processing/ |  |
| `tests/test_context_budget.py` | 427 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_context_window_abort_persistence.py` | 169 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_context_window_discovery.py` | 347 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_credential_pipeline.py` | 588 | 2 | MOVE -> tests/credentials/ |  |
| `tests/test_credential_resolver.py` | 53 | 0 | MOVE -> tests/credentials/ |  |
| `tests/test_crisp_end_after_deliverable.py` | 124 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_data_fetch.py` | 854 | 18 | MOVE -> tests/fetchers/ |  |
| `tests/test_declarative_library.py` | 1361 | 1 | MOVE -> tests/runtime/ |  |
| `tests/test_declarative_temporal.py` | 190 | 0 | MOVE -> tests/runtime/ |  |
| `tests/test_describe_keywords.py` | 79 | 0 | MOVE -> tests/search/ |  |
| `tests/test_dev_tool_invoke_handler.py` | 217 | 2 | MOVE -> tests/tools/ |  |
| `tests/test_digitize_water_body.py` | 210 | 1 | MOVE -> tests/processing/ |  |
| `tests/test_discovery_expands_gate_lane_a.py` | 134 | 0 | MOVE -> tests/search/ |  |
| `tests/test_dispatch_guards_stage3.py` | 267 | 13 | MOVE -> tests/emission/ |  |
| `tests/test_door_dissolution.py` | 85 | 1 | MOVE -> tests/search/ |  |
| `tests/test_duplicate_flood_layer_fix.py` | 204 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_emit_on_fetch_equivalence.py` | 62 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_emit_on_fetch_seam.py` | 213 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_empty_completion_retry.py` | 120 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_engine_room_posture.py` | 97 | 2 | MOVE -> tests/solver/ | straddler: HOME not split - the worker-doctrine posture is a solver fact |
| `tests/test_enhance_satellite_image.py` | 242 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_envelope_case_tagging_job0277.py` | 80 | 2 | MOVE -> tests/emission/ |  |
| `tests/test_ephemeral_cases.py` | 113 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_extract_landcover_class.py` | 402 | 5 | MOVE -> tests/processing/ |  |
| `tests/test_extract_model_at_observations.py` | 476 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_extract_timeseries_at_point.py` | 217 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_fallback_ladder.py` | 970 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_fallback_sweep_guard.py` | 129 | 8 | MOVE -> tests/fetchers/ |  |
| `tests/test_fetch_resolution_gate.py` | 243 | 4 | MOVE -> tests/gates/ |  |
| `tests/test_fetch_reuse_dispatch_f96.py` | 119 | 7 | MOVE -> tests/emission/ |  |
| `tests/test_file_persistence.py` | 257 | 2 | MOVE -> tests/server/ |  |
| `tests/test_full_stream_persistence_job0267.py` | 580 | 22 | MOVE -> tests/adapters/ |  |
| `tests/test_gate_collapse_specs.py` | 37 | 2 | MOVE -> tests/gates/ |  |
| `tests/test_gate_timeout_local.py` | 25 | 2 | MOVE -> tests/gates/ |  |
| `tests/test_gemini_kwargs_fuzz.py` | 205 | 25 | MOVE -> tests/tools/ + TRIM | fuzz cross product COLLAPSES to a per-pattern sweep: -3,243 collected, -85 pure LOC |
| `tests/test_gemini_schema_compliance.py` | 184 | 6 | MOVE -> tests/adapters/ |  |
| `tests/test_geometry_composition_tools.py` | 90 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_hook_colocation.py` | 82 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_hydro_validation_metrics.py` | 96 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_hydrology_primitives.py` | 253 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_ingest_layer_http_route.py` | 230 | 0 | MOVE -> tests/server/ |  |
| `tests/test_input_layer_surfacing.py` | 236 | 8 | MOVE -> tests/emission/ |  |
| `tests/test_input_review_gate.py` | 188 | 1 | MOVE -> tests/gates/ |  |
| `tests/test_invariant_logging_p10.py` | 177 | 2 | MOVE -> tests/adapters/ |  |
| `tests/test_job0305_memoryfile_lifetime.py` | 33 | 1 | MOVE -> tests/processing/ |  |
| `tests/test_law9_consequence_guard.py` | 104 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_layer_delete_and_reuse_job0325.py` | 306 | 9 | MOVE -> tests/emission/ |  |
| `tests/test_layer_handles_adr0014.py` | 316 | 2 | MOVE -> tests/emission/ |  |
| `tests/test_layer_persist_survives_cancel.py` | 125 | 3 | MOVE -> tests/emission/ |  |
| `tests/test_layer_uri_emit.py` | 54 | 3 | MOVE -> tests/emission/ |  |
| `tests/test_list_run_frames.py` | 178 | 19 | MOVE -> tests/solver/ |  |
| `tests/test_live_run_harness.py` | 270 | 0 | MOVE -> tests/scripts/ |  |
| `tests/test_living_atlas.py` | 272 | 2 | MOVE -> tests/search/ |  |
| `tests/test_local_models_http_route.py` | 134 | 1 | MOVE -> tests/adapters/ |  |
| `tests/test_local_subprocess_runner.py` | 238 | 0 | MOVE -> tests/solver/ |  |
| `tests/test_loop_exhausted_envelope.py` | 135 | 2 | MOVE -> tests/adapters/ |  |
| `tests/test_main_startup.py` | 15 | 0 | MOVE -> tests/server/ |  |
| `tests/test_max_turns_cap.py` | 92 | 6 | MOVE -> tests/server/ |  |
| `tests/test_mesh_declaration_travel.py` | 56 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_mesh_gate_loop.py` | 395 | 0 | MOVE -> tests/mesh/ |  |
| `tests/test_mesh_meshers.py` | 142 | 0 | MOVE -> tests/mesh/ |  |
| `tests/test_mesh_om2d.py` | 528 | 0 | MOVE -> tests/mesh/ + TRIM | function-level chop: the roster assertion duplicates test_mesh_meshers.py:58-59 |
| `tests/test_mesh_polygon_domain.py` | 247 | 0 | MOVE -> tests/mesh/ |  |
| `tests/test_mesh_shoreline_ladder.py` | 95 | 0 | MOVE -> tests/mesh/ |  |
| `tests/test_mesh_topology_and_bed.py` | 368 | 0 | MOVE -> tests/mesh/ |  |
| `tests/test_model_conformance.py` | 77 | 0 | MOVE -> tests/model/ |  |
| `tests/test_model_debris_flow.py` | 153 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_model_selector.py` | 164 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_multi_turn_loop.py` | 593 | 5 | MOVE -> tests/gates/ |  |
| `tests/test_nested_substep_persistence_job168.py` | 191 | 5 | MOVE -> tests/emission/ |  |
| `tests/test_no_markdown_in_tool_results.py` | 92 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_open_water_domains.py` | 124 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_openai_adapter.py` | 806 | 1 | MOVE -> tests/gates/ |  |
| `tests/test_outputs_manifest_schema.py` | 105 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_outputs_seam.py` | 121 | 2 | MOVE -> tests/emission/ |  |
| `tests/test_pandas_pin_regression.py` | 25 | 13 | DELETE | ORPHAN: hydromt-sfincs is gone; also asserts pandas, not a product behaviour |
| `tests/test_parallel_call_bundling.py` | 203 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_payload_warning_flow.py` | 390 | 2 | MOVE -> tests/server/ |  |
| `tests/test_persistence.py` | 226 | 4 | MOVE -> tests/server/ + TRIM | SURRENDERED MockMCPClient / _fresh_case_summary to tests/_fakes/ BEFORE any file moves |
| `tests/test_persistence_sessions.py` | 223 | 5 | MOVE -> tests/server/ |  |
| `tests/test_persistence_singleton_wiring.py` | 65 | 0 | MOVE -> tests/server/ |  |
| `tests/test_pipeline_emitter.py` | 1235 | 20 | MOVE -> tests/emission/ |  |
| `tests/test_pipeline_emitter_substeps.py` | 165 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_plugin_repo_http_route.py` | 363 | 0 | MOVE -> tests/server/ |  |
| `tests/test_poor_fit_widen_lane_a.py` | 75 | 1 | MOVE -> tests/search/ |  |
| `tests/test_postprocess_telemac.py` | 136 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_postprocess_telemac_wse.py` | 168 | 1 | MOVE -> tests/telemac/ |  |
| `tests/test_presets.py` | 142 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_probe_point.py` | 196 | 5 | MOVE -> tests/server/ |  |
| `tests/test_probe_point_http_route.py` | 142 | 0 | MOVE -> tests/server/ |  |
| `tests/test_proof_basemap_credit.py` | 36 | 0 | MOVE -> tests/scripts/ |  |
| `tests/test_provenance_channel.py` | 66 | 1 | MOVE -> tests/tools/ |  |
| `tests/test_provider_config_http_route.py` | 422 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_provider_discipline.py` | 206 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_publish_discipline_job0270.py` | 115 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_publish_layer.py` | 66 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_publish_layer_envelope.py` | 47 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_publish_layer_legend.py` | 198 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_publish_layer_vector_and_overviews.py` | 229 | 3 | MOVE -> tests/emission/ |  |
| `tests/test_publish_manifest_register_only_phase4.py` | 192 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_query_point_hazard.py` | 212 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_rain_on_grid_cn_and_nodes.py` | 50 | 0 | MERGE -> tests/telemac/test_telemac_rain_on_grid_cn.py | the _and_ name is two subjects; the charter merges it into its subject's file |
| `tests/test_read_run_diagnostics.py` | 169 | 0 | MOVE -> tests/solver/ |  |
| `tests/test_region_choice_picker.py` | 334 | 4 | MOVE -> tests/gates/ |  |
| `tests/test_register_case_layer.py` | 295 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_register_tool_wave15_kwargs.py` | 102 | 7 | MOVE -> tests/tools/ |  |
| `tests/test_release_containment.py` | 127 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_remote_daemon_access.py` | 257 | 0 | MOVE -> tests/credentials/ |  |
| `tests/test_rerun_with_overrides.py` | 385 | 0 | MOVE -> tests/runtime/ |  |
| `tests/test_resolution_doctrine_0224.py` | 75 | 1 | MOVE -> tests/tools/ |  |
| `tests/test_resolution_sensitivity.py` | 82 | 0 | MOVE -> tests/runtime/ |  |
| `tests/test_restyle_surface.py` | 105 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_resume_replays_case_layers.py` | 206 | 2 | MOVE -> tests/emission/ |  |
| `tests/test_router_3dep_extra.py` | 96 | 3 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_aorc_precip.py` | 134 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_arcgis_odd.py` | 196 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_buildings.py` | 113 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_cds.py` | 158 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_chained.py` | 307 | 3 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_delegate_resolve.py` | 88 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_dem.py` | 240 | 3 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_engine.py` | 209 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_envelope.py` | 197 | 6 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_executors.py` | 697 | 10 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_fanout_routing.py` | 244 | 3 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_fault_sources.py` | 132 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_field_boundaries.py` | 48 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_firms.py` | 119 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_flood_extent_observation.py` | 119 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_glm.py` | 214 | 3 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_goes_animation.py` | 255 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_goes_archive.py` | 179 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_goes_satellite.py` | 286 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_grib.py` | 151 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_groundwater_recharge.py` | 196 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_hooks.py` | 309 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_hrrr.py` | 131 | 3 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_hyriver.py` | 83 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_jrc.py` | 137 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_keyed_misc.py` | 72 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_landcover.py` | 129 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_lter_records.py` | 171 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_mapserver_export.py` | 124 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_nldas2.py` | 74 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_noaa_sst.py` | 126 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_nwis.py` | 130 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_nwm_streamflow.py` | 251 | 4 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_opera_dswx.py` | 66 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_overpass.py` | 330 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_population.py` | 156 | 4 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_promotion.py` | 220 | 12 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_river.py` | 190 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_sentinel1.py` | 89 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_slider_timestamps.py` | 116 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_soilgrids.py` | 133 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_spec_loader.py` | 166 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_stac_composite.py` | 183 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_stac_raster.py` | 361 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_stations.py` | 199 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_statsgo.py` | 85 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_storm_tracks.py` | 452 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_topobathy.py` | 288 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_transport.py` | 279 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_vector_ogr.py` | 199 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_viirs_day_fire.py` | 117 | 2 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_wfigs_incident.py` | 208 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_zell_sanford_groundwater.py` | 256 | 4 | MOVE -> tests/fetchers/ |  |
| `tests/test_router_zip_multifile.py` | 168 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_run_journal.py` | 117 | 0 | MOVE -> tests/runtime/ |  |
| `tests/test_run_river_dye_scenario.py` | 493 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_run_telemac_chain.py` | 102 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_runaway_guard.py` | 158 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_sandbox_box.py` | 151 | 0 | MOVE -> tests/sandbox/ |  |
| `tests/test_satellite_slider.py` | 56 | 0 | MOVE -> tests/fetchers/ |  |
| `tests/test_scenario_reuse_dispatch_job0326.py` | 129 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_scenario_reuse_fetch_f96.py` | 143 | 4 | MOVE -> tests/runtime/ |  |
| `tests/test_scenario_reuse_job0326.py` | 220 | 2 | MOVE -> tests/runtime/ |  |
| `tests/test_scripted_adapter.py` | 109 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_search_spatial_functions.py` | 51 | 1 | MOVE -> tests/search/ |  |
| `tests/test_search_tools.py` | 247 | 8 | MOVE -> tests/search/ |  |
| `tests/test_search_tools_mongo_backend.py` | 178 | 6 | MOVE -> tests/search/ |  |
| `tests/test_section_tool.py` | 140 | 0 | MOVE -> tests/processing/ |  |
| `tests/test_server.py` | 60 | 1 | MOVE -> tests/emission/ |  |
| `tests/test_server_case_handlers.py` | 420 | 6 | MOVE -> tests/server/ + TRIM | SURRENDERED MockWebSocket to tests/_fakes/ BEFORE any file moves |
| `tests/test_session_durability_jobs_bc.py` | 323 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_show_nexrad_radar.py` | 115 | 6 | MOVE -> tests/processing/ |  |
| `tests/test_sim_card_persistence_task208.py` | 295 | 4 | MOVE -> tests/emission/ |  |
| `tests/test_solve_survive_disconnect.py` | 282 | 0 | MOVE -> tests/emission/ |  |
| `tests/test_solver.py` | 52 | 6 | MOVE -> tests/solver/ |  |
| `tests/test_solver_confirm_gate.py` | 127 | 5 | MOVE -> tests/gates/ |  |
| `tests/test_solver_local_docker.py` | 449 | 6 | MOVE -> tests/solver/ |  |
| `tests/test_spatial_input_gate.py` | 270 | 4 | MOVE -> tests/gates/ |  |
| `tests/test_spatial_input_invalid_resolve.py` | 180 | 1 | MOVE -> tests/gates/ |  |
| `tests/test_spatial_input_neutral_line.py` | 169 | 0 | MOVE -> tests/gates/ |  |
| `tests/test_spatial_query.py` | 561 | 4 | MOVE -> tests/search/ |  |
| `tests/test_spatial_roles.py` | 100 | 1 | MOVE -> tests/gates/ |  |
| `tests/test_spill_fraction_chainage.py` | 50 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_sticky_anonymous_user.py` | 75 | 2 | MOVE -> tests/credentials/ |  |
| `tests/test_stream_scoped_turns_job0269.py` | 175 | 1 | MOVE -> tests/server/ |  |
| `tests/test_sync_tool_offload_dispatch.py` | 68 | 3 | MOVE -> tests/tools/ |  |
| `tests/test_sync_tool_offload_stage0.py` | 43 | 7 | MOVE -> tests/tools/ |  |
| `tests/test_system_prompt.py` | 162 | 26 | MOVE -> tests/adapters/ |  |
| `tests/test_telemac3d_vertical_grid.py` | 67 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_boundary_contract.py` | 102 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_cas_validate.py` | 86 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_catalog_drift.py` | 41 | 0 | MOVE -> tests/scripts/ |  |
| `tests/test_telemac_do_sag.py` | 305 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_event_time.py` | 99 | 1 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_input_provenance.py` | 55 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_mesh_coverage.py` | 69 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_module_surface.py` | 864 | 1 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_outflow_stage.py` | 166 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_rain_forcing.py` | 89 | 1 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_rain_on_grid_cn.py` | 81 | 0 | MOVE -> tests/telemac/ | absorbs test_rain_on_grid_cn_and_nodes.py |
| `tests/test_telemac_rain_on_grid_template.py` | 335 | 2 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_reach_mesh_session.py` | 182 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_reach_staged_inputs.py` | 135 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_result_reader.py` | 94 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemac_run_reads.py` | 185 | 0 | MOVE -> tests/telemac/ |  |
| `tests/test_telemetry.py` | 221 | 1 | MOVE -> tests/server/ |  |
| `tests/test_telemetry_accuracy_panel.py` | 285 | 0 | MOVE -> tests/server/ |  |
| `tests/test_telemetry_cache_emission.py` | 98 | 1 | MOVE -> tests/server/ |  |
| `tests/test_telemetry_summary_http.py` | 187 | 2 | MOVE -> tests/server/ |  |
| `tests/test_template_hygiene.py` | 60 | 0 | MOVE -> tests/tools/ |  |
| `tests/test_terminal_narration_and_failure_card.py` | 202 | 6 | MOVE -> tests/adapters/ |  |
| `tests/test_thinking_persistence.py` | 222 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_thought_signature.py` | 177 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_tool_annotations.py` | 171 | 4 | MOVE -> tests/search/ |  |
| `tests/test_tool_arg_normalizer.py` | 260 | 3 | MOVE -> tests/tools/ |  |
| `tests/test_tool_candidates_stage3.py` | 189 | 2 | MOVE -> tests/search/ |  |
| `tests/test_tool_candidates_waves.py` | 99 | 1 | MOVE -> tests/search/ |  |
| `tests/test_tool_description_surface.py` | 56 | 1 | MOVE -> tests/server/ |  |
| `tests/test_tool_gating_stage3.py` | 202 | 6 | MOVE -> tests/search/ |  |
| `tests/test_tool_not_found_exception.py` | 189 | 3 | MOVE -> tests/adapters/ |  |
| `tests/test_tool_retrieval.py` | 138 | 1 | MOVE -> tests/search/ |  |
| `tests/test_tool_retrieval_shadow.py` | 233 | 0 | MOVE -> tests/search/ |  |
| `tests/test_tool_retry_on_failure.py` | 193 | 9 | MOVE -> tests/gates/ |  |
| `tests/test_tools_cache.py` | 289 | 5 | MOVE -> tests/tools/ |  |
| `tests/test_tools_registry.py` | 119 | 7 | MOVE -> tests/tools/ |  |
| `tests/test_turn_invariants_stage3.py` | 183 | 2 | MOVE -> tests/adapters/ |  |
| `tests/test_turn_telemetry.py` | 285 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_turn_timeout_hardening.py` | 128 | 0 | MOVE -> tests/adapters/ |  |
| `tests/test_unique_layer_id_mint_f97.py` | 89 | 6 | MOVE -> tests/emission/ |  |
| `tests/test_uri_registry.py` | 656 | 11 | MOVE -> tests/emission/ |  |
| `tests/test_us_states.py` | 65 | 1 | MOVE -> tests/fetchers/ |  |
| `tests/test_user_input_species.py` | 109 | 0 | MOVE -> tests/runtime/ |  |
| `tests/test_vector_tiles_f94.py` | 178 | 3 | MOVE -> tests/emission/ |  |
| `tests/test_web_fetch.py` | 347 | 1 | MOVE -> tests/search/ |  |
| `tests/test_workflow_skeleton.py` | 273 | 2 | MOVE -> tests/runtime/ |  |
| `tests/test_ws_bridge_signal_signatures.py` | 102 | 0 | MOVE -> tests/plugin/ | asserts against plugin/net/ws_bridge.py by ast parse; must stay collectable offline |
| `tests/test_ws_heartbeat.py` | 67 | 0 | MOVE -> tests/server/ |  |
| ~~`tests/workflows/__init__.py`~~ | 0 | 0 | DELETED | ORPHAN: the directory held nothing else and went with it |

## tests/fixtures/ - data, not code  (16 files)

| path | pure | hist | fate | note |
|---|---:|---:|---|---|
| `tests/fixtures/case2_news_article.txt` | - (2730B) | - | DELETE | ORPHAN fixture: no reference anywhere |
| `tests/fixtures/finite_fault/chignik_ak0219neiszm_1.fsp` | - (3092B) | - | DELETE | ORPHAN fixture dir: no reference to finite_fault |
| `tests/fixtures/sfincs_aoi/dem.tif` | - (52256B) | - | DELETE | ORPHAN fixture dir: SFINCS product code is gone |
| `tests/fixtures/sfincs_aoi/landcover.tif` | - (4757B) | - | DELETE | ORPHAN fixture dir: SFINCS product code is gone |
| `tests/fixtures/sfincs_aoi/manifest.json` | - (339B) | - | DELETE | ORPHAN fixture dir: SFINCS product code is gone |
| `tests/fixtures/swmm_wq/wq_smoke.inp` | - (3214B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/swmm_wq/wq_smoke.out` | - (8357B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/swmm_wq/wq_smoke.rpt` | - (11964B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/telemac_o2_sp_idealized_profile.json` | - (34728B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/flood_extent/mcdwd_l3_f3_h16v06_window.tif` | - (3860B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/stn/michael_2018_filtered_hwms.json` | - (15284B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/telemac/completion.json` | - (1050B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/telemac/telemac.stdout` | - (0B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/telemac/telemac_metrics.json` | - (2626B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/telemac_ok/completion.json` | - (674B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |
| `tests/fixtures/validation/telemac_ok/telemac_metrics.json` | - (111B) | - | KEEP tests/fixtures/ | fixtures/ is unchanged apart from the three culled entries |

## plugin/tests/ - stays in its distribution  (44 files)

| path | pure | hist | fate | note |
|---|---:|---:|---|---|
| `plugin/tests/headless_case_switch_proof.py` | 220 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_dye_redrive_proof.py` | 193 | 0 | DELETE | ORPHAN: zero inbound references; the flagship canary carries its own packet |
| `plugin/tests/headless_first_run.py` | 194 | 2 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_mesh_gate_drive.py` | 164 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_oq_chart_proof.py` | 176 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_probe_point_proof.py` | 136 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_push_layer_proof.py` | 142 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_run_invocation_proof.py` | 91 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_store_reads_proof.py` | 103 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/headless_telemac_p4_acceptance.py` | 277 | 3 | DELETE | ORPHAN: one-shot acceptance driver, zero inbound references |
| `plugin/tests/headless_thinking_proof.py` | 139 | 1 | DELETE | ORPHAN: zero inbound references; covered offline by test_thinking_persistence.py |
| `plugin/tests/qt_bridge_harness.py` | 40 | 1 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_case_bbox_harness.py` | 147 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_charts_harness.py` | 229 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_dock_ui_harness.py` | 958 | 12 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_mesh_temporal_harness.py` | 193 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_provider_config_harness.py` | 102 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_remote_endpoints_harness.py` | 75 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/qt_tool_picker_harness.py` | 149 | 1 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/stub_server.py` | 834 | 3 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_case_bbox.py` | 114 | 0 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `CASE-BBOX-OK`; reshaped in its own change |
| `plugin/tests/test_charts.py` | 212 | 0 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `CHARTS-OK`; reshaped in its own change |
| `plugin/tests/test_client.py` | 401 | 1 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_code_exec.py` | 123 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_credential.py` | 217 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_dock_ui.py` | 86 | 6 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `DOCK-UI-OK`; reshaped in its own change |
| `plugin/tests/test_envelope_gaps.py` | 279 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_form_and_draw_cards.py` | 147 | 1 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_formatting.py` | 101 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_install_dependencies.py` | 266 | 0 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `rc only`; reshaped in its own change |
| `plugin/tests/test_mesh_temporal.py` | 53 | 0 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `QT-MESH-TEMPORAL-OK`; reshaped in its own change |
| `plugin/tests/test_metadata_parses.py` | 24 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_milestone2.py` | 581 | 2 | SPLIT in place -> plugin/tests/ subject-named files | milestone-named, 5 unrelated seams: gate cards, canvas AOI, reconnect, case list, layer grouping |
| `plugin/tests/test_milestone3.py` | 1000 | 3 | SPLIT in place -> plugin/tests/ subject-named files | milestone-named, 6 unrelated seams: case list fetch, case switch, resume/debounce, selection AOI, token expiry, chat-history parse, settings |
| `plugin/tests/test_probe.py` | 208 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_provider_config.py` | 163 | 0 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `rc only`; reshaped in its own change |
| `plugin/tests/test_push_layer.py` | 184 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_qt_conformance.py` | 128 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_raster_render.py` | 608 | 1 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_remote_endpoints.py` | 54 | 0 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `REMOTE-ENDPOINTS-OK`; reshaped in its own change |
| `plugin/tests/test_remote_streaming.py` | 220 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_run_invocation.py` | 119 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `plugin/tests/test_tool_picker.py` | 239 | 1 | KEEP (plugin/tests/) | STANDING EXCEPTION: subprocess shim asserting only the harness marker `TOOL-PICKER-OK`; reshaped in its own change |
| `plugin/tests/validate_mesh_gate_driver_offline.py` | 17 | 0 | KEEP (plugin/tests/) | separate distribution; joins the run as slice 6, not the tree |

## contracts/tests/ - stays in its distribution  (21 files)

| path | pure | hist | fate | note |
|---|---:|---:|---|---|
| ~~`contracts/tests/__init__.py`~~ | 0 | 0 | DELETED | the KEEP fate did not survive measurement: while it existed, pytest named `contracts/tests/conftest.py` `tests.conftest` under importlib too, and the combined invocation still died (`Plugin already registered under a different name`). Empty file; removing it names that conftest `contracts.tests.conftest` and `tests` + `contracts/tests` collect together (9,507 cases). The distribution is unchanged - `contracts/tests` is not a package in the wheel |
| `contracts/tests/conftest.py` | 9 | 0 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_auth.py` | 85 | 4 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_case.py` | 547 | 11 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_catalog.py` | 156 | 2 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_chart_contracts.py` | 208 | 2 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_collections.py` | 468 | 8 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_common.py` | 102 | 0 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_engine_run_args_mixin.py` | 51 | 0 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_envelope.py` | 175 | 3 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_errors.py` | 81 | 0 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_execution.py` | 160 | 2 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_export_schemas.py` | 37 | 1 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_gate_spec.py` | 82 | 0 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_payload_warning.py` | 291 | 6 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_sandbox_contracts.py` | 102 | 7 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_schema_drift.py` | 92 | 0 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_secrets.py` | 186 | 4 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_tool_registry.py` | 318 | 9 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_user.py` | 71 | 4 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |
| `contracts/tests/test_ws.py` | 708 | 20 | KEEP (contracts/tests/) | separate distribution; joins the run as slice 6, not the tree |

## totals

| | files | pure LOC |
|---|---:|---:|
| scope | 397 | 79450 |
| &nbsp;&nbsp;`.py` | 381 | 79450 |
| &nbsp;&nbsp;non-`.py` fixtures | 16 | - |
| DELETE rows | 15 | 1539 |

The DELETE pure-LOC total is the ORPHAN class only; the charter's 1,627 adds the
3-LOC duplicate chop in `test_mesh_om2d.py` and the 85-LOC fuzz collapse in
`test_gemini_kwargs_fuzz.py`, both of which are `+ TRIM` rows above rather than whole
files. This manifest measures 79,450 pure LOC against the eval's 79,680: the eval was
taken at an earlier HEAD, and the difference is drift in the tree, not in the method.

files listed: 395 / files read: 395 / rows written: 395 (the import-mode checkpoint deleted `tests/__init__.py`, `tests/workflows/__init__.py` and `contracts/tests/__init__.py` and added `tests/_fakes/__init__.py`; the four rows are kept above, struck where the file is gone)

## LANDED - what the wave executed against the map above  (2026-09-09)

The fates above are discharged. This section carries the rows the execution
ADDED or CHANGED; every other row above stands as written, with `tests/<file>`
now read as `tests/<destination>/<file>`.

### Files this stage created (each read end to end as it was written)

| path | pure | hist | fate | note |
|---|---:|---:|---|---|
| `tests/README.md` | - | 0 | NEW | the directory map, the six slice invocations with their collect counts, and the standing-exception list the ruling asks for |
| `tests/telemac/conftest.py` | 34 | 0 | NEW | `_offline_cas_parse` + `telemac_result`, moved verbatim out of `tests/conftest.py` |
| `tests/_fakes/read_through.py` | 23 | 0 | NEW | `make_read_through_s3_injector`, moved verbatim; a conftest is not a module |
| `plugin/tests/test_gate_cards.py` | 139 | 0 | NEW (split of test_milestone2.py) | gate card parsing + the round trip |
| `plugin/tests/test_canvas_aoi.py` | 81 | 0 | NEW (split of test_milestone2.py) | the pure canvas bbox math |
| `plugin/tests/test_reconnect.py` | 89 | 0 | NEW (split of test_milestone2.py) | backoff + the outbound queue |
| `plugin/tests/test_case_list_parsing.py` | 50 | 0 | NEW (split of test_milestone2.py) | case-list parsing |
| `plugin/tests/test_case_layer_grouping.py` | 258 | 0 | NEW (split of test_milestone2.py) | the layer group a case opens into |
| `plugin/tests/test_case_list_fetch.py` | 246 | 0 | NEW (split of test_milestone3.py) | cold case-list fetch + the case switch |
| `plugin/tests/test_chat_history_replay.py` | 140 | 1 | NEW (split of test_milestone3.py) | chat-history replay extraction |
| `plugin/tests/test_fallback_bbox.py` | 43 | 1 | NEW (split of test_milestone3.py) | the auto-focus fallback bbox scan |
| `plugin/tests/test_case_command.py` | 85 | 1 | NEW (split of test_milestone3.py) | the New / Delete case plumbing |
| `plugin/tests/test_startup_case_choice.py` | 151 | 1 | NEW (split of test_milestone3.py) | resume > select-newest > create |
| `plugin/tests/test_case_list_refresh.py` | 41 | 0 | NEW (split of test_milestone3.py) | the resume round trip + debounce |
| `plugin/tests/test_selection_aoi.py` | 38 | 0 | NEW (split of test_milestone3.py) | selection AOI precedence |
| `plugin/tests/test_auth_failure.py` | 52 | 0 | NEW (split of test_milestone3.py) | token-expiry classification |
| `plugin/tests/test_qt_bridge.py` | 57 | 0 | NEW (split of test_milestone3.py) | STANDING EXCEPTION: the `QT-BRIDGE-OK` subprocess shim, now marked |
| `plugin/tests/test_dock_settings.py` | 208 | 0 | NEW (split of test_milestone3.py) | the settings the dock stores |

### Rows whose stated fate the execution corrected

| path | as ruled | as landed | why |
|---|---|---|---|
| `tests/conftest.py` (`empty_registry`) | -> `tests/tools/conftest.py` | STAYS at the root | measured users are `tests/tools/test_tools_registry.py` and `tests/search/test_tool_annotations.py`; `tests/tools/` is not their common parent. The evaluation's third and fourth users were name matches inside two test names in `test_uri_registry.py` |
| `tests/test_pandas_pin_regression.py` | DELETE, pins go with it | file DELETED, pins QUEUED | removing a dependency and lifting a version cap is a build-surface change, outside a documentation-and-structure wave |
| `plugin/tests/test_install_dependencies.py` | STANDING EXCEPTION (ninth shim) | NOT an exception, NO marker | read end to end it drives no Qt harness: `TestMain` asserts the product's own return codes over a mocked `subprocess.run`. Eight shims carry `qt_harness_shim` |
| `tests/adapters/test_gemini_schema_compliance.py` | MOVE only | MOVE + the startup registry import | the catalog tools register through the daemon startup import, not through `trid3nt_server.tools`; in the mirror this module collects before whatever used to trigger that, and its sweep silently lost two tools. It runs the import itself now, and its case count is back to 181 |
| four package-relative walk-ups | (not stated) | depth unchanged | `Path(<package>.__file__).resolve().parents[1]` is relative to the package, not to the test file, so the move does not shift it: `test_tool_description_surface.py`, `test_mesh_gate_loop.py`, `test_build_mesh_tool.py`, `test_door_dissolution.py` |


## Added by the guards leg: `tests/hygiene/`

The three sweep guards THE GUARDS ruling names, written and read end to end by
the agent that landed them. Same columns; `hist` is the standing regex count
after the read, `fate` is `KEEP` for all five - this directory is the guard, so
it is never a move candidate. It joins the `test-server` slice.

| path | pure | hist | fate | note |
| --- | ---: | ---: | --- | --- |
| `tests/hygiene/__init__.py` | 0 | 0 | KEEP | empty; the package marker `tests._fakes` already establishes for this tree |
| `tests/hygiene/_source.py` | 219 | 0 | KEEP | the shared scanner: tracked-file discovery, AST docstrings with the LLM-facing and exemption-marker seams, comment blocks and comment tokens, the disallowed-class table, the exemption-ledger renderer |
| `tests/hygiene/test_docstring_standard.py` | 47 | 0 | KEEP | content-line limits, the 1000-char routing budget, the idle-marker refusal, the ten-entry ceiling, the rendered ledger diff, the disallowed classes over docstrings and comment blocks |
| `tests/hygiene/test_history_markers.py` | 11 | 0 | KEEP | the HISTORY IN CODE classes over every comment token and docstring in every product tree |
| `tests/hygiene/test_dead_references.py` | 67 | 0 | KEEP | paths, bare script names and dotted module names resolved against the tracked tree, the package roots and a README's own directory |

Scope delta: `git ls-files tests plugin/tests contracts/tests` gains five files,
344 pure LOC.

## Added by the docs stage: the proof-coverage guard

Read end to end by the agent that wrote it. Same columns.

| path | pure | hist | fate | note |
| --- | ---: | ---: | --- | --- |
| `tests/hygiene/test_proof_coverage.py` | 29 (28) | 0 | KEEP | The fourth guard: a registered template with no `docs/proof/templates/<name>/` directory is a gap, not a silence. The coverage assertion FAILS today and is `xfail(strict=True)` with the three live templates that have never had a packet assembled named in `AWAITING_FIRST_PACKET` and the condition stated. Two companion assertions keep the exemption from rotting: the set may name only templates that register, and a name whose directory appears must leave it - which the strict xfail also catches as an unexpected pass. Module docstring is 3 content lines against the 5-line limit; no function carries one. It joins the `test-server` slice with its siblings. |

Scope delta: one file, 29 pure LOC (28 as `loc_report` prints it - the
double-subtracted blank inside the module docstring, the same instrument defect
every lens records).

## Added by the template-docs leg: two more guards in `tests/hygiene/`

Written and read end to end by the agent that landed them. Same columns as the
guards leg; both are KEEP and both join the `test-server` slice.

| path | pure | hist | fate | note |
| --- | ---: | ---: | --- | --- |
| `tests/hygiene/test_template_docs.py` | 92 | 0 | KEEP | every registered template has a page, a run record and figures; the pages are byte-current against the generator; a figure whose stamped commit does not contain its template's last change fails; the figures stay inside the doc-size ceilings |
| `tests/hygiene/test_map_readmes.py` | 70 | 0 | KEEP | a package map names only what exists beside it, and names every tracked top-level module and immediate subfolder |

Scope delta: `git ls-files tests plugin/tests contracts/tests` gains two files,
162 pure LOC. `tests/README.md`'s `hygiene/` row is restated from 3 files to 6.
