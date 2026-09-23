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
| `default_seconds` | parameter | trid3nt_server/gates/confirm.py:86 | 100 | 1 |
| `parse_qs` | import | trid3nt_server/render/uri_registry.py:18 | 90 | 1 |
| `unquote` | import | trid3nt_server/render/uri_registry.py:18 | 90 | 1 |
| `raw_user_text` | parameter | trid3nt_server/server/dispatch/emitter.py:849 | 100 | 1 |

## Callable tier (confidence 60): unused functions, methods, classes

Below the 80 floor because vulture cannot distinguish a dead callable from
one reached dynamically. Treat as candidates, not verdicts.

| symbol | kind | file:line | loc |
|---|---|---|---|
| `_summarize_raster` | function | trid3nt_server/render/charts.py:132 | 57 |
| `estimate_mb` | function | trid3nt_server/tools/payload_sampling.py:106 | 37 |
| `_summarize_vector` | function | trid3nt_server/render/charts.py:190 | 33 |
| `_write_geojson` | function | trid3nt_server/tools/derive/_hydrology_common.py:347 | 29 |
| `scan_third_party_imports` | function | plugin/install_dependencies.py:249 | 28 |
| `get_session_record` | method | trid3nt_server/persistence.py:569 | 27 |
| `legend_key` | function | trid3nt_server/render/presets.py:607 | 20 |
| `estimate_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:231 | 18 |
| `_selection_bbox4326` | method | plugin/ui/dock.py:633 | 17 |
| `pin_fetch_resolution` | function | trid3nt_server/gates/cards/solver_confirm.py:251 | 17 |
| `upsert_session_record` | method | trid3nt_server/persistence.py:435 | 16 |
| `update_current_progress` | method | trid3nt_server/render/pipeline_emitter.py:1087 | 13 |
| `format_number` | function | plugin/render/formatting.py:83 | 12 |
| `describe` | method | trid3nt_server/workflows/runtime/plan.py:297 | 12 |
| `decimals_for_range` | function | plugin/render/formatting.py:70 | 11 |
| `count_outputs` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:195 | 11 |
| `read_output_required` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:207 | 11 |
| `_default_corpus_path` | function | trid3nt_server/tools/search/search_tools/search_tools.py:270 | 10 |
| `_canvas_bbox4326` | method | plugin/ui/dock.py:623 | 9 |
| `coverage_summary` | method | trid3nt_server/fallbacks/walker.py:141 | 6 |
| `uri_for_short` | method | trid3nt_server/render/uri_registry.py:278 | 6 |
| `read_stdout_optional` | method | trid3nt_server/workflows/solver/diagnostics/_common.py:228 | 6 |
| `tripped` | method | trid3nt_server/gates/runaway_guard.py:190 | 5 |
| `_ctg_tile_bounds` | function | trid3nt_server/tools/fetchers/_router/executors/raster_cog.py:940 | 5 |
| `with_value` | method | trid3nt_server/workflows/runtime/params.py:216 | 5 |
| `current_chart_id` | method | plugin/ui/charts_window.py:285 | 4 |
| `storage_scheme` | function | trid3nt_server/tools/cache.py:268 | 4 |
| `known_handles` | method | trid3nt_server/render/uri_registry.py:705 | 3 |
| `overrides_domain` | method | trid3nt_server/workflows/runtime/plan.py:265 | 3 |
| `_obj_uri` | function | trid3nt_server/tools/cache.py:274 | 2 |
| `mtime` | method | trid3nt_server/tools/fetchers/_router/transport/range_file.py:163 | 2 |
| `_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:1467 | 2 |
| `_param_refs` | function | trid3nt_server/workflows/runtime/interpreter.py:1471 | 2 |

## Whitelisted false-positive classes

| rule | muted |
|---|---|
| declarative row/field DSL: read off the class by the framework | 3 |
| descriptor/typing decorator | 6 |
| dunder: interpreter-called | 4 |
| protocol/framework-called name | 14 |
| registry decorator: no static caller by construction | 4 |
| test-support hook (tests are excluded from the scavenge) | 1 |
