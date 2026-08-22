# Declarative workflows - the step-list architecture

NATE-shaped design (2026-08-21/22 discussion), for redline before build.
Proving case: telemac_river_dye (3,503 lines -> a ~25-line step list).
Focus engines: SWMM + MODFLOW (top priority, EPA/USGS), TELEMAC, HEC-RAS
(tail, skippable). One principle applied everywhere: DECLARE THE WHAT,
CENTRALIZE THE HOW.

## The two layers

A workflow is a Python file with two parts:

1. A PARAM SHEET - frozen `Param` declarations (the GateSpec idiom).
2. A STEP BODY - an async function whose lines are `s.run(...)` /
   `s.gate(...)` / `s.chart(...)` calls. Plain Python: threading between
   steps is ordinary variables; a domain override is a visible
   rebinding (`aoi = shed.boundary`); an optional stage is a visible
   `if p.delineate:`.

The step runner (`s`) wraps every call with what composers currently
hand-roll: emit-on-fetch, substep progress, provenance stamping, typed
error envelopes, gates. The intelligence lives in registered tools; the
ceremony lives in the runner; the file is the contract you read.

## Rulings in force (NATE, 2026-08-21/22)

- R1 GATE WAITS - HYBRID: v1 gates wait in-turn with TTL (timeout = a
  typed refusal naming the unmet gate; rerun is cheap since fetches and
  meshes cache). The runner's state is a RESUMABLE STEP LEDGER from day
  one (ordered record of completed steps, resolved params, artifact
  URIs) so full pause/walk-away/resume is an additive later feature,
  not a redesign.
- R2 IMMUTABLE RECIPES: the agent fills params and answers gates; it
  NEVER alters the step list. A question the recipe cannot serve routes
  to the playground or another workflow. What you read is what runs.
- R3 MIGRATION ACCEPTANCE: (a) every incident-hardening behavior in the
  old composer is inventoried and consciously re-homed (a declared
  bound, a tool-level check, or a documented deliberate deletion -
  never silently lost); (b) one reference run old-vs-new, same question
  -> same physical answer within tolerance, QGIS-true renders side by
  side.
- R4 NET-LOC LAW: the campaign is measured by rolling net LOC per
  landing; an engine family must finish NET-NEGATIVE (TELEMAC today:
  12,139 lines across 8 templates). Moving mess around is failure.
- PURITY RULE (charter-grade): tool/workflow code carries zero demo
  constants and zero baked decks. Demo decks live in banner-labeled
  demo scripts. Open per-engine fork: mechanism-compare templates
  either take deck-as-input (fixtures script-side) or leave the
  registry and become demo scripts.

## The param system

Each `Param` declares: name, `desc` (one sentence - rendered into the
tool docstring for the LLM, the form label for the human, and read by
nothing else: one source, no drift), `door`, optional default, bounds,
units, `resolve` (dotted-path derivation), `user_lever`, `gate`.

DOORS, in resolution order per param:

1. BYO        - caller supplied it (uri or value): take it. Universal
                "if it exists, use it" (dem_uri precedent).
2. QUESTION   - the agent fills it from the ask (question-bearing).
3. FETCHED /  - real data via fetchers, or a derivation seam
   DERIVED      (roughness_resolve idiom). Fallback ladders apply.
4. SCENARIO   - labeled default, declared bounds, surfaced at the gate.
5. CONSTANT   - non-question physics (water viscosity): defaulted,
                never asked, always inspectable in the sheet.
6. GATE/REFUSE- whatever remains is asked for (form/draw) or refused
                typed. Nothing is ever invented (law 9).

Coercion, clamping, and validation happen in the resolver from the
declared bounds - the per-composer try/except/clamp/log blocks die.

## Gates

All gates ride the existing pending-confirmation spine. Two modes
preserved exactly (auto = labeled defaults / typed refusals, never
hangs; user_gated = waits in-turn per R1).

- FORM GATE (`s.gate(gates.form)`): renders the resolved param sheet as
  an editable form in the dock - name, value, unit, SOURCE BADGE
  ("geocoded from your prompt" / "derived from NLCD" / "default"),
  edit field. Editing flips the param's basis to user and revalidates;
  derived params are editable-with-warning. The submitted sheet
  snapshot persists as the run's input record. Question-bearing params
  on top; constants under an "advanced" fold. This is the ModelMuse/
  SWMM-GUI property grid, pre-filled by a sentence.
- DRAW GATE (`s.gate(gates.draw, param=..., geometry=point|polyline|
  polygon|rectangle, prompt=..., constrain=...)`): puts the pen in the
  user's hand for required geometry (release point, dam line, zone).
  Extends the existing AOI-rectangle machinery. No ghost suggestions
  (NATE ruling): user_gated waits; auto refuses typed if the geometry
  was not supplied as a param. Constraints validate at draw time
  (within(reach), on-mesh).
- Plugin cost: two new card types on the existing spine.

## Steps beyond fetch/solve

- PRE/POST/RENDER are first-class declared steps, not buried
  implementation. `s.run("style_layers", preset=..., zero="transparent")`
  - the render toolset formalizes the single existing styling seam
  (publish_layer) into declared primitives; zero-as-transparent is a
  RENDERING choice only (the raster keeps its zeros - law 9 applies to
  pixels). Render steps are also agent-callable conversationally.
- CHART STEPS (`s.chart(kind, series=..., x=..., y=...)`): the chart
  SPEC (data + kind + axes) is the persisted product; the plugin chart
  dock is the one renderer. Closes the chart-restore gap (case reopen
  re-renders from specs); removes server-side figure generation
  (matplotlib retirement, DELETION_LEDGER row queued); specs are JSON,
  so MCP clients receive charts as readable data.
- SENSOR EMISSION: station-shaped fetchers (gauges, buoys, tide
  stations) publish their sensor POSITIONS as a context layer alongside
  the data.
- QGIS-TRUE PROOF RENDERER: a PyQGIS headless utility renders layers
  through QGIS's own engine + the plugin's style presets + ESRI
  basemap - pixel-identical to the canvas. Becomes the montage engine;
  retires the matplotlib interpretation scripts (ledger row exists).

## Testing

Declarative: a test is a declared invocation (`!run tool(args)` in the
dock, a workflow stepped line by line, or the same over MCP). Offline
pytest remains for CI; the scripts/ driver population shrinks.

## Migration order and the generalization checkpoint

1. Design doc redline (this file).
2. telemac_river_dye migration (proving case; R3 acceptance; net-LOC
   meter on).
3. GENERALIZATION CHECKPOINT: one SWMM and one MODFLOW template migrate
   before any mass conversion - the runner must not be TELEMAC-shaped.
4. SWMM + MODFLOW engine-complete campaigns (purity + meshing + BYO
   mesh adoption), TELEMAC family completion, HEC-RAS tail (skippable).
   Row-19 wiring (river_dem_uri -> real streambeds) lands inside the
   MODFLOW campaign.

## Open items deliberately NOT in v1

- Full pause/resume persistence (R1 ledger enables it later).
- Streaming emission through MCP; chart cards over MCP beyond specs.
- The mechanism-template purity fork (per-engine call at migration).
- Calibration capability (separate design; consumes the form snapshot
  and basis machinery this campaign builds).
