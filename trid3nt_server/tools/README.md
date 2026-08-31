# `tools/` - the atomic-tool surface

Every tool the model can call is registered here at import time. A fetcher is a
declared source spec the router executes; a processing tool is a function; the
registry and the cache shim are the two seams they all pass through.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The registry: the `@register_tool(metadata)` decorator, `TOOL_REGISTRY`, and the sorted snapshot the agent loop builds its tool declarations from. |
| `cache.py` | The cache shim - content-addressed read-through / write-on-miss, the sole writer of the cache bucket's `cache/` prefix. |
| `_example_tool_template.py` | A complete, working, trivial registered tool to copy when starting a new one. |
| `duckdb_spatial_functions.json` | The DuckDB spatial-function reference `search_spatial_functions` answers from. |
| `payload_sampling.py` | Sampled payload-size estimation, so the size a warning quotes is measured rather than modelled. |
| `tool_arg_normalizer.py` | Call-site kwargs cleanup, so an invented argument does not fail a call the tool could still take. |
| `tool_query_corpus.yaml` | The routing phrasings tool retrieval scores an ask against. |
| `_uri_util.py` | Layer-uri helpers shared by tools that resolve a case layer's uri to its underlying file. |
| `vector_tiles.py` | The densify / PMTiles seam a dense vector layer takes instead of the inline-GeoJSON emit path. |

## Subfolders

| folder | what it is |
| --- | --- |
| `display/` | Tools that change what the canvas SHOWS rather than what it holds: `restyle_layer`, `show_nexrad_radar`. |
| `fetchers/` | Data fetchers, one folder per phenomenon measured (`biodiversity`, `climate`, `hazard`, `hydrology`, `imagery`, `ocean`, `socioeconomic`, `soil`, `terrain`, `weather`), plus the shared helpers at its root and `_router/`. See below. |
| `meta/` | Utility tools: `code_exec_tool`, `compose_case_report`, `list_run_frames`, `spatial_input_tool`. |
| `processing/` | Compute / clip / extract / vector-edit / chart tools, one folder per tool, flat, plus the shared GDAL, geometry, hydrology and chart cores. |
| `search/` | Dataset and tool discovery: the YAML catalog tools, the Living Atlas index, the OGC adapter, `search_tools` retrieval and `web_fetch`. |

## `fetchers/` - the router and its shared root

| entry | what it is |
| --- | --- |
| `_fetch_common.py` | The typed fetch errors and bbox helpers every fetcher shares. |
| `_public_s3.py` | Anonymous access to public AWS S3 buckets, independent of the caller's credentials. |
| `us_states.py` | US state and NWS area-code resolution, shared by the alert fetchers. |
| `_router/router.py` | The router engine: a declared spec plus the ask to a request, a response and a typed layer. |
| `_router/spec.py` | The source-spec loader - schema validation, co-located corpus pickup, tree walk. |
| `_router/registration.py` | Promotion: a spec becomes a registered tool with a synthesized signature and schema. |
| `_router/emit_on_fetch.py` | Surfacing a fetched INPUT as a `role=context` layer through the emission seam. |
| `_router/errors.py` | The router's typed-error hierarchy over the shared fetch bases. |
| `_router/shape_classifier.py` | The one classifier for what shape a response came back in. |
| `_router/stratified.py` | The stratified data pool the router reads a pooled source through. |
| `_router/executors/` | How a request is actually run: HTTP JSON, raster COG, vector FlatGeobuf, zipped vector, station timeseries, library delegates, animation frames. |
| `_router/hooks/` | Per-source `build_request` / `parse_response` overrides - one file per source that needs more than the spec can declare. |
| `_router/transforms/` | Post-fetch shaping: `fan_out`, `join`, `tiled_mosaic`. |
| `_router/transport/` | The HTTP client, opener, staged and range-read file access, zip-object reads, and their errors. |
