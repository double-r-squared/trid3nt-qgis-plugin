# Dead symbols -- vulture, min-confidence 80

Scope: `trid3nt_server/` + `plugin/`, tests excluded.
Vulture scores unused imports at 90, unused variables/unreachable code at 100,
and unused functions/classes at 60 -- so an 80 floor is an import/variable/
unreachable-code report by construction, and the callable tier is carried
separately below. An unused parameter is reclassified from `variable` to
`parameter`: it is a knob callers still pass, not a dead local.

| symbol | kind | file:line | confidence | loc |
|---|---|---|---|---|
| `return` | unreachable_code | trid3nt_server/server/protocol/connections.py:92 | 100 | 30 |
| `token_envelope` | parameter | trid3nt_server/credentials/auth_handshake.py:188 | 100 | 1 |
| `default_seconds` | parameter | trid3nt_server/gates/confirm.py:117 | 100 | 1 |
| `poll_interval_seconds` | parameter | trid3nt_server/sandbox/sandbox_runner.py:569 | 100 | 1 |
| `logging_client` | parameter | trid3nt_server/sandbox/sandbox_runner.py:570 | 100 | 1 |
| `raw_user_text` | parameter | trid3nt_server/server/dispatch/emitter.py:1179 | 100 | 1 |
| `isochlor_value` | parameter | trid3nt_server/tools/processing/charts_common.py:1088 | 100 | 1 |
| `lat_deg` | parameter | trid3nt_server/tools/processing/extract_model_at_observations/extract_model_at_observations.py:632 | 100 | 1 |
| `gs_backend` | parameter | trid3nt_server/workflows/shared/cog_io.py:542 | 100 | 1 |
| `runs_bucket_default` | parameter | trid3nt_server/workflows/shared/cog_io.py:544 | 100 | 1 |

## Callable tier (confidence 60): unused functions, methods, classes

Below the 80 floor because vulture cannot distinguish a dead callable from
one reached dynamically. Treat as candidates, not verdicts.

| symbol | kind | file:line | loc |
|---|---|---|---|
| `build_vadose_breakthrough_chart` | function | trid3nt_server/tools/processing/charts_common.py:798 | 79 |
| `build_hazard_quantile_band_chart` | function | trid3nt_server/tools/processing/charts_common.py:535 | 78 |
| `build_ates_recovery_chart` | function | trid3nt_server/tools/processing/charts_common.py:1243 | 62 |
| `pin_flood_run_settings` | function | trid3nt_server/gates/cards/solver_confirm.py:509 | 51 |
| `serve_user_supplied_bed` | function | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py:1832 | 37 |
| `get_session_record` | method | trid3nt_server/persistence/persistence.py:725 | 31 |
| `scan_third_party_imports` | function | plugin/install_dependencies.py:326 | 30 |
| `_pick_property` | function | trid3nt_server/tools/processing/charts_common.py:1523 | 24 |
| `_selection_bbox4326` | method | plugin/ui/dock.py:842 | 23 |
| `update_compute_status` | method | trid3nt_server/emission/pipeline_emitter.py:1728 | 22 |
| `estimate_flood_run_settings` | function | trid3nt_server/gates/cards/solver_confirm.py:485 | 22 |
| `update_current_progress` | method | trid3nt_server/emission/pipeline_emitter.py:1523 | 20 |
| `estimate_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:442 | 20 |
| `pin_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:464 | 19 |
| `upsert_session_record` | method | trid3nt_server/persistence/persistence.py:547 | 19 |
| `_default_corpus_path` | function | trid3nt_server/tools/search/search_tools/search_tools.py:391 | 16 |
| `describe` | method | trid3nt_server/workflows/lib/plan.py:490 | 16 |
| `decimals_for_range` | function | plugin/render/formatting.py:84 | 15 |
| `require_layer` | method | trid3nt_server/testing/live_run.py:162 | 15 |
| `_default_corpus_path` | function | trid3nt_server/server/protocol/catalog_http.py:75 | 13 |
| `run_forever` | method | plugin/net/trid3nt_client.py:2088 | 12 |
| `make_work_dir` | function | trid3nt_server/workflows/lib/_setter_envelope.py:430 | 11 |
| `count_outputs` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:240 | 11 |
| `read_output_required` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:252 | 11 |
| `require_chart` | method | trid3nt_server/testing/live_run.py:178 | 10 |
| `_tool_chip_style` | function | plugin/ui/cards.py:79 | 9 |
| `_canvas_bbox4326` | method | plugin/ui/dock.py:832 | 9 |
| `require_metric_close` | method | trid3nt_server/testing/live_run.py:209 | 7 |
| `uri_for_short` | method | trid3nt_server/emission/uri_registry.py:479 | 6 |
| `coverage_summary` | method | trid3nt_server/fallbacks/walker.py:158 | 6 |
| `read_stdout_optional` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:273 | 6 |
| `_toggle_connection` | method | plugin/ui/dock.py:1448 | 5 |
| `tripped` | method | trid3nt_server/gates/runaway_guard.py:263 | 5 |
| `_ctg_tile_bounds` | function | trid3nt_server/tools/fetchers/_router/executors/raster_cog.py:1417 | 5 |
| `with_value` | method | trid3nt_server/workflows/lib/params.py:226 | 5 |
| `current_chart_id` | method | plugin/ui/charts_window.py:400 | 4 |
| `current_turn_drawn_geometry` | function | trid3nt_server/emission/pipeline_emitter.py:159 | 3 |
| `jail_available` | function | trid3nt_server/sandbox/sandbox_hardening.py:393 | 3 |
| `_toggle_thinking` | method | plugin/ui/cards.py:993 | 2 |
| `known_handles` | method | trid3nt_server/emission/uri_registry.py:1071 | 2 |
| `_obj_uri` | function | trid3nt_server/tools/cache.py:347 | 2 |
| `mtime` | method | trid3nt_server/tools/fetchers/_router/transport/range_file.py:187 | 2 |
| `_now_iso` | function | trid3nt_server/tools/processing/charts_common.py:299 | 2 |

## Whitelisted false-positive classes

| rule | muted |
|---|---|
| descriptor/typing decorator | 3 |
| protocol/framework-called name | 12 |
| registry decorator: no static caller by construction | 6 |
| signature mandated by an external API (repo's own noqa: ARG) | 1 |
| test-support hook (tests are excluded from the scavenge) | 2 |
