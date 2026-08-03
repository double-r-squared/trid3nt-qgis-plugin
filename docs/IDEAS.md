# Rolling ideas list

Unwritten ideas captured from design discussions so they are not lost.
Append-only with dates; promote to an ADR/build item when picked up.
(NATE 2026-07-26: "keep a rolling list of these ideas because nothing has
been written yet but I don't want you to forget it.")

- 2026-07-26 DRAW-OVER-DESCRIBE: some spatial edits (local mesh refinement
  zones, breaklines, terrain patches) are easier to DRAW than to describe in
  prose - route them through the QGIS spatial-input card / user drawing, not
  LLM text args. Local quadtree-refinement polygon knob for SFINCS is the
  first case.
- 2026-07-26 KNOB COST-CLASS: manifest field per knob - file-edit (setter,
  seconds) vs rebuild (re-instantiation, full build+solve) - so agent and
  user know the price before turning.
- 2026-07-26 KNOB LEDGER + REBUILD REPLAY: decks carry their applied-knob
  state (resolved values + ops); a rebuild (mesh change) replays the ledger
  onto the new deck; knobs are declared in PHYSICAL/geographic terms (values,
  polygons, laws - never mesh-native cell indices) so they survive mesh
  changes; any knob that cannot re-apply is REPORTED, never silently dropped.
- 2026-07-26 KNOB-MANIFEST SEARCH: when manifests outgrow tool schemas
  (SWMM deepening), a BM25+dense index over per-template knob manifests -
  discovery two-stage: find knob, then generic setter call.
- 2026-07-26 DESCRIBE_MODEL PRIMITIVE: enumerate an imported archive's
  structure (plans/geometries/scenarios/meshes) as dataframes - the
  ras-commander RasPrj pattern; needed the moment we download a USGS archive.
- 2026-07-26 SMART EXECUTION SKIP: staleness detection on template solves
  (only re-run decks whose inputs changed) - steal from ras-commander v0.88.
- 2026-07-26 ENGINE-NATIVE OBSERVATION POINTS: SFINCS obsfile / TELEMAC
  gauge sections are uncovered - free timeseries-at-points from the engine
  itself; feeds mode-B pairing without rasterization.
- 2026-07-26 SFINCS DIAGNOSTICS DEDUP: diagnostics/sfincs.py reimplements
  raw netCDF parsing that hydromt_sfincs read_results already provides.
- 2026-07-26 HONEST TEMPLATE LIMITATION ENVELOPE: ADR 0025 rule needs
  implementation - typed limitation naming what the template cannot express
  + pointing at the escape hatch (authoring flow / manual QGIS edit).
- 2026-07-26 OCEANMESH SCOPING: oceanmesh (Python port of OceanMesh2D) for
  new unstructured coastal domains - not needed while replaying published
  meshes.
- 2026-07-26 RAS-COMMANDER MCP STUDY: read gpt-cmdr/ras-commander-mcp's tool
  surface before designing our HEC-RAS surface; possibly consume directly.
- 2026-07-26 META-TEMPLATE SCAFFOLDING: the shared tool/template scaffolding
  (stage -> solve -> wait -> postprocess -> emit, asyncio.to_thread seams)
  could itself become a meta-template so byte-similar engine families
  (MODFLOW-style) stop duplicating it. NATE: hesitant because not all
  engines follow this structure - explicitly NOT NOW, backlogged; revisit
  after the engine-door refactor proves the per-engine shapes.
- 2026-07-27 DOOR RRF BOOST: dense-channel scores drown door tools in RRF
  fusion for some phrasings even when BM25 ranks the door #1 (2 known
  dewatering cases); as doors carry more routing weight across engine
  slices, consider a door-tier fusion boost or channel weighting - design
  properly if the pattern recurs in other engine slices.
- 2026-07-28 PLUGIN UPDATE v2 (REVISED to QGIS-NATIVE, NATE): daemon serves
  plugins.xml + plugin zip on the catalog HTTP port; clients add the repo URL
  ONCE in QGIS Plugin Manager, then QGIS natively handles update discovery /
  notify-on-startup / one-click install / version filtering - zero custom
  client code, plugin<->daemon version match free. v1 button keeps the DEV
  niche only (sync the live working checkout incl. uncommitted changes).
  Lands post-hygiene-sweep. LESSON: check native platform affordances before
  building custom (NATE 2026-07-28).
- 2026-07-28 MODFLOW TEMPLATE GROWTH (NATE source): mine
  modflow6-examples.readthedocs.io notebook examples (USGS-official, ~70
  documented problems w/ flopy deck code) as published-first template
  candidates - each becomes workflows/modflow/<template>/ (deck recipe +
  knob manifest + corpus), door discovers automatically. Post-wave work;
  candidate spread: GWF/GWT/GWE/CSUB/PRT families.
- 2026-07-28 LANDLAB TEMPLATE GROWTH (NATE source): mine the official Landlab
  tutorial notebooks (landlab.readthedocs.io tutorials index) as
  published-first template candidates for run_landlab - overland flow
  (the contract's noted free folder-add), flow routing, landscape evolution,
  erosion/deposition, ecohydrology; component recipe + knob manifest each.
- 2026-07-28 GEOCLAW TEMPLATE GROWTH (NATE source): mine LeVeque's geoclaw
  tsunami tutorial (rjleveque.github.io/geoclaw_tsunami_tutorial - the
  engine author's own) for run_geoclaw templates: dtopo tsunami sources,
  propagation + AMR inundation, fgmax/fgout grids, gauges; doubles as the
  canonical-setup grounding the tsunami fix-chain lesson demanded.
- 2026-07-28 ELMFIRE TEMPLATE GROWTH (NATE source): mine elmfire.io (official
  docs/tutorials/verification cases) for run_elmfire templates - ignition/
  wind/fuel-moisture scenario family; the verification suite's published
  expected outputs double as future calibration-lane replication targets.
- 2026-07-28 OPENQUAKE TEMPLATE GROWTH (NATE source): mine GEM's official
  hazard training (training.openquake.org/manual-hazard-training) for
  run_openquake templates + the oq-engine demos tree (docs.openquake.org demos-tutorials - complete runnable job.ini decks shipped with the engine) - classical PSHA variants, event-based hazard,
  scenario shaking, disaggregation; each exercise = job.ini recipe + knob
  manifest.
- 2026-07-28 PELICUN TEMPLATE GROWTH (NATE source): mine the NHERI SimCenter
  pelicun example gallery for run_pelicun templates - FEMA P-58 component
  assessment, HAZUS earthquake/hurricane building assessment, fragility
  variations, loss aggregation; weakest inventory engine (5 pct), biggest
  headroom.
- 2026-07-28 SWMM TEMPLATE GROWTH (NATE source): mine openswmm.org/SWMMExamples
  (curated complete .inp models) for run_swmm templates - LID/green
  infrastructure, CSO, detention/storage, water-quality washoff, pump/RTC;
  this IS the feedstock for the deferred SWMM-deepening pick (21 pct
  capability, largest uncovered surface).
- 2026-07-28 PYQGIS SANDBOX (NATE): upgrade the code-exec sandbox runtime
  from generic Python to PyQGIS-capable - platform-native outputs (styled
  layers, correct orientation for the QGIS product) instead of charts-only;
  pairs with the template-authoring flow. Scope: bindings availability in
  the sandbox env, containment implications, layer-emission seam.
- 2026-07-28 GENAI TYPES IR DECOUPLING (queued, NATE): own internal message-IR
  dataclasses (Content/Part/FunctionDeclaration/FunctionResponse), rewrite
  the 4 importers (bedrock_adapter, openai_adapter, context_budget,
  adapter remnants), drop google-genai - the last GCP-era dependency.
- 2026-07-28 VERTEX TEST-HARNESS MIGRATION (queued, NATE): replace the
  vertex-shaped test fakes (conftest MODEL_PROVIDER=vertex pin + 28 files
  faking build_client/generate_content_stream chunks) with a first-class
  fake-provider seam; then remove the Vertex generate path from adapter.py
  (typed UnsupportedModelProviderError already guards dispatch).
- 2026-07-28 /OPT/GRACE2 IMAGE-REBUILD JOB (queued, NATE): the last deadname
  stratum - container-baked /opt/grace2 paths (solver, geoclaw, swan,
  sandbox_hardening), ELMFIRE batch job-def + ECR names (live AWS - never
  rename in place; new-name + cutover), test env pins; retag + rebuild +
  env overrides as one job.
- 2026-07-29 MCP INTEROP (NATE ask): (a) TRID3NT-as-MCP-server - thin dynamic
  bridge over TOOL_REGISTRY via the official mcp SDK (schemas already exist;
  ~200 lines); HARD PART = gate/confirm-card mapping for MCP clients that
  cannot render our QGIS cards (elicitation or headless policy first).
  (b) MCP as a router SOURCE TYPE - an mcp executor so source specs can
  point at MCP servers as endpoints (ras-commander-mcp = the HEC-RAS
  motivator). No MCP inside the current LLM loop - would add, not reduce,
  boilerplate. Persistence's "MCP-shaped" naming = wave-C rename, not MCP.

- 2026-07-29: numpy DEM-product swap (hillshade/slope/aspect/color-relief as pure-wheel numpy stencils/LUT) - Mac-test-gated follow-on to processing-decloud-refactor; would remove the system gdal-bin machine prerequisite entirely. Contour vertex drift is the hard part.
- 2026-07-30: QGIS-native integration trio (from the PyQGIS piping discussion):
  (1) CREDENTIALS FOLD - QgsAuthManager (encrypted store + native UI + authcfg
  tokens) becomes the single credential home; plugin brokers server-consumed
  secrets over the existing auth_handshake/secrets_handler WS seam at connect
  (localhost transit = accepted in the one-user monolith, record as decision).
  Kills custom credential storage/UI. Near-term candidate.
  (2) DISPLAY-SERVICES POOL - 4th stratum of the pools architecture: public
  WMS/WMTS/XYZ endpoints as catalog entries (provider + uri-template +
  authcfg) surfaced via one generic load_service_layer verb, executed
  natively by the plugin (QgsRasterLayer/QgsVectorLayer providers). Display
  lane only - analysis lane stays on the router. Queued behind the 3 signed
  strata.
  (3) CLIENT-EDGE OBSERVABILITY HARVESTER - plugin hooks QgsMessageLog
  messageReceived + QgsNetworkAccessManager reply signals, scoped around
  every layer add (incl. published MinIO artifacts), reports structured
  outcomes over WS (HTTP status + provider messages). Closes the
  render-failure blind spot (layer-poison class); strengthens the honesty
  floor at the client edge. Standalone, no pool dependency.

## 2026-08-03 - Loud, user-gated cross-dataset fallbacks (NATE)
Split fallback classes: same-data mirrors (identical dataset, different host)
may fail over silently; CROSS-DATASET substitution (different resolution or
measurement method, e.g. fetch_dem 3DEP 1-10m lidar -> Copernicus GLO-30 30m
radar) must be loud and user-gated - silent substitution degrades map integrity
while looking like success. Mechanism: honest typed error naming the substitute
as the suggested retry arg (source enum), through the tool-retry loop so the
agent narrates the tradeoff (the #154 granularity-gate pattern). Consequence:
gating fetch_dem's auto-ladder dissolves its biggest fold blocker (ADR 0090
cross-tool provenance restamp) and simplifies topobathy's fallback_warning
field (ADR 0089). Blast radius: 8 nested terrain consumers (flood, topobathy,
contours, elmfire, geoclaw, swmm, landslide) would pause-and-ask mid-scenario
instead of silently degrading - lands as its own gated wave with the flood
canary, not a rider.

## 2026-08-03 - Charts window: TUFLOW Viewer pattern + template output accounting (NATE)
Charts never surface inline in chat. They get their own window: a
TUFLOW-Viewer-like dock - horizontal, at the BOTTOM of the app window,
interactive maps/displays linked to the canvas (click a feature -> its
plot; scrubber/time integration). The charts button STAYS in chat and
clicking it SHOWS/raises the window (entry point, not container).
Plugin work item: replace the collapsible "Charts (N)" panel under the
message list (ui/charts.py) with a bottom-area QDockWidget the chat
button toggles. Paired template rule: every template accounts for its
output shape (map raster / graph / both) and its graphs are more-or-less
COPIED from the cited published example (modflow6-examples is chart-rich)
- a template wave without its example's charts is incomplete.
