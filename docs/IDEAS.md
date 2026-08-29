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

- CONSTANT-DOOR WIRE ENFORCEMENT (NATE 2026-08-24, ruling b): CONSTANT-door
  params DROP OFF the model-facing wire entirely - the factory excludes
  them from the synthesized tool signature; they remain form-editable
  surfaces for the user (user_lever-style). The door becomes a binding
  authority contract, not documentation. A param that deserves model
  access gets deliberately re-doored (one-line template edit) - e.g.
  sim_duration_s per-template where warranted. Implement in the next
  cohort-hardening batch alongside the remaining redline rulings.

- STAGE-SEQUENCE ENFORCEMENT DEFERRED (NATE 2026-08-24, ruling b): stays
  stamped-and-displayed for now; enforcement (validate_plan refuses stage
  regression, facade ops become the stamping authority, gates
  anchored-or-exempt) lands as the FIRST COMMIT of the mesh-gate wave -
  the first feature whose correctness depends on ordering. Contractual
  but unenforced until then, recorded in the amendment.

- ACQUIRE_DOMAIN SURFACE (NATE 2026-08-24, ruling a): keep the landed
  explicit **slots call - the template names exactly which params/data
  feed acquisition (read-the-page dataflow). Facade evolution rule: a
  NEW acquisition input arrives as a keyword WITH A DEFAULT, so existing
  templates are untouched (one-runner law holds); only templates wanting
  the new input name it.

- PHYSICS BUNDLE STAYS WHOLE (NATE 2026-08-24, ruling a): river_dye's
  Physics stays ONE bundle - no Bed/Dredging split; that would be
  presentational complexity (three classes + facade flattening) for
  lines already readable in one place. PRINCIPLE (NATE, verbatim
  intent): "don't overcomplicate or overthink" - a split/abstraction
  earns itself only when a real second consumer needs it, never for
  tidiness.

- FLEET MIGRATION ORDER (NATE 2026-08-24): after the TELEMAC family ->
  MODFLOW workflows next (similar control flow to TELEMAC; the USGS
  modflow6-examples notebooks they were derived from remain the
  referencable published-first sources for parity/coverage checks) ->
  then SWMM. Same per-template regime throughout: parity via existing
  drivers first, spot-check proofs through the scripts/ harness lane,
  readability principle, ledger rows, LOC ledger rolling net.

- 2026-08-25 COASTAL RESULTS MESH IS PUBLISHED AT A FALSE ORIGIN (queued
  defect, found by the TELEMAC-family wave's own proof sheet): the coastal
  worker writes LOCAL, origin-shifted coordinates into `res_coastal.slf`
  (verified on run `01M0VVBYVXTNBN1YFC53NVR5HY`: x 0..11391 m, y 0..12397 m)
  and `results_mesh_seam` publishes that file as `crs_authid=EPSG:<utm>`.
  In QGIS the animated "Model results (time series)" layer therefore lands at
  the UTM zone's false origin - roughly 1,600 km off, near the equator - while
  the peak-depth COG beside it is correct, because `postprocess_coastal`
  recovers the origin from `domain_bbox` before it reprojects. PREDATES the
  declarative migration (the composer always published it this way). Two
  possible fixes, both worker-side and therefore image-rebuild work: write
  ABSOLUTE UTM coordinates into the result SELAFIN, or fill the SELAFIN
  header's X-ORIGIN / Y-ORIGIN (IPARAM 3/4), which MDAL honours. Until then
  the diagnostic sheet names it: `render_all_layers_proof` now takes the
  canvas extent over the largest mutually-intersecting group of layers and
  captions the strays "OFF-CANVAS ... a georeferencing defect, not a view
  choice", so a misplaced layer can no longer hide itself by zooming every
  panel out to nothing. Check the other worker-written meshes for the same
  shape when the wave-module templates migrate.

- 2026-08-25 A `!run` DISPATCH EMITS NO COMPLETION `tool-io` FRAME (queued
  defect, found while building the family canaries): the model lane fills in
  `function_response` at completion (`turn/stream.py`), but the
  `dev-tool-invoke` lane emits only the EARLY input-only frame
  (`dispatch/emitter.py:_emit_early_input_frame`, response `null`,
  `is_error` false) - so the operator's tool card shows "Running..." forever,
  and a tool that RETURNED `{"status": "error"}` was indistinguishable from
  one that succeeded. The testing harness now reads the terminal state off the
  `pipeline-state` card instead (which BOTH lanes stamp honestly), so the
  gates are trustworthy; the card's own expander is still empty on a `!run`
  and wants the completion frame emitted from the shared dispatch seam.

- 2026-08-25 TOMAWAC Hs COG WAS AT THE UTM FALSE ORIGIN (FIXED in the family
  wave, recorded because the class is wider): the wave worker builds its grid
  with node 0 at the AOI's SW corner and only offsets by that corner when it
  SAMPLES the bed, so the result SELAFIN carried local metres - and
  `postprocess_tomawac` reprojected them as ABSOLUTE UTM. Every Hs map this
  template ever published landed near the equator (measured: the Lake Superior
  canary's COG at lon -91.49..-90.81, lat -0.001..0.50) while the bed COG beside
  it sat correctly on the lake. `postprocess_coastal` already recovered the
  origin from its `domain_bbox`; `postprocess_tomawac` now does the same through
  a shared `_local_mesh_origin` (and the idealized basin, which has no corner,
  passes none). CHECK THE SAME SHAPE on artemis + telemac3d when those templates
  migrate - both are the same worker family and neither postprocessor takes a
  domain bbox today.

  SUPERSEDED 2026-08-25 (the closing sentence only - the TOMAWAC finding above
  stands as written). Both templates were checked; the "neither postprocessor
  takes a domain bbox today" claim was already false for one of them and is now
  false for both:

  - TELEMAC-3D: WAS affected, FIXED. `postprocess_telemac3d` reprojected the
    re-emitted surface / bottom layer SELAFINs as absolute UTM, so both COGs
    landed at the zone false origin exactly as TOMAWAC's Hs did.
    `_rasterize_t3d_field` now takes `domain_bbox=wm.get("bbox")` (landed in
    `bd0b84cd`, the stratified_flow migration) and `b24feb64` hardened it: the
    worker echoes the bbox it actually meshed, `solved_domain_bbox` prefers that
    echo over the staged bbox and never the raw AOI, and `_local_mesh_origin`
    REFUSES a malformed bbox instead of reading it as absent and landing a real
    domain at the false origin.
  - ARTEMIS: NEVER affected. `postprocess_artemis` has taken `request_bbox` and
    added the SW-corner offset back before the UTM -> 4326 inverse since before
    this campaign; its docstring names the failure mode ("or the field
    georeferences to the UTM-zone origin (near lon -91, lat 0) instead of the
    real harbour") as something it already prevents. `b24feb64` only folded its
    private copy of that arithmetic into the shared `_local_mesh_origin` - a
    de-duplication, not a fix. No ARTEMIS Kd COG was ever published at the false
    origin.

  So the false-origin class is CLOSED for the raster products of all three
  AOI templates. It remains OPEN for the coastal RESULTS MESH layer (the
  separate entry above): that defect is in what the worker WRITES into the
  SELAFIN, not in what the postprocessor reads, and it is still image-rebuild
  work.

- 2026-08-25 THE OPEN-WATER "FIELD" RASTERS ARE DOT LATTICES, not fields (queued
  defect, visible in every proof sheet this wave produced): `postprocess_coastal`
  and `postprocess_tomawac` rasterize the mesh nodes onto a grid sized by a fixed
  `target_ground_res_m=30.0` and fill only within `clip_dist_deg = 2 output
  cells` of a node. With a 250 m coastal grid - let alone a 3000 m wave grid -
  that is ~60 m of fill around nodes kilometres apart, so the published "peak
  inundation depth" and "significant wave height" layers are a lattice of
  isolated pixels with nodata between them. The scalars are unaffected (they are
  computed from the MESH, not the raster), which is why this has never shown up
  in a number. Fix shape: size the output grid from the run's own `dx_m`
  (something like `max(30, dx_m / 4)`) so the clip distance scales with the mesh
  the run actually solved. NOT done in the migration wave: it changes the
  published raster, and this wave's evidence is same-question-same-answer.

- PER-WORKFLOW DELIVERY NORM (NATE 2026-08-25): during fleet migration
  every migrated workflow is shown to NATE INDIVIDUALLY as it lands -
  per-template packet (LOC delta, parity one-liner, full proof render
  set) - never batched at wave end. Applies TELEMAC and onward (MODFLOW,
  SWMM).

- 3D RENDERING CAPABILITY RULING (NATE 2026-08-25): 3D results get a
  QGIS-renderable 3D product - res3d_t3d.slf publishing as two stills is
  NOT the end state. This is a CAPABILITY track, not a one-off: more 3D
  outputs AND inputs are coming (telemac3d prisms, MODFLOW layered
  aquifers - fold the queued FloPy/QGIS-3D-aquifer-viz recon in here).
  Research-first: MDAL 3D stacked-mesh support (which formats: SELAFIN
  3D, TUFLOW FV, UGRID NetCDF), QGIS 3D map views + mesh averaging
  methods, what the plugin must do vs what QGIS gives free, export
  path per engine. Blocked-by: the local-coordinate mesh defect (same
  fix as coastal's false-origin mesh layer). Build lands after the
  fleet migration; recon runs now.

- DOT-LATTICE FIX SHAPE RULED (NATE 2026-08-25, "too hand wavy... can we
  get finer?"): the open-water raster fix is BARYCENTRIC INTERPOLATION
  over the result triangulation onto the fine output grid (30 m or
  finer) - the FEM solution IS piecewise-linear over triangles, so this
  is the solver's own representation, zero invented data; matches QGIS's
  native mesh rendering. SUPERSEDES the earlier "size the grid from
  dx_m" shape (continuous but blocky - rejected). Applies to
  postprocess_coastal + postprocess_tomawac (+ any node-dot rasterizer).
  Lands in the post-family fix batch w/ before/after proof renders on
  the SAME runs. Mesh fineness itself stays the user granularity lever.

- 2026-08-25 OPEN-WATER MESH-LAYER PUBLISHING GAP (NATE spot-check, TELEMAC
  family wave - QUEUED, do not build): the four AOI templates disagree with each
  other and with the reach family about whether a mesh reaches the canvas at all.
  artemis and tomawac publish NO mesh layer (artemis emits three layers: the
  surveyed breakwater, the bed, the Kd field; tomawac two); coastal publishes one
  and it lands at the false origin; telemac3d writes an 11-frame `res3d_t3d.slf`
  that nothing publishes; the reach family ships both a mesh PREVIEW (the
  approve-mesh gate) and a results mesh. Whether the AOI templates should ship
  mesh preview + result-mesh layers like the reach family joins the res3d /
  3D-rendering decision cluster - it is the same canvas-products question and it
  has the same blocker underneath it, the local-coordinate meshes (a mesh layer
  cannot be published honestly until the worker writes absolute coordinates or
  fills the SELAFIN X/Y-ORIGIN header). Until then the diagnostic lane covers it:
  `scripts/render_selafin_animation.py` takes `--origin-bbox` and puts the
  wireframe over the field correctly, which is how the artemis barrier cut-outs
  and the coastal tide are proved this wave.

- RESOLUTION-SENSITIVITY RULING (NATE 2026-08-25, ruling b+c): (b) NOW -
  answers in resolution-sensitive classes carry an explicit honesty
  label when solved on a coarse mesh: concentration/magnitude PEAKS,
  local feature LOCATIONS, wet/dry BOUNDARY areas, gradient-zone values
  = "resolution-limited, treat as bound" notes on the answer artifact
  (the refined-mesh pass 2026-08-25 is the evidence base: dye peak 6x
  low, flooded land 4x low, crest artifact 2x high, upwind Hs -62%, Kd
  absolutes -30-50%, stratification dT -25% - all unsafe-direction;
  converged classes: integrals, saturated maxima, ratios - DO min,
  hs_max, sheltering ratio). (c) LATER - the granularity gate learns the
  same map: suggestion steers fine for sensitive question classes and
  says why; user keeps the lever. Mechanism is skeleton-level
  (answer-path labeling), lands with the held fix batch; generalizes
  past TELEMAC (MODFLOW plume peaks next).

- CASE DATA DELETE-ON-WHIM (NATE 2026-08-25): Claude-driven
  spot-check/canary/diagnostic cases and run prefixes are disposable at
  Claude's discretion, no per-ask approval. RETAIN: 1-2 most-complex
  showcase cases per engine (the ones whose proofs persist in
  docs/proof/templates/), NATE's own cases, live-session cases, and run
  prefixes still referenced by in-flight agents or proof evidence. The
  persisted renders + evidence JSONs are the durable record; the run
  data behind them is recompute-on-demand. Sweeps ride wave close-outs
  keep-list-first; first sweep = cleanup phase 2 (root disk at 93%).

- ARTEMIS RESOLUTION BOUND CONTRADICTS ITS OWN DEFAULT (found 2026-08-25,
  driving the new idealized canary): `target_resolution_m` declares
  `bounds=(20.0, 2000.0)` while its `derived_when_absent` declares the
  analytic-domain default as 8 m. So the labeled default is BELOW the
  declared minimum, and any EXPLICIT ask for the analytic spacing is
  floored to 20 m - at which the 100 m wide basin's 25 m mouth lands on
  two grid columns, the dividing wall's opening goes asymmetric, and
  ARTEMIS aborts on FRONT2 (now a typed ARTEMIS_BOUNDARY_DEGENERATE
  refusal rather than an MPI exit code 2). The 20 m floor was authored
  for REAL harbour AOIs; the analytic basin is two orders smaller. Fix
  shape: the floor is a property of the DOMAIN, not the template, so it
  belongs beside the bed path (real harbour 20 m, analytic 2 m) rather
  than as one bound covering both. Worked around for now by leaving the
  canary's lever unsupplied so the labeled default rides. NOT a physics
  change - do not widen the bound without deciding which domain it guards.

- do_sag DECLARES NO OUTPUT-CADENCE LEVER (found 2026-08-25, NATE's
  denser-frames ask): `telemac_do_sag` PARAMS has no
  `output_interval_min`, though `write_reach_deck` accepts one and its
  cohort sibling `telemac_river_dye` declares it (door=USER, bounds
  0.1-1440 min). So the do_sag animation is stuck at the worker's
  graphic_period default - 6 frames over the refined 600 s window, which
  NATE called too coarse to spot-check. river_dye now runs 30 frames.
  Fix is the 4-line declaration mirroring river_dye's plus passing it in
  `plan`; held because it is workflow code and the directive scoped that
  pass to params only. Cheap, and it closes an inconsistency inside one
  cohort rather than adding a feature.

- COASTAL PEAK RASTER PAINTS THE PERMANENT BAY (found 2026-08-25, the
  t=0 wet-land diagnosis): `coastal_depth_max.tif` is per-node max WATER
  DEPTH over ALL frames including t=0, with no subtraction of the
  initial water line - so the permanently submerged bay floor renders in
  the same "inundation depth" ramp as land that actually flooded. On the
  diagnosed run 44.8% of the wet raster is deeper than 2 m. The worker's
  own `flooded_land_km2` metric already does the right `bed > init_wl`
  discrimination; the raster does not, so the scalar and the picture
  disagree. Fix shape is one of: mask the product to `bed > init_wl`, or
  publish depth-above-initial as the primary and keep total depth as a
  companion. This changes what the product MEANS, so it is NATE's call,
  not a silent correction - recorded, not done.

- SETTER SENTIMENT INTO THE SKELETON (NATE 2026-08-25, decision 1): the
  set_* family's capability becomes SKELETON machinery - (a)
  RERUN-WITH-OVERRIDES: derive a run from a parent run's resolved sheet
  w/ named overrides through the USER door; ledger/resume reuses
  unchanged stages; copy-on-write via run-prefix separation;
  approved==ran holds by construction. (b) COUPLED-VALIDITY RULES:
  declared cross-param validators (friction_law change inverts
  friction_coefficient meaning -> re-confirm or refuse). This IS the
  calibration substrate (a calibration loop = automated
  rerun-with-overrides vs observations) - build as the calibration
  track's FIRST wave (slow, research-first, post-MODFLOW per NATE's
  sequencing). The four set_* tools = interim mechanism; DELETION_LEDGER
  rows conditioned on the skeleton capability proving the same live
  recalibration, then they die.

- DECISION 1 PLACEMENT AMENDED (NATE 2026-08-25): rerun-with-overrides +
  coupled-validity rules are NOT calibration-land - they are a PRIMITIVE
  behavior of a workflow (skeleton core capability) that UNDERPINS
  calibration. Build placement: the skeleton lane (workflows/lib -
  natural fit alongside the ledger/resume machinery it composes), not
  the calibration track; calibration later consumes it as a loop driver.
  Setter-family deletion condition unchanged.

- DECISION 1 THIRD CONSUMER (NATE 2026-08-25): rerun-with-overrides is
  also a TROUBLESHOOTING helper - a FAILED workflow reruns with adjusted
  params from its own ledger (composes with resume-from-failure /
  restart_clean). Three consumers: failure recovery, manual what-if
  perturbation, calibration loops.

- DECISION 2 RULED (NATE 2026-08-25, ruling b): coastal inundation
  product SPLITS - primary answer layer = depth over INITIALLY-DRY land
  (the planning quantity, matches flooded_land_km2, uses the
  datum-corrected t=0 wet/dry mask), honestly named; the full
  water-depth field stays published as a CONTEXT layer. The
  resolution-sensitivity label rides the inundation layer (flooded-land
  = resolution-bound class). Implementation joins the accumulated
  TELEMAC fix batch (w/ the coastal results-mesh X/Y-ORIGIN worker fix
  + the resolution labels).

- RUN JOURNAL RULED (NATE 2026-08-25): append-only JSONL database of run
  RECORDS, decoupled from artifacts - one line per completed run written
  by the skeleton PUBLISH stage (one seam, all engines free): run_id,
  template, engine, full resolved sheet w/ doors/bases/notes, ANSWER
  fields, provenance rows, mesh facts, wall times, compute class,
  canary-vs-user. Artifacts stay delete-on-whim; journal lines survive
  every sweep (categorically excluded from sweeps). Enables: deriving
  defaults from run telemetry (e.g. sim_duration from prior convergence),
  self-accumulating resolution-sensitivity evidence, calibration priors,
  wall-time/autoscaler data, regression baselines, digital-twin substrate.
  Backfill pass seeds from surviving run prefixes + proof evidence JSONs
  (swept cases honestly gone). Build: skeleton lane, small; joins the
  accumulated batch.

- USER-INPUT SPECIES RULED (NATE 2026-08-25): typed user-input
  normalizers (point-from-shapes, polyline, polygon, bbox, BEARING wrap,
  later BYO-object acceptors) become ONE species in workflows/lib
  LIVING WITH the gate machinery - because a value can arrive drawn
  (DrawGate response) or typed (wire coercion) and BOTH routes must pass
  the SAME normalizer (no-double-middleware; gate geometry vocabulary ==
  coercion shape vocabulary). wind_bearing folds entirely into the lib
  species; release_points SPLITS - shape normalization to lib, the
  seed-vs-source POLICY stays in the template sibling calling the lib
  normalizer. Mesh/BYO acceptance joins the species at the mesh-gate
  wave (the species = "things the user hands us: clicks, sketches,
  values, objects"). Joins the accumulated batch.

- PROOF FOLDER ORGANIZATION RULED (NATE 2026-08-25): spot-check GIFs +
  showcase renders live in INHERITED NAMED FOLDERS for quick reference -
  docs/proof/templates/<template_name>/<variant>/ (variant = coarse |
  refined | postmigration | addendum) instead of the flat
  name-prefixed pile. Mechanical reorg: git mv existing files into
  their folders (names kept), proof/render scripts write the new layout
  going forward, README documents the scheme. The per-engine showcase
  KEEP-LIST (delete-on-whim carve-out) points at these folders. Joins
  the accumulated batch.

- STYLE MODIFIER GRAMMAR RULED (NATE 2026-08-25): style override is a
  DECLARATION MODIFIER in the .byo()/.ladder()/.resample() family -
  .style(preset=|rescale=|colormap=), optional, absent = the contract
  default; lives in workflows/lib; resolves against contracts
  styles.yaml. Emission stays AUTOMATIC on all surfaces (fetch declared
  -> default style emitted); the modifier only specializes ad hoc where
  defaults cannot express the output. The .render verb RETIRES (renders
  are the plugin's job; workflows describe products). Joins the batch's
  emission chapter (styles->contracts YAML, one resolver, mirror dies).

- ANIMATION LEGEND SHIFT BUG (NATE 2026-08-25, spot-check catch): GIF
  legends' discrete color sections SHIFT between frames - per-frame
  autoscaling makes the same color mean different values per frame
  (dishonest visualization). Fix in render_selafin_animation.py: scale
  fixed ONCE across the whole time axis (global range or the declared
  style rescale), legend static; pinned test = extracted-frame legend
  regions byte-identical across the animation. Joins the batch
  (proof-lane).

- STYLE MODIFIER PRECISION (NATE 2026-08-25): .render never shipped in
  live templates - it is dormant lib machinery + a design-doc example
  only; the six migrated templates declare zero styles (absence-is-
  default already validated). The REAL live coupling is engine step
  publishers importing preset CONSTANTS - those retire: publishers
  declare QUANTITIES, the contract owns quantity->preset. .style()
  replaces the dormant .render machinery + the doc example.

- DATA-DRIVEN SCALING RULED (NATE 2026-08-25): rescale: data is a
  first-class declared policy in the style contract and the DEFAULT for
  model-output quantities - hardcoded scales make outputs less
  informative (refined river_dye peaked 28.7 vs the old 0-10 preset).
  Two boundaries: (1) the SCOPE of "data" is the RUN, never the frame -
  range computed once over the whole time axis/planes; every frame +
  legend uses that one range (this IS the legend-shift fix: the bug was
  per-frame scoping, not the policy); (2) COMPARISON paths (rerun-with-
  overrides before/after, calibration iterations, coarse-vs-refined
  pairs) compute the range across the compared SET and share it, legend
  stating so. Fixed rescales remain for domain-standard bounded
  quantities. Legends always state which policy + the range.

- SCALE KNOB + RESTYLE TOOL RULED (NATE 2026-08-25): style is
  DISPLAY-STATE not solve-state - rescaling needs zero recompute, so the
  policy is available BOTH upfront and post-hoc. (1) Declared knob:
  .style(scale=...) modifier; a template exposes it as a Param fed via
  ParamRef (one declared param away, same pattern as mesh policy). (2)
  Registered restyle_layer tool (LLM-invokable, agentic ad hoc or on
  command): re-emits the DISPLAY FACE of an already-published layer only
  - NOT publish_layer resurrected (cannot create visibility); also takes
  layer_ids+shared_scale=true for honest comparisons (calibration
  before/after on one range). (3) Scale vocabulary beyond min/max, all
  declared VALUES: policy data|fixed, transform linear|log|sqrt|
  percentile, clip, range - ONE schema serving contract default,
  modifier, param knob, and tool args. Later stages override earlier;
  every override labeled; data immutable throughout.

- TEMPLATE DECLARATIONS SIBLING RULED (NATE 2026-08-25, "settle now at 6
  templates - it costs more later"): UNIFORM norm, no threshold - every
  template folder carries declarations.py holding PARAMS + _DOC; the
  template file hydrates via one import and keeps QUESTION docstring,
  DATA, plan, ANSWER, chart, ResolutionSpec, metadata, registration
  (the recipe readable on one page, the contract one file over).
  PYTHON not JSON (types, resolve= hooks, and the three render surfaces
  keep working; params-as-YAML deferred to the user-tool-builder
  authoring format, which COMPILES to Param objects). Apply to all six
  migrated templates in the accumulated batch; every future migration
  lands in this shape.

- REUSE-SWEEP NORM + CANDIDATES (NATE 2026-08-25): utilities promote to
  shared only on a CONFIRMED second consumer, then a deliberate reuse
  sweep - not preemptively. The sweep is each family migration's opening
  act. Candidate list from the telemac root: read_selafin, the
  node/mesh-to-grid rasterizers, the barycentric interpolator,
  _local_mesh_origin, publish_peak_layer (13 hits/4 defs repo-wide -
  first name on the MODFLOW sweep). Decision 5 fully CLOSED: all 21
  ambiguous MinIO prefixes KEPT.

- PHYSICS NAME CONFIRMED + SIBLING SHAPE APPROVED (NATE 2026-08-25):
  after deliberate challenge (Process/Inputs considered), Physics stays -
  no rename churn. The declarations.py separation approved as exemplified
  (PARAMS + _DOC out; QUESTION docstring, DATA, plan, ANSWER, chart,
  spec/metadata, registration stay; plan body unchanged by one
  character). Apply to all six in the batch.

- MESH WAVE CHARTER GROWS (NATE 2026-08-25 discussion): (1) CONTEXT-OBJECT
  SLOTS: producer-less optional Data (.byo geometry-typed, .optional()) -
  no baked default producer (even naming a fetcher is opinion); sources =
  standalone fetchers (fetch_osm_breakwaters gets BUILT as one), QGIS-
  authored layers, draw gate, or omitted (labeled absence); lazy
  demand-pulled producers in the lib. (2) ENFORCEMENT vocabulary:
  obstacle(polygon)=punch hole, barrier(polyline)=blocked edge,
  refine(zone) - neutral declarations, per-engine writers realize
  (TELEMAC islands, SWMM blocked links); artemis TRUTH: already a real
  1-element-wide topological slit (marching-cell thin barrier, LIHBOR 2
  faces) - thin cuts need ZOOM-CROP proof panels as the norm. (3) OPEN
  BOUNDARIES: open_boundary(stretch, forcing)/free_exit(stretch)/closed
  default - declared neutrally, realized per-engine (LIHBOR 1/2/4 exists
  natively, wasted today: coastal hardcodes ocean_edge E); subsumes the
  OceanMesh BYO boundary-tagging question; reviewable at the mesh gate.
  Misrepresented coastal connectivity = quiet physics corruption
  (reflections that should radiate).

- DECISION 6 RULED (NATE 2026-08-25): (1) STATIC-PLAN RULE BLESSED -
  all-P/D, module binding blocks, plan(ops), When-only conditionals,
  deep-freeze + error provenance + validator re-point; p-view read
  recording and p.get DELETED. Settle at six. (2) The batch is a TELEMAC
  WORKFLOWS REFACTOR incl a STEPS AUDIT - spot-check every telemac steps
  file for breakwater-class offenders (tool-shaped fetch code, baked
  opinions) and remediate with the architecture. (3) MODFLOW family and
  the 3D build DEFERRED. (4) rain_on_grid refactored AFTER the batch
  with the finalized philosophy (TELEMAC 7 of 7). (5) CALIBRATION NEXT
  after the 7th: coastal surge vs a REAL EVENT against event-relevant
  gauges (CO-OPS observed water levels; research-first per standing
  doctrine, NATE-first methodology sign-off BEFORE runs; consumes the
  rerun-with-overrides primitive). Mesh wave waits behind calibration;
  NOTE: calibration may surface open-boundary needs early - surface to
  NATE if so, do not scope-expand silently.

- MESH WAVE ACCEPTANCE CASE RULED (NATE 2026-08-25): the ARTEMIS BYO
  REMATCH - when the mesh features land, rerun the Marquette harbor
  agitation with a BYO OceanMesh-generated mesh (adaptive sizing: fine
  at the breakwater/shore, coarse offshore) fed through the BYO door,
  the breakwater as an ENFORCEMENT obstacle (punched hole in OUR mesh),
  open boundaries declared at the harbor mouth, zoom-crop proof of the
  slit in the finer triangulation. Direct comparison vs the worker's
  uniform-grid run (same harbor, same forcing, same question): does
  adaptive fidelity sharpen Kd fringes + the sheltering answer. One run
  exercises every mesh-charter pillar end to end.

- CONFORMAL ENFORCEMENT REQUIRED (NATE 2026-08-25, BYO rematch
  refinement): enforcement geometry must be CONFORMING in the generated
  mesh - the obstacle/barrier polyline becomes a CONSTRAINED BOUNDARY:
  nodes placed ON the polyline, element edges coincident with it, zero
  gap/offset between the declared geometry and the mesh (vs the worker's
  stair-step grid approximation, offset up to cell/2). OceanMesh/gmsh
  support this natively (shoreline-conforming meshing). ACCEPTANCE
  (rides the artemis BYO rematch): measured max distance from the
  barrier polyline to the nearest coincident mesh edge ~ 0 within vertex
  tolerance, plus the zoom-crop showing edges tracking the line exactly.

- EDITABLE MESH LAYERS RULED (NATE 2026-08-25, observed rasterized
  meshes in the mesh folder): a mesh reaching the CANVAS ships as a
  native MDAL MESH LAYER in an EDITABLE format (2DM the reliable
  writable one; verify SELAFIN write/edit support per QGIS version) -
  NEVER rasterized to pixels (pictures of meshes are diagnostic-lane
  only). Sweep: find every mesh currently published as an image and
  convert its path. THE PAYOFF: QGIS's native mesh-editing toolbar
  (3.22+: move/add/delete vertices) BECOMES the mesh gate's refinement
  mechanism - publish editable mesh -> user edits in QGIS -> edited mesh
  returns through the BYO door as the approved domain. No custom editor
  built (leverage-libraries); joins the mesh wave charter, blocked-by
  the SELAFIN local-coords fix where applicable.

- .SUPPLIED() RENAME + THE SLATE PRINCIPLE (NATE 2026-08-25): .byo() ->
  .supplied() everywhere (matches the user_supplied ladder rung +
  "supplied on this invocation" provenance - one word, three surfaces;
  no alias, demolition in the same commit). ARCHITECTURE CLARIFIED:
  mesh AUTHORING is its own pipeline - enforcement ops (obstacle,
  barrier, refine, boundary tagging) are AUTHORING TOOLS in the mesh
  front, composable standalone (fetch/draw polyline -> generate base
  mesh -> conformal cut -> tag boundaries -> optional QGIS hand-edit)
  producing a MESH ARTIFACT; the sim workflow is a SLATE consuming it
  via .supplied() with zero baked opinions, its generated default a
  labeled fallback not a stance. Two routes, one mechanism set. The
  artemis BYO rematch is route 1 end to end - it proves the slate
  principle, not just the meshing.

- ONE FLAGSHIP CANARY RULED (NATE 2026-08-25): the NATE-facing canary =
  ONE end-to-end flagship run leveraging the full buildup - mesh
  AUTHORED (generate -> conformal enforcement -> boundary tags) -> FED
  IN via .supplied() -> SOLVED -> proven to the described standard
  (every layer full-size in emission order + composite canvas + charts +
  GIF when the outcome is animated). The artemis BYO rematch BECOMES the
  standing flagship once the mesh wave lands; interim flagship = coastal
  surge (animated, richest layers, freshest machinery). The six coarse
  per-template runs DEMOTE to silent internal parity pins - never
  rendered, never delivered, no packets; suite-level tripwires only.

- WORKER DOCTRINE + FETCH-MIGRATION-FIRST RULED (NATE 2026-08-25): a
  WORKER is the ENGINE ROOM - the solver binary + minimal glue to run it
  on a fully-staged run directory and write results/metrics. NOTHING
  else: no fetchers (in-worker fetches migrate to router specs, staged
  via the manifest-inputs mechanism), no mesh builders (extract to the
  mesh front - the mesh wave IS this extraction), no baked values (the
  manifest feeds them, adjustments narrate), no publishing. DoD =
  --network none: a worker that cannot reach the internet provably
  contains no fetchers. The workers today are fossilized composers -
  every smell is a doctrine violated below the waterline. ORDER RULED:
  batch wave B (purity audit = the inventory) -> rain_on_grid (7th) ->
  IN-WORKER FETCH MIGRATION (bed bathymetry etc through the substrate,
  --network none gate) -> CALIBRATION (on a thin worker with visible,
  laddered, provenance-stamped inputs - half the point of calibrating)
  -> mesh wave pulls the mesh builders. The worker dissolution completes
  across these waves, not as one.

- WORKER DOCTRINE RATIONALE: CLOUD-READY BY ENCAPSULATION (NATE
  2026-08-25): the black box is not hygiene, it is deployment strategy -
  compose the sim's complete inputs server-side, ship the staged run
  directory to the box WHEREVER it lives (local docker today, EC2/Batch
  tomorrow), collect results. The staged dir IS the job submission
  (stage->submit->collect, the native HPC grammar); this is why the
  compute tier was preserved - compute_class maps to container resources
  locally, instance types/queues in cloud, values unchanged. MIGRATION
  TEST for the fetch-migration wave: "would this code change if the box
  moved to EC2?" - yes -> server tier (fetchers/publishing/values), no ->
  stays (staged-inputs->solver-files, run binary, write results).
  Dividends: statelessness = spot/s2z safe; --network none locally =
  no-egress groups in cloud (one guarantee, two enforcements); the run
  dir = the reproducibility artifact the rerun primitive + calibration
  pin. FUTURE DOOR, not built: a declared box-side reduce stage if cloud
  result-transfer costs ever bite (mechanism only, manifest-listed).

- WORKER RATIONALE CORRECTED (NATE 2026-08-25): the cloud lane is NOT a
  consideration - no cloud planning, no reduce-stage future door
  (struck). The doctrine is only this: the worker is a true black box
  (staged run dir in, results out), and trivial deployability is a
  PROPERTY that falls out of real encapsulation, not a goal to design
  for. The portability test survives purely as a purity heuristic for
  classifying code out of the box. Nothing else from the cloud framing
  carries.

- WORKER-PURITY INVENTORY, TELEMAC (wave B audit 2026-08-25, MIGRATED half in
  ADR 0315, REST QUEUED): the six builders + entrypoint were walked and every
  baked literal classified. MIGRATED this wave: the coastal leg's four
  unreachable knobs (friction_law, friction_coefficient, wind_speed_mps,
  wind_direction_from_deg were CoastalConfig fields the deck writer never
  filled, so the image's defaults were the only values a coastal run could
  have); four NARRATE-ON-ADJUST violations now echo (the rescaled inflow
  discharge, the coastal mesh origin, the ARTEMIS shoal's silent override of
  wave_height_m/wave_period_s, the one parser stamp). STILL QUEUED, as
  migrate-to-manifest: (1) FOUR auto-spacing divisors (max(Lx,Ly)/120 in
  coastal+tomawac+artemis, /40 in telemac3d) and FOUR grid floors (20/150/20/400
  m) plus three MORE contradicting local floors in artemis and telemac3d - every
  one is the opinionated-mesh lever and every floor is DUPLICATED against the
  matching Param's declared bounds, two sources of truth for one number; (2) the
  TOMAWAC spectral discretisation (ndir 24, nfreq 32, fmin 0.04, fratio 1.1) -
  the core numerics budget with no Params at all, plus the JONSWAP friction
  0.038 and the Battjes-Janssen GAMMA1/GAMMA2; (3) the TELEMAC-3D turbulence
  constants (four 1.E-4 diffusions, LAW OF BOTTOM FRICTION 5 + 0.01, iturbv 2)
  and its two hardcoded timesteps (dt=20 stratification, dt=10 wind) written
  into the solve body rather than the config; (4) the river_dye centerline
  family - resample_ds_m 18, smooth_window 7, the 15/9-point smoothers, the 30 m
  run-accept radius, the release-point accept radius 2*width, min/max bed slope,
  init_depth_m 2.5 - none declared, several NATE named by name; (5) the six
  hardcoded fetch endpoints (NGDC DEM_all x4, Planetary Computer STAC, USGS
  3DEP, NLDI, NHDPlus_HR layers 3 and 8) which ride the in-worker-fetch
  migration, the other half of the same end state; (6) the coastal WET_TOL 0.02
  that the flooded-area discriminant is computed on, and four DIFFERENT wet-node
  AOI-acceptability thresholds (0.05/0.05/0.20/0.25) across four legs; (7)
  tomawac_build.py:534 stamps utm_epsg=32615 on the idealized path regardless of
  anything - a hardcoded CRS. Two remaining NARR violations: smooth_tries
  (logged, not echoed) and the oil clearance-snap coordinates.

- LOCAL-COORDINATE RESULT MESHES, the LATENT three (wave B 2026-08-25): coastal
  is FIXED (ADR 0315 - the geometry SELAFIN carries X-ORIGIN/Y-ORIGIN, verified
  on the canary at 691577/3286076 -> lon -85.02..-84.90, lat 29.69..29.80).
  telemac3d, tomawac and artemis write the SAME shape - local metres with a zero
  origin - and are latent rather than broken only because none of them publishes
  a mesh layer and each postprocessor re-adds the origin itself. The one-line
  fix per leg is the same `add_mesh(orig=...)`; the origin values are already
  computed and discarded at telemac3d_build.py:638-639, tomawac_build.py:450-451
  and artemis_build.py:589 (artemis's build_mesh even HAS unused x0/y0
  parameters). Do it with the decision about whether the AOI templates ship mesh
  layers at all, which is the same canvas-products cluster as res3d and the
  3D-rendering track.

- STEPS AUDIT, THE QUEUED ROWS (wave B 2026-08-25; 41 files walked, verdict
  table in the wave report). REMEDIATED this wave: the breakwater-class offender
  and everything that existed to support it; the three prose-holds-a-number
  resolution default pairs (now in the declarations that promise them); the four
  mesh_resolution_label copies (one helper); the do_sag defaults' second AND
  third copies (deck.py and products.py now READ the resolved config); the grain
  clamp's two copies; the coastal 180 m and 30 h constants; three silent
  substitutions turned into typed refusals (unknown compute_class, unknown
  bank_source, and the sediment-class truncation's sibling). QUEUED, each with
  its reason: (a) the five undeclared AOI half-widths (_HARBOR_HALF_DEG,
  _COAST_HALF_DEG, _BASIN_HALF_DEG, _LAKE_HALF_DEG, DEFAULT_RIVER_AOI_HALF_DEG)
  - they should be declared Params, but five new form rows across five templates
  belongs with the granularity-gate wave that already owns "resolution is a user
  lever"; (b) `write_reach_deck`'s ten signature defaults duplicating
  river_dye/declarations.py - a caller sweep first, because mesh_preview and two
  drive scripts call it outside the plan; (c) `_DISCHARGE_QUERY_HALF_DEG` 0.03,
  which decides WHICH reach carries the flow - a real physics dial, and it rides
  the queued nearest-to-seed discharge_resolve change; (d) `wave.py`'s
  bottom_friction self-arming rule, which wants a DERIVED door reading another
  param and therefore a resolve-fn seam that does not exist; (e) `GREAT_LAKES`,
  a coverage table that belongs on the fetcher spec (`supports_global_query` +
  a coverage bbox), not in a step module; (f) `Param` has no `choices=`, so
  deck.py's `_BEDLOAD_FORMULAE`/`_FRICTION_LAWS` closed sets still drop an
  out-of-set value silently - a lib change; (g) `k2_formula = 0` (constant k2) is
  a solver-mode choice with no Param and no provenance row - exposing the
  O'Connor-Dobbins formulae is a physics decision; (h) rain_on_grid's whole
  class-C set (17 bare signature defaults, the invented AOI-centroid pour point,
  the unreachable Huang slope correction, the 24-hour solve timeout, the
  duplicated UTM-zone formula) is blocked on migrating that template onto the
  skeleton, which is the job rather than a fix; (i) `_mesh_override_provenance`
  vs `mesh_sizing_provenance` - two implementations of "narrate the grid ask the
  run moved", learned at different times (deck-time vs metrics-time), so
  unifying them is a design call not a de-duplication.

- FLAGSHIP RUNS REFINED (NATE 2026-08-26, "I saw points again"): the
  delivered flagship canary ALWAYS runs at refined resolution (coastal
  50 m: continuous inundation fringe, 85%+ fill, 41-frame GIF); coarse
  runs are silent pins only, never delivered. The sparse triangles NATE
  saw were the coarse 250 m run - barycentric working (whole elements,
  not node dots) but extent-class physics at coarse resolution.
  Rasterization method stays MECHANISM, not a knob (one honest method -
  the solver's own piecewise-linear representation); a second honest
  method would be a style-contract entry. Canary-evolution discussion
  QUEUED at the mesh wave's close (interim refined coastal -> the
  artemis BYO rematch w/ authored conformal mesh).

- BED-INPUT DOTS = THIRD NODE-DOT INSTANCE (NATE spot-check 2026-08-26,
  co-reviewed): the worker-written bed_bathymetry.tif is node SAMPLES
  written as isolated pixels - the in-worker fetch path the barycentric
  fix (result rasterizers only) never touched. NO interim patch - the
  producer dies in the fetch-migration wave: bed input becomes a STAGED
  server-fetched artifact (ladder/provenance/visibility) and the input
  layer becomes the continuous clipped source raster at native
  resolution (more honest as an "input": what the run was GIVEN, not a
  scatter of what the solver kept). ACCEPTANCE DELIVERABLE of the
  fetch-migration wave: this exact panel re-rendered continuous.

- TELEMAC REMAINS OPEN (NATE 2026-08-26): the family does NOT close until
  (1) VIOLATION 1 - the in-worker DEM/bathymetry fetch - is OUT (the
  fetch-migration wave; --network none on the TELEMAC image is the
  gate), and (2) the flagship proof set is RE-RENDERED current-standard
  with MEASURED legend stability: legend-region hash count == 1 across
  all frames (NATE's moving-ramp observation measured at 41/41 distinct
  on the stale pre-wave-A render - per-frame GIF palette re-quantization,
  fixed in wave A but the proof store still holds pre-fix renders =
  tainted evidence). The assembler's --check flags stale/drifting GIFs;
  all pre-fix animations get re-rendered, and the max-vs-colorbar
  question (2.59 m label vs 0.94 bar) gets settled in the same pass
  (if the ramp deliberately clips, the legend must SAY so).

- PROOF PACKET = THE DELIVERY MECHANISM (NATE 2026-08-26, "the checklist as a
  script that cannot forget"). `scripts/assemble_proof_packet.py --template X
  --variant coarse|refined|postmigration|addendum` renders the full NATE
  checklist (every published layer as a full-size panel in emission order, the
  canvas view, the contact sheet, every chart dock-true, the GIF + its
  peak-or-final still) and writes `packet.json` beside it: the ORDERED
  deliverable paths + a one-line caption + a per-item verdict. The delivery step
  is now "send exactly what packet.json lists", zero judgement. It REFUSES with
  a named missing-list on: a missing GIF where the run's own SELAFIN MEASURES
  more than one frame (header + file-length arithmetic, cross-checked against
  the worker's ntimestep - the decision is measured, never remembered); a GIF
  whose frames are not all distinct or whose colorbar strip is not byte-identical
  across frames; a short panel set; a zero-byte file; a PNG whose
  `trid3nt_run_id` stamp is not this run's; anything older than the evidence JSON
  beside it (STALE, with both mtimes); and frames drawn off the run's AOI.
  `--check` audits an old folder without rendering. Wired into
  `canaries.main`, so every canary close produces a packet or exits non-zero.
  FIRST CATCHES: (a) the wave-B coarse coastal folder - stale GIF/chart/sheet and
  TWO panel generations in one directory; (b) every GIF in the repo predated the
  StablePaletteWriter fix and drifts its legend; (c) the refined coastal run's
  telemac_metrics (parser coastal-tidal-2) records no bbox, so its animation had
  been landing at the UTM false origin - the assembler now falls back to the
  canary's declared bbox and refuses when the drawn extent misses the AOI.

- DELETION POSTURE BIFURCATED + THE CAMPAIGN THESIS (NATE 2026-08-26):
  THESIS on record - the whole-repo goal is cohesion: TELEMAC is the
  sculpting SAMPLE refactored n times; then the switch flips and the
  fleet migrates once onto the settled architecture; stale code weighs
  down every future refactor, so shrinking is itself a goal. POSTURE:
  (1) WORKFLOW/physics surfaces = CAREFUL - parity binding,
  keep-until-superseded w/ named conditions (blanket coherence). (2)
  EVERYTHING ELSE = AGGRESSIVE - evidence of staleness is sufficient
  cause, delete now, ledger line for traceability only, no conditions,
  no supersession ceremony. Unlocks: the scripts/ audit (34k), server/
  web-era sweep, dead-path tests, composer leftovers outside workflows -
  chartered as the STALE SWEEP wave once the current three agents land.

- tools/meta/ CHOP CANDIDATE (NATE 2026-08-26): "meta" is a junk-drawer
  category-era label - named target for the STALE SWEEP wave. Expected
  verdict: death by dispersal - code_exec_tool -> its own proper home
  (the playground is load-bearing doctrine, not "meta"),
  spatial_input_tool -> beside the gate/user-input machinery,
  list_run_frames -> emission/display seam, compose_case_report ->
  staleness check (who invokes it? aggressive posture applies),
  passthroughs -> inspect. The folder dies even where the tenants live.

- data/ DELETED 2026-08-26 - the category-era fossil is gone. Last six
  tenants (model_debris_flow, postprocess_pelicun, the four
  set_<engine>_parameters setters, the three unregistered MODFLOW engine
  surfaces) relocated to workflows/<engine>/ or tools/processing/; see
  docs/DELETION_LEDGER.md for the per-tenant table.

- CHOP CANDIDATES VERDICTS (NATE-named, co-verified 2026-08-26):
  runaway_guard.py = KEEP, live safety-critical (turn stream +
  context_budget import it; born from the 2026-06-25 runaway incident;
  a local daemon wedges identically) - but its cloud-era docstring
  archaeology (EC2/SSM narration) gets scrubbed in the stale sweep.
  MALPASSET constellation (cases/malpasset_obs.py 285, scripts/
  run_l2_malpasset.py 614, tests, fixtures, staged case) = HARVEST THEN
  CHOP: it violates the US-only hard rule (French case) BUT the harness
  is a working mini-calibration loop (obs pairing, NSE/KGE/RMSE skill
  metrics, friction adjustment toward the published band, re-run,
  re-score) - the best prior art for the calibration track. It lives
  until the calibration design harvests its shape; then dies under
  US-only, superseded by coastal-surge-vs-CO-OPS as the V&V exemplar,
  ledger row citing supersession.

- DOCUMENTATION STANDARD CARRIED FORWARD (NATE 2026-08-26): code
  comments/docstrings state CONSTRAINTS ONLY - no ADR/SRS/job-id
  references, no HISTORY narration, and NO PERSON ATTRIBUTION ("NATE
  ruling/NATE-provided/per NATE" in code is a violation - names live in
  decision records: IDEAS, ADRs, ledgers, commit messages; the code
  states the rule itself, not who made it or when). EFFECTIVE
  IMMEDIATELY in every wave charter so the problem stops growing; the
  BACKLOG (old ADR refs, name refs, era narration across the tree)
  remediates in a DOCUMENTATION REFACTOR WAVE folded into the stale
  sweep - mechanical scan: grep for ADR-\d+/SRS/FR-\d+/job-\d+/NATE in
  *.py comments/docstrings, rewrite each as the constraint it protects
  or delete if it protects nothing.

- MALPASSET: CHOP, NO HARVEST + US-ONLY RULE REFINED (NATE 2026-08-26):
  the L2 malpasset harness is NOT kept for prior art - the calibration
  track builds FRESH (published methods remain design references via
  paper-first, but no code inheritance). The whole constellation
  (cases/malpasset_obs.py, scripts/run_l2_malpasset.py, tests, fixtures,
  staged case data) chops in the stale sweep. FENCE STRUCK (corrected
  2026-08-26, verified against the tree): this entry claimed "a live
  import in postprocess_telemac.py, so the chop waits for wave C to
  land". There is no such import. postprocess_telemac.py's only
  malpasset mention is a comment at :293 recording where the
  free-surface variable names were verified from (the bundled
  f2d_malpasset-small.slf header) - a provenance sentence about a
  fixture, not a dependency. The complete repo-wide importer list for
  trid3nt_server.cases.malpasset_obs is scripts/run_l2_malpasset.py and
  tests/test_malpasset_obs.py, both inside the constellation being
  chopped; zero live product modules import it. The chop is UNBLOCKED
  and awaits the stale sweep, not an import removal or any wave landing.
  Registered in docs/DELETION_LEDGER.md as QUEUED with that condition.
  RULE REFINED: "US-only" was an INFRASTRUCTURE scope, not a
  nationality rule - spot-check/validation/calibration cases go wherever
  GAUGES AND SENSORS our substrate can fetch live; the US simply has the
  best observation infrastructure, so it dominates. Cases elsewhere are
  legal when the observations are fetchable through our fetchers.

- ANIMATED FIELD IS DECLARED, NEVER DEFAULTED (NATE 2026-08-26, caught in the
  first assembled packet). The packet assembler's mechanical re-render painted
  coastal WATER DEPTH because the variable choice lived in hand-typed
  `--var`/`--mask-var` flags and the script fell back to a default. Depth over a
  tidal bay is bathymetry-dominated and barely moves; the ruled field is FREE
  SURFACE masked to `WATER DEPTH > 0.02` (the coastal worker's own WET_TOL, the
  same discriminant `peak_wl_max_m`/`flooded_land_km2` use - TELEMAC sets FREE
  SURFACE = BOTTOM on dry nodes, so unmasked it is scaled by the highest hill).
  MEASURED: the ruled field changes a median 430,025 px/step against depth's
  77,183. FIX: `trid3nt_server/testing/proof_animations.PROOF_ANIMATIONS` -
  per-template variable / mask_var / mask_threshold / still / plane / physics
  reason / exempt_reason, homed beside the canary declarations and re-exported
  from `canaries.py` so the two read as one declaration surface. A time-stepped
  template with NO declaration refuses to render an animation; there is no
  default variable anywhere.
  QUEUED, two style-contract gaps the declaration had to work around: there is
  no `water_level` / water-surface-elevation row and no `dissolved_oxygen` row,
  so coastal and do_sag declare `quantity=None` and take the neutral ramp rather
  than borrow `flood_depth`'s label for a field that is not that quantity. Two
  `quantity_defaults` rows would close it.

- DOUBLE DEM FETCH (NATE spot-check 2026-08-26, rain_on_grid packet):
  design is sound (delineation DEM over the broad pre-catchment box vs
  bed DEM over the catchment through the 3dep->copernicus ladder) BUT
  this run exposed two defects, both fetch-migration-wave scope:
  (1) 3DEP FELL BACK AT COWEETA (US lidar heartland - should hit);
  the provenance names the winner but NOT the firing reason - the
  fallback norm demands "3DEP FAILED: <error> -> copernicus" LOUD in the
  label; diagnose the actual 3DEP failure. (2) SAME-SOURCE REUSE RULE:
  when a ladder resolves to the same source+resolution as an
  already-fetched raster covering the window, CLIP from the raster in
  hand instead of refetching (the watershed front half-designed this;
  the fallback path does not participate).

- COASTAL t0 WETTING RESOLVED + RUN-VS-CODE STALENESS GAP (2026-08-26):
  the inspected 50 m run PREDATES the datum fix (offset 0.0 vs the
  independently re-verified -0.232) - wet-at-t0 split: 84.2% genuine
  intertidal, 15.8% (19 km2) stale-forcing bias (persistent, never
  drains - drain-transient hypothesis refuted 99.92% stay wet),
  disconnected fill negligible (37 nodes). NO spin-up change warranted.
  Fix = the family-close flagship RERUN through current code, ACCEPTANCE
  = t0 wet area drops ~19 km2 to ~101 km2 (a predicted number, not a
  refresh). NEW MECHANICAL GAP: nothing checks RUN-vs-CODE staleness -
  runs record the dispatch git-sha in the manifest; the packet lists
  commits touching the engine's paths since that sha ("this run predates
  N relevant fixes" as a named warning). Queue to the assembler/journal
  lane.

- PROOF-LANE STINT 3 (NATE spot-checks, 2026-08-26). Three panel defects and two
  animation defects, all fixed as DECLARATIONS or ADAPTIVE rules, none as a
  per-case tweak. (1) Vector styling is adaptive on VERTEX count with a dark
  casing under every stroke - 1,902 river reaches were drawn as near-invisible
  hairlines over imagery. (2) Per-layer panels frame the LAYER, not the canvas
  union, and say in the caption how much closer they sit (rog panel 05: 32x); the
  CANVAS VIEW keeps the shared extent. Mesh layers draw as adaptive-weight
  wireframe triangles - what NATE read as "sparse orange node-dots" was a
  0.12 pt wireframe over a speck. (3) PROOF_ANIMATIONS became a per-tool LIST:
  coastal declares surge_dynamics + inundation (initially-dry-land gate off the
  run's own init_wl_m, the flooded_land_km2 discriminant), rog declares
  inundation_depth (LOG ramp) + flow_dynamics (VELOCITY MAGNITUDE derived from
  U/V, streamlines at declared density 1.4 on a declared 200-point interpolated
  grid). The checklist requires ALL declared animations. (4) Three style-contract
  rows added: continuous_water_level_m (cividis), continuous_dissolved_oxygen_mgl
  (rdylbu), continuous_flow_velocity_ms (plasma) - so coastal/do_sag/rog velocity
  stop taking the neutral ramp and no field borrows flood_depth's label.
  FOUND ALONG THE WAY: the coastal WET_TOL 0.02 is a COASTAL discriminant and was
  wrong for rain-on-grid, whose entire field peaks at 0.0273 m - masking at 0.02
  kept a 7 mm sliver and threw the answer away. rog masks at depth > 0 instead,
  declared with the reason. QUEUED: the streamline styling is the reference for
  QGIS-native mesh vector rendering when mesh-layer publishing lands - the dock's
  vector symbology must reproduce streamlines over a magnitude ramp at a declared
  density, or the proof sheet and the product show the same run two ways.

- 2026-08-26 SFINCS RETRIEVAL FLOOR IS FRAGILE (RECORD-ONLY, ledger audit of
  0f7a6351..02acbfed - finding, no build proposed). `sfincs_flood` is a
  hardcoded literal in CORE_FLOOR (tool_retrieval.py:77), the always-visible
  set, so it is handed to the model every turn regardless of ranking. Three
  things follow, and the third is measured.
  (1) Its 22-query corpus (workflows/sfincs/flood/corpus.yaml) carries none of
  its findability. The corpus could regress to empty and every retrieval test
  would still pass, because what those tests assert is `CORE_FLOOR <= res`
  (tests/test_tool_retrieval.py:56), which the floor satisfies by itself. The
  canonical test query is "model the flood" - verbatim corpus line 2 - but the
  assertion it feeds cannot tell the corpus from the floor.
  (2) Nothing asserts a CORE_FLOOR name is a REGISTERED tool. The only guard is
  a negative, name-specific one added after publish_layer was deleted
  (test_tool_retrieval.py:43, `assert "publish_layer" not in CORE_FLOOR`), and
  the shadow test FILTERS instead of asserting
  (test_tool_retrieval_shadow.py:193, `{t for t in CORE_FLOOR if t in
  TOOL_REGISTRY}`). A rename of sfincs_flood leaves a dead string in the floor
  and no test fails - and a rename is exactly what the template-capability
  naming rule queues, since a name should be the question class, not the
  engine.
  (3) MEASURED: removing the floor and leaving the corpus and ranking untouched,
  sfincs_flood drops out of the top-8 for 2 of 5 natural flood asks - "how deep
  will the flood water get here" and "will my street flood" both MISS, while
  "model the flood", "model flooding in this county" and "simulate inundation
  from the storm" hit. The first miss is a near-verbatim paraphrase of the
  template's own corpus line "how deep will the flood water get at this
  location". So the corpus is measurably weaker than the floor makes it look,
  and the floor is what stops anyone from finding that out. The sibling
  sfincs_advanced_numerical_physics_knobs has no floor entry and 12 far more
  literal queries; it survives paraphrase today, which is the comparison that
  shows the flood template's corpus was never under the same pressure.

- 2026-08-26 `When` IS PRODUCTION-UNEXERCISED (RECORD-ONLY, same audit -
  finding, no build proposed). The declarative library's conditional construct
  (workflows/lib/plan.py:416, decided by the interpreter at
  interpreter.py:259) has ZERO production uses. `grep -rn "When(" --include=*.py`
  over the whole repo, excluding workflows/lib/ and tests/, returns NOTHING;
  all 12 call sites live in one file, tests/test_declarative_library.py. Seven
  static plans are registered and none declares a branch.
  Why it matters rather than being trivia: ADR 0314 turned a validator refusal
  INSIDE OUT for this construct. `_check_revisable_branches` - which refused a
  plan that declared a FormGate and branched on a revisable param - was DELETED
  because the shape it forbade became the intended one, and
  `_check_when_conditions` replaced it. So the library carries a
  late-bound-condition contract, a scope rule (a When body is a scope, which
  ADR 0315 section 3 cites as the reason the structure slot could not have been
  written as a literal When), guard-chain flattening in `_flatten_guarded`, and
  the demand-pull property that a When-guarded consumer whose branch does not
  fire costs no fetch - and every one of those is asserted only by its own unit
  test. The first template that branches will be the first real reader of that
  contract, and ADR 0315 already records one case where the construct was
  reached for and turned out not to fit.

- 2026-08-26 `Step.kwargs` IS FROZEN SHALLOWLY (RECORD-ONLY, same audit -
  finding, no build proposed). `Step.__post_init__` (workflows/lib/plan.py:342)
  does `MappingProxyType(dict(self.kwargs))` - one level. plan.py does not
  import `deep_freeze` at all. `Slot.__init__` (workflows/lib/slots.py:70) does
  `MappingProxyType({k: deep_freeze(v) ...})`, so the binding blocks ARE frozen
  all the way down. The two halves of one static plan are frozen to different
  depths.
  MEASURED, from the shipped classes: a Step built with
  `kwargs={"cfg": {"a": [1,2]}, "seq": [3,4]}` yields `Step.kwargs` type
  mappingproxy, nested `cfg` type dict, nested `seq` type list;
  `s.kwargs["cfg"]["a"].append(99)` and `s.kwargs["seq"].append(5)` both
  SUCCEED, and `s.kwargs["cfg"] is inner` is True - the proxy aliases the
  caller's own object rather than copying it. The same values through `Slot`
  come back mappingproxy and tuple.
  Why it matters: ADR 0314's whole argument for deep-freezing is that a
  declared value "lives at module scope for the life of the process and every
  run of the template reads the same object, so a mutable container inside one
  is a cross-run channel" - `deep_freeze`'s own docstring, slots.py:34-37. The
  static plan is built ONCE at registration and has exactly that lifetime, so a
  container nested in a step's kwargs is the same cross-run channel the
  docstring names, in the half of the plan that was not frozen.
  CURRENTLY LATENT, not live: walking all 7 registered static plans and their
  45 steps finds ZERO `Step.kwargs` values that are still dict/list/set. Landed
  templates pass scalars, Refs and strings. So this is a guarantee the type does
  not carry rather than a bug anyone is hitting - which is the right time to
  record it, and the reason it will not announce itself when someone does.

- 2026-08-26 TELEMAC3D SILENTLY FLOORS A LEGAL RESOLUTION ASK (RECORD-ONLY,
  ledger audit of 0f7a6351..02acbfed - finding, no build proposed).
  `stratified_flow/declarations.py:74` declares `target_resolution_m` with
  `bounds=(50.0, 20000.0)`, so a 50 m ask passes the resolver's clamp and is a
  legal, in-contract request. `workers/telemac/telemac3d_build.py:139` then sets
  `GRID_H_FLOOR_M = 400.0` and `:629-630` floors the ask to it with no note and
  no provenance row. The declared floor and the enforced floor differ by 8x, and
  the run reports the number it was asked for rather than the number it solved.
  This is the narrate-on-adjust rule broken in the direction that matters: an
  adjustment nobody is told about. The open-water front already has the honest
  shape for this - `mesh_sizing_provenance` exists precisely to turn a silent
  override into a stated one, and telemac3d does not reach it for the horizontal
  floor.
  Two halves, and they land in different places. The PRODUCT half is a contract
  that promises a range the engine will not honour: either the declaration's
  lower bound rises to what the solver actually accepts, or the floor becomes a
  narrated clamp carrying a provenance row. The WORKER half cannot land without
  an image rebuild, so nothing here is inert-safe to change piecemeal.
  Related and separate: the same audit found three (not four) grid floors
  genuinely duplicated against their params' declared bounds. Those are a
  de-duplication chore; this one is a contradiction, and only this one is a
  correctness defect.

- 2026-08-26 THE STYLE CONTRACT CAN MIRROR ITSELF, AND NOTHING WATCHES FOR IT
  (RECORD-ONLY, panel-2 remediation - finding, gate attempted and withdrawn).
  Collapsing the publish path's preset-to-label table meant giving the contract
  a row for each of the seven presets that had existed only in code. Three of
  those seven turned out to be second spellings of rows the contract already
  had: `categorical_aspect` against `aspect_compass_deg`,
  `continuous_impervious_surface` against `impervious_surface_pct` (both
  byte-identical but for the label), and `continuous_slope_pct` against
  `slope_angle_deg`. None of the three had a single consumer anywhere in the
  tree. Migrating a dead mirror INTO the contract is not collapsing it, so the
  three rows were deleted rather than kept as synonyms.
  What this exposes is a blind spot in the policing gate: it walks CODE for
  literals keyed on preset names, so it cannot see a mirror that fits inside the
  one file the whole design rests on.
  A gate was attempted and withdrawn, and the reason is worth keeping. Comparing
  rows by their painting decisions with the label removed flags ten groups, and
  almost all are legitimate: `era5_2m_temperature`, `gridmet_tmmn`,
  `gridmet_tmmx` and `hrrr_2m_temperature` are four genuinely different
  quantities that happen to be painted alike. Same look is not same quantity, so
  that fingerprint is the wrong test.
  The property that would actually catch it is REACHABILITY - a preset row no
  producer and no `quantity_defaults` entry can arrive at is dead weight that
  can only ever drift. Measured on the tree: 14 of 69 rows are unreachable by a
  static search, but 13 of those are the dataset-named `era5_*` / `gridmet_*` /
  `goes_*` / `hrrr_*` family, whose names are plausibly composed at runtime from
  a variable name. A hard gate would fire on all 13 and be turned off within a
  week. Closing this properly means first establishing whether those names are
  composed or dead; if composed, the gate needs the composition sites declared
  rather than guessed at, which is the same lesson as every other mirror here.

- REACH-FAMILY FETCH MIGRATION - THE MONSTER LEFT UNBROKEN (2026-08-26, the
  fetch-migration wave, ADR 0317). The four open-water families are migrated and
  run `--network none`; `telemac_river_dye_build.py` still holds SIX network
  fetches (NLDI `comid/position` snap, two NHDPlus_HR `/3/query` flowline
  re-seeds, the NLDI navigate that IS the model centerline, the NHDArea `/8/query`
  bank polygons, and the private Copernicus-STAC -> 3DEP DEM ladder), so
  river_dye / do_sag cannot take the posture. TWO blockers, both measured rather
  than assumed. (1) NATE OWES A RULING on the producer: the canvas shows an OSM
  `fetch_river_geometry` layer while the mesh is built on an NLDI centerline
  nobody sees, and making the declared layer the CONSUMED one moves the seed and
  therefore the physics - the false-surface repair is not free. (2) The DEM rung
  is NOT a like-for-like swap: over the Eel River canary reach the router's
  `fetch_dem(source="copernicus")` mosaic differs from the worker's own
  `/vsicurl` sample of the same GLO-30 tiles by RMS 3.87 m, max 22.3 m on valley
  walls (mean 0.002 m - a robust along-channel fit may survive it, which is worth
  MEASURING on the real reach nodes before deciding). The parity-exact route is a
  native-grid STAC mosaic that does not re-grid, an executor change with its own
  risk. Sequence: measure the fit first (cheap, decides everything), then take
  NATE's producer ruling, then migrate all six together.

- do_sag REFINED CANARY IS NON-DETERMINISTIC (found 2026-08-26 proving the
  fetch-migration wave; NOT caused by it). Two consecutive runs, no code change:
  one sampled the sag curve every 7.4 m with BOD zero at every station and DO
  minimum 8.9964 @ 692.1 m, the next reproduced the recorded pin exactly
  (9.0081 @ 123.5 m). Cause is almost certainly `_mainstem_flowline_seed` being
  FAIL-OPEN: a slow NHDPlus_HR query keeps the raw position seed and meshes a
  DIFFERENT reach, and nothing in the record says which happened. Two lessons:
  a silent-ladder bug costs REPEATABILITY as well as visibility, and a canary
  whose pin can flip is not a pin. Dies with the reach-family migration; until
  then a do_sag comparison that disagrees deserves a second run before it is
  called a regression.

- SAME-SOURCE REUSE RULE - DESIGNED, NOT BUILT (2026-08-26, ADR 0317). "A ladder
  that resolves to the same source AND resolution as a raster already fetched for
  a window COVERING this one clips rather than refetches." Shape: per-process
  registry of raster-cog fetches keyed on (source_class, every non-bbox resolved
  param), reuse only when the held bbox strictly CONTAINS the request, provenance
  stamped with what was reused. Not built in the fetch wave for two reasons: it
  lives in the router, whose wrong answer is wrong DATA rather than a wrong
  layout; and the case that motivated it turned out NOT to be one (see the Coweeta
  finding - two DEMs, two datasets, two purposes, nothing to reuse). Wants a real
  motivating case before it earns the risk.

- COMPUTE-CLASS VOCABULARY RENAME (medium -> standard) QUEUED (2026-08-26). The
  schema contract says `small|standard|large|gpu`; the fleet says `medium`.
  `COMPUTE_CLASS_ALIAS` is now the ONE definition (exported, read by everything
  that validates a class) and `medium` is a documented synonym rather than a
  parallel vocabulary. Finishing it is 84 occurrences across 42 files, most of
  them model-facing `Param` defaults and template declarations whose provenance
  rows and recorded canary args all move - a fleet-wide rename, so it belongs with
  an engine-wide sweep and NOT smuggled into an engine wave. Also open: the schema
  `Literal` has no `xlarge` while the alias map, `run_solver`'s docstring and
  `telemac/steps/solve.py` all accept one.

- RIVER TRUTH RULED (NATE 2026-08-26, ruling a): NLDI EVERYWHERE - the
  NLDI mainstem centerline is promoted to a server-side router spec like
  every other fetch, and it becomes the DISPLAYED river input layer (the
  meshed river is the visible river; zero physics change - the mesh was
  always NLDI). OSM waterways demote to optional context (pretty map
  under the plumbing). Unblocks the reach-family migration = the last
  six in-worker fetches = --network none for the whole TELEMAC image =
  the family CLOSES.

- SAMPLE PURITY RULED (NATE 2026-08-26): the TELEMAC sample must always
  be END-STATE-PURE - no interim solutions inside its boundary (interim
  code inside the sample lies about the architecture; "interim" may
  exist only OUTSIDE, as pre-migration code that dies at its engine's
  migration). Generalization style: reusable INTERFACES + small HOOKS
  any new model fills - never per-engine code for a shareable need.
  CONSEQUENCE: rerun-with-overrides + coupled-validity rules become the
  NEXT TELEMAC-scoped skeleton wave (after the reach close), landing
  WITH set_telemac_parameters deleted in the same series (ledger
  condition met, not waited on); calibration then consumes a landed
  primitive instead of discovering a missing one. The other three
  setters are outside the sample - they die at their engines'
  migrations.

- REACH-FAMILY MIGRATION LANDED, AND THE COPERNICUS DEFERRAL CLOSED BY FIXING THE
  SAMPLING (2026-08-26, ADR 0318). All six in-worker fetches are server tier; the
  TELEMAC image runs `--network none` on every leg. The measured lesson worth
  keeping: ADR 0317's RMS 3.87 m was NOT a data difference, it was the router
  resampling a 1-arcsecond source onto a lattice of its own (142x124 px where the
  source is 3600 px/deg). The `stac_float` executor gained a declared
  `px_per_deg` sizing whose PHASE is read off the source's header - and the phase
  is the whole trick, because a global 1-arcsecond DEM tile is pixel-is-POINT, so
  a lattice snapped to the prime meridian lands every destination pixel centre
  exactly BETWEEN two source centres and is measurably WORSE than the metric grid
  it replaces (RMS 1.26 vs 1.03 m). Snapped to the source's own origin with
  NEAREST: exact per-point parity, 0.000000 m. GENERAL RULE: before accepting a
  tolerance on a migrated fetch, check whether the difference is the DATA or the
  DESTINATION GRID - the second one is fixable and the first one is not.

- FAIL-OPEN COSTS REPEATABILITY, NOT JUST VISIBILITY (2026-08-26, ADR 0318,
  closing the do_sag flake). The reach seed ladder degraded to the raw seed on any
  exception, so a slow query meshed a different river and the record could not say
  which run had happened. The repair is a DISTINCTION, not a retry: "the query
  answered and the answer was no improvement" is a decision and gets a named rung;
  a fetch failure raises. Plus a checkable determinism artifact - the run records
  a sha256 of the staged centerline bytes, so "the same run twice" is verifiable
  rather than an impression. Worth copying to every other silent-ladder site.

- 2026-08-26 THE PROOF RENDERER SPOKE THE WRONG COLORMAP DIALECT (found + fixed,
  ADR 0318). The style contract's colormap names are rio-tiler's (lowercase);
  matplotlib spells every ColorBrewer ramp CamelCase. `render_all_layers_proof`
  handed the legend name straight to `imshow`, so ANY layer whose legend names
  `rdylbu` (dissolved oxygen) took the whole delivery packet down with a hard
  ValueError - while `viridis` and `gray` spell the same in both dialects and hid
  the mismatch for as long as nobody delivered a DO packet. TWO-VOCABULARY CLASS:
  the same disease as `medium` vs `standard`. Resolved case-insensitively against
  matplotlib's own registry rather than a second hand-written table.

- 2026-08-26 STALE PANEL GENERATIONS IN FIVE PROOF FOLDERS (found by the
  assembler, cleaned). A layer roster that changes renames the panels, so the
  previous generation sits in the folder under names nothing writes any more and
  the count check refuses. Caught in do_sag, river_dye, coastal, tomawac and
  telemac3d refined folders. WORTH A GATE: the assembler already knows the
  expected panel set, so it could OFFER the superseded list rather than only
  refusing on the count - "these N files are from a previous roster" is a more
  actionable refusal than "found 14, expected 9".

- 2026-08-26 TWO REFINED OPEN-WATER PINS WERE STALE, NOT MOVED (ADR 0318). The
  tomawac and telemac3d REFINED baselines were last written at 33e879cf, before
  ADR 0317's own bed migration - that wave re-ran and re-pinned only the COARSE
  legs of those two families. So a comparison this wave made read as a regression
  and was not one. LESSON: a wave that re-pins a family must re-pin EVERY variant
  of it, or the unre-pinned ones become landmines for the next wave. Worth a check
  in the canary runner: when one variant's evidence is newer than another's by
  more than a wave, say so.

- REFERENCES ARE BENCHMARKS, NEVER SHAPE (NATE 2026-08-26): external
  systems/packages/papers (tpilz, JMSE, walkthroughs) serve to JUDGE our
  ergonomics and ground our physics - the architecture's shape derives
  from our own conclusions and rulings, never from imitating a
  reference. Cite them as measuring sticks in reports; never as design
  drivers in ADRs.

- RERUN-WITH-OVERRIDES LANDED, AND set_telemac_parameters IS GONE (2026-08-26,
  ADR 0319). Decision 1 is built: a run derives from a run, the parent's
  records are planted under the child's ledger key so inherited work is the
  parent's own objects, and coupled-validity rules carry the setter's law-aware
  bounds as a declared predicate. The measured lesson worth keeping: the reuse
  a derivation gets is exactly as fine-grained as the PLAN'S NODE BOUNDARIES.
  `telemac_do_sag` stages its terrain inside the `deck` step, so a `k1_per_day`
  override - which has nothing to do with terrain - re-executes the step that
  stages it. Nothing was wrong (the bed is content-addressed and the same object
  came back), but the general rule is: a value a template wants reusable across
  overrides gets its OWN node, and node granularity is now a template-authoring
  decision with a visible consequence. QUEUED for whoever next opens the reach
  family's plan shape.

- CALIBRATION-LOOP GAP, NAMED (2026-08-26, ADR 0319). The primitive is the
  loop's engine and the three pieces it still needs are: OBSERVATIONS to score
  against, an OBJECTIVE that turns answer-vs-observation into a scalar, and a
  PROPOSER that picks the next override. The hard rule for that wave: the driver
  CONSUMES `rerun_workflow` and never grows its own re-run path - two
  implementations would disagree about what a derived run is.

- CANARY REPLAY IS A DIRECT CALL NOW (2026-08-26). `scripts/replay_canary_evidence.py`
  re-issues every committed canary from its own evidence file (`tool` + `args`)
  and diffs the ANSWER field-for-field, so family parity is one command instead
  of a WS session per template. Two things it surfaced: (1) four canaries were
  recorded in `user_gated` sessions and CANNOT run headless at all - law 9
  refuses their physics-consequential labeled defaults with no card to approve
  them on - so the driver has an `--approve-defaults` flag that supplies those
  declared defaults BY NAME and reports which rows it approved; (2)
  `telemac_river_dye/coarse`'s evidence file predates the `tool`/`args`/`metrics`
  fields and describes a run nobody can re-issue. WORTH DOING: re-record that one
  so the family has no unreplayable member.

- PARK AFTER CALIBRATION + OFFICIAL-TELEMAC-PYTHON RECON (NATE
  2026-08-26): TELEMAC work PARKS after the calibration campaign.
  BEFORE more TELEMAC code: recon gitlab.pam-retd.fr/otm/telemac-mascaret
  - the distribution in our OWN image ships telapy / data_manip (SELAFIN
  IO) / postel / validation cases; audit our hand-rolled readers/writers
  (read_selafin, slf/cli/cas authoring, mesh writers) against it:
  verdict per module = JUSTIFIED-BESPOKE (server-side, must not depend
  on a TELEMAC install) vs WHEEL-REINVENTED (in-worker, the official
  lib sits beside it). References-are-benchmarks applies to the
  ARCHITECTURE; for ENGINE-NATIVE IO the official implementation is the
  presumptive winner (leverage-libraries rule).

- TELEMAC PARK-WORK LIST (recon verdict 2026-08-26: we did NOT reinvent
  much - workers already wrap official TelemacFile; server clean-room
  reader JUSTIFIED by weight (compiled Hermes .so = full env); cas/cli
  plain-text authoring stays). Sized S: (1) THE DICO - wire the engine's
  own keyword dictionary as deck validation + param bounds source + the
  full-engine-control searchable layer (the one genuinely new leverage);
  (2) dedupe assemble_proof_packet's internal copy of read_selafin
  (~90 LOC); (3) decision note codifying the server/worker IO split so
  nobody "fixes" the intentional clean-room reader; (4) FEEDSTOCK: mine
  the official per-module examples tree (bump/donau/artemis/tomawac/
  gaia cases) as V&V template candidates - the modflow6-examples pattern
  for TELEMAC; (5) telapy = the future coupling door, not now. Executes
  at the post-calibration park.

- TELAPY RULED IN + THE STEPPABLE-ENGINE BRIDGE (NATE 2026-08-26,
  supersedes the recon's not-now on telapy): the post-calibration
  TELEMAC park-work centers on adopting telapy behind OUR solve-stage
  bridge - a SteppableEngine interface (BMI-lite: initialize/step/get/
  set/probe/finalize), one engine-blind GENERIC STEP DRIVER as the box
  entrypoint, telapy as the first adapter (T2D/T3D/ART/WAC), Mf6Xmi +
  Pyswmm as thin future adapters, SubprocessFallback w/ honest
  capability flags for API-less engines. THE BOX CONTRACT IS UNCHANGED
  (staged dir in, results out, --network none, stateless across jobs) -
  only the interior changes. Unlocks: per-step gauge PROBES (the
  calibration objective in-flight), live frame streaming, early-stop
  guards, WARM-RESTART calibration sessions (one session = one box
  job). TWO-AUTHORITY LAW: the deck/manifest is the sole authored
  record; runtime set() legal only in sanctioned loops and journaled as
  overrides. Examples = feedstock + V&V pins, not running workflows;
  meshing generation unchanged (telapy does not mesh). Blueprint
  section 5 carries the UML. Handrolled code that the library
  supersedes DELETES per the demolition clause. Sequencing: calibration
  v1 runs on the current subprocess path; the bridge lands at the park
  and calibration v2 gains the warm loop.

- THE FIVE-CATEGORY INPUT TAXONOMY + VERBS (NATE 2026-08-26): adopt the
  classical simulation decomposition as the DECLARED classification of
  every Param/slot member - GEOMETRY (mesh/domain) / BOUNDARY
  CONDITIONS / INITIAL CONDITIONS / PHYSICAL PARAMETERS / NUMERICAL
  PARAMETERS (spatial resolution, time steps) - retiring the ad-hoc
  consequence vocabulary. Buys: form card grouped by category,
  agnostic setter/rerun vocabulary ("override a boundary condition"
  means the same on every engine), per-category sensitivity labeling,
  trivial cross-engine generalization. VERBS as the mental model:
  select (retrieval/template roster) -> collect (doors + substrate,
  categorized) -> [author bridge adapts the agnostic JSON sheet to the
  domain] -> solve -> emit. STEPPING DEMOTED to a capability flag on
  the SteppableEngine bridge - run-to-completion stays the default
  verb; stepping serves only warm calibration, live streaming, and the
  digital-twin door. Emission unchanged and generalizes across DATA
  TYPES (raster/mesh/vector/series/chart) on the one seam, never new
  seams.

- BRIDGE + TEMPLATE FRAME NAMED; MESH = A SUBSTRATE (NATE 2026-08-26):
  the architecture is BOTH patterns, orthogonal - Template Method
  vertically (the plan is the fixed verb spine; templates supply
  values never structure) x Bridge horizontally (every domain-varying
  step has an agnostic left face and a per-engine right face; the
  right face = the engine's official library where one exists). MESH
  STEP ISOLATED LIKE THE FETCHERS: workflow declares only the MESH
  slot (shape + policies = frozen ASKS, never meshes); ONE router,
  supplied-first then registered strategies (watershed TIN,
  coastal_water_edge, hecras_rog, oceanmesh...); every path converges
  on MeshArtifact; compat gate refuses loudly. MeshHandle dissolves
  into MeshArtifact when strategies build eagerly (mesh wave).
  QGIS-TRANSLATION PLACEMENT (recommended, awaiting nod): split along
  the bridge - mesh/ owns SOLVER-facing formats (.slf/.gr3/bundle);
  EMISSION owns the DISPLAY face (domain mesh -> MDAL layer; the
  _write_2dm writer moves out of generate_mesh onto the seam) - razor:
  feeds a solver = mesh/, feeds a screen = emission. Emission stays
  DECLARATIVE (styles.yaml, .style(), restyle_layer), generalizing by
  DATA TYPE (raster/mesh/vector/series/chart), never by engine.
  Target-arch UML published: the Bridge Blueprint artifact
  (supersedes the Skeleton Blueprint as the forward picture).

- THE MESH TOOL SHAPE LOCKED (NATE 2026-08-26, clean-slate walkthrough;
  fetch + styles FROZEN as settled subsystems): mesh = a TOOL FAMILY
  like fetch. Declaration block beside DATA/PARAMS:
  MESH = tool.build_mesh(engine=om2d|telapy|hecras|reg_grid,
  kind=structured_grid|unstructured_tri|unstructured_quad_flex|
  curvilinear|node_link, aoi, refine={edge_length,min_spacing,
  gradation}, bed=D.dem) - FROZEN at declaration, LAZY (nothing builds
  at import; demand-pulled). Same tool standalone = builds now +
  stashes in case. Policy zoo (MeshPolicy/CorridorPolicy/
  CatchmentPolicy) DIES into spec fields; spec typed AT THE ROUTER
  (each mesh engine declares its consumed fields; loud refusal).
  MESH ENGINE distinct from SOLVER ENGINE (one OM2D build feeds
  TELEMAC/SWAN/SCHISM). Runtime SESSION opens over the declaration;
  .edit(action, **inputs) from a per-engine EDIT-ACTION REGISTRY (thin
  hooks wrapping the official library: OceanMesh2D toolkit, telapy
  mesh.py) - declared edits form the recipe PREFIX
  (.edit("add_obstacle", D.breakwaters)); gate edits APPEND; restart
  truncates to the prefix; accept -> MeshArtifact (multi-format +
  MDAL display). ALL THREE SURFACES CONVERGE on actions (direct
  params / agent GENERATED tools mounted only while session open /
  future QGIS drawing delivers geometry into the same actions - an
  action never cares who authored its inputs). QGIS hand-edit
  round-trip = edit("apply_layer_edits", layer) - one recorded
  action, layer hashed, honestly non-replayable. RECIPE IS THE RECORD
  (spec + ordered chain, journaled, deterministic replay). AGENT
  EYES = numeric probes (nodes, edge-length histogram, min-angle,
  boundary segments, obstacles) + wireframe SNAPSHOT read with
  vision; human at gate stays the final eye. Supplied mesh:
  explicit-first, case-discovery-second, declared-spec default.
  Static plan spine KEPT; boundaries defined in the same gate loop.
  Bridge Blueprint artifact rev 2 = the picture.

- VOCABULARY FIXED: MESHER, NOT ENGINE (NATE 2026-08-26): "engine" is
  RESERVED for solvers (TELEMAC/MODFLOW/SWMM/HEC-RAS - the thing a box
  runs). The mesh-building libraries behind tool.build_mesh are
  MESHERS (arg name: mesher=om2d|telapy_mesh|hecras|reg_grid), not
  registered engines. Full ladder: engine=solver, mesher=mesh library,
  fetcher=data spec behind the fetch router, worker/box=the
  network-isolated container an engine runs in. Stick to this in every
  charter, kickoff, docstring and sub-agent prompt. Scope note
  ratified: the bigger mesh wave is WORTH IT - "before none of the
  functionality existed in one area."

- SPEC TRIMMED, THREE CORRECTIONS (NATE 2026-08-27): (1) the
  FIVE-CATEGORY INPUT TAXONOMY is DESCRIPTIVE VOCABULARY ONLY - words
  for talking about inputs in forms/docs/gates. NO code change: the
  resolved sheet, doors, journal, rerun and the consequence= tags stay
  exactly as they are; the earlier "retire consequence vocabulary"
  clause is WITHDRAWN. (2) Design-pattern language (bridge/template
  method) DEMOTED to private analysis vocabulary - the spec describes
  the architecture plainly ("fixed spine of steps; engine-touching
  steps delegate to the official library"); no pattern names in
  charters/kickoffs/docstrings. (3) STEPPABLE ENGINE REMOVED FROM THE
  SPEC - the existing run paradigm (box dispatch, run to completion)
  carries everything; telapy stepping is a later free win, still
  parked post-calibration but NOT part of the target architecture.
  The spec = the mesh tool (ratified section C) + the emission
  display-face placement, over today's unchanged run + emission
  machinery. Workflow Blueprint artifact rev 4 = the trimmed spec.

- SPEC FORMAT FORMALIZED (NATE 2026-08-27): every future spec = one
  self-contained HTML page in docs/specs/, published as an artifact:
  vocabulary table when terms are load-bearing, one section per
  concern, CODE SNIPPETS of the real surface, UML (mermaid class
  diagrams + simple flow/state diagrams) as the abstraction, PLAIN
  LANGUAGE (no pattern names/jargon), everything buildable (exclusions
  marked out-of-spec in place), revision line in the header. Full
  ruling: docs/decisions/0320-spec-format.md. First instance:
  docs/specs/workflow-blueprint.html (the mesh-tool spec, rev 4).

- RENAME ops.solver_spec -> ops.solve (NATE 2026-08-27): the plan
  surface names WHAT the step is (the solve), not how it is
  implemented (building a dispatch spec). Pure rename - the physics
  process-selector refusal, compute_class lever, .named("solve") and
  Ref("solve") wiring all unchanged. Lands with the mesh wave's
  template rewrites (every plan line is touched then anyway), not as
  a standalone churn wave.

- RENAME ops.read_results -> ops.read (NATE 2026-08-27): full symmetry
  with the verb spine (mesh/author/solve/read). Pure rename, rides the
  mesh wave with ops.solve.

- ROADMAP REORDERED, CALIBRATION LAST (NATE 2026-08-27): settle the
  SHAPE first, calibrate the settled system once. ORDER: (1) stale
  sweep (running); (2) MESH WAVE - build the tool + migrate ALL of
  workflows/telemac/ (kickoff docs/design/mesh-wave-kickoff.md, now
  UN-DRAFTED: calibration no longer gates it; open boundaries land as
  slice 6 regardless); (3) SECOND ENGINE = SCHISM as a THIN
  GENERALIZATION PROBE - it ingests OceanMesh2D output (hgrid.gr3) and
  REQUIRES open-boundary segmentation, so set_boundary is tested
  load-bearing; attack its smallest examples, migrate its existing
  templates onto the architecture, prove ONE om2d mesh feeds TWO
  engines. Scoped thin - focus-four depth (TELEMAC/SWMM/MODFLOW/
  HEC-RAS) is unchanged, SCHISM validates the tool, it does not join
  the four; (4) FLEET ROLL onto the settled architecture (MODFLOW ->
  SWMM -> HEC-RAS); (5) CALIBRATION LAST, on the settled system.
  (SWAN rejected as the probe: its worker is regular-grid-only;
  consuming om2d meshes would be NEW capability, not a migration.)

- MESH SESSION ECONOMICS (NATE 2026-08-27, correcting the landed om2d
  edit path): per-edit eager re-realization REJECTED - it re-fetched
  staged inputs (violates the reuse-resources rule) and rebuilt per
  edit. Ruling, three parts: (1) LAZY BATCHED REALIZATION - edit()
  mutates accumulated state only; realization fires on demand
  (present/probes/accept), one gate cycle = one rebuild regardless of
  edit count; (2) STAGED-ONCE INPUTS - bed/shoreline/fetched
  geometries staged into the session exactly once, reused by every
  realize, zero refetch; (3) PREFIX-KEYED SNAPSHOT CACHE - every
  realized mesh cached in the session keyed by recipe-prefix hash;
  undo/restart = instant restore, no rebuild; also pins om2d
  nondeterminism within a session (prefix P always returns the mesh
  the user inspected). Re-realization SEMANTICS stand for conformal
  generative edits (measured: surgical punch = 80.1 m outline offset
  vs 0.0 m constrained - DistMesh has no local re-mesh); telapy
  adoption path stays pure surgical mutation. Lands as a remediation
  stage after the mesh wave's current leg.
- MESH SPOT-CHECK DRIVER (NATE 2026-08-27): a standing test workflow
  for basic mesh builds - direct TOOL_REGISTRY invocation of
  build_mesh with coarse defaults, prints probes + emitted layer uri,
  for QGIS spot checks whenever mesh functionality lands.

- GMSH MESHER RULED IN (NATE 2026-08-27): gmsh joins the registry as a
  third mesher COMPOSING oceanmesh - om2d keeps the sizing
  intelligence (its actual strength; NATE: "narrow generation and
  refinement responsibility"), gmsh takes geometry (OCC boolean cuts),
  generation (background size field from the om2d grid, pinned
  RandomSeed), and EDITING (session-held model, in-process re-mesh -
  solves edit economics at the root). Embedded curves = zero-offset
  conformality BY CONSTRUCTION. .msh is an official TELEMAC import
  (telapy parser_gmsh). Hard acceptance bar: determinism 3-run
  sha256, conformality survives cleanup, sizing fidelity vs om2d on
  Duck NC, measured edit economics, engine ingestion, suite zero.
  om2d generator stays registered through the comparison period;
  telapy adoption path unchanged. Decision spec:
  docs/specs/gmsh-mesher.html (artifact published).

- LANDSCAPE VERDICT + WIRING C VALIDATED (2026-08-27): NATE's
  patch-edit wiring (om2d builds the base, edits are patch-local and
  in-place) is OM2D'S OWN TRODDEN CONCEPT - MATLAB ships remesh_patch
  + extract_subdomain, never ported to python. Community default =
  regenerate (fails our determinism bar, measured). Vehicle contest:
  gmsh planar patch (TELEMAC-mainstream, clean wheel) vs OCSMesh
  remesh_by_shape (NOAA, active, right community; binary fragile in
  our sandbox - measured, determinism undocumented). seamsh
  narrow+stale, qmesh dead, mmg wrong community. Spike measures both
  on Duck NC: seam min-angle, outline offset 0.0, 3-run determinism,
  friction. Report: docs/research/coastal-mesh-edit-landscape.md;
  spec: docs/specs/gmsh-mesher.html rev 4.

- REMESH ECONOMY REVISED + CAPABILITY PARAM RULED (NATE 2026-08-27,
  after his own research): regeneration-on-edit IS economical for the
  COMMON small-mesh case and is the community-typical workflow - the
  "uneconomical" ruling narrows to LARGE fine meshes only. Resolution:
  ADOPT regenerate + session polish now (lazy batched realization,
  staged-once inputs, snapshot-cache undo - these stand); PATCH-LOCAL
  editing DEFERRED to measured large-mesh pain, its design recorded in
  the spec so it is not relitigated (gmsh planar patch vs OCSMesh
  remesh_by_shape spike, MATLAB remesh_patch precedent). CAPABILITY IS
  THE PARAM: mesher names become capabilities (coastal/adopt/corridor/
  grid), each tailoring its own edit-action set, kernels swappable
  behind them. Spec rev 5 adds a HOW-MESHING-WORKS primer (sizing
  functions, generation families, conformality, cost scaling,
  boundary bookkeeping) - specs teach as well as specify.

- FOR= CONSUMER DECLARATION RULED (NATE 2026-08-27): build_mesh gains
  for=<engine(s)> - the upstream consumer declaration binding the
  session to ENGINE_MESH_REQUIREMENTS at BUILD time: declaration-time
  refusals (swan+unstructured refuses before building), ADAPTIVE edit
  tools (set_boundary -> .cli/LIHBOR for telemac, contiguous gr3
  segments for schism), live "solve-ready for <engine>" probes during
  editing, accept() as a CONTRACT (refuses until requirements hold),
  format writers narrowed to the declared engine + display. Templates
  AUTO-INJECT for= from their engine; standalone optional +
  list-valued; omitted = today's generic behavior. Orthogonal to and
  composes with the capability param. Wire name "for", python kwarg
  for_. Spec rev 6.

- THREE-AXIS MESH SIGNATURE CONVERGED (NATE 2026-08-27, supersedes
  "capability is the param"): domain (coastal|river|catchment|
  open_water - the required word of intent) / kind (defaulted per
  domain) / mesher (auto-routed builder, visible-but-optional) +
  for_ + fields. THE (domain, kind) EDIT-OPERATION MATRIX: a contract
  table sibling to ENGINE_MESH_REQUIREMENTS; builders register FOR
  CELLS and registration REFUSES AT IMPORT unless every cell verb is
  implemented - guaranteed by construction. Unoffered verbs are NOT
  MOUNTED (selection-by-declaration beats refusal-at-call; typed
  refusal only for direct calls to unoffered verbs). Common path =
  tool.build_mesh(domain="coastal", aoi=...) - one word + a place.
  MESH.edits known statically at declaration. Lands in the follow-up
  wave with for_, session polish, rename. Spec rev 7. Two small
  opens: axis name domain-vs-type (lean domain), mesher visibility
  (lean visible-but-optional).

- MESH SIGNATURE CONVERGED FINAL, REV 8 (NATE 2026-08-27): DOMAIN
  VOCABULARY DROPPED entirely (label AND named constructors - both
  still implied; and no domain param ever existed in the product: the
  AOI is THREADED, acquire_domain binds template shapes). The domain
  IS aoi + declared CONSTITUENTS (bed, shoreline, flowline, pour
  point - ordinary data refs). CAPABILITIES DERIVE FROM PRESENCE via
  one constituent rule table (bare aoi = grid verbs; +shoreline =
  add_obstacle/set_boundary; +flowline = corridor verbs; +pour point
  = closed catchment): unoffered verbs never mounted, direct calls
  refuse by naming the missing constituent, capabilities disclose
  progressively. Builders register per constituent PATTERN, refused
  at import unless the pattern's verbs are implemented. SOURCING:
  DATA-first is canonical in templates (declared ladders, gate
  visibility, journal); in-tool "fetch:" specs = standalone sugar
  through the SAME router. kind/mesher=auto/for_ survive. Supersedes
  the capability-param and three-axis rulings. Spec rev 8.

- GROUNDING UNIFICATION SCHEDULED + SPEC REWRITTEN CLEAN (NATE
  2026-08-27): NATE's read confirmed - the AOI (bbox) contains the
  BASIS of the reach; the reach is supplemental grounding filled in
  by fetches (flowline -> where the river is, seed -> valid release
  point, discharge -> carrier flow). REFACTOR SCHEDULED: unify
  Ref("aoi")/Ref("reach") into ONE progressively-filled GROUNDING
  record (aoi always present; bed/shoreline/flowline/seed/discharge/
  pour_point fill as fetches land); filled fields ARE the
  constituents; presence unlocks BOTH mesh capabilities and scenario
  validity (release placeable because seed exists) - one mechanism.
  acquire_domain becomes the record's builder; seed/discharge
  consumers read the record; deck byte-parity required. Spec
  REWRITTEN whole (rev 10, retitled "Mesh Grounding") - current
  meaning only, superseded framings left to git history; explicitly
  a DESIGN spec with implementation HELD; keeps-vs-refactors delta
  table confirms NO landed code implements superseded framings
  (spec-only history, nothing thrown away). Follow-up wave order:
  grounding record -> constituent rules -> auto-routing -> for_ ->
  session polish -> template surface diffs.

- NATE'S GO ON THE DOMAIN/MESH MODEL (2026-08-27, spec rev 18):
  threaded-AOI framing DEAD - aoi is a REQUIRED, DEFAULTED, USER-
  EDITABLE DECLARATION (move/redraw legitimate; containment rule
  prices it: crop within staged coverage, restage-via-rerun outside,
  binary test). extent ALWAYS declared in templates (context is
  valuable, the line is free). MESHER EXPLICIT, NEVER AUTO. SCOPE:
  (1) wrap ALL of OceanMesh2D's exposed functions as the om2d
  mesher's param/edit surface - "the wrapping was the whole deal" -
  with tests; (2) migrate the 7 telemac templates to the model;
  (3) telapy wrapper maybe after; gmsh + cross-tool edit extension
  LATER once the feature exists. !run shape unchanged (kwargs/JSON,
  positional rejected). Clear-safety already structural (spec line
  vs edit chain). D2-D5 (wave verifier findings) remain the open
  rulings before the stopped wave can close.

- D2-D5 RULED, WAVE-CLOSE-FIRST (NATE 2026-08-27): all four verifier
  recommendations ADOPTED - D2 accept() implies stageable (hand-edit
  regenerates via telapy or the edit refuses at the gate); D3 numeric
  knobs as param_sheet the shipped client renders (plugin changes only
  if unavoidable, flagged for NATE's live pass); D4 approve-mesh chop
  finished client-side; D5 worker-bundle round-trip test + one live
  adopted-mesh solve. SEQUENCE: close the stopped wave (remediation ->
  final verify -> artemis flagship) THEN the model wave (aoi
  declaration, om2d full wrapper, 7-template migration per spec rev
  18).

- SPEC-CONFORMANCE GATE (NATE 2026-08-27): every wave close-out, after
  tests and before done/push: a FRESH-EYES agent walks the governing
  spec clause by clause producing a conformance table (clause ->
  implementation -> CONFORMS/DEVIATES) plus a LIVE behavior
  walkthrough transcript (real declaration beside the spec's worked
  example, real gate session, real !run). Deviations are design
  questions - reported to NATE, never auto-fixed. Applies to the
  running wave at its completion (against workflow-blueprint + the
  D2-D5 rulings) and is baked into every future wave script as the
  mandatory final stage.

- MCP PURGED (NATE 2026-08-27): mcp_server.py (351 LOC, zero
  consumers) + test + design doc + .mcp.json.example + the mcp dep +
  trid3nt-mcp entrypoint all chopped (ledger lines; decision 0302
  stays as history). KEPT + SCHEDULED SEPARATELY: the persistence
  seam is NAMED after MCP (MCPClientProtocol / FileMCPClient in
  persistence.py, covered by the doubly-stale-named
  test_mongo_mcp_wiring.py) but is the LIVE case-persistence path -
  purging it is a rename refactor (DocumentStoreProtocol or similar +
  test rename + comment scrub of "MCP" mentions in loop.py/tools),
  queued for its own attention after the wave. Stale trid3nt-mcp
  script in the venv bin clears on the next natural editable
  reinstall (not forced mid-wave).

- GEOMETRY-BY-NAME + CANVAS PICKER (NATE 2026-08-27, ruled direction):
  geometry-valued edit inputs enter the gate card BY REFERENCE - build
  the object in QGIS, fetch it, or generate it, then feed the OBJECT/
  LAYER NAME: a string row on the SAME param_sheet channel D3 landed
  (no new protocol). Server resolves name -> case layer -> geometry ->
  session.edit; name SHOWS, durable layer id TRAVELS; ambiguous typed
  names refuse listing matches; not-found/wrong-type/empty refuse
  typed naming what the action needed. QGIS native feature SELECTION
  = the subset operator (layer name = all features; selection = the
  subset). PICKER UX: a card row arms a canvas pick mode - candidates
  attention-flash/highlight, chosen name fills the row - built on the
  EXISTING SpatialInputCard/draw-gate machinery. LANDS: model wave
  (rev 18) for the resolution seam + rows; picker is plugin UI
  needing NATE's live pass.

- TWO-POINT BOUNDARY PICK + UI-REWORK SEQUENCING (NATE 2026-08-27):
  set_boundary's natural card input = TWO PICKED POINTS on the mesh
  exterior (DrawGate-style) - the stretch between them IS the open
  boundary (contiguity free by construction; prevents the wrong-
  water-body class the compass-side pick hit live). SEQUENCING RULED:
  make-it-work-now with the current card channels (numeric +
  string-choice rows, geometry by TYPED name); the interaction design
  (canvas picker, attention flash, two-point draw, card polish) is a
  DEDICATED PLUGIN-UI REWORK PASS immediately after the 7 workflows
  ship - NATE-led, his long-standing itch. Model wave carries only
  the server-side name->layer->geometry resolution seam so the UI
  pass finds its substrate ready.

- BOUNDARY INPUT CORRECTED (NATE 2026-08-27, supersedes the two-point
  pick line above): two points on a closed loop are UNDERDETERMINED
  (two stretches - a guess). The boundary's data structure is a
  POLYLINE, so the selectable thing IS the polyline/polygon feature -
  drawn, fetched, or derived, fed by name/selection. This collapses
  to ONE geometry input primitive for obstacles, refine regions AND
  boundaries (no special modes). Server maps the selected polyline
  onto the exterior walk (snap -> contiguous covered node stretch =
  the open boundary) and REPORTS the measured snap distance, never
  asserts the match.

- TESTING LANE EMPHASIS (NATE 2026-08-27): until the subsystems are
  fleshed out there is NO user/UI testing - the lane is DIRECT
  INVOCATION with all params (!run / drivers) plus SCRIPTED tests,
  not UI glue. Wave + model-wave acceptance runs through the driver
  lane and offline suites; plugin-visible changes stay recorded for
  NATE's eventual live pass but block nothing; the UI rework pass
  stays parked at its slot in the sequence.

- MESH WAVE CLOSED + ALL NINE DEVIATIONS RULED (NATE 2026-08-28):
  D-1 ALL MESHES EDITABLE (grids route through the tool - extent is
  ad hoc editable, so every declared MESH sessions); D-2 explicit
  named build step IS the spec shape; D-3 MUST-FIX (declaration read
  whole incl the edit chain = recipe prefix); D-4 vision PNG dropped;
  D-5/6/7 specs trimmed to reality (bed= line gone, real roster +
  kinds incl graded_cells, unimplemented kind words DELETED, layout
  refreshed); D-8 preview_gate CHOPPED (done, suite green); D-9
  discovery offer wired in the model wave (offer detail deliberately
  unspecified); om2d's four flagship fragilities = the wrapper
  slice's ACCEPTANCE BAR. Domain-and-Mesh spec consolidated REV 19
  and FROZEN for the model wave. MODEL WAVE LAUNCHED from
  docs/design/model-wave-kickoff.md - 7 slices, scripted-lane
  acceptance, conformance gate final, design-decision stop rule in
  the charter.

- MODEL WAVE SURFACE DESIGN-STOPS RULED (NATE 2026-08-28): DS-1
  catchment ROUTES THROUGH A SESSION (D-1 consistency; the loud
  refusal was interim); DS-2 corridor extent RENAMES to
  set_reach_length, its own action, CONTAINMENT-JUDGED against the
  staged flowline/DEM coverage (within = edit; beyond = typed
  escalation to rerun with a reach_length_km override) - and the
  fetching meshers must WRITE staged_coverage so containment is
  testable, not vacuous; DS-3 deck + ANSWER read the ACCEPTED
  ARTIFACT (mesh_size_m, mesh_node_estimate from probes where an
  artifact exists - measured truth over estimate, same principle as
  dt); AOI SHAPE: aoi stays the ACQUIRED STEP RESULT (declared
  editable inputs = location/bbox; extent=Ref("aoi") written in every
  MESH declaration is the visibility) - the spec's P.aoi wording
  amends at the conformance gate. Surface review also fixed
  mechanically: staged coverage no longer collapses onto the last
  crop (second crops + undo work).

- FRESH-START PURGE + TELAPY INTERIOR RULED (NATE 2026-08-28, "let's
  go"): (1) TELAPY IN THE WORKER: YES - the box interior goes
  telapy-driven (down one abstraction level INSIDE the box; box
  contract unchanged). SEQUENCE: telemac worker cleanup -> library
  integration -> mesh work. (2) workflows/mesh is THE ONLY mesh dir;
  the old root trid3nt_server/mesh/ moves OUTSIDE THE REPO to the
  attic (reference only) - live consumers absorbed first. (3) MESHER
  PURGE: telapy_mesh, watershed, hecras, corridor_tin removed (NATE
  deleted several himself); coastal_edge to attic (driver deleted;
  water-edge prep folds into om2d). Roster: om2d + reg_grid. (4)
  WORKFLOWS ONE AT A TIME: all non-telemac engine workflow dirs move
  to the attic (calibration/elmfire/geoclaw/hecras/landlab/modflow/
  openquake/pelicun/schism/sfincs/swan/swmm); telemac + mesh + lib +
  shared + solver stay; refactor from scratch from the settled arch.
  (5) TESTS + SCRIPTS: non-telemac workflow tests and scripts move
  out with their engines; the suite RE-BASELINES (new zero). ATTIC =
  ~/Documents/trid3nt-attic (outside the repo, reference only; git
  history remains the archive). Anti-bloat doctrine: old iterations
  LEAVE the repo, always.

- THE LEGO RULING (NATE 2026-08-28, verbatim intent): "composability
  pureness modularity not bloat and shims" - MESHERS NEVER GROW
  DOMAIN PREPS. Domain narrowing is PLAN-LEVEL CHAINING of processing
  tools: subdomain = delineate(aoi, pour_point) / corridor =
  corridor_of(flowline, width_m) -> build_mesh(bbox=subdomain, ...).
  delineate + corridor_of become REGISTERED PROCESSING TOOLS
  (harvested from the attic'd watershed/corridor code AS TOOLS);
  build_mesh's extent accepts bbox OR polygon; om2d gains ONLY the
  polygon-domain path (SDF from the supplied polygon - the mechanism
  the attic'd watershed driver already used with authentic
  om.generate_mesh). The reach/catchment templates repoint to the
  chain. LESSON RECORDED: when a capability gap appears, the answer
  is a composable tool in the chain, never a mesher/step growth.
- DS RULINGS (NATE 2026-08-28): write_telemac_pair moves to a NEW
  mesh/shared/ subdir with a better name (shared format writers);
  SCOUT MDAL for format-IO boilerplate reduction where applicable
  (report first). SWMM gate chunk + moved engines' solver diagnostics
  -> attic. persistence/ KEPT (32 live call sites = session/chat
  restart durability); rename stays queued.
