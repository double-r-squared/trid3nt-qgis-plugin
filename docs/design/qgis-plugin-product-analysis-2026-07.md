# TRID3NT QGIS Plugin -- Product Analysis (2026-07)

Per NATE's sequencing: "first we do an analysis on what the product will do and
then we execute." This is that analysis. It is a PRODUCT document: what the
plugin does for a QGIS user comes first; feasibility second. It builds on the
QGIS bridge already shipped (roadmap track 5: the `export_case_to_qgis` tool,
the Export-to-QGIS UI button, the `/api/export-qgis` endpoints) and on what
TRID3NT Local is today: a local agent with ~176 tools (85 of them dedicated
`fetch_*` data fetchers), 8 physics engines proven end-to-end locally, and a
pluggable LLM (Ollama local model or any OpenAI-compatible endpoint).

The one-sentence pitch: **the TRID3NT agent, docked inside QGIS -- ask for
data or a hazard simulation in plain language, and the answer arrives as
styled layers on the canvas you are already working in.**

---

## 1. Who it is for

QGIS has millions of users, and the plugin ecosystem tells you what they
struggle with: the most-installed utility plugins are DATA-ACQUISITION shims
(QuickOSM for OSM extracts, OpenTopography DEM Downloader, dozens of
per-country service plugins) and hazard tooling is a scatter of narrow,
form-driven plugins (CanFlood, FloodRisk2, Rain2Flood, RiverGIS-for-HEC-RAS).
Three personas anchor the product:

### P1 -- Municipal floodplain / stormwater analyst

Works at a city, county, or small district. Hires QGIS for: parcel and
floodplain overlays, drainage asset maps, map layouts for council packets.
QGIS because an ArcGIS seat (plus Spatial Analyst) is a budget line item they
lost. Friction today:

- **Data assembly is a plugin scavenger hunt.** DEM from one plugin, NHD
  streams from a download portal, NOAA Atlas 14 precip from a PDF table,
  building footprints from a third source -- each in its own CRS, each
  manually clipped.
- **Actual flood modeling happens OUTSIDE QGIS.** A 2D pluvial or coastal run
  means HEC-RAS or a consultant. The QGIS flood plugins that exist compute
  indices or damage curves over depth rasters SOMEONE ELSE produced; none of
  them run the hydraulics.

### P2 -- Environmental consultant (small firm)

Phase I/II site assessments, contamination screening, permit support. Hires
QGIS for: site maps, buffer/overlay analysis, figure production. Friction:

- Per-project data assembly is repeated billable-but-boring labor (soils,
  wells, wetlands, flood zones, historical imagery for every new site).
- Groundwater questions ("which way does the plume go, roughly, and what does
  it reach") require a MODFLOW specialist and a week, even when the client
  needs a screening-level answer tomorrow.

### P3 -- Hazard researcher / graduate student

Multi-hazard studies, methods papers, teaching. Hires QGIS for: everything
that is not the model itself. Friction:

- **Engine wrangling.** Compiling SFINCS/GeoClaw/SWAN, hand-building input
  decks, converting outputs back to GIS formats is most of the calendar time;
  the science is the residual.
- Reproducibility: the fetch-preprocess-run-postprocess chain lives in ad-hoc
  scripts nobody can re-run a year later.

Common thread: **QGIS is where they LOOK at hazard data but not where they can
MAKE it.** The plugin's job is to close that gap without making them leave
the canvas.

---

## 2. What the plugin does (candidate capabilities, v1 vs v2)

Five candidates were weighed. Each is phrased as a user outcome with a
one-line example interaction, then scored on user value vs build cost.

### (a) Dock-panel chat that drives the local TRID3NT agent -- layers land on the canvas

> "Get me the 1-m DEM and building footprints for this area" -> two new
> layers appear in the QGIS layer tree, styled, zoomed to the AOI.

A dockable chat panel (like Kue's, but pointed at YOUR local agent) speaks the
existing WS chat protocol on `ws://localhost:8765`. Every `publish_layer` the
agent emits is materialized as a native QGIS layer instead of a MapLibre
layer: vectors via the GeoPackage path the exporter already implements,
rasters as local COG copies with the TiTiler rescale/colormap params
translated to a QGIS pseudocolor renderer -- **that translation code exists
and is tested** (`export_case_to_qgis._raster_pipe_element`). No web app in
the loop.

- Value: **the whole product.** This is the single capability that makes
  everything else reachable.
- Cost: moderate. The chat protocol, tool catalog, and style translation all
  exist; the new work is a Qt dock widget, a WS client that stays off the Qt
  main thread, and rendering the protocol's structured cards (see the
  granularity-gate note under (c)).
- **Verdict: v1, non-negotiable core.**

### (b) Fetcher access: 85+ data fetchers as native QGIS layers

> "Add NLCD landcover, SSURGO soils, and the latest Sentinel-2 scene here."

Two presentation forms: conversational (free with (a) -- the agent already
routes to fetchers) and a **searchable dialog** (a browsable, form-driven
catalog of every fetcher, no LLM required). The dialog matters because it is
the trust on-ramp: a floodplain analyst who does not yet trust chat will
still use "one dialog that replaces ten download plugins," and it works even
when Ollama is not running.

- Value: high and broad -- data acquisition is the #1 documented QGIS pain,
  and 85 fetchers behind one door is a stronger offer than any single
  downloader plugin on the repository.
- Cost: conversational form is free with (a). The dialog is real work:
  per-fetcher parameter forms generated from tool schemas, plus curation of
  which fetchers make sense interactively (the tool-support matrix already
  classifies all of them, including the 6-8 KEY-gated ones with typed
  missing-key errors).
- **Verdict: conversational fetching in v1; the searchable dialog in v2**
  (same seam, pure additive UI).

### (c) Run simulations from QGIS -- AOI from the canvas

> Zoom to a neighborhood, then: "Run a 10-year design storm pluvial flood
> here" -> resolution-picker card -> ~1-5 min -> a depth raster lands on the
> canvas with a rainbow legend.

The plugin injects the current canvas extent (or a selected polygon) as the
AOI context on every turn, so "here" means the map the user is looking at.
All 8 engines (SFINCS, MODFLOW, SWMM, GeoClaw, SWAN, OpenQuake, Landlab,
pfdf) are already proven LLM-driven locally; results are COGs in MinIO, which
is exactly what the raster export path consumes.

Two product obligations carry over as v1 MUSTS, not options:

1. **The granularity gate renders in Qt.** Resolution is a user lever
   (standing directive); the plugin must show the autoscaler's suggestion and
   let the user coarsen before the solve starts -- especially locally, where
   a default-resolution SWMM run is a 25-minute mistake.
2. **Honest engine expectations.** The engines page documents real local
   runtimes (~40 s SFINCS small AOI, ~2 min SWMM 3-block, ~40 s GeoClaw) and
   real physics caveats (ETOPO-fallback tsunamis can honestly produce zero
   inundation). The plugin surfaces solver progress and typed errors verbatim
   -- never a spinner that ends in silence.

- Value: **the differentiator.** No QGIS plugin on earth runs multi-engine
  physics from a chat box (see section 4). This is the demo, the conference
  talk, and the reason to install.
- Cost: low-to-moderate ON TOP of (a) -- solver dispatch, progress events,
  and result publication are the same protocol stream; the new work is the
  canvas-extent injection and the Qt confirmation/resolution cards, which (a)
  needs anyway.
- **Verdict: v1.** Shipping a chat panel that can fetch but not simulate
  would be indistinguishable from the existing AI-assistant plugins.

### (d) Two-way project bridge (import a TRID3NT case / export to one)

> Case -> QGIS: "Open my Tampa flood case in QGIS." -- already works TODAY
> with zero plugin code: `export_case_to_qgis` writes a `.qgz` that desktop
> QGIS opens natively.

- Case -> QGIS (import side): the plugin adds a one-click "Open case..."
  list (cases enumerable via the agent's HTTP/persistence seam) that calls
  the existing export endpoint and opens the resulting project. Near-zero
  cost, real convenience. **v1** (it is glue over shipped code).
- QGIS -> case (export side): pushing the user's current QGIS project INTO a
  TRID3NT case is the roadmap's `import_qgis_project` (v2 there too), with a
  known lossy-symbology scope guard. **v2** -- valuable for round-tripping,
  but nobody installs the plugin for it.

### (e) Processing-algorithm registration (TRID3NT tools in the Processing Toolbox)

> Drag `trid3nt:fetch_3dep_dem` and `trid3nt:model_debris_flow` into a QGIS
> graphical model and batch it over 20 watersheds.

Registering tools as `QgsProcessingAlgorithm`s makes them chainable in the
Graphical Modeler and callable headlessly -- the deepest possible QGIS
integration, and the established pattern for external engines (WhiteboxTools,
OTB, SAGA all ship as Processing providers).

- Value: high for P3 and power users; it also future-proofs (models built on
  our algorithms embed TRID3NT in institutional workflows).
- Cost: **the highest of the five.** 176 tool schemas must map to typed
  Processing parameters; async streaming tools must adapt to Processing's
  synchronous run model; solver confirmation/granularity cards have no
  natural place in a Processing dialog and need a parameters-up-front
  redesign per engine. Doing this shallowly (dump 176 raw algorithms) would
  produce a bad toolbox.
- **Verdict: v2, curated.** Start with ~10-20 hand-picked algorithms
  (fetch DEM/landcover/soils, watershed primitives, debris flow, the
  cheapest composers), not the full catalog.

### v1 scope, summarized

| Capability | v1 | Rationale |
|---|---|---|
| (a) Chat dock, results as QGIS layers | YES | the core; everything routes through it |
| (b) Fetchers -- conversational | YES | free with (a); #1 user pain |
| (b) Fetchers -- searchable dialog | v2 | additive UI, no new seam |
| (c) Sims from canvas extent + granularity gate | YES | the differentiator; cheap on top of (a) |
| (d) Open-case-in-QGIS (import side) | YES | glue over shipped exporter |
| (d) QGIS-project -> case (export side) | v2 | matches roadmap's own v2 |
| (e) Processing provider | v2 | highest cost; do curated, not bulk |

---

## 3. What it explicitly does NOT do in v1

The web app remains the home for:

- **Animation scrubbing / time-series playback.** QGIS has a Temporal
  Controller, but wiring our COG frame sequences into it is real work and the
  web scrubber already exists. v1 delivers the final/summary rasters (max
  depth, arrival time); frame stacks are listed but not animated.
- **deck.gl 3D scenes** (extrusions, flythroughs). No QGIS equivalent worth
  faking with the 3D map view in v1.
- **Multi-case management, model selector, telemetry dashboards, lessons
  loop.** One active case per QGIS session in v1; switching cases = opening a
  different case. Settings beyond "agent URL + AOI source" stay in the web
  app.
- **Running the stack.** The plugin does not start/stop MinIO, Ollama,
  TiTiler, or the agent (it detects and guides -- section 5). Process
  supervision inside a QGIS plugin is a support-ticket factory.
- **Editing physics decks.** The conversation and the granularity card are
  the interface; hand-editing SFINCS inputs stays out of scope at every
  phase.

One deliberate consequence: a user who wants the full experience runs BOTH --
QGIS for analysis-in-context, the web app for presentation and animation.
That is a feature (the case is shared state via the same local stack), not a
gap to engineer away in v1.

---

## 4. Competitive / ecosystem scan

AI-assistant plugins in the official repository, as of mid-2026:

| Plugin | What it is | What it is not |
|---|---|---|
| [Kue AI](https://plugins.qgis.org/plugins/kue-ai/) (Bunting Labs) | The most polished: embedded assistant that edits symbology, runs geoprocessing, adds basemaps. $19/month, cloud-hosted LLM. | No physics simulation; no data-fetcher fleet; your data and prompts leave the machine. |
| [IntelliGeo](https://plugins.qgis.org/plugins/intelli_geo/) | LLM generates PyQGIS/Processing scripts from prompts (research project). | Script generation, not tool execution against a governed catalog; no hazard data or engines. |
| [QChatGPT](https://plugins.qgis.org/plugins/QChatGPT/) | OpenAI chat window in QGIS -- gives GUIDANCE on how to do tasks. | Does not do the tasks. |
| [QGPT Agent](https://plugins.qgis.org/plugins/qgpt_agent_release/) | GPT-driven automation of QGIS processes (experimental). | Cloud OpenAI only; no domain tools. |
| [Q-LLM](https://plugins.qgis.org/plugins/q_llm/) | Local Gemma-3 assistant; automates map navigation. | Proves local-LLM-in-QGIS demand exists; capability is minimal. |
| GIS Copilot ([academic, 2025](https://www.tandfonline.com/doi/full/10.1080/17538947.2025.2497489)) | Autonomous spatial-analysis agent research. | Not a supported product. |

Hazard/flood plugins: [CanFlood](https://plugins.qgis.org/plugins/canflood/)
(flood RISK toolbox over externally-produced hazard rasters),
[FloodRisk2](https://plugins.qgis.org/plugins/floodrisk2/),
[Rain2Flood](https://plugins.qgis.org/plugins/Rain2Flood/) (SCS runoff +
inundation approximations), RiverGIS (HEC-RAS geometry PREPARATION). The
pattern is consistent: QGIS plugins either consume model outputs or prepare
model inputs -- **the model itself always runs somewhere else, driven by an
expert.**

Data-access plugins: QuickOSM, OpenTopography DEM Downloader, and dozens of
regional service plugins -- each covers ONE source with ONE dialog.

**The gap TRID3NT fills, that nothing in the ecosystem touches:**
conversational, multi-engine, real-physics simulation (8 engines) fused with
a broad governed fetcher catalog (85 sources) -- **fully local and private**.
Kue proves users will pay $19/month for a cloud assistant that cannot run a
single model; we ship the assistant, the data, and the physics, with prompts
and data that never leave the building (the exact requirement of utilities,
municipalities, and consultancies under data-governance constraints).

---

## 5. Distribution + requirements

**QGIS minimum version:** 3.34 LTR floor (3.28-era project XML is what the
exporter already emits, so compatibility pressure is low). Write against the
Qt5/Qt6-neutral API surface (`qgis.PyQt`) from day one -- QGIS is mid-flight
on the Qt6 transition and new plugins are expected to survive it.

**Official repository:** the plugin itself is pure Python + Qt speaking WS/
HTTP to `localhost` -- no bundled binaries, so it clears the repository's
no-binaries rule ([publishing requirements](https://plugins.qgis.org/docs/publish/)).
The precedent is exactly the WhiteboxTools/OTB model: a thin provider in the
repo, an external engine installation it points at.

**The install-footprint problem, honestly:** the full TRID3NT Local stack is
Linux x86_64 + Python 3.12 + uv + Docker (3 engine images) + Ollama + MinIO +
TiTiler. That is a developer-grade install, and **most QGIS users are on
Windows.** No plugin marketing fixes that; tiering does:

1. **Tier 0 -- detect and guide (v1).** On first open the plugin probes
   `:8765/:8766`; if absent, it shows a setup panel linking the install docs
   and re-probes. It never attempts the install itself.
2. **Tier 1 -- remote-agent mode (v1-cheap, decision needed).** The agent URL
   is a setting; pointing it at a TRID3NT cloud endpoint gives Windows/macOS
   users the full product with zero local stack -- at the cost of the
   privacy pitch and an auth story (Cognito token entry in a Qt dialog).
   This is also the natural commercial lever.
3. **Tier 2 -- degraded fetch-only mode (v2 candidate).** No LLM required:
   the searchable fetcher dialog (2b) invoking tools directly over the HTTP
   seam. Needs a direct tool-invocation endpoint on `:8766` (today it serves
   catalog + stats) -- small, but new surface.

Bundling a "lighter stack" inside the plugin is rejected: repository rules,
Qt-Python environment fragility, and the Docker/Ollama dependencies make it a
false economy. The honest v1 statement is: *native local mode is
Linux-first; everyone else uses remote-agent mode until the local stack is
ported.*

---

## 6. Risks + open questions for NATE (each a decision)

1. **Windows strategy.** Most QGIS users are on Windows; the local stack is
   linux-amd64. DECIDE: ship v1 as Linux-local + remote-agent-for-Windows
   (recommended), or hold v1 until a Windows/WSL2 local-stack story exists?
2. **Remote-agent mode in v1 at all?** It doubles the audience and creates a
   commercial lever, but drags in auth (Cognito in Qt), the per-user
   isolation stack, and dilutes "fully local" positioning. DECIDE: v1 with
   remote mode, or pure-local v1?
3. **Repository timing.** Publish to plugins.qgis.org at v1 (discovery +
   credibility, but public review and support burden) vs a GitHub-zip beta
   for invited users first? DECIDE: public repo now or after a beta cycle?
4. **Raster delivery seam.** Local COG copies (durable, works offline,
   duplicates disk) vs XYZ layers from local TiTiler (zero copy, but layers
   die when TiTiler stops). Recommendation: COG copies in v1 -- a QGIS
   project that still opens next week beats elegance. DECIDE: confirm.
5. **Name and identity.** "TRID3NT" plugin listing ties to the rebrand
   (user-visible naming only; grace2 stays internal). DECIDE: plugin listed
   as "TRID3NT" from day one, and is the QGIS plugin part of the public
   rebrand moment or a quiet release?
6. **License/price.** Kue charges $19/month. Free-and-open plugin with the
   local stack (adoption play), or free plugin + paid remote-agent tier?
   DECIDE: v1 licensing posture.
7. **Direct tool-invocation endpoint** (enables Tier 2 fetch-only and the v2
   Processing provider). DECIDE: green-light adding it to `:8766` upstream
   in GRACE-2 as dormant env-gated code, per the vendoring model?

---

## 7. Success criteria for v1 (measurable)

- **Install-to-first-layer <= 15 minutes** for a user with the TRID3NT Local
  stack already running (plugin install from zip/repo <= 2 min, connect <= 1
  min, first fetched layer on canvas the rest). Stack-from-scratch is
  measured separately and belongs to the install docs, not the plugin.
- **Fetch-to-canvas <= 60 s** for a bread-and-butter fetch (3DEP DEM,
  ~3 km AOI) from prompt-send to styled layer in the tree, local LLM
  (qwen3:8b class) doing the routing.
- **One full simulation from canvas extent**: SFINCS pluvial on a small AOI
  from prompt to depth raster on canvas in <= 5 minutes, including the
  granularity-gate interaction; MODFLOW archetype in <= 2 minutes.
- **Open-case round trip**: any case exported via the existing bridge opens
  with correct layer order, styling, and extent (already the exporter's
  contract) -- and the plugin's "Open case..." does it in <= 3 clicks.
- **Honesty floor holds in Qt**: every typed tool error and KEY-gated fetcher
  surfaces as a visible card; zero cases of a spinner ending in silence
  during the acceptance script.
- **Stability**: 30-minute mixed session (5 fetches, 2 sims, 1 case open)
  with zero QGIS freezes >1 s (WS client provably off the Qt main thread)
  and zero crashes.
- **Adoption signal (post-launch)**: 100 installs and 3 unsolicited external
  bug reports in the first month -- proof of real third-party use.

---

## Recommendation

Build v1 as **chat dock + conversational fetch + canvas-extent simulation +
open-case**, Linux-local first with the agent URL as a setting. It is the
smallest thing that is simultaneously (a) impossible to confuse with the
existing AI-plugin crowd, (b) built almost entirely on shipped, tested seams
(chat protocol, tool catalog, style translation, local solvers), and (c) an
honest product for the three personas: the data scavenger hunt collapses to a
sentence, and for the first time in the QGIS ecosystem, the model runs WHERE
THE MAP IS.
