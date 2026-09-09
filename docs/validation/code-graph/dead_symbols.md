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
| `unquote` | import | trid3nt_server/emission/uri_registry.py:69 | 90 | 1 |
| `default_seconds` | parameter | trid3nt_server/gates/confirm.py:118 | 100 | 1 |
| `raw_user_text` | parameter | trid3nt_server/server/dispatch/emitter.py:1173 | 100 | 1 |
| `gs_backend` | parameter | trid3nt_server/workflows/shared/cog_io.py:481 | 100 | 1 |
| `runs_bucket_default` | parameter | trid3nt_server/workflows/shared/cog_io.py:483 | 100 | 1 |

## Callable tier (confidence 60): unused functions, methods, classes

Below the 80 floor because vulture cannot distinguish a dead callable from
one reached dynamically. Treat as candidates, not verdicts.

| symbol | kind | file:line | loc |
|---|---|---|---|
| `write_fort14` | function | trid3nt_server/workflows/mesh/shared/formats/mesh_formats.py:111 | 87 |
| `mesh_quality_report` | function | trid3nt_server/workflows/mesh/shared/formats/mesh_formats.py:228 | 60 |
| `serve_user_supplied_bed` | function | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py:1836 | 38 |
| `get_session_record` | method | trid3nt_server/persistence.py:704 | 31 |
| `scan_third_party_imports` | function | plugin/install_dependencies.py:326 | 30 |
| `_selection_bbox4326` | method | plugin/ui/dock.py:844 | 23 |
| `update_compute_status` | method | trid3nt_server/emission/pipeline_emitter.py:1673 | 22 |
| `update_current_progress` | method | trid3nt_server/emission/pipeline_emitter.py:1468 | 20 |
| `estimate_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:252 | 20 |
| `pin_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:274 | 19 |
| `upsert_session_record` | method | trid3nt_server/persistence.py:526 | 19 |
| `_default_corpus_path` | function | trid3nt_server/tools/search/search_tools/search_tools.py:388 | 16 |
| `decimals_for_range` | function | plugin/render/formatting.py:84 | 15 |
| `require_layer` | method | trid3nt_server/testing/live_run.py:161 | 15 |
| `_default_corpus_path` | function | trid3nt_server/server/protocol/catalog_http.py:75 | 13 |
| `run_forever` | method | plugin/net/trid3nt_client.py:2031 | 12 |
| `format_number` | function | plugin/render/formatting.py:101 | 12 |
| `describe` | method | trid3nt_server/workflows/runtime/plan.py:342 | 12 |
| `count_outputs` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:240 | 11 |
| `read_output_required` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:252 | 11 |
| `require_chart` | method | trid3nt_server/testing/live_run.py:177 | 10 |
| `_tool_chip_style` | function | plugin/ui/cards.py:79 | 9 |
| `_canvas_bbox4326` | method | plugin/ui/dock.py:834 | 9 |
| `require_metric_close` | method | trid3nt_server/testing/live_run.py:208 | 7 |
| `uri_for_short` | method | trid3nt_server/emission/uri_registry.py:372 | 6 |
| `coverage_summary` | method | trid3nt_server/fallbacks/walker.py:158 | 6 |
| `read_stdout_optional` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:273 | 6 |
| `_toggle_connection` | method | plugin/ui/dock.py:1464 | 5 |
| `tripped` | method | trid3nt_server/gates/runaway_guard.py:263 | 5 |
| `_ctg_tile_bounds` | function | trid3nt_server/tools/fetchers/_router/executors/raster_cog.py:1413 | 5 |
| `with_value` | method | trid3nt_server/workflows/runtime/params.py:226 | 5 |
| `current_chart_id` | method | plugin/ui/charts_window.py:400 | 4 |
| `current_turn_drawn_geometry` | function | trid3nt_server/emission/pipeline_emitter.py:157 | 3 |
| `known_handles` | method | trid3nt_server/emission/uri_registry.py:892 | 3 |
| `_toggle_thinking` | method | plugin/ui/cards.py:993 | 2 |
| `_obj_uri` | function | trid3nt_server/tools/cache.py:347 | 2 |
| `mtime` | method | trid3nt_server/tools/fetchers/_router/transport/range_file.py:187 | 2 |
| `_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:633 | 2 |
| `_param_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:637 | 2 |

## Whitelisted false-positive classes

| rule | muted |
|---|---|
| declarative row/field DSL: read off the class by the framework | 6 |
| descriptor/typing decorator | 6 |
| protocol/framework-called name | 13 |
| registry decorator: no static caller by construction | 4 |
| test-support hook (tests are excluded from the scavenge) | 2 |
