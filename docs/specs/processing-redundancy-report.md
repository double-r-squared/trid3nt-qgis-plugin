# Processing-tool redundancy report ("does the same thing")

Scope: EVERY registered tool under `server/src/trid3nt_server/tools/processing/` (48 tools). FETCHERS are OUT OF SCOPE (NATE's rule). Purpose: cull candidates for a second-pass live-replication proof. A tool only gets cut AFTER its full function (envelope shape, layer emission/publishing, legend behavior, not just the math) is successfully replicated with SURVIVING tools. Branch `refactor/engine-doors`. Read-only analysis; no code changed.

STATUS (dated snapshot, corrected 2026-08-25): this report predates a cull wave that has since executed most of the CLEAR-DUPLICATE / COMPOSABLE verdicts below (ADR 0043 + cleanup wave phase 1 -- see `docs/DELETION_LEDGER.md`). At least 16 of the named tools no longer exist under `trid3nt_server/tools/processing/`: `analyze_affected_fields`, `clip_raster_to_bbox`, `clip_vector_to_polygon`, `compute_canopy_height`, `compute_overtopping`, `compute_terrain_profile`, `compute_wave_nomograph`, `compute_zonal_statistics`, `cut_features_with_polygon`, `fill_gaps`, `generate_choropleth_legend`, `generate_damage_distribution`, `generate_histogram`, `generate_time_series`, `merge_features`, and `aggregate_claims_across_sources` (that one deleted whole, module and all -- see the `aggregate_claims_across_sources` entry below). The per-tool analysis prose below is left as-authored for its historical value; the "48 tools" scope count and the "33 of 48 KEEP" summary are therefore both stale. Verify current existence against the live tree / `TOOL_REGISTRY` before acting on any verdict here.

## The four replacement surfaces and what they can actually do

- (a) another registered tool - a surviving atomic tool that already does the job.
- (b) `spatial_query` + the DuckDB spatial-functions surface - ONE read-only `SELECT` over the Case's VECTOR layers with the full `ST_*` set (`search_spatial_functions` looks a function up). Geometry-bearing results are MATERIALIZED as a painted FlatGeobuf layer (`SpatialQueryLayerURI`, generic `affected_buildings` preset). HARD LIMITS: VECTOR ONLY (a raster ref raises `RASTER_UNSUPPORTED`); NO data-driven legend (generic vector preset only); NO raster output; NO chart; NO zoom-to camera. Verified `ST_*` present in the vendored catalog: ST_Difference, ST_Union, ST_Union_Agg, ST_Intersection, ST_Intersects, ST_Within, ST_Centroid, ST_Dump, ST_InteriorRingN, ST_NumInteriorRings, ST_ExteriorRing, ST_MakePolygon, ST_Extent, ST_Area, ST_Buffer, ST_Collect.
- (c) `code_exec_request` Python playground - subprocess sandbox (rasterio/numpy/geopandas/shapely/scipy/matplotlib), inputs staged via `layer_refs`. Emits a chart-emission envelope (matplotlib Figure -> PNG wrapped in a minimal Vega image spec) OR a scalar/dict/DataFrame summary. HARD LIMITS: NO network (cannot fetch satellite/NOAA/etc.); CANNOT paint a new map `LayerURI` (there is no layer-emit path out of the sandbox - only charts/tabular); explicitly FORBIDDEN for bbox/extent math (adapter guidance -> `compute_layer_bounds`).
- (d) `qgis_process` passthrough - CURRENTLY DISABLED on-box: RUN returns an honest typed `QGIS_PROCESSING_OFFLOADED` error (`did_run:false`) unless an operator sets `TRID3NT_QGIS_ONBOX_DOCKER=on`. So a "replace with QGIS algorithm X" verdict is NOT live-viable today (blocked on the job-0308 QGIS-on-AWS-Batch lift). Any (d) replacement carries this caveat.

## Cross-cutting facts that drive the verdicts

- EMISSION is the usual irreplaceable part. A tool that returns a `LayerURI`/subclass PAINTS a raster or vector layer (the ADR-0014 dispatch seam mints an `L<n>` handle and the emit seam draws it). spatial_query paints only VECTOR query results with a generic preset; code_exec paints NOTHING; qgis_process is offloaded. So most raster-painting and legend-bearing tools are KEEP by emission alone.
- RASTER READS are categorically out of spatial_query's reach (vector-only). Any tool that samples/computes over a raster cannot be folded into spatial_query.
- INTERNAL CONSUMERS (real functional imports, not catalog refs): `compute_hillshade` exports `_gdaldem_subprocess_env`/`_translate_to_cog`/`_get_gdaldem_bin` (used by slope, aspect, colored_relief, contours, blended_composite, fetch_landcover); `query_point_hazard` exports `layers_from_case`/`resolve_case_id`/`resolve_point` (probe_point, compose_case_report); `compute_exposure_summary` exports `get_session_exposure` (compose_case_report); `extract_timeseries_at_point` exports `detect_frame_sequences` (probe_point); `compute_slope`/`compute_aspect` imported by run_elmfire; `clip_raster_to_polygon`/`clip_vector_to_polygon`/`compute_zonal_statistics` imported by model_flood_habitat_scenario (the latter two since deleted -- verify current callers before relying on this); hydrology tools share `_hydrology_common`. (`aggregate_claims_across_sources` was claimed here as a SHARED PRIMITIVE for a stated `model_groundwater` consumer that was never built -- see the deletion note below -- so it never actually blocked unregistering.) A tool other code imports is not "just an LLM tool" - unregistering it from the catalog is safe only if the module stays importable.

---

## Per-tool evidence

Legend: KIND = what reaches the map/UI. V = verdict (CLEAR-DUPLICATE / COMPOSABLE / KEEP).

### Vector geoprocessing (paint vector; spatial_query CAN reproduce - it materializes geometry results)

- clip_vector_to_polygon - clip points/lines/polys to a polygon mask (geopandas within/intersects) -> painted vector `LayerURI`. KIND: paints-vector. V: CLEAR-DUPLICATE (b). Replacement: `spatial_query(sql="SELECT f.* EXCLUDE geom, ST_Intersection(f.geom, (SELECT ST_Union_Agg(geom) FROM mask)) AS geom FROM feats f WHERE ST_Intersects(f.geom, ...)", layer_refs={feats,mask})` - the geometry result paints. Caveat: `feature_filter` becomes a WHERE on the mask; `keep_partial=False` -> `ST_Within`; INTERNAL CONSUMER model_flood_habitat_scenario imports it (unregister but keep module).
- cut_features_with_polygon - per-feature `ST_Difference` by a dissolved cutter, keep attributes -> painted vector. KIND: paints-vector. V: COMPOSABLE (b). Replacement: `spatial_query("SELECT t.* EXCLUDE geom, ST_Difference(t.geom,(SELECT ST_Union_Agg(geom) FROM cutter)) AS geom FROM target t")`. Caveat: `delete_emptied` -> add `WHERE NOT ST_IsEmpty(...)`; MULTI-promotion handled by the FlatGeobuf writer; INTERNAL CONSUMER none functional.
- merge_features - `unary_union`/dissolve selected features into one, keep one survivor's attrs -> painted vector. KIND: paints-vector. V: CLEAR-DUPLICATE (b). Replacement: `spatial_query("SELECT ST_Union_Agg(geom) AS geom, any_value(name) FROM layer [WHERE rowid IN (...)]")`. Caveat: "keep which feature's attributes" collapses to an aggregate pick.
- fill_gaps - interior rings of the union of adjacent polygons -> painted gap polygons. KIND: paints-vector. V: COMPOSABLE (b). Replacement: spatial_query with `ST_Dump(ST_Union_Agg(geom))` then `ST_MakePolygon(ST_InteriorRingN(part, gs.i))` over `generate_series(1, ST_NumInteriorRings(part))`. Caveat: multi-step SQL; verify `ST_MakePolygon` accepts the ring linestring; `max_gap_area` -> `WHERE ST_Area(...) <= x`.

### Raster clipping (paint raster; spatial_query is vector-only; qgis offloaded; code_exec cannot paint)

- clip_raster_to_bbox - `gdal_translate`/`gdalwarp` crop (+ optional reproject) -> painted raster `LayerURI`. KIND: paints-raster. V: KEEP. Irreplaceable: raster output + optional CRS reproject; qgis `gdal:cliprasterbyextent` is OFFLOADED; code_exec cannot paint. Note: overlaps clip_raster_to_polygon (bbox = rectangle) but adds reprojection - minor consolidation candidate, not a generic-surface cut.
- clip_raster_to_polygon - `rasterio.mask` crop to a polygon (+ feature_filter) -> painted raster. KIND: paints-raster. V: KEEP. Irreplaceable: raster output; INTERNAL CONSUMER model_flood_habitat_scenario; qgis `gdal:cliprasterbymasklayer` OFFLOADED.

### Raster terrain / DEM derivatives (gdaldem family - paint raster; qgis offloaded; code_exec cannot paint)

- compute_slope - `gdaldem slope` -> COG, painted raster (slope_angle_deg preset). KIND: paints-raster. V: KEEP. Irreplaceable: painted COG + preset; qgis `gdal:slope` OFFLOADED; INTERNAL CONSUMER run_elmfire.
- compute_aspect - `gdaldem aspect` -> COG, painted raster (aspect_compass_deg). KIND: paints-raster. V: KEEP. Same as slope; INTERNAL CONSUMER run_elmfire.
- compute_hillshade - `gdaldem hillshade` (5 presets, swiss_double numpy blend) -> COG. KIND: paints-raster. V: KEEP (HARD). SHARED PRIMITIVE: exports the gdaldem env/COG/bin helpers the whole family + fetch_landcover import; deleting the module breaks them.
- compute_colored_relief - `gdaldem color-relief` (4 ramps, DEM-span normalized) -> RGBA COG. KIND: paints-raster. V: KEEP. Painted RGBA raster; imports hillshade primitives; qgis `gdal:colorrelief` OFFLOADED.
- compute_blended_composite - server-side two-raster blend (multiply/screen/overlay) -> RGBA COG. KIND: paints-raster. V: KEEP. Unique server-side blend + NLCD palette colorization; code_exec cannot paint.
- compute_contours - `gdal_contour` DEM -> vector isolines. KIND: paints-vector. V: KEEP. RASTER INPUT (out of spatial_query) + painted vector; qgis `gdal:contour` OFFLOADED; self-fetches DEM for bbox path.
- compute_canopy_height - Meta HighResCanopyHeight ML inference on NAIP via AWS Batch -> painted raster. KIND: paints-raster. V: KEEP. Unique ML model + Batch dispatch; self-fetch; code_exec has no network/GPU/paint.

### Satellite / land-cover / physical-model rasters (self-fetch + paint; code_exec has no network and cannot paint)

- compute_ndvi - fetch Sentinel-2, `(NIR-Red)/(NIR+Red)` -> painted raster + ndvi legend. KIND: paints-raster. V: KEEP. Self-fetch (no-network sandbox can't) + painted raster + legend + typed no-imagery.
- compute_impervious_surface - NLCD -> impervious fraction raster (product auto-detect). KIND: paints-raster. V: KEEP. Raster input + painted raster; code_exec cannot paint.
- compute_change_detection - Sentinel-2 two-date NDVI/NDWI diff -> gain/loss vector + categorical legend (`ChangeDetectionLayerURI`). KIND: paints-vector. V: KEEP. Self-fetch + painted vector + legend + typed no-change.
- compute_urban_heat_island - MODIS LST x Esri/IO land cover -> painted LST raster + per-class metrics (`UrbanHeatIslandLayerURI`). KIND: paints-raster. V: KEEP. Self-fetch multi-source + painted raster + honest null delta.
- compute_building_density - MS Global footprints -> rasterized density grid. KIND: paints-raster. V: KEEP. Self-fetch + rasterize + painted raster.
- digitize_water_body - Sentinel-2 NDWI threshold -> water polygons. KIND: paints-vector. V: KEEP. Self-fetch + painted vector + typed no-water.
- extract_landcover_class - NLCD -> binary class mask raster. KIND: paints-raster. V: KEEP. Raster input + painted raster; code_exec cannot paint.
- enhance_satellite_image - de-haze/white-balance/sharpen/upscale RGB -> painted raster. KIND: paints-raster. V: KEEP. Painted RGB raster; exports pure passes but no external importer; code_exec cannot paint.
- compute_sediment_yield - RUSLE `A=R*K*LS*C*P` -> painted soil-loss raster + log-scaled legend (`SedimentYieldLayerURI`). KIND: paints-raster. V: KEEP. Domain model + painted raster + legend; playground could recompute but cannot paint.

### Raster-reading analysis (sample/compute over a raster - out of spatial_query's vector-only reach)

- compute_zonal_statistics - aggregate raster values within raster/vector zones -> tabular dict. KIND: tabular. V: KEEP. This IS the designated raster companion spatial_query points to on `RASTER_UNSUPPORTED`; INTERNAL CONSUMER model_flood_habitat + analyze_affected_fields; a core primitive, not an ad-hoc analysis.
- compute_exposure_summary - population sum + building count + area inside a hazard footprint -> tabular. KIND: tabular. V: KEEP. Raster population sum (spatial_query can't) + per-component honest degrade; SHARED PRIMITIVE `get_session_exposure` (compose_case_report).
- query_point_hazard - sample EVERY Case raster at one point -> tabular. KIND: tabular. V: KEEP. Multi-layer raster probe; SHARED PRIMITIVE `layers_from_case`/`resolve_case_id`/`resolve_point` (probe_point, compose_case_report).
- compute_model_residuals - bilinear-sample a model raster at obs points, residuals -> painted vector + diverging legend (`ModelResidualsLayerURI`). KIND: paints-vector. V: KEEP. Raster sampling + painted layer + legend + datum reconciliation.
- extract_model_at_observations - pair model raster/time-series with observations (datum/units/quantity reconciliation) -> painted paired vector (`PairedObsLayerURI`). KIND: paints-vector. V: KEEP. Raster sampling + painted layer + typed datum/quantity-mismatch honesty; feeds compute_skill_metrics.
- extract_timeseries_at_point - sample each animation-frame raster at a point -> tabular time series. KIND: tabular. V: KEEP. Multi-frame raster sampling; SHARED PRIMITIVE `detect_frame_sequences` (probe_point).
- compute_flood_depth_damage - HAZUS depth-damage curve at structure points from a depth raster -> painted damage vector + categorical legend (`FloodDepthDamageLayerURI`). KIND: paints-vector. V: KEEP. Raster sampling + HAZUS curve + painted layer + legend.
- analyze_affected_fields - plume raster x FTW field polygons, ranked -> tabular readout (composes compute_zonal_statistics internally). KIND: tabular. V: COMPOSABLE (c). Replacement: `compute_zonal_statistics(plume, fields, [max,mean])` then rank/join in code_exec (or spatial_query for the join once zonal per-field values exist). Caveat: threshold consistency with `postprocess_modflow` PLUME_DETECTION_FLOOR_MGL and deterministic ranking must be re-encoded; no layer painted (pure readout) - this is the "analysis is playground, not a tool" pattern.

### Hydrology (D8 over a DEM raster -> painted vector; shared `_hydrology_common`)

- delineate_watershed - pysheds D8 upstream catchment -> painted watershed polygon (`WatershedLayerURI`). KIND: paints-vector. V: KEEP. Raster D8 + painted polygon + shared hydrology primitives; qgis grass/native watershed OFFLOADED.
- extract_stream_network - pysheds D8 accumulation threshold -> painted stream lines (`StreamNetworkLayerURI`). KIND: paints-vector. V: KEEP. Raster D8 + painted lines + shared hydrology primitives.

### Movement / KDE (vector in/out but needs scipy KDE or geodesic per-segment; and it paints)

- compute_home_range_kde - scipy gaussian_kde UD isopleths from tracking fixes -> painted isopleth polygons. KIND: paints-vector. V: KEEP. KDE has no DuckDB equivalent; code_exec has scipy but cannot paint.
- compute_movement_trajectory - per-segment geodesic speed/bearing/turn-angle from timestamped points -> painted trajectory lines. KIND: paints-vector. V: KEEP. Geodesic ellipsoid + turn-angle + painted lines; a spatial_query window-function port is a stretch and still could not carry the metrics onto a painted layer with a preset.

### Charts (chart-emission via charts_common; code_exec matplotlib can also emit a chart)

- generate_histogram - 10-bin distribution of a raster/vector field -> Vega bar chart. KIND: chart. V: COMPOSABLE (c). Replacement: code_exec `layer_refs={L}` -> numpy histogram -> matplotlib bar -> `result=fig`. Caveat: charts_common emits an INTERACTIVE Vega-Lite spec; code_exec emits a rasterized PNG-in-Vega (UX downgrade); self-fetch handled by the staged layer_ref.
- generate_time_series - per-band mean or vector time column -> Vega line chart. KIND: chart. V: COMPOSABLE (c). Replacement: code_exec rasterio per-band mean / vector sort-by-time -> matplotlib line. Caveat: same Vega-vs-PNG fidelity downgrade.
- generate_damage_distribution - pelicun `ds_mean` binned to DS0..DS4 -> Vega bar chart. KIND: chart. V: COMPOSABLE (c). Replacement: code_exec read fgb `ds_mean`, round/clip to DS0..DS4, matplotlib bar. Caveat: DS-binning logic + `MISSING_DAMAGE_COLUMN` guard must be re-encoded; Vega-vs-PNG downgrade.
- generate_choropleth_legend - quantile class breaks + per-class counts -> Vega bar chart. KIND: chart. V: COMPOSABLE (b+c). Replacement: spatial_query for vector quantile counts (`quantile_cont`/`NTILE`) then a code_exec bar, or fully code_exec. Caveat: raster input path needs code_exec (spatial_query vector-only); Vega-vs-PNG downgrade.
- compute_idf_curve - fetch NOAA Atlas 14 PFDS + 19x10 IDF multi-line chart. KIND: chart. V: KEEP. The NETWORK FETCH of the full Atlas14 matrix is the irreplaceable part (code_exec sandbox has NO network); no surviving tool returns the full duration x ARI matrix (lookup_precip_return_period returns point return periods, not the chartable matrix).

### Section / profile (sample raster along a line -> chart)

- compute_terrain_profile - sample a DEM (+ extra layers) along a line -> elevation long-profile chart. KIND: chart. V: CLEAR-DUPLICATE (a). Replacement: `compute_cross_section(layer_uri=DEM, line=..., extra_layer_uris=...)` - the generic sample-along-line-and-chart tool; terrain_profile is its DEM-specialized twin (same rasterio-sample-along-line + Geod-distance + charts_common core). Caveat: keep cross_section as the survivor.
- compute_cross_section - sample raster value(s) along a line -> cross-section chart. KIND: chart. V: COMPOSABLE (c) [preferred survivor of the pair]. Replacement (if the whole family goes): code_exec rasterio sample-along-line + pyproj Geod + matplotlib. Caveat: the per-raster CRS-reproject correctness guard (4326 stations -> raster CRS) must be reproduced or the profile is wrong; Vega-vs-PNG downgrade. Recommendation: keep ONE of {cross_section, terrain_profile}; cut terrain_profile first.

### Closed-form domain formulas (scalar dict; pure math, no layer/chart/network)

- compute_wave_nomograph - USACE SPM 1984 fetch-limited wave growth (Hs, Tp) -> scalar dict. KIND: scalar. V: COMPOSABLE (c). Replacement: code_exec implementing eqns 3-33/3-34/3-35. Caveat: transcribing SPM eqns per-call is error-prone; a validated primitive - low-value cut.
- compute_overtopping - EurOtop 2018 mean overtopping discharge -> scalar dict. KIND: scalar. V: COMPOSABLE (c). Replacement: code_exec implementing eqns 5.10/5.11. Caveat: same transcription-risk; low-value cut.

### Text / camera / substrate

- aggregate_claims_across_sources - deterministic regex claim reconciliation + source-agreement scoring over fetched news text -> tabular dict. KIND: tabular. V (as-written): KEEP - not a spatial op (none of b/c/d fit); at the time this was claimed as a SHARED PRIMITIVE (`_extract_contaminants` for modflow contamination), but that consumer was never built. DELETED (module and all, commit 0ff5231f, cleanup wave phase 1): NATE had already deregistered it as an LLM tool in ADR 0043 and kept the module alive only for the never-built consumer; once confirmed dead the whole module + its test went with it. The `ClaimSet`/`NumericClaim` contract it once fed stays live (a different producer now owns it). Any news-claim reconciliation today is playground composition (`code_exec_request`), not a library import -- see `docs/playbooks/frame-animation-recipe.md` Recipe C.
- compute_layer_bounds - EPSG:4326 extent of a raster OR vector AND EMIT a `map-command(zoom-to)` camera fit -> dict. KIND: map-command. V: KEEP. Irreplaceable: the zoom-to camera emission (spatial_query `ST_Extent` returns numbers but moves no camera and is vector-only; code_exec is EXPLICITLY FORBIDDEN for bbox/extent by adapter guidance).
- spatial_query - the read-only SQL substrate itself. KIND: tabular or paints-vector. V: KEEP. It is surface (b); it already folded three older analytical tools.

---

## KEEP summary

33 of 48 tools are KEEP. The category is mostly irreplaceable because the SURVIVING generic surfaces cannot reproduce EMISSION: spatial_query paints only vector query results with a generic preset (no rasters, no data-driven legends), code_exec paints NOTHING (charts/tabular only), and qgis_process is DISABLED on-box (honest offloaded error) so it cannot execute any replacement live. Layered on top: raster-reading tools are categorically outside spatial_query (vector-only); several are self-fetching data sources and the code_exec sandbox has NO network; and a dozen are shared primitives imported by workflows/other tools, so they are not merely LLM tools. The cut candidates are exactly the tools whose full function (INCLUDING emission) a surviving surface can reproduce: vector geoprocessing that spatial_query materializes as a painted layer, chart/analysis/formula tools the code_exec playground reproduces, and one intra-catalog duplicate (terrain_profile ⊂ cross_section).

## Candidates (CLEAR-DUPLICATE / COMPOSABLE) - the compact cull list

| tool | verdict | replacement (one line) | caveat (one line) |
|---|---|---|---|
| clip_vector_to_polygon | CLEAR-DUPLICATE | spatial_query ST_Intersection/ST_Within over feats+mask; geometry result paints | INTERNAL CONSUMER model_flood_habitat imports it - unregister but keep module |
| merge_features | CLEAR-DUPLICATE | spatial_query SELECT ST_Union_Agg(geom) [WHERE rowid IN ...] | "keep which feature's attrs" collapses to an aggregate pick |
| compute_terrain_profile | CLEAR-DUPLICATE | compute_cross_section(layer_uri=DEM, line=...) - the generic twin | cut this one, keep cross_section as survivor |
| cut_features_with_polygon | COMPOSABLE | spatial_query ST_Difference(t.geom, ST_Union_Agg(cutter)) keeping t.* | delete_emptied -> WHERE NOT ST_IsEmpty(...) |
| fill_gaps | COMPOSABLE | spatial_query ST_Dump + ST_MakePolygon(ST_InteriorRingN over ST_NumInteriorRings) | multi-step SQL; verify ST_MakePolygon takes the ring |
| compute_skill_metrics | COMPOSABLE | code_exec numpy NSE/KGE/PBIAS/RSR/RMSE from paired arrays | re-encode Moriasi verdict + NaN->None honesty floor |
| compute_flood_extent_skill | COMPOSABLE | code_exec rasterize benchmark + 2x2 confusion -> HR/FAR/CSI | reads rasters (not spatial_query); nodata exclusion |
| analyze_affected_fields | COMPOSABLE | compute_zonal_statistics(plume,fields) then rank/join in code_exec | re-encode plume threshold floor + deterministic ranking |
| generate_histogram | COMPOSABLE | code_exec numpy histogram -> matplotlib bar (layer_refs) | interactive Vega -> rasterized PNG UX downgrade |
| generate_time_series | COMPOSABLE | code_exec per-band mean / time column -> matplotlib line | same Vega-vs-PNG downgrade |
| generate_damage_distribution | COMPOSABLE | code_exec bin ds_mean to DS0..DS4 -> matplotlib bar | re-encode DS binning + MISSING_DAMAGE_COLUMN guard |
| generate_choropleth_legend | COMPOSABLE | spatial_query quantile counts (vector) or code_exec -> bar | raster input needs code_exec; Vega-vs-PNG downgrade |
| compute_cross_section | COMPOSABLE | code_exec rasterio sample-along-line + Geod + matplotlib | must reproduce per-raster CRS-reproject guard; keep-if-only-one |
| compute_wave_nomograph | COMPOSABLE | code_exec USACE SPM 1984 eqns 3-33/3-34/3-35 | transcription-risk; validated primitive, low-value cut |
| compute_overtopping | COMPOSABLE | code_exec EurOtop 2018 eqns 5.10/5.11 | transcription-risk; validated primitive, low-value cut |
| run_model_flood_habitat_scenario (workflow, appended per NATE) | COMPOSABLE | model_flood_scenario + fetch_wdpa_protected_areas + compute_zonal_statistics (+ optional clip_raster_to_polygon/clip_vector_to_polygon) | loses CaseOneResult envelope + case_summary_text; model narrates from the zonal dict; per-species GBIF fetch becomes explicit calls |

### run_model_flood_habitat_scenario - full recipe (pre-decided by NATE)

The composer (`workflows/sfincs/model_flood_habitat_scenario`) is orchestration sugar over already-registered SURVIVING tools; each underlying tool paints its own layer via its own `LayerURI`, so no bespoke envelope is needed:

1. `sfincs_flood(bbox, rainfall_event)` -> flood-depth `LayerURI` (the surviving modeling workflow; paints the depth raster itself).
2. `fetch_wdpa_protected_areas(bbox, designation_filter?)` -> habitat/protected-area polygon layer (paints itself). Optionally `fetch_gbif_occurrences(species_key, bbox)` per species for occurrence points.
3. `compute_zonal_statistics(value_raster=<flood depth layer>, zone_input=<wdpa layer>, statistics=["mean","max","count"])` -> per-polygon flood-depth impact dict (the "flood result x habitat-polygon intersection" step; SURVIVING raster-zonal tool). The LLM narrates worst-hit habitats from this dict.
4. Optional place clip: `clip_raster_to_polygon(<flood layer>, place_polygon)` and `clip_vector_to_polygon(<wdpa/species layers>, place_polygon)`.

Caveat: replacing the composer drops its deterministic `CaseOneResult` + `case_summary_text`; the model composes the narrative from step-3 metrics instead (the "analysis is playground, not a tool" norm). All four underlying tools survive, so this is a genuine cut once the multi-call sequence is live-proven.
