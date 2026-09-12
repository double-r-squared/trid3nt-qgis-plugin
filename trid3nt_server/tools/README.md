# `tools/` - the atomic-tool surface

Every tool the model can call is registered here at import time. A tool is one
of two kinds: a fetcher produces data from outside - a declared source spec the
router executes - and a derive tool ingests data and outputs data - a function.
Search and the meta tools stand beside them as infrastructure. The registry and
the cache shim are the two seams they all pass through.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The registry: the `@register_tool(metadata)` decorator, `TOOL_REGISTRY`, and the sorted snapshot the agent loop builds its tool declarations from. |
| `cache.py` | The cache shim - content-addressed read-through / write-on-miss, the sole writer of the cache bucket's `cache/` prefix. |
| `_example_tool_template.py` | A complete, working, trivial registered tool to copy when starting a new one. |
| `payload_sampling.py` | Sampled payload-size estimation, so the size a warning quotes is measured rather than modelled. |
| `tool_arg_normalizer.py` | Call-site kwargs cleanup, so an invented argument does not fail a call the tool could still take. |
| `tool_query_corpus.yaml` | The routing phrasings tool retrieval scores an ask against. |
| `_uri_util.py` | The layer-uri query-strip helper, shared by the tools that read a published layer's object. |
| `vector_tiles.py` | The dense-vector seam: simplify, cap and round a FeatureCollection before it is attached to the inline-GeoJSON emit path. |

## Subfolders

| folder | what it is |
| --- | --- |
| `derive/` | The simulation-automation tools - domain geometry, observation pairing and skill, the analytic overlays, point reads, restyle - one folder per tool, flat, plus the shared GDAL, geometry, hydrology and chart cores; and the two session tools (`run_qgis_algorithm`, `run_pyqgis`), each a request the plugin runs in the user's QGIS session. A derive tool takes a layer and never fetches. |
| `fetchers/` | Data fetchers, one folder per phenomenon measured (`climate`, `hazard`, `hydrology`, `imagery`, `ocean`, `socioeconomic`, `soil`, `terrain`, `weather`), plus the shared helpers at its root and `_router/`. See below. |
| `meta/` | Utility tools: `compose_case_report`, `list_run_frames`, `spatial_input_tool`. |
| `search/` | Dataset and tool discovery: `search_living_atlas` and `fetch_living_atlas_layer` over the harvested Living Atlas, `search_tools` retrieval, the OGC adapter and `web_fetch`. |

## `fetchers/` - the router and its shared root

| entry | what it is |
| --- | --- |
| `fetchers/_fetch_common.py` | The typed fetch errors and bbox helpers every fetcher shares. |
| `fetchers/_public_s3.py` | Anonymous access to public AWS S3 buckets, independent of the caller's credentials. |
| `fetchers/us_states.py` | US state and NWS area-code resolution, shared by the alert fetchers. |
| `fetchers/_router/router.py` | The router engine: a declared spec plus the ask to a request, a response and a typed layer. |
| `fetchers/_router/spec.py` | The source-spec loader - schema validation, co-located corpus pickup, tree walk. |
| `fetchers/_router/registration.py` | Promotion: a spec becomes a registered tool with a synthesized signature and schema. |
| `fetchers/_router/emit_on_fetch.py` | Surfacing a fetched INPUT as a `role=context` layer through the emission seam. |
| `fetchers/_router/errors.py` | The router's typed-error hierarchy over the shared fetch bases. |
| `fetchers/_router/shape_classifier.py` | The one classifier for what shape a response came back in. |
| `fetchers/_router/executors/` | How a request is actually run: HTTP JSON, raster COG, vector FlatGeobuf, zipped vector, station timeseries, library delegates, animation frames. |
| `fetchers/_router/hooks/` | The hook contract (`RequestPlan`, `register_hook`, `resolve_hook`) and the modules SEVERAL specs share; the loader walks both this folder and the co-located `hooks.py` files. |
| `fetchers/<group>/<spec>/hooks.py` | One spec's own `build_request` / `parse_response` overrides - what the spec cannot declare, beside the spec, registered by the tree walk. |
| `fetchers/_router/transforms/` | Post-fetch shaping: `fan_out`, `join`, `tiled_mosaic`. |
| `fetchers/_router/transport/` | The HTTP client, opener, staged and range-read file access, zip-object reads, and their errors. |
