# TRID3NT Local roadmap — tools, docs, telemetry, self-improvement, QGIS bridge

NATE directives 2026-07-06 (queued while the pass-3 routing sweep runs). Five
tracks, ordered by leverage; each has a concrete v1 scope.

## 1. New LLM tools (brainstorm -> backlog)

Grouped by effort. "Free" = no API key, works offline-adjacent (public HTTP).

### Quick wins (existing deps, inputs already fetchable)

- **model_debris_flow** -- pfdf (USGS post-fire debris-flow) is ALREADY a
  vendored dependency; expose likelihood/volume/hazard as a composer over
  fetch_mtbs_burn_severity + DEM + soils. High demo value, near-zero new deps.
- **compute_sediment_yield** -- RUSLE from slope (have) + soils K-factor
  (SSURGO/STATSGO, have) + landcover C-factor (have) + precip R-factor (have).
  Pure raster algebra.
- **compute_flood_depth_damage** -- standalone depth-damage-curve estimator
  (FEMA HAZUS curves) over any depth raster + building footprints; the
  lightweight cousin of the full Pelicun chain.
- **compute_flow_accumulation / delineate_watershed / extract_stream_network**
  -- pysheds or richdem over the DEM we already fetch. Fills the biggest
  hydrology-primitive gap; pairs with Landlab.
- **compute_change_detection** -- two-date Sentinel-2/Landsat diff (NDVI/NDWI
  delta, thresholded polygons). Reuses the imagery fetchers.
- **compute_rainfall_idf_curve** -- expose the full NOAA Atlas 14 IDF curve
  (lookup_precip_return_period already hits the endpoint) as a chart tool.
- **compute_urban_heat_island** -- MODIS LST (have) x landcover (have) zonal
  contrast.

### New fetchers (free, no key)

- fetch_prism_climate (PRISM monthly/normals), fetch_daymet (ORNL daily met)
- fetch_hurdat_tracks / fetch_ibtracs (historical hurricane tracks -- feeds
  coastal demos and surge ensembles)
- fetch_nwm_retrospective (National Water Model zarr on AWS Open Data --
  historical streamflow anywhere)
- fetch_gebco_bathymetry (global bathy; topobathy/CUDEM is US-only)
- fetch_ghcn_daily (station climate observations)
- fetch_smap_soil_moisture, fetch_grace_groundwater_anomaly (GRACE mascons --
  thematic + namesake), fetch_swot_water_levels (new SWOT river/lake heights)
- fetch_3dep_lidar_ept (point clouds -> DSM/DTM/canopy workflows)
- fetch_osm_buildings_3d (heights for deck.gl extrusion)

### Bigger swings (new engine or routing infra)

- **model_dam_break_scenario** -- breach hydrograph into SFINCS (or GeoClaw);
  pairs with fetch_usace_dams. Top demo candidate.
- model_storm_surge_ensemble -- HURDAT/forecast track perturbations -> surge
  envelope (SFINCS batch fan-out).
- model_wildfire_spread -- ELMFIRE (open, containerizable) = 8th engine.
- model_coastal_erosion -- XBeach (open) = 9th engine.
- compute_evac_isochrones / least_cost_path -- needs a local routing engine
  (Valhalla/OSRM container) over OSM.
- compute_viewshed, compute_solar_irradiance (GRASS/WhiteboxTools algos --
  or arrives free with the QGIS Processing bridge, see track 5).

## 2. Local documentation (match the cloud docs)

The MkDocs Material site (GRACE-2 task #211) documents the cloud stack. Rather
than a second site: add a first-class **"TRID3NT Local"** section to the same
site, sourced from this repo.

- Pages: install (make venv / binaries / docker images / ollama), architecture
  (MinIO/TiTiler/FilePersistence/local solvers diagram), model matrix +
  GRACE2_OPENAI_* env reference, engine matrix (which engine runs how locally),
  the full .env.local reference (every GRACE2_* knob with default + why),
  troubleshooting (the sweep's greatest hits: MinIO endpoint redirect, /no_think,
  warm index, docker group, loop offload).
- Source of truth stays in trid3nt-local/docs/; the GRACE-2 mkdocs.yml pulls it
  in (git submodule-free: sync_from_grace2.sh already runs the other direction;
  add a docs-sync step or mkdocs multirepo plugin).
- The 3-pass tool sweep reports become a living "tool support matrix" page
  (PASS/KEY/slow-local per tool, auto-generated from the JSONL).

## 3. Telemetry + tool-usage stats locally

Most of this exists -- the per-tool telemetry covers all tools and the
RoutingQualityDashboard is in the web build. Local gaps to close:

- Verify the dashboard's httpBase resolves to 127.0.0.1:8766 in local dev and
  the telemetry store is FilePersistence-backed (not DynamoDB-coded).
- Persist telemetry across agent restarts (append JSONL under data/telemetry/).
- Add a "stats" page/panel: per-tool call count, success rate, p50/p95 wall
  time, last error -- fed by the same store; the sweep JSONLs seed it.
- Surface the shadow-selection (recall@K) telemetry the retrieval layer already
  emits -- locally this is the K-tuning dashboard.

## 4. LLM self-improvement loop (don't repeat failing patterns)

Design sketch (v1 deliberately simple, no training):

- **Lessons store**: data/lessons.jsonl -- one lesson per entry: {trigger
  pattern, what went wrong, correction, evidence turn ids, hit count}.
- **Write side (automatic)**: when a turn contains a tool call that FAILED
  validation/typed-error and a LATER call in the same turn/case SUCCEEDED with
  corrected args or a different tool, distill the (bad -> good) delta into a
  lesson row. Also capture repeated NO_CALL-then-user-rephrase sequences.
- **Read side (per turn)**: embed the user prompt, retrieve top 2-3 relevant
  lessons, inject as a short system-prompt appendix ("Past corrections: when
  asked X, tool Y needs Z argument; do not call W for this."). Token budget
  ~200; local 16k ctx can afford it.
- **User feedback**: thumbs-down on a chat turn writes a lesson stub the user
  can annotate ("should have used the slope tool") -- the explicit feedback
  loop NATE described.
- **Guardrails**: lessons are advisory text only (never mutate schemas), capped
  store with LRU by hit count, and a settings toggle. Bench before/after with
  the pass-3 harness -- the sweep doubles as the eval set for this feature.

## 5. QGIS bridge (export local project <-> QGIS)

Ties to the standing dual-export directive (deck.gl scene + per-layer QGIS) and
becomes the natural on-ramp for the QGIS PLUGIN phase (product analysis first,
per NATE's sequencing).

- **export_case_to_qgis (v1)**: for the active case, write a GeoPackage with
  every vector layer + copy/reference the COGs, and generate a .qgz project:
  layer tree mirroring the LayerPanel order/visibility, raster styling
  translated from our TiTiler rescale/colormap params to QGIS QML, AOI as a
  bookmark. Local paths (file://) since everything is on disk -- simpler than
  the cloud variant (which would need presigned/URL layers).
- **import_qgis_project (v2)**: read a .qgs/.qgz, ingest file-based layers into
  MinIO as case layers with names/styles, set the case AOI from the project
  extent. Scope guard: QGIS-native symbology beyond simple ramps maps lossily;
  v2 imports geometry + basic styling only.
- Implementation note: writing .qgz does NOT need PyQGIS (it is XML in a zip);
  template + fill. That keeps the tool dependency-free; full PyQGIS only when
  the plugin phase lands.
- Exposed BOTH as an LLM tool ("export this case to QGIS") and a UI button.

## Suggested execution order (post pass-3)

1. Track 3 (telemetry local) -- small, unblocks measuring everything else.
2. Track 5 export_case_to_qgis v1 -- high user value, bounded scope.
3. Track 1 quick wins (debris flow, RUSLE, watershed primitives, IDF chart).
4. Track 2 docs section (rolls up 1-3's changes).
5. Track 4 lessons loop -- last, so the pass-3 harness can A/B it properly.
