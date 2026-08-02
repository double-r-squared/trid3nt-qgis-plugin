# 0043 - processing/ redundancy cull wave (charts + clip + zonal + aggregate)

Context: `docs/specs/processing-redundancy-cull-proposal.md` (NATE-approved) found
the `agent/tools/processing/` folder at its redundancy floor with four approved
actions. Cull doctrine held throughout: a tool dies only with LIVE replication
evidence on retained surfaces (deterministic grading), function is never lost,
and corpus intents re-home with a model-free `retrieve_visible_tools(prompt,
None, 8)` proof. Executed on branch `refactor/engine-doors` (2026-07-29).

Decision (four actions):

1. GENERIC CHART PRIMITIVE + 4 CHART CULLS. Registered ONE new tool
   `generate_chart` (charts/generate_chart/) exposing
   `charts_common.build_chart_payload(vega_lite_spec, title, caption)` with data
   injection from inline `records` or a `layer_uri`, emitting the SAME
   `ChartEmissionPayload` envelope. Interactivity is guaranteed BY CONSTRUCTION:
   every mark is normalized to `tooltip: true` and `image` (rasterized-PNG) marks
   are rejected (`IMAGE_MARK_REJECTED`) -- the honesty floor that distinguishes it
   from the code_exec matplotlib->PNG path. NEW-TOOL GATE passed (corpus.yaml +
   model-free top-8 retrieval). Per-tool replication gate passed live for
   `generate_histogram` / `generate_time_series` / `generate_damage_distribution`
   / `generate_choropleth_legend`: each reproduced an equivalent INTERACTIVE chart
   (bar/line marks, tooltip true, NOT image) whose per-bin/step/state counts
   matched the original tool EXACTLY. The 4 tool folders + tests + categories +
   corpus were deleted; intents re-homed onto `generate_chart/corpus.yaml`. The
   ~10 `charts_common` engine-postprocessor builders are LIBRARY calls, untouched.
   The plugin renders the same envelope by mark type (`ui/charts.py`) -- no plugin
   code change (verified).

2. CLIP MERGE. Folded `clip_raster_to_bbox` into `clip_raster_to_polygon`: the
   polygon tool gained `bbox` / `bbox_crs` / `target_crs` params (exactly one of
   polygon_uri/bbox), building a rectangular polygon and running the SAME
   in-process `rasterio.mask` path -- the gdal_translate/gdalwarp subprocess is
   gone (pre-empting the decloud refactor's gdal-subprocess rewrite for this tool;
   removed from its worklist). Parity gate on a real raster: same-CRS clip is
   EXACT pixel parity with `gdal_translate -projwin` (40x40, transform + CRS +
   `np.array_equal`); the `target_crs` path reproduces `gdalwarp -t_srs` (correct
   EPSG:3857, values preserved; a benign 1-column default-grid difference).
   `clip_raster_to_bbox` folder + tests deleted; consumers + corpus re-pointed;
   bbox-style prompts still retrieve `clip_raster_to_polygon` (proven).

3. ZONAL DEMOTE. `compute_zonal_statistics` demoted to the code_exec playground.
   Replication gate (`run_user_code` on pre-staged layer handles): the playground
   rasterize+numpy path reproduced the tool's per-zone and aggregate
   count/sum/mean/min/max/median/percentile EXACTLY for a vector-polygon zone and
   a raster threshold zone, and the CRS-missing honesty floor raised in BOTH the
   tool and the recipe. `docs/playbooks/zonal-statistics-recipe.md` documents the
   recipe with the HONEST LOSSES (arbitrary-s3 self-staging; 1h read-through
   cache; codified `CRSMismatchError`) and their workarounds (stage via fetch
   tools first; keep the CRS guard). Tool + tests + categories + corpus deleted;
   zonal intents re-homed to `code_exec_request` (raster-zonal) +
   `compute_exposure_summary` (population/buildings) + `spatial_query` (vector).

4. AGGREGATE_CLAIMS DEMOTE. `aggregate_claims_across_sources` deregistered (the
   `@register_tool` decorator removed) but KEPT as an importable library --
   `model_groundwater_contamination_scenario` imports its private extractors
   (`_extract_contaminants` / `_extract_locations` / `_extract_scale`), verified
   still importing. Its phase-A news-ingest intents re-homed AGAIN onto `web_fetch`
   / `fetch_nws_event` (cross-listed to news_events) / `fetch_storm_events_db` and
   the frame-animation-recipe Recipe C (now an importable-library call in the
   playground); news intents proven to still route to a retained surface.

Consequence: registry 197 -> 191 (-4 charts, +1 generate_chart, -1
clip_raster_to_bbox, -1 compute_zonal_statistics, -1 aggregate_claims_across_sources,
each delta accounted). HOT_SET 16 -> 14 (generate_chart replaced the histogram +
time_series slots; the zonal slot retired to spatial_query + code_exec_request).
The offline suite FAILED set is unchanged (the 9 pre-existing fetch_resolution x4
+ river_dye x5). Dead-tool-name hygiene: routing-hint references to the culled
tools re-pointed across the tree.
