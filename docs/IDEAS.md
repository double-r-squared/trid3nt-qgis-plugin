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
