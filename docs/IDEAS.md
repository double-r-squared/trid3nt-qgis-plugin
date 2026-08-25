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

## 2026-08-06 - Real quadtree run to replace the fixture mesh proof (NATE)
The ADR 0159 native-mesh proof used a HAND-BUILT UGRID fixture (the
cht deck-builder worker image is absent locally, only stock
sfincs-cpu) - its haphazard block placement is fixture authorship,
not generator output. Owed: build/stage the cht_sfincs quadtree
worker image locally, run one real quadtree flood (2:1-balanced
refinement from composer criteria), regenerate the mesh proof from
the genuine sfincs_map.nc, and confirm the MDAL load in QGIS
(NATE's visual). Until then the proof folder's quadtree mesh image
is labeled a schema fixture.

## 2026-08-06 - Order-dependent SFINCSSetupError reload flake (0162 finding)
test_sfincs_autoscale.py's importlib.reload rebinds SFINCSSetupError,
breaking pytest.raises in LATER files holding a stale top-of-file
import (test_model_flood_scenario.py / _v2.py affected; the
re-fetch-at-call-time pattern in test_sfincs_spiderweb.py is the
fix). Sweep candidate when convenient.

## 2026-08-06 - Layer-emission audit (NATE norm)
NATE norm: emitted layers = map-app citizens; proofs must be
georeferenced map renders, and validation-fixture templates whose
deliverable is a chart/scalar should NOT emit orphan local-unit
rasters. AUDIT candidate: sweep the registered templates for
non-georeferenced layer emissions (modflow package_validation cases,
schism transport_validation, any local-unit fixture COGs) and either
(a) drop the layer emission in favor of charts/scalars, or (b) add a
real-AOI georeferenced mode (the 0165 capture-zone pattern). Run when
a lane frees.
AUDITED 2026-08-07 (ADR 0180): swept all 68 registered templates. NO
strict orphan exists - the norm was already upheld. The two named
suspects (modflow_package_validation, schism_transport_validation) and
every fixture/validation/comparison/schematic-deck template are
chart/scalar-only with NO map layer (10 carry explicit "no map layer"
docstrings); every map-emitting template is real-AOI georeferenced
(bbox/latlon/geocode/deck-projection/transect). Two ADJACENT follow-ups
left for NATE's call, NOT chased: (1) elmfire_verification_elliptical_
replication publishes an idealized constant-fuel ToA COG at a FIXED demo
center (Kansas -98.5/38.5) - georeferenced so not a strict orphan, but
its true deliverable is the RMSE/correlation scalar + ellipse-overlay
chart; option-(a) drop is a return-type/contract change -> NATE rules.
(2) postprocess_modflow._write_reprojected_cog SILENTLY falls back to
Affine.identity() (null-island) when the deck can't load, inconsistent
with modflow_mesh which SKIPS on geo=None; harden to a loud typed error
(or the same skip) in a modflow-hardening job WITH the MODFLOW canary.

## 2026-08-06 - Wellhead-protection track: REEVAL flag (NATE)
Nothing wrong - NATE wants CONTINUED DEVELOPMENT of the capture-zone/
wellhead surface (ADRs 0163/0165/0166). Candidate next rungs:
transient pumping schedules + multi-well interference, heterogeneous
K (SSURGO/aquifer-property data instead of uniform defaults), kriged
potentiometric surface over the 3-point plane fit (more wells = real
curvature), NHD river as a head-dependent boundary (river capture
fraction), backward-in-time contaminant source attribution, and the
permit-grade checklist (what a state wellhead-protection submission
actually requires). Revisit when NATE picks it up.

## 2026-08-06 - "Hazard" vocabulary audit (NATE identity correction)
TRID3NT = GENERAL GEOSPATIAL INTELLIGENCE, not a hazard workbench.
Audit queued: the hazard_modeling primary category (misnamed for
terrain diagnostics, validation gates, ecology-class tools), "hazard"
phrasing in docstrings/corpus/board section framing, and category
taxonomy generally - rename to question-class-honest names (the
template-capability-naming norm applied to categories). Also reframes
the scope calls: Landlab ecology/biogeography/tidal + MODFLOW GWE
geothermal are in-scope coverage, priority TBD by NATE.

## 2026-08-08 - Mesh as optional user-supplied precondition (NATE design)
Mesh creation = explicit user prompts via the standalone mesh tool
(watershed-then-mesh lives HERE, never auto-guessed inside model
templates). Model templates gain precondition polymorphism: if a
mesh artifact exists in the case -> USER GATE "use this mesh?";
accepted -> engine consumes it; declined/absent -> existing
AOI-bounded mesh authoring unchanged. Rationale: delineating within
a bbox reproduces the cut-off problem + complexity in a bbox-first
design. Basis ranking: user mesh > drawn box > geocoded AOI (same
seam family as DrawnGeometry + input-review gates). Orchestrator
addition, NATE-unruled: the gate validates engine compatibility
(format/CRS/open-boundary needs per SCHISM/TELEMAC/SWAN) and
honestly declines mismatches rather than force-fitting; standalone
tool keeps emitting all engine formats per mesh to make acceptance
the common case. Build AFTER mesh v2 (ADR 0193) lands + NATE
validates alignment.

## 2026-08-12 - Procedural pipeline LIBRARY (NATE design sketch, no build yet - NOT a DSL: library code like OpenCV, plain well-named functions composed in ordinary Python)
A thin abstraction over the existing seams making templates read as
chained verbs: load (fetchers, cached+provenance) -> gate (the
reusable envelope gates: input-review/payload/mesh) -> simulate
(run_solver dispatch) -> plot (publish_layer + LayerURI + emitter
as ONE explicit required call; charts likewise). Rationale from
evidence: today plot is three seams + an ambient emitter, and that
ambiguity produced real latent bugs (the modflow archetype family
never plotting its map; pipeline_emitter=None bypasses; dict-vs-
LayerURI auto-load). The DSL formalizes what composers already do
informally - load-before-plot becomes structural, gates become
composable pieces (already true), templates become extendable
recipes. NATE: 'I liked it to be procedural... build all the
pieces of the pipeline flexible so we can make more with the same
features.' Candidate shape: a small module wrapping the seams, one
template ported as the demonstration, adopted opportunistically.

## 2026-08-12 - Zero-dependency chart rendering (NATE: why aren't libs included?)
QGIS plugins have NO dependency mechanism; matplotlib is compiled
(unvendorable); QGIS 4 stopped bundling it. 0.3.9 = detect-and-guide
(the ecosystem best practice). Two zero-dep options if wanted later:
(a) QPainter-native chart widget (always available, real rewrite);
(b) server-rendered PNG degrade - client reports no-matplotlib, the
chart card arrives as an image (server always has mpl; fits the
loud-degrade doctrine; loses interactivity, keeps the data visible).

## 2026-08-12 - Universal target_resolution_m (DISCUSSION OPEN, do not build)
NATE design state: universal OPTIONAL knob named target_resolution_m
(decided: target = intent, not fact) on all gridded-data tools,
contract-level (shared field + sweep enforcement + generic
discoverability). Resolution model (NATE 2026-08-12 final discussion shape): NOT
rungs but two sources + one propagation rule - (1) EXPLICIT
(basis=user): target_resolution_m passed at any signature (workflows
expose the knob like any tool) and INHERITED by all inner calls
(fetch/mesh/solve) unless overridden deeper; workflows NEVER
hardcode their own resolution, they conduct the user's downward
(the surge TIN bug = the anti-pattern: a private inner resolution
ignoring the passed one). (2) SETTINGS (the single bottom layer, NATE
2026-08-12 refinement): settings.default_resolution = a declared
global value ('native' declarable) OR 'auto' = our autoscaling
method as the user's CHOSEN policy - autoscale is never an imposed
fallback, always a settings selection (reconciles the #154
suggestion+override ruling: even the automatic path traces to an
explicit user decision). Provenance records the resolving rung
(basis=user / workflow / settings(<value>|auto)). Open Qs: settings
location (server config vs QGIS-side), the inheritance MECHANISM (how inner calls receive the outer value - context object vs param forwarding; previews the pipeline LIBRARY). Vector
fetchers keep separate explicit levers. NATE: discuss MORE after the
0229 code walkthrough - "while I'm all for generics, templates, and
meta programming I want to know before I go."

## 2026-08-13 - Speculative-intervention gate (NATE design, DEFERRED until after the real-data proofs)
Generic precondition pattern, NOT per-template patchwork - the
byo-mesh/mesh-gate model generalized: some runs have an optional
INTERVENTION precondition (a speculative breakwater at the marina,
a speculative fire blocker/fuel break, a levee, a dam removal...)
that can be USER-SUPPLIED (drawn/params) or NATURALLY DERIVED
(OSM surveyed structure / the real river) - "could either be added
or derived, it really depends on what the user wants." A simple
user gate removes the ambiguity: offer the derived/natural state +
the option to add the speculative object; decline never cancels.
NATE: "our user gates kinda already cover these problems simply."
Discuss AFTER NATE reviews the real-data marina + river-jump
proofs. Candidate seam: the tool-payload/precondition gate family
(the mesh gate precedent).

## 2026-08-13 - Fetcher-owned emission + ambient reusable assets (NATE design, DISCUSS-ONLY)
1. EMIT-ON-FETCH: context-layer emission becomes the FETCHER's
   responsibility, not per-composer patchwork - one implementation at
   the router seam, gated by a declarative visualize:true flag in the
   spec YAML; all specs inherit; forgetting becomes impossible (the
   0231 audit found ~20 forgotten composer call sites). Design Qs:
   per-call suppression for internal probe fetches (the river-AOI
   harness fetched 4 candidate reaches - only the consumed one should
   surface); emitter binding (ambient vs explicit-arg, ties to the
   pipeline-library discussion). load() == fetch+emit in the library.
2. AMBIENT REUSABLE ASSETS: fetched data = first-class visible
   reusable asset, not invisible plumbing - the cache already dedupes
   and 0227/0231 layers already ride existing objects by reference;
   the paradigm makes the shared substrate VISIBLE ("the context
   layers show that the reusable data isn't invisible").
3. Fire-growth diagnostic refinement (queued): 5-min early frames in
   the growth montage so the point-source origin + early acceleration
   are visible (the triangle question - correct wind-ellipse physics,
   30-min cadence hid the birth).

## 2026-08-13 - Emit-on-fetch SETTLED SEMANTICS (NATE, discussion converged; build awaits go)
No boolean flag. The spec's RENDER DECLARATION (style_preset /
display face) IS the visualization intent: presence = the data has
a visual form and WILL be emitted wherever fetched (both calling
modes - the tool-wrapper path already honors it; the in-composer
bare-function path is the gap to close, at the shared fetch seam);
absence = the data genuinely has no visual form (records/series)
and nothing tries. "The intent lives with this decision - omitting
the param means it can't really be visualized." Per-call
visualize=False = belt-and-suspenders reserved ONLY for probe
fetches of visualizable data (AOI candidate scans); using it on
consumed data = re-hiding a layer (sweep-test policeable). The
purpose= label arg (composer contributes a word, not a pathway)
stays from the earlier discussion. This is pipeline-library brick
2 (load() = fetch + declared-emit).

## 2026-08-16 - BMI coupling + the digital-twin loop (NATE direction)
BMI (init/update/get_value/set_value) as the composition rung below
ESMF: persistent model instances stepped in time, exchanging state -
cross-engine feedback without tight coupling. Real anchors in our
images today: MODFLOW6 XMI (libmf6 already baked), SFINCS ships a BMI,
landlab is BMI-native, pywatershed queued. The digital-twin loop rides
it: a living Case bound to an AOI; new observations (CO-OPS/NWM/
gridMET/USGS series) trigger -> series library disaggregates/aligns
forcing (the melodist verb) -> model advances incrementally -> computed
vs observed -> correct. Two tiers: re-run twin (cheap engines re-solve
the window; buildable with today's machinery) vs BMI twin (state
carried, assimilation possible). Natural sequence: series library ->
single-engine BMI pilot -> twin loop -> cross-engine exchange.

## 2026-08-19 - aquifer thickness goes derivable + two recon fronts

- **Aquifer thickness fetcher (P5 queue, AHEAD of recharge - NATE ruling).**
  Verified sources: USGS Zell & Sanford 2020 CONUS 1-km surficial
  groundwater model - derived water-table depth / K / SATURATED THICKNESS
  (ScienceBase item 631405c5d34e36012efa3190, data doi 10.5066/P91LFFN1,
  paper doi 10.1029/2019WR026724; ships as Data_CONUS.zip model arrays ->
  one-time convert-and-host to COG, the staged-dataset pattern). Cross-check
  /fallback: ISRIC SoilGrids-2017 BDTICM absolute depth-to-bedrock, 250 m
  global, range-reads verified live
  (files.isric.org/soilgrids/former/2017-03-10/data/BDTICM_M_250m_ll.tif);
  thickness ~= depth-to-bedrock - water-table depth. HONEST LIMIT: surficial
  /unconfined only; confined-aquifer thickness keeps the scenario tag with a
  typed message naming why.
- **User-supplied data = TOP RUNG of every fallback ladder (NATE).** Onsite
  surveys, well logs, measured K: basis="user_supplied" rides the existing
  input-review basis machinery; ladder = user_supplied -> derived -> gate/
  refuse. Generalizes to any surveyed input; no new envelope.
- **SWMM ecosystem recon (NATE, queued after next-step kickoff).** Survey
  new developments in the engine + ecosystem (EPA SWMM 5.2.x line, pyswmm
  releases, swmmio/swmm-pandas, OWA activity) and what our SWMM surface
  should adopt.
- **3D aquifer visualization in QGIS (NATE, queued).** FloPy has 3D viz
  export (VTK path, PyVista) we do not surface; recon what QGIS can honestly
  render (3D map view, mesh layers, Qgis2threejs, voxel limits) and design
  the MODFLOW 3D output story (layer-per-raster 2.5D vs VTK mesh vs
  external viewer).

## 2026-08-20 - recharge fetcher sources verified (NATE: research + build)

- **Recharge fetcher joins the build queue WITH thickness (NATE ruling).**
  Primary: USGS Reitz et al. 2017 CONUS ~800m recharge grids, 2000-2013
  (ScienceBase parent 56c49126e4b0946c65219231, data doi 10.5066/F7PN93P0,
  paper doi 10.1111/1752-1688.12546; child item 55d383a9e4b0518e35468e58
  carries EffRecharge_<year>.zip x14 + TotalRecharge_0013.zip mean - staged
  COG conversion, same pattern as Zell-Sanford). Cross-check: Wolock 2003
  baseflow-derived 1-km rech48grd (item 63140610d34e36012efa3838, 3.6 MB) -
  methodologically independent (baseflow partition vs empirical regression),
  the BDTICM-style sanity pair.
- **network_import verdict evidence (NATE asked where the demo lives):**
  storm depth resolves user -> NOAA Atlas-14 (real, AOI) -> labeled 90.0 mm
  ONLY when the network has no AOI bbox (network_import.py:544), declared as
  SyntheticInput basis=default_demo consequence=scenario at the gate; DEM-
  interpolated inverts carry consequence=physics (auto-mode refuses). The
  inline last-rung literal migrates into a declared ladder rung
  (refuse-by-default) in fallback wave F2.

## 2026-08-20 addendum - recharge serving-endpoint probe + audit close

- Recharge live-service probe NEGATIVE (2026-08-20): the Reitz child item
  advertises ArcGIS REST + WMS distribution links but gis.usgs.gov times
  out and the sciencebase.gov OWS endpoint 404s (stale links); a MapServer
  would serve rendered images, not values, regardless. VERDICT: staged-COG
  import from the zips is the build path for both recharge and thickness.
- network_import row ACCEPTED by NATE (storm-depth ladder user->Atlas-14->
  labeled 90mm no-AOI rung, gate-declared; inline literal migrates to a
  declared refuse-default ladder rung in F2). Demo-physics audit now FULLY
  ADJUDICATED 34/34.

## 2026-08-21 - SWMM ecosystem recon: first concrete input (NATE: folded)

Agentic SWMM (Zhonghao et al., MDPI AI for Engineering, 2026-06;
github.com/Zhonghao1995/agentic-swmm-workflow, doi 10.3390/aieng1010005)
= a serious convergent single-engine framework: MCP + Skills over EPA
SWMM, QGIS preprocessing, verification-first provenance artifacts per run,
byte-identical CLI-vs-MCP parity across 60 paired sims, calibration +
Monte Carlo uncertainty.

ADOPTION CANDIDATES (the shopping list):
1. Calibration as a first-class workflow (calibrate, then batch
   precipitation-scaled climate scenarios over the calibrated model) -
   our V&V doctrine wants exactly this; unbuilt.
2. Uncertainty envelopes (Monte Carlo parameter perturbation ->
   hydrograph envelope) - echoes the uncertainty-pre-reasoning idea
   from the GeoClaw_Claude recon.
3. Climate scenario batching as a clean scenario lever.
4. Per-run experiment notes (their experiment_note.md pattern) as a
   lighter sibling of our ADRs.
5. Their parity-proof pattern (byte-identical outputs across drive
   paths) as an acceptance idiom.

STRUCTURAL POSITION (why we are not obsoleted): they are a SWMM
framework; TRID3NT is an engine framework (one contract - outputs.json,
gates, ladders - across 10 engines, cross-engine coupling the goal).
Their inputs = user files, one Canada 35-city network source, or
SWMManywhere bbox SYNTHESIS (honestly labeled, but the invent-the-world
path our law 9 refuses/gates); ours = the 95-spec real-data substrate.
Their QGIS = preprocessing library; ours = the product surface. Raw
SWMM API coverage: their discrete skills vs our playground-layer full
pyswmm control - the real gap is calibration/uncertainty WORKFLOW
classes, not API endpoints. MCP itself is orthogonal transport we could
expose later (plugin-platform goal).

## 2026-08-21 - F1d verifier side-observation (queued, not ladder work)

- TARGET_CRS default EPSG:32616 applied to Pacific-coast AOIs leaves the
  reprojected AOI covering only ~56% of its axis-aligned output grid
  (vs 97% for Gulf AOIs) - ~44% of an 8.8M-pixel COG is off-AOI padding.
  Candidate: pick UTM zone from AOI centroid. Worth a small wave.

## 2026-08-21 - honesty follow-up batch (from the ADR 0295 verify lens)

- Supervisor completion writer folds ONLY the manifest pointer; the
  worker's status/error_code verdict is still clobbered (empty-field
  honesty-gate hit -> supervisor writes ok over error; caught downstream
  as NO_RENDERABLE_LAYER, typed code lost). Fix shape: fold status/
  error_code like the pointer, or gate flood.py on manifest.status.
- GeoClaw inundation still narrates .get("max_depth_m", 0.0) - the
  zeros-never-narrate doctrine is SFINCS-only; apply the presence guard.
- test_composer_refuses_with_a_typed_code_when_metrics_are_missing
  asserts on source text, not behavior - rewrite as a behavior test.
- _discover_publish_manifest_uri bare except deserves a debug log.
- Stale prose in urban_flood.py:857,1419 (deleted-lane references);
  em dashes in emitter.py:738,799 narration strings.

## 2026-08-21 - aquifer thickness fork (ADR 0297); recharge landed

- **Recharge SHIPPED** as `fetch_groundwater_recharge` (ADR 0297): the first
  STAGED-DATASET fetcher. Reitz 2017 (~800 m, 2000-2013 total) and Wolock 2003
  (1 km, baseflow-derived natural) are both staged as validated mm/yr COGs under
  `s3://trid3nt-cache/staged/groundwater_recharge/`, selected by a `source`
  enum; their disagreement IS the uncertainty estimate. Live cross-check over
  Story County IA: reitz 166.7 vs wolock 62.3 mm/yr.
- **Aquifer thickness PARKED - the queued premise is false.** Zell & Sanford
  2020 ships NO saturated-thickness array. `Data_CONUS.zip` is five
  unsaturated-zone ancillary files (c param, rooting depth, idomain, 2 CSVs);
  the CONUS rasters live in `Output_CONUS_trans_dtw.zip` (918 MB) and are
  depth-to-water + transmissivity. The model is 250 m, not 1 km. Three
  derivation routes are open, all of them NATE's call (ADR 0297 Decision 3):
  A1 serve the published depth-to-water as `fetch_water_table_depth`;
  A2 thickness = SoilGrids BDTICM minus staged DTW (cheap, mixes an ML global
  bedrock model with a calibrated US groundwater model); A3 recover the model's
  own `b = T/K` or `top - dtw - bottom` from the 75 subdomain archives (~4 GB,
  faithful, expensive).
- Next staged dataset reuses `scripts/stage_groundwater_recharge.py`'s shape
  rather than inventing a second staging idiom.

## 2026-08-21 (later) - the thickness fork is CLOSED (ADR 0298)

- NATE ruled **A1 + A3**. Both shipped: `fetch_water_table_depth` (published
  raster) and `fetch_aquifer_thickness` (derived `b = T / K`), staged under
  `s3://trid3nt-cache/staged/zell_sanford_groundwater/`.
- A3 turned out CHEAP, not the ~4 GB the fork assumed: K is piecewise constant
  on the calibration zones, so `hk_<zone>` from `PEST_Subdomain.zip` (62 MB)
  through the zone map in `Data_Subdomain.zip` (53 MB) reproduces the model's
  `_1.hk` array EXACTLY. That also dodges an auth wall -- six of the eighteen
  `models.NN.zip` moved to S3 behind an authenticated GraphQL endpoint.
- A2 survives as a REPORTED cross-check, not a product: the derived thickness
  exceeds BDTICM-implied available thickness in 88-99% of cells across three
  AOIs, by 26-57 m. That is the clearest evidence that the model bottom is a
  PRESCRIBED zonal constant (20-170 m) rather than geology -- the honest limit
  is stronger than the "surficial/unconfined only" wording ADR 0297 recorded.
- OPEN, still no tool: thickness of any NAMED or CONFINED aquifer (Ogallala,
  Floridan, Gulf Coast). Needs an aquifer-specific hydrogeologic framework.
- QUEUED, one spec away: `fetch_aquifer_transmissivity`. The staging script
  already builds and validates the raster; it is not uploaded because
  `NormalizeSpec.quantity` is a single static stamp, so transmissivity cannot
  ride the thickness spec without mislabelling the layer.
- 2026-08-21 LANDED: NATE ruled REGISTER on the item above.
  `fetch_aquifer_transmissivity` now ships (its own spec, units m2/day, its
  own style preset). Same-run verifier review also found the "20-170 m" zone
  band cited two entries up is wrong -- CONUS-wide the true range is 5-150 m
  (a real 5 m coastal zone in LA/ME/NY/FL that a 2-degree spot-check window
  never touched); the fetchers' caveats/docstrings and `validate()`'s
  structural check are corrected in ADR 0298's amendment section, not here
  (append-only).

## 2026-08-21 - naming ruling (NATE): thickness keeps its name

fetch_aquifer_thickness stays (question-class naming rule); the honest
identity moves to the HUMAN surfaces: published layer name + legend read
"Surficial saturated thickness (modelled)". Small labeling change, next
batch. Same treatment check for water_table_depth/transmissivity layers.

## 2026-08-21 - F2b verifier observations, queued not fixed

Six non-blocking findings from the wave-F2 adversarial review, plus one stale
sandbox driver. All confirmed against the code; none touched in F2b.

- OBS 5 DEAD RUNG PARAMS: `BATHYMETRY_LADDER`'s `regional_fine` rung declares
  `params={"include_regional_fine": True}` (`topobathy.py:1861`), but an
  `enhancement` rung is never in `Ladder.alternatives` and can never be permitted
  through `fallback=`, so the walker never invokes it and never applies those
  params. Either the walker should apply an enhancement rung's params when the
  capability switches it on, or the declaration should go - a param nobody reads
  is the "no reader, no feature" rule applied to a rung.
- OBS 6 TWO DISAGREEING EXEMPTION SETS: the fetch's own gates exempt THREE params
  (`topobathy.py:1501` and `:1664-1667`: force_bathy_base, skip_cudem,
  include_regional_fine) while the ladder's `coverage_exempt_params` is now TWO
  (`:1835`, deliberately dropping include_regional_fine in F2). The two sets are
  meant to agree; today a `include_regional_fine` request skips the capability's
  coverage refusal but still gets measured rows from the walker. Decide which set
  is authoritative and derive the other from it.
- OBS 7 NAME COLLISION: `spec.endpoint_fallback` (the renamed same-data mirror
  list) and `ingest.http_source.endpoint_fallback` (the hook-plan mirror chain,
  audit row 3) are different mechanisms wearing one name. Neither is wrong; a
  reader grepping the word gets both.
- OBS 8 MARKER BRITTLENESS: the parked register's markers are string anchors.
  F2b tightened them to stable identifiers and refused whitespace-bearing
  markers, but an anchor still cannot tell a real fix from a rename, and a site
  can keep its constant while losing its defect. A semantic check (AST: does this
  default still reach a physics field with no SyntheticInput?) is the real
  answer, and is a wave of its own.
- OBS 11 OVERSIZED COASTAL BED: `generate_mesh._fetch_coastal_bed` fetches
  topobathy at native resolution - 46 MB at ~3.43 m for a mesh whose finest edge
  is 40 m and whose max is 150 m. Passing `min_pixel_m` (the geoclaw composer's
  lever) would cut the fetch, the download and the sampling cost with no effect
  on the sampled bed. Cost, not honesty.
- OBS 12 ADR EVIDENCE HYGIENE: ADR 0299 presents some evidence as claims without
  the command that produced it, and cites line numbers that shift. Convention
  question for NATE: pin evidence to a rerunnable command + a stable anchor
  (symbol name, test id) rather than a line number.
- STALE DRIVER: `scripts/sandbox/oceanmesh/build_coastal_mesh.py:91` still has
  the pre-F2 `fetch_dem`-shaped bed helper the product path deleted in ADR 0299.
  It is a sandbox driver, not product code, but it now demonstrates the shape the
  sweep guard exists to prevent.

## 2026-08-24 - wave-2 riders (NATE)

- Emit the RELEASE/OUTFALL POINT as a context vector layer (derived seed
  today, the drawn point when the draw card lands) - the source is
  physics + provenance but invisible on the map.
- Persist the chart SPEC + physical-answer metrics (do_min etc) to the
  run prefix - verification shows the product's own chart, NEVER a
  rederivation (NATE ruling 2026-08-24; the do_sag verification had to
  rederive because the payload was chat-turn-only).
- Verification/showcase runs surface as PERSISTENT showcase cases (no
  auto-cleanup - they are showcases, not smoke tests); the do_sag pair
  landed as "showcase: telemac do sag (Eel River near Scotia,
  declarative v1)" by hand - the seed driver should own this shape.

- Proof-script rider (NATE 2026-08-24, scope CORRECTED same day): the
  ANIMATION-EXPORT (frames -> GIF, basemap + the product's presets) is
  DIAGNOSTIC TOOLING ONLY - a scripts/ proof utility (eventually
  PyQGIS-true), NEVER a workflow/plan step. Product animation UX =
  QGIS Temporal Controller, unchanged. Same for layer-montage renders:
  the chat proof channel exists for NATE diagnosing from afar, and
  stays out of the workflow library. First use: river_dye's plume
  frames (wave 3 acceptance).

- Context-budget seam (NATE 2026-08-24): per-model context window
  DISCOVERED at adapter startup (OpenRouter /models context_length;
  Anthropic Models API max_input_tokens), never hardcoded; the turn
  engine manages history against the budget CLIENT-SIDE (trim/summarize
  before overflow - generic hosts do nothing for you); the anthropic
  adapter additionally opts into HOST-SIDE compaction (beta - append
  compaction blocks back). Interim: point TRID3NT_OPENAI_MODEL at a
  larger-context free model. Closes the overload class permanently.

- Two-tier testing doctrine (NATE 2026-08-24): TIER A = !run with all
  unfilled params supplied - gates SATISFIED not skipped (every row
  arrives door-USER; nothing unresolved to ask) - the mechanical
  contract/physics check, one declared line, run frequently. TIER B =
  the harness walkthrough (declared gate answers + assertions) - the
  full product-path audit, run at wave acceptance/push gates. A-green +
  B-red isolates faults to the interaction machinery; the split is
  itself diagnostic. Wave 3's design-doc touch states this in the
  Testing section.

- Three-path testing model FINAL (NATE 2026-08-24) + the mechanism-
  template fork RESOLVED: Path A = all-params-upfront !run; DEMO VALUES
  LIVE IN THE DECLARATION (a demo script IS a saved, banner-labeled
  Tier-A invocation) - never hardcoded in workflow code. This resolves
  the parked purity fork: mechanism-compare templates take deck/params
  AS INPUT, their canonical decks become demo-script declarations
  supplying them. Path B = gate-by-gate walkthrough (harness). Path C =
  NATE in QGIS - plugin-UI coverage only (A+B own the logic). SWMM
  campaign executes the fork resolution per template.

- 2026-08-24 BED BATHYMETRY INPUT LAYER 404s ON THE RIVER FAMILY (queued
  defect, found by the all-layers contact sheet): the TELEMAC worker writes
  `bed_bathymetry.tif` into its DATA dir and records `bed_cog` in
  `telemac_metrics.json`, and `steps/products._surface_bed_bathymetry_input`
  publishes a `role=context` layer at
  `s3://<runs>/<run_id>/bed_bathymetry.tif` off that record - but
  `steps/deck.stage_manifest`'s `outputs` list does not name the file, so
  the supervisor never uploads it. Both `telemac_river_dye` and
  `telemac_do_sag` therefore put a layer on the canvas whose object 404s
  (verified on fresh runs `01M0TAZNJDSTTSEQZ39GRZZP67` and
  `01M0TA46JPHRNB14FT068ZC6AM`). `coastal_tidal_surge` and `wave_field` name
  it in their own outputs lists and are unaffected; the pre-migration
  river_dye composer omitted it too, so this predates the declarative
  migration. Two parts: the one-line manifest fix, and the honesty question
  underneath it - `publish_raster_input_cog` returns True for an object that
  is not there, so an unloadable layer reads as a published one.

- dev-tool-invoke flattens raised typed errors to INTERNAL_ERROR
  (pre-existing, exposed by 3b's refused drive; the banks gate suffers
  the same) - the envelope should carry the exception's own error_code.
  Small dispatch fix, queue for the next server-touching wave.

- TELEMAC family item (NATE spot-check 2026-08-25): the bed-sampling
  Copernicus GLO-30 fetch happens IN-WORKER - outside emit-on-fetch,
  ladders, cache, and provenance. Migrate it to a declared agent-side
  Data producer (the DEM then surfaces as a canvas layer - NATE wants
  to SEE it - and gains ladder/coverage protection), worker consumes
  the staged raster. Also queue: bank polygons as a publishable layer.

- ESCALATED (NATE spot-check 2026-08-25): the in-worker Copernicus fetch
  is one instance of a CLASS - all FIVE telemac worker builders
  (river_dye, coastal, telemac3d, tomawac, artemis) make external
  fetches from inside containers (NOAA NGDC exportImage, nationalmap
  3DEP/NHD, USGS water API, Planetary Computer STAC), incl. at least
  one PRIVATE in-worker fallback ladder ("the ladder still works
  without it" - telemac_river_dye_build.py:1560ff) - all outside
  emit-on-fetch, declared ladders, cache, provenance, and the F2
  audit's denominator (server-tree only; workers were never audited).
  This is why the tomawac showcase died raw on NGDC 500s. THE TELEMAC
  FAMILY WAVE's core scope: migrate every builder fetch agent-side as
  declared Data (staged into the container), delete the private
  ladders, then a workers-wide external-fetch sweep guard. openquake/
  opengis URL hits look like doc headers - verify in the recon.

- NATE spot-check trio (2026-08-25, TELEMAC family wave items):
  (1) used_in_sim highlight - the NWM station layer marks the ingested
  dominant-reach gauge (distinct styling + discharge label), context
  gauges muted - the map says what drove the physics.
  (2) SELECT GATE - a new gate species: pick a FEATURE from a published
  layer (gauge vertex, NPDES outfall, well) -> basis=user w/ feature
  identity as provenance; auto mode = labeled default (nearest-to-seed
  refinement for discharge). Same spine as form/draw.
  (3) downstream-gauge VALIDATION check - solved outflow vs the
  downstream NWM reach per run (free computed-vs-reference honesty
  line; later the calibration objective). Plus: nearest-to-seed
  replaces largest-in-bbox in discharge_resolve; mesh-economy A/B
  (do_sag coarsening) rides the same wave.

- WORKER PURITY PRINCIPLE (NATE 2026-08-25): "the image holds
  MECHANISMS, never VALUES." All tunable worker-side constants (mesh
  target edge, accept radius, smoothing passes, node budgets, bed
  treatment...) become declared Params threaded via the deck/config -
  tunable per question class (the opinionated-mesh lever), no rebuild
  for a default change, visible on the form. Combined w/ the in-worker
  fetch migration the worker end state = A PURE EXECUTOR (data staged
  in, values fed in, mechanism only). The TELEMAC family wave executes
  both halves together; the external-fetch audit's migration plan
  targets this end state; per-engine constant inventories ride the
  engine campaigns.

- TEMPORAL DOCTRINE (NATE 2026-08-25, generalizes event_time): every
  regularly-updated/timestamped source gets (1) a time param, default
  latest, ALWAYS pinned in provenance; (2) declared temporal metadata
  (cadence, retention window, snap granularity) on the source spec;
  (3) two-tier invalid-interval resolution - in-window off-cycle SNAPS
  to nearest w/ a provenance note; out-of-retention REFUSES typed
  naming the window + the archive gap. Never a silently different
  time. Shared resolution seam (the doors idiom), not per-fetcher
  code. Dataset VINTAGE (NLCD year, DEM release) noted as the adjacent
  cousin - selectable where sources version, same pinning rule.

- TEMPORAL DOCTRINE EXTENSION (NATE 2026-08-25): cadence MISMATCH
  (6-min tides vs hourly NWM vs solver dt) -> INTERPOLATION/
  NORMALIZATION as DECLARED prep steps/modifiers (.resample(to=,
  method=)), never implicit consumer-side alignment (the wave-A
  clocks-align bug class). Rules: method + source cadence stamped in
  provenance ("6-min CO-OPS linearly interpolated to 60 s" - observed
  vs manufactured values distinguishable); within-resolution
  interpolation = refinement, across-gap = invention (refuse/gate);
  unit/datum normalization = same declared family, zero invention.
  Lands with the temporal-inventory adoption waves.

- TEMPORAL TRANSFORMS v1 BLESSED (NATE 2026-08-25): the modifier form
  (.resample(to=, method=, max_gap=) / .normalize(units=)) as designed
  - all four dials as proposed: modifier not step-line; per-quantity
  method defaults (rates=conservative, states=linear, categorical=
  nearest) overridable; max_gap default native*3; temporal-only v1.
  One shared library implementation (temporal.py), three surfaces
  (form badge, provenance transform stamp, typed gap refusal). First
  consumer: the migrated SWMM templates' declared forcing (the wave-A
  clock-mismatch site becomes the proving case). Queue: behind
  event_time + the beacon kill.

- SEQUENCING RULED (NATE 2026-08-25): after temporal transforms v1 ->
  THE TELEMAC FAMILY WAVE(S): in-worker fetch migration to declared
  Data via manifest staging, private DEM ladder deleted, worker-purity
  constants, false-surface river fix, mesh economy + coarsening A/B,
  the gauge trio (used_in_sim / select gate / downstream validation),
  discharge DERIVED-door, event_time family-wide, remaining 4
  templates onto shared steps (family net-LOC verdict due). SWMM
  B/C/D after. --network none endorsement still open - pose at the
  TELEMAC kickoff (it is the natural definition-of-done).

- SKELETON REFACTOR DEMOLITION CLAUSE (NATE 2026-08-25): this is a
  GENERALIZATION refactor - absorbed functionality is DELETED outright,
  NO backward compatibility: no dual paths, no deprecation shims, no
  transition aliases. The composite dies (not deprecated); pass-through
  signatures deleted; old tool bodies replaced by the registration
  factory and removed; dotted-string chart builders -> function refs
  with no string fallback; data/simulation shims deleted as engines
  migrate. DISTINCTION: interfaces/wiring owe nothing to the past -
  PHYSICS ANSWERS still owe parity (R3 stands: same question -> same
  answer; "no back compat" is about API shape, never about results
  drifting). Ledger rows tell each removal's story.

- NO-DOUBLE-MIDDLEWARE LAW (NATE 2026-08-24, skeleton discussion): fetcher
  tool invocations are treated as DATA; the fetcher router's existing
  middleware (cache, fallback ladders, provenance, staleness, typed
  refusals) is authoritative and lives ONCE. No step tier, engine facade,
  or skeleton stage re-implements or re-wraps it at a different level of
  abstraction - the acquire stage INTERPRETS DATA declarations, it never
  fetches.

- SKELETON NAMING RULING (NATE 2026-08-24): base class = Workflow (the
  abstract skeleton); engine facades = TelemacWorkflow / SwmmWorkflow /
  ModflowWorkflow - engine name only, NO domain qualifiers ever ("Reach"
  rejected: it welded a domain assumption into the engine facade; domain
  shape arrives through acquire_domain slots + shared domain steps).
  Template files DECLARE workflows; the class IS one - the apparent name
  collision is coherence. Analysis-only templates ride the same skeleton
  and simply leave solve-family slots unfilled.

- PUBLISH_LAYER TOOL KILLED (NATE 2026-08-24, ruling b): emission becomes
  automatic on ALL three paths - processing-primitive rasters auto-emit
  their outputs (intermediates included: they are useful input checks;
  the user hides what they don't want). Mechanism (styling seam,
  _resolve_titiler_style_params, overview enforcement, registration)
  moves OUT of the tool file into emission/ as the single home; the
  registered publish_layer tool is then DELETED (NATE: "something I've
  been wanting to do for a while but always somehow survives").

- MESH RULING COMPLETE (NATE 2026-08-24): mesh = BYO-optional DATA -
  AUTHORED (user-supplied, e.g. 2dm import; top ladder rung) or GENERATED
  (shared front workflows/mesh/, default). Default generated policy =
  OPINIONATED TOWARD SPEED (fast normal baseline, never a slow optimized
  guess); mesh-economy A/B calibrates it. BOTH paths converge at the MESH
  GATE (pending-confirmation spine, preview surface) where USER-DRIVEN
  refinement happens via atomic mesh tools (refine-region, densify,
  coarsen) acting on the ENGINE-NEUTRAL artifact (ESMF-shaped: neutral
  artifact + thin per-solver writers). Cross-engine translation = writer
  within a mesh SPECIES (unstructured tri: TELEMAC/SCHISM/HEC-RAS 2D);
  across species (structured MODFLOW grids, SWMM node-link) = typed
  refusal, never silent conversion. EngineOps.build_mesh(domain, policy)
  is the frozen interface; strategies/writers evolve behind it. TELEMAC
  campaign folds the private corridor mesher into the shared front as a
  generation strategy; private ladder dies (ledger).

- SKELETON HARDENING METHODOLOGY (NATE 2026-08-24, mid-launch): INSIDE-OUT,
  SMALL COHORT FIRST. Wave 2 builds Workflow + TelemacWorkflow and migrates
  ONLY do_sag + river_dye; iterate in short loops against NATE's taste
  until it meets his expectations, THEN the fleet migrates. During
  hardening the un-migrated fleet has NO operability/testability
  obligation - it may go dark; its tests may sit red/skipped on a TRACKED
  list; broken-by-hardening templates are fixed by their own migration
  wave, never chased mid-iteration. Gates during hardening = cohort
  canary + library/core tests + NATE redline checkpoints; the full-suite
  baseline re-binds at fleet-migration completion. Rationale: converge
  the skeleton quickly on a few real workflows instead of dragging 30
  templates through every revision.

- SKELETON LOC LEDGER (NATE 2026-08-24): every skeleton-campaign wave
  records LOC before/after in docs/validation/skeleton-loc-ledger.md -
  per surface (lib skeleton / facade+steps / templates / deleted), delta,
  running campaign net, reproducible counting command in the header, and
  a one-line honest verdict per wave. Purpose: bloat detector +
  removed-what-it-superseded check + measures whether the generalization
  is making a real difference. Wave 2 writes row 0 (pre-change baseline)
  + its own rows.

- TEMPLATE FILE READABILITY PRINCIPLE + COHORT LGTM (NATE 2026-08-24):
  cohort taste review PASSED ("it's in fine form"). Principle going
  forward: the template FILE is the plan + supplementary declarations
  (PARAMS/DATA/plan/ANSWER/chart/registration) readable without
  ambiguity; bespoke helpers (e.g. river_dye release_points coercion)
  may live in sibling files within the template's own folder. Charts
  stay colocated in the file (meaning, not plumbing). Apply as the norm
  at fleet migration; no immediate cohort churn required.
