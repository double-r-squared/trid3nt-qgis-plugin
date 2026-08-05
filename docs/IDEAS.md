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

## 2026-08-03 - USER TOOL BUILDER (NATE, tabbed for later integration)
A tool-builder feature: create custom tools on the side to fill gaps in
the tool surface. The feature captures EVERYTHING a regular tool needs
(registration metadata, docstring w/ front-loaded routing block, corpus
queries, typed errors, payload estimate, cache class) so a user-authored
tool is a first-class citizen. Authoring shapes: ask the AI to build it,
or import one. Custom tools live in separate "user" subfolders under the
tools/workflows trees. They REUSE existing components: call fetchers
(TOOL_REGISTRY / route()), trigger popup cards for user input (the
INPUT_REQUIRED gate seam), emit charts/layers via the standard envelopes.
STATUS: idea only - do NOT build the feature yet. IMMEDIATE deliverable:
custom-authoring documentation living at the tool/workflow dirs (a
docs/authoring/custom-user-tools.md + README pointers in the trees)
describing the full tool contract + reusable seams, written AFTER the
door dissolution (it must document the post-dissolve structure).

## 2026-08-05 - Real-lakes ensemble recipe (NATE + orchestrator discussion)
"Where are the real lakes" is a data-composition question, not a Landlab
question: reconcile (1) fetch_nhd_waterbodies polygons (authoritative),
(2) satellite-observed water (NDWI / JRC Global Surface Water occurrence
- a GSW source.yaml would be a cheap router addition), (3)
landlab_lake_mapping closed basins (potential impoundments only; existing
pools are flat in the DEM and invisible by construction). Ship as a
documented playground recipe first; promote to a composed template with
per-lake provenance (mapped / observed / potential) only if it earns it.
Landlab chains themselves need NO change - fill-and-route treats real
lakes and noise pits identically BY DESIGN and that is correct for
routing; the discrepancy only ever mattered at the reporting boundary.

## 2026-08-05 - Drawn-geometry supply path for gated spatial knobs (NATE)
Spatial knobs the model must not place blindly (AMR refinement windows,
mesh refine_regions, future drawn structures like walls/dams) should be
suppliable by DRAWING in the QGIS plugin (rectangle/polygon on canvas)
-> arrives as basis=user input through the input-review gate, replacing
the model's prompt_interpreted proposal. Server-side gate wiring for
geoclaw amr_regions lands with ADR 0147; the plugin draw affordance +
WS plumbing is the follow-on leg. Pairs with the mesh layer's
refine_regions component and the user-drawn-structures ideas.

## 2026-08-05 - GeoClaw eta (wave anomaly) product layer (NATE, via Clawpack gallery)
The canonical Clawpack plots color sea-surface ANOMALY (eta, diverging
blue-white-red about 0) - far more legible for wave propagation than
depth. Candidate product addition: emit eta frames (h+B - sea_level)
alongside the depth frames for the tsunami templates, styled with a
diverging ramp in QGIS, feeding the time-animation norm (wave arc
visibly propagating on the scrubber). Proof-side the style is already
adopted (ADR 0147 wave); this idea is the EMITTED-LAYER half.
General norm captured in memory: where an engine has a canonical
published plot style, proofs replicate IT rather than improvising.

## 2026-08-05 - geoclaw-landspill engine candidate (NATE)
barbagroup/geoclaw-landspill (JOSS, BSD-3): GeoClaw fork for pipeline-
rupture oil overland flow (point sources, Darcy-Weisbach, evaporation,
temp-dependent viscosity, inland-waterbody contact). Engine-ADJACENT
landing: rides the existing geoclaw worker/docker pattern, fetch_dem
3DEP, fetch_nhd_waterbodies; completes the hazmat class as the surface
complement to the MODFLOW-GWT plume track. Published-first sources:
JOSS paper (verify) + bundled cases (utah-flat-maya). Natural chart:
volume ledger (spilled/evaporated/on-land/in-waterbody) over time.
Awaiting NATE roadmap placement.

## 2026-08-05 - Strict worker spec parsers (from the ADR 0148 lesson)
Every services/workers/<engine> build-spec parser should HARD-ERROR on
unknown fields instead of silently ignoring them. The stale geoclaw
image silently dropped manning_coefficients/amr_regions and two
registered knob templates ran as no-ops. A strict parse would have
failed the very first live smoke with a loud unknown-field error.
Sweep candidate: all worker parse_build_spec/parse_* entry points +
a shared strict-parse helper.

## 2026-08-06 - Offline-suite hermeticity: mock the Atlas-14 lookup
NOAA PFDS went down (301 to /cgi-bin/new/ + 503) and 9 "offline" tests
failed (test_urban_flood_publish_offloop x7 +
test_swmm_two_card_sim_observability x2) because they exercise the
LIVE lookup_precip_return_period path. Follow-on: monkeypatch the
design-storm lookup in those tests (the honest-gate behavior itself
already has dedicated coverage); sweep for other live-endpoint
dependencies in the offline suite. Also: when NOAA settles, check
whether the /cgi-bin/hdsc/new/ -> /cgi-bin/new/ redirect is permanent
and update _ATLAS14_PFDS_URL at the source.
