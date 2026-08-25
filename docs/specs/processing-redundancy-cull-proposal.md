# processing/ Redundancy Cull Proposal (FOR NATE REVIEW)

> **OUTCOME TABLE - cleanup wave phase 2, 2026-08-25.** NATE ordered six of the
> KEEP/PROTECTED rows below re-examined for demotion to the code_exec
> playground, per the `compute_zonal_statistics` precedent (ADR 0043). One
> demoted; five hold, and the reason they hold is one fact this document's
> own reason codes already encode. Verdicts, with what changed:
>
> | Tool | Phase-1 verdict | Phase-2 outcome | Why |
> |---|---|---|---|
> | `compute_urban_heat_island` | KEEP (FETCH x2 + EMIT) | **DEMOTED** | The EMIT half does not survive inspection. Its map product was the MODIS LST resampled onto the 10 m land-cover grid, painted with `style_preset="land_surface_temp_c"` - which its own source calls "the fetch_modis_lst paint". `fetch_modis_lst` paints that layer itself at NATIVE resolution and, since emission became automatic (ADR 0313), without being asked. So the recipe loses no layer, only the upsample - and losing a ~1 km measurement dressed as 10 m data is an honesty gain. What is left is per-class means + the built-minus-vegetation delta: the zonal recipe with land-cover classes as zones. `docs/playbooks/urban-heat-island-recipe.md`. |
> | `compute_change_detection` | KEEP (FETCH + EMIT) | **KEEP** | Its product is a VECTORIZED gain/loss FGB with a categorical legend, derived from a two-date NDVI/NDWI diff. No registered fetcher paints that; the playground cannot write an FGB to the object store (no network, and `_cleanup_workdir` discards the workdir unconditionally). A recipe would return a JSON blob where the tool returns a map. |
> | `compute_flood_depth_damage` | KEEP (EMIT) | **KEEP** | Same shape: a painted POINT layer of per-structure damage classes with a categorical legend, sampled from a depth raster at NSI structures. The HAZUS curve itself is inlinable, the painted layer is not. |
> | `compute_sediment_yield` | KEEP (FETCH x3 + EMIT) | **KEEP** | Same shape (a styled log-class RUSLE COG), plus a hard cross-module import: `emission/publish.py:540` reads `SEDIMENT_YIELD_LOG_CLASSES` + `hex_to_rgba` from this module as the single source of truth for its render classes. Deleting it breaks the publish styling ladder. If it ever demotes, the table moves to `emission/quantity_styles.py` FIRST (ledger row, cleanup wave phase 2). |
> | `compute_model_residuals` | PROTECTED-VNV | **KEEP** | Protected lane, NATE-scoped, and not merely by label: it paints a diverging per-point residual layer AND carries the USGS pcode unit-family reconciliation that is the honesty floor of the V&V lane. `extract_model_at_observations` defines itself by reference to it ("this tool omits the residual on purpose"), so deleting the residuals half leaves its sibling describing itself against a dead name. |
> | `compute_exposure_summary` | KEEP (FETCH + session store) | **KEEP** | The only one of the six that clears the EMIT gate (it returns a plain dict). It fails a different one: `compose_case_report.py:354` imports `get_session_exposure`, reading a Case-keyed in-memory session store this tool populates. A `code_exec_request` return value cannot repopulate a store in the agent process - the sandbox is a separate, network-denied process. Demoting deletes the exposure section of every Case report, and the `else` branch that replaces it names the tool that would no longer exist. This is the cross-check's original ruling (§ Resolved disagreements 1), re-confirmed. |
>
> The load-bearing fact, stated once: **the playground cannot paint.** The
> sandbox runs under `--unshare-net` with a workdir destroyed in a `finally`,
> and `sandbox_executor.convert_result` discriminates on `chart` / `dataframe` /
> `scalar` / `json` / `repr` - a LayerURI-shaped dict returns as an inert JSON
> blob. `compute_zonal_statistics` was demotable in ADR 0043 precisely because
> it was tabular; the `EMIT` reason code in the table below is therefore a hard
> gate, not a preference, and five of the six carry it.

Branch `refactor/engine-doors`. Read-only synthesis over 5 per-tool audit batches +
`processing-redundancy-report.md` (prior art) + `processing-decloud-refactor.md` (queued).
Scope: `agent/tools/processing/` only (40 registered tools + 2 register-nothing infra modules).
CULL STANDARD: a tool dies only with live replication evidence on remaining surfaces; function
never lost; usage is never a cut criterion. This doc proposes verdicts and gates; it runs no
live replication (read-only agent), so every DEMOTE/MERGE below is a CANDIDATE pending NATE's gate.

## Headline

The folder is essentially at its redundancy floor. Of the prior report's 15 cull candidates,
**8 are already deleted**, **2 were elevated to PROTECTED-VNV**, and **5 are held by the
chart-interactivity contract** - zero of the prior 15 remain live cull candidates. This audit
surfaces exactly **one new DEMOTE candidate** (`compute_zonal_statistics`) and **one MERGE**
(`clip_raster_to_bbox` -> `clip_raster_to_polygon`). Everything else is genuinely irreducible.

Confirmed ABSENT (prior candidates already folded/removed since the report): clip_vector_to_polygon,
merge_features, cut_features_with_polygon, fill_gaps, compute_overtopping, compute_wave_nomograph,
compute_terrain_profile, analyze_affected_fields. Stale name references to these still linger (see
Corpus / hygiene).

## Verdict tally + LOC accounting

| Verdict class | Tools | LOC | Cullable now? |
|---|---|---|---|
| KEEP-PRIMITIVE (irreducible) | 28 | 16,048 | No |
| KEEP-PRIMITIVE (interactivity-blocked, chart) | 5 | 1,537 | No (unblock = new generic chart primitive) |
| KEEP-PRIMITIVE (merge pair, both stay functional) | 2 | 1,165 | Consolidation only (~300 LOC dedup) |
| PROTECTED-VNV | 4 | 3,635 | No (NATE-scoped) |
| DEMOTE-TO-PLAYGROUND (candidate, gated) | 1 | 809 | Only after gate passes |
| **Total registered tools** | **40** | **23,194** | ~809 LOC net cullable |
| Infra (register 0 tools) | 2 | 1,448 | No |

Only `compute_zonal_statistics` (809 LOC) is a true removal candidate. The merge nets ~300 LOC of
dedup while keeping both capabilities. The 5 chart tools (1,537 LOC) are cullable only if a generic
interactive-chart primitive is registered first (a net-new tool, out of cull scope).

## Per-tool verdict table

Reason codes: EMIT=code_exec cannot paint a LayerURI; FETCH=self-fetches external data (no network
in sandbox); ALGO=algorithm outside sandbox sanctioned imports; HUB=shared primitive imported by
other modules; CASE=reads live in-session Case state; LIVE=live map-command side-effect;
LIB=imported as a Python library by a workflow/composer; CORRECT=correctness gap on the alternative
surface; CHART=interactive-Vega contract; VNV=protected calibration/V&V lane.

| Tool | LOC | Verdict | Reason |
|---|---|---|---|
| aggregate_claims_across_sources | 685 | KEEP | LIB (model_groundwater imports its private helpers; curated tables drift-prone in sandbox) - borderline, see OQ5 |
| clip_raster_to_bbox | 600 | MERGE -> clip_raster_to_polygon | EMIT; bbox = rectangular special case; merge pre-empts decloud gdal-subprocess rewrite |
| clip_raster_to_polygon | 565 | KEEP (merge target) | EMIT; canonical geographic-clipping-pattern (fetch_admin->clip->publish) |
| compute_aspect | 370 | KEEP | LIB (run_elmfire direct import) + EMIT |
| compute_blended_composite | 597 | KEEP | EMIT (MapLibre has no client-side raster multiply-blend) |
| compute_building_density | 774 | KEEP | FETCH (MS Global ML footprints) |
| compute_canopy_height | 608 | KEEP | Batch ML dispatch (no sandbox ML runtime); INERT until env flip |
| compute_change_detection | 804 | KEEP | FETCH (S2 STAC) + EMIT (categorical legend) |
| compute_colored_relief | 490 | KEEP | EMIT (cached raster LayerURI) |
| compute_contours | 609 | KEEP | ALGO (gdal_contour marching-squares, no sandbox equiv) + EMIT |
| compute_cross_section | 745 | KEEP (interactivity-blocked) | CHART (playground yields rasterized PNG, loses dual-axis tooltips) |
| compute_exposure_summary | 525 | KEEP | FETCH (population+buildings) + session store consumed by compose_case_report [ruled vs reader] |
| compute_flood_depth_damage | 609 | KEEP | EMIT (painted point LayerURI + categorical legend) |
| compute_flood_extent_skill | 533 | PROTECTED-VNV | VNV (categorical H/F/CSI; complementary to skill_metrics) |
| compute_hillshade | 734 | KEEP | HUB (GDAL/COG helpers imported by 6+ tools) + EMIT |
| compute_home_range_kde | 741 | KEEP | ALGO (scipy gaussian_kde + skimage contour, not sanctioned) + EMIT |
| compute_idf_curve | 324 | KEEP | FETCH (NOAA Atlas 14 PFDS, open_world) |
| compute_impervious_surface | 561 | KEEP (weakest) | EMIT; simplest math in folder, survives on emission gate only - re-examine if a batch-reclass primitive is ever built |
| compute_layer_bounds | 424 | KEEP | LIVE (map-command zoom-to via current_emitter); canonical bbox-math replacement |
| compute_model_residuals | 861 | PROTECTED-VNV | VNV (per-point residuals + USGS pcode unit-family honesty) |
| compute_movement_trajectory | 774 | KEEP | CORRECT (pyproj.Geod ellipsoidal; DuckDB ST_Azimuth/ST_Distance are planar) + EMIT |
| compute_ndvi | 495 | KEEP | FETCH (S2 STAC/SAS-sign) + EMIT |
| compute_sediment_yield | 769 | KEEP | FETCH x3 (DEM+soils+landcover) + EMIT (styled log-class COG) |
| compute_skill_metrics | 714 | PROTECTED-VNV | VNV (continuous NSE/KGE/PBIAS via spotpy) |
| compute_slope | 364 | KEEP | LIB (run_elmfire direct import feeds ELMFIRE topo grid) + EMIT |
| compute_urban_heat_island | 611 | KEEP | FETCH x2 (MODIS LST+landcover) + EMIT (styled COG) |
| compute_zonal_statistics | 809 | DEMOTE-TO-PLAYGROUND (candidate) | tabular (no EMIT), 0 real importers, doctrine's literal zonal example; gated, see below |
| delineate_watershed | 367 | KEEP | ALGO (pysheds D8 flow routing, not sanctioned) |
| digitize_water_body | 625 | KEEP | FETCH (S2 PC STAC NDWI) + EMIT |
| enhance_satellite_image | 705 | KEEP | EMIT (pixel math composable, but paints a LayerURI) |
| extract_landcover_class | 527 | KEEP | EMIT + read-through cache contract other tools rely on |
| extract_model_at_observations | 1527 | PROTECTED-VNV | VNV (datum/quantity reconciliation gates) |
| extract_stream_network | 288 | KEEP | ALGO (pysheds D8 + hand-written junction tracer) |
| extract_timeseries_at_point | 361 | KEEP | CASE + web LayerPanel frame-token contract |
| generate_choropleth_legend | 184 | KEEP (interactivity-blocked) | CHART |
| generate_damage_distribution | 184 | KEEP (interactivity-blocked) | CHART |
| generate_histogram | 181 | KEEP (interactivity-blocked) | CHART |
| generate_time_series | 243 | KEEP (interactivity-blocked) | CHART |
| query_point_hazard | 415 | KEEP | CASE (samples every Case raster at a point; no sandbox Case access) |
| spatial_query | 892 | KEEP | fold TARGET, not a candidate - read-only SQL guard + materialize-to-LayerURI |
| charts_common.py (infra) | 1202 | KEEP (no verdict) | register-0; single Vega-Lite builder for 4 tools + ~10 engine postprocessors |
| _hydrology_common.py (infra) | 246 | KEEP (no verdict) | register-0; shared pysheds conditioning for 2 hydrology tools |

## Resolved disagreements (cross-check vs reader)

1. **compute_exposure_summary** - reader=COMPOSED_WRAPPER (fold candidate), cross-check=IRREDUCIBLE.
   RULED cross-check. Reader conceded the two fetches are separately-registered so the LLM could
   call them first, but the decider is that `compose_case_report.py` imports `get_session_exposure`
   and reads a Case-keyed in-memory session store this tool populates; a code_exec return value
   cannot repopulate that store. Demoting breaks situation reports. -> KEEP-PRIMITIVE.
2. **4 generate_* chart tools + compute_cross_section** - reader=PLAYGROUND_EXPRESSIBLE,
   cross-check=effectively KEEP. RULED cross-check on the CULL STANDARD. code_exec's
   matplotlib->PNG path emits a rasterized image-mark Vega-Lite spec, not tooltip/point interactive
   marks; the qgis-plugin `trid3nt.ui.charts` panel + `qt_charts_harness` lock the interactive
   shapes. Folding loses the interactive function and breaks the plugin harness. -> KEEP
   (interactivity-blocked); unblock = register a generic interactive-chart primitive first.
3. **compute_zonal_statistics** - reader=PLAYGROUND_EXPRESSIBLE (cleanest fold), cross-check=holds
   only for pre-staged handles. RULED SPLIT. Reader is right it is the single genuine demote
   candidate: verified tabular output (no LayerURI), zero real Python importers (all ~30 hits are
   docstring cross-refs), and it is doctrine-1's literal "fetch X + fetch Y + zonal + summarize"
   example. Cross-check is right the demote is CONDITIONAL: verified (lines 248-251, 787-794) it
   self-stages arbitrary `s3://` URIs via `read_object_bytes_s3`, which code_exec cannot do. So I
   rule WITH the reader on demote-viability but BAKE the caveat into the gate below.

## DEMOTE gate: compute_zonal_statistics

Live replication recipe NATE (or a driver) runs before deletion:
1. Load a value raster + a zone vector as Case layers (so both arrive in code_exec as pre-opened
   `layer_refs` handles - the dominant workflow shape).
2. In `code_exec_request`: `geopandas.read_file(zone).to_crs(value.crs)` + `rasterio.features.rasterize`
   per polygon (or a numpy threshold mask for a raster zone) + `np.mean/sum/percentile`; assign the
   `by_zone`/`aggregate` dict to `result`. Compare numerically to the tool output.
3. Confirm the CRS-mismatch case: feed mismatched-CRS inputs and verify the playground path raises/
   guards rather than silently mis-placing zones.

NATE must accept the tool loses three things on demotion: (a) arbitrary-`s3://` self-staging (the
LLM must load inputs as Case layers first), (b) the 1h read-through cache (repeated identical
queries recompute), (c) the codified `CRSMismatchError` honesty floor (an ad-hoc script can silently
rasterize a mismatched zone). If any of these is load-bearing, KEEP instead.

## MERGE: clip_raster_to_bbox -> clip_raster_to_polygon

Both are IRREDUCIBLE (EMIT). `clip_raster_to_bbox` (gdal_translate/gdalwarp subprocess) is a
rectangular special case of `clip_raster_to_polygon` (in-process `rasterio.mask`). Merging into one
rasterio-based tool with an optional `bbox` convenience arg removes the gdal-subprocess path
entirely - which is exactly what the decloud refactor would otherwise have to rewrite for this tool.
Gate: prove `rasterio.mask` on a rectangular polygon reproduces `gdal_translate -projwin` output
(same-CRS) and the `-t_srs` reproject path. Net ~300 LOC dedup; no capability lost.

## Sequencing vs the approved processing-decloud refactor

**Recommend: CULL/MERGE FIRST, then decloud the survivors.** Argument:
- The decloud refactor has byte-identity gates (identical registered names/docstrings/corpus).
  Running it over a tool about to be demoted (`compute_zonal_statistics`) or merged
  (`clip_raster_to_bbox`) wastes the identity freeze and forces a re-freeze. Do the small
  redundancy change first to shrink decloud's surface by 2 modules.
- `compute_zonal_statistics` rolls its own rasterio+numpy (no gdal subprocess), so it is NOT in
  decloud's shared-GDAL-runner family - its demote is fully orthogonal, either order works, but
  first is cheaper.
- The clip_raster merge is the one point of contact with decloud: `clip_raster_to_bbox` IS a
  gdal-subprocess tool decloud's raster-runner touches. Landing the merge first (which replaces the
  subprocess with `rasterio.mask`) accomplishes decloud's goal for that tool and deletes it from
  decloud's worklist. So the merge should land before or be co-scheduled with decloud, never after.
- A single unified freeze wave is only justified if NATE wants one review window; the two
  workstreams have different lenses (redundancy judgment vs mechanical byte-identity), so ordered
  phases keep reviews clean. I recommend ordered, not unified.

## Corpus re-homing + hygiene plan

- `compute_zonal_statistics` (if demoted): remove its `corpus.yaml` retrieval queries; re-home
  zonal-stats-style prompt queries to `code_exec_request` (and `spatial_query` for vector-in-vector
  cases) so routing lands in the playground. Add a decisions/ note.
- `clip_raster_to_bbox` (if merged): fold its corpus queries into `clip_raster_to_polygon`; delete
  its own corpus entries. Re-run `retrieve_visible_tools(prompt, None, 8)` to confirm bbox-style
  prompts still retrieve the merged tool.
- Dead-tool-name hygiene (clean-as-you-go, independent of any cull): strip references to removed
  tools that could nudge the LLM toward dead names - `compute_terrain_profile` in
  `agent/gates/spatial_input.py` (5 refs) + `agent/gates/cards/spatial_input.py`;
  `analyze_affected_fields` in `agent/categories.py` (lines 479, 667); `compute_wave_nomograph` in
  `compute_cross_section.py` (line 138 comment). These are stale prose, not corpus, but should be
  cleaned in the same wave.
- Chart tools: no corpus change now. IF a generic interactive-chart primitive is later registered,
  re-home the 4 chart-tool + cross_section chart corpus queries to it.

## Open questions for NATE

1. `compute_zonal_statistics` demote: accept losing arbitrary-`s3://` self-staging + 1h cache +
   `CRSMismatchError` honesty floor? Or keep it as the one non-emitting convenience primitive?
2. clip_raster merge: OK to merge bbox -> polygon into one rasterio tool (pre-empting decloud's
   bbox rewrite)? Preferred merged name/signature?
3. Chart-interactivity unblock: authorize a net-new generic interactive-chart primitive (registered
   `build_chart_payload(vega_lite_spec)`) so the 5 chart tools can later demote? Or keep the fixed-
   shape chart tools indefinitely (they stay KEEP either way)?
4. Sequencing: ordered (cull/merge -> decloud) as recommended, or a single unified freeze wave?
5. `aggregate_claims_across_sources`: mechanically PLAYGROUND_EXPRESSIBLE but a workflow imports its
   private helpers as a library and its curated tables (30 contaminants, 50-state map, confidence
   formula) would drift if re-pasted into the sandbox. I lean KEEP (module stays regardless of the
   LLM-tool question). Confirm, or demote the LLM-facing tool while retaining the module?
