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
| `token_envelope` | parameter | trid3nt_server/credentials/auth_handshake.py:127 | 100 | 1 |
| `unquote` | import | trid3nt_server/emission/uri_registry.py:18 | 90 | 1 |
| `default_seconds` | parameter | trid3nt_server/gates/confirm.py:90 | 100 | 1 |
| `raw_user_text` | parameter | trid3nt_server/server/dispatch/emitter.py:813 | 100 | 1 |
| `entry_id` | parameter | trid3nt_server/tools/search/fetch_living_atlas_layer/fetch_living_atlas_layer.py:128 | 100 | 1 |
| `gs_backend` | parameter | trid3nt_server/workflows/publishing/cog.py:373 | 100 | 1 |
| `runs_bucket_default` | parameter | trid3nt_server/workflows/publishing/cog.py:375 | 100 | 1 |

## Callable tier (confidence 60): unused functions, methods, classes

Below the 80 floor because vulture cannot distinguish a dead callable from
one reached dynamically. Treat as candidates, not verdicts.

| symbol | kind | file:line | loc |
|---|---|---|---|
| `write_fort14` | function | trid3nt_server/workflows/mesh/shared/formats/mesh_formats.py:93 | 83 |
| `mesh_quality_report` | function | trid3nt_server/workflows/mesh/shared/formats/mesh_formats.py:203 | 59 |
| `serve_user_supplied_bed` | function | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py:1693 | 35 |
| `run_gdal` | function | trid3nt_server/tools/derive/_gdal_runner.py:75 | 31 |
| `scan_third_party_imports` | function | plugin/install_dependencies.py:249 | 28 |
| `get_session_record` | method | trid3nt_server/persistence.py:572 | 27 |
| `estimate_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:231 | 18 |
| `resolve_gdal_contour` | function | trid3nt_server/tools/derive/_gdal_runner.py:36 | 18 |
| `_selection_bbox4326` | method | plugin/ui/dock.py:719 | 17 |
| `pin_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:251 | 17 |
| `upsert_session_record` | method | trid3nt_server/persistence.py:438 | 16 |
| `read_raster_bytes` | function | trid3nt_server/tools/derive/_gdal_runner.py:108 | 16 |
| `update_compute_status` | method | trid3nt_server/emission/pipeline_emitter.py:1261 | 14 |
| `update_current_progress` | method | trid3nt_server/emission/pipeline_emitter.py:1132 | 13 |
| `run_forever` | method | plugin/net/trid3nt_client.py:1600 | 12 |
| `format_number` | function | plugin/render/formatting.py:83 | 12 |
| `describe` | method | trid3nt_server/workflows/runtime/plan.py:284 | 12 |
| `decimals_for_range` | function | plugin/render/formatting.py:70 | 11 |
| `count_outputs` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:195 | 11 |
| `read_output_required` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:207 | 11 |
| `_default_corpus_path` | function | trid3nt_server/tools/search/search_tools/search_tools.py:270 | 10 |
| `_canvas_bbox4326` | method | plugin/ui/dock.py:709 | 9 |
| `_tool_chip_style` | function | plugin/ui/cards.py:69 | 8 |
| `_default_corpus_path` | function | trid3nt_server/server/protocol/catalog_http.py:42 | 7 |
| `uri_for_short` | method | trid3nt_server/emission/uri_registry.py:282 | 6 |
| `coverage_summary` | method | trid3nt_server/fallbacks/walker.py:141 | 6 |
| `read_stdout_optional` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:228 | 6 |
| `_toggle_connection` | method | plugin/ui/dock.py:1271 | 5 |
| `tripped` | method | trid3nt_server/gates/runaway_guard.py:190 | 5 |
| `_ctg_tile_bounds` | function | trid3nt_server/tools/fetchers/_router/executors/raster_cog.py:1307 | 5 |
| `with_value` | method | trid3nt_server/workflows/runtime/params.py:205 | 5 |
| `current_chart_id` | method | plugin/ui/charts_window.py:322 | 4 |
| `_round_bbox_to_6dp` | function | trid3nt_server/tools/fetchers/_router/hooks/topobathy.py:369 | 4 |
| `current_turn_drawn_geometry` | function | trid3nt_server/emission/pipeline_emitter.py:105 | 3 |
| `known_handles` | method | trid3nt_server/emission/uri_registry.py:704 | 3 |
| `_toggle_thinking` | method | plugin/ui/cards.py:871 | 2 |
| `_obj_uri` | function | trid3nt_server/tools/cache.py:220 | 2 |
| `mtime` | method | trid3nt_server/tools/fetchers/_router/transport/range_file.py:163 | 2 |
| `_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:592 | 2 |
| `_param_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:596 | 2 |

## Whitelisted false-positive classes

| rule | muted |
|---|---|
| declarative row/field DSL: read off the class by the framework | 11 |
| descriptor/typing decorator | 5 |
| dunder: interpreter-called | 4 |
| protocol/framework-called name | 14 |
| registry decorator: no static caller by construction | 4 |
| test-support hook (tests are excluded from the scavenge) | 1 |
