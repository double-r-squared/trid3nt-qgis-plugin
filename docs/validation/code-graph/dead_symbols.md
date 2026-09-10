# Dead symbols -- vulture, min-confidence 80

Scope: `trid3nt_server/` + `plugin/`, tests excluded.
Vulture scores unused imports at 90, unused variables/unreachable code at 100,
and unused functions/classes at 60 -- so an 80 floor is an import/variable/
unreachable-code report by construction, and the callable tier is carried
separately below. An unused parameter is reclassified from `variable` to
`parameter`: it is a knob callers still pass, not a dead local.

| symbol | kind | file:line | confidence | loc |
|---|---|---|---|---|
| `return` | unreachable_code | trid3nt_server/server/protocol/connections.py:63 | 100 | 30 |
| `token_envelope` | parameter | trid3nt_server/credentials/auth_handshake.py:136 | 100 | 1 |
| `unquote` | import | trid3nt_server/emission/uri_registry.py:18 | 90 | 1 |
| `default_seconds` | parameter | trid3nt_server/gates/confirm.py:90 | 100 | 1 |
| `raw_user_text` | parameter | trid3nt_server/server/dispatch/emitter.py:950 | 100 | 1 |
| `gs_backend` | parameter | trid3nt_server/workflows/shared/cog_io.py:390 | 100 | 1 |
| `runs_bucket_default` | parameter | trid3nt_server/workflows/shared/cog_io.py:392 | 100 | 1 |

## Callable tier (confidence 60): unused functions, methods, classes

Below the 80 floor because vulture cannot distinguish a dead callable from
one reached dynamically. Treat as candidates, not verdicts.

| symbol | kind | file:line | loc |
|---|---|---|---|
| `write_fort14` | function | trid3nt_server/workflows/mesh/shared/formats/mesh_formats.py:93 | 83 |
| `mesh_quality_report` | function | trid3nt_server/workflows/mesh/shared/formats/mesh_formats.py:203 | 59 |
| `serve_user_supplied_bed` | function | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py:1736 | 35 |
| `scan_third_party_imports` | function | plugin/install_dependencies.py:259 | 28 |
| `get_session_record` | method | trid3nt_server/persistence.py:530 | 27 |
| `estimate_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:231 | 18 |
| `_selection_bbox4326` | method | plugin/ui/dock.py:718 | 17 |
| `pin_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:251 | 17 |
| `upsert_session_record` | method | trid3nt_server/persistence.py:396 | 16 |
| `require_layer` | method | trid3nt_server/testing/live_run.py:145 | 15 |
| `update_compute_status` | method | trid3nt_server/emission/pipeline_emitter.py:1263 | 14 |
| `update_current_progress` | method | trid3nt_server/emission/pipeline_emitter.py:1133 | 13 |
| `run_forever` | method | plugin/net/trid3nt_client.py:1601 | 12 |
| `format_number` | function | plugin/render/formatting.py:83 | 12 |
| `describe` | method | trid3nt_server/workflows/runtime/plan.py:284 | 12 |
| `decimals_for_range` | function | plugin/render/formatting.py:70 | 11 |
| `count_outputs` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:203 | 11 |
| `read_output_required` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:215 | 11 |
| `require_chart` | method | trid3nt_server/testing/live_run.py:161 | 10 |
| `_default_corpus_path` | function | trid3nt_server/tools/search/search_tools/search_tools.py:282 | 10 |
| `_canvas_bbox4326` | method | plugin/ui/dock.py:708 | 9 |
| `_tool_chip_style` | function | plugin/ui/cards.py:69 | 8 |
| `_default_corpus_path` | function | trid3nt_server/server/protocol/catalog_http.py:42 | 7 |
| `require_metric_close` | method | trid3nt_server/testing/live_run.py:192 | 7 |
| `uri_for_short` | method | trid3nt_server/emission/uri_registry.py:314 | 6 |
| `coverage_summary` | method | trid3nt_server/fallbacks/walker.py:141 | 6 |
| `read_stdout_optional` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:236 | 6 |
| `_toggle_connection` | method | plugin/ui/dock.py:1270 | 5 |
| `tripped` | method | trid3nt_server/gates/runaway_guard.py:192 | 5 |
| `_ctg_tile_bounds` | function | trid3nt_server/tools/fetchers/_router/executors/raster_cog.py:1325 | 5 |
| `with_value` | method | trid3nt_server/workflows/runtime/params.py:200 | 5 |
| `current_chart_id` | method | plugin/ui/charts_window.py:322 | 4 |
| `current_turn_drawn_geometry` | function | trid3nt_server/emission/pipeline_emitter.py:110 | 3 |
| `known_handles` | method | trid3nt_server/emission/uri_registry.py:788 | 3 |
| `_toggle_thinking` | method | plugin/ui/cards.py:865 | 2 |
| `_obj_uri` | function | trid3nt_server/tools/cache.py:225 | 2 |
| `mtime` | method | trid3nt_server/tools/fetchers/_router/transport/range_file.py:163 | 2 |
| `_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:592 | 2 |
| `_param_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:596 | 2 |

## Whitelisted false-positive classes

| rule | muted |
|---|---|
| declarative row/field DSL: read off the class by the framework | 6 |
| descriptor/typing decorator | 6 |
| protocol/framework-called name | 13 |
| registry decorator: no static caller by construction | 4 |
| test-support hook (tests are excluded from the scavenge) | 2 |
