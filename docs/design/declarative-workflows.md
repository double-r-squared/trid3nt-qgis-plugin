# Declarative workflows - the plan-value architecture

NATE-shaped design (2026-08-21/23 discussion). V1 LANDED (ADR 0303) -
do_sag MIGRATED. WAVE 2 LANDED (ADR 0304) - the FORM and DRAW cards, on
the existing spines, plugin 0.3.17. WAVE 3 LANDED (ADR 0305) -
telemac_river_dye MIGRATED (3,469 -> 671 lines over a shared
`workflows/telemac/steps/` family), the form card's first live proof,
and the live-run harness (`trid3nt_server/testing/`). WAVE 4 LANDED
(ADR 0306) - the GENERALIZATION CHECKPOINT PASSED:
`modflow_regional_water_budget` and `swmm_aquifer_baseflow_to_node`
migrated onto shared `workflows/<engine>/steps/` families, both
bit-identical, on four small library additions and no redesign. WAVE 5
LANDED (ADR 0307) - SWMM ENGINE CAMPAIGN WAVE A: the two standalone
solve templates (`swmm_rdii_rtk_unit_hydrograph`,
`swmm_snowmelt_degree_day`) declared, both bit-identical INCLUDING their
deck text, on NO library change at all; the shared SWMM family grew
multi-attribute sampling, the one line-chart spec and the deck
time-series helpers, and paid back its first 18 lines into the
checkpoint's own template. SWMM is 3 of 15; the remaining twelve are two
composer families (published-deck, mechanism-comparison) and three AOI
giants, and the wave boundaries follow the shared machinery rather than
the file sizes (ADR 0307's tranche plan). Focus engines: SWMM + MODFLOW
(top priority, EPA/USGS), TELEMAC, HEC-RAS (tail, skippable).
One principle everywhere: DECLARE THE WHAT, CENTRALIZE THE HOW - and
the what is a VALUE.

## The form: workflow = inputs + plan, both values

A workflow file has three parts, none of which execute anything:

1. PARAMS - frozen `Param` declarations (values: numbers, strings,
   flags, drawn coordinates). Resolve through the doors; render as the
   form; clamp by declared bounds.
2. DATA - frozen `Data` declarations (artifacts: rasters, layers,
   meshes, decks). Each carries its PRODUCER description
   (`Fetch.dem()`, `BuildMesh.channel(...)`) which the runner executes
   - or an explicit BYO artifact satisfies it instead.
3. `plan(p, d)` - a PURE function returning the step tree. No awaits,
   no side effects: real Python conditionals during construction, a
   fully-inspectable plan value as the output (the declarative-UI
   `body()` idiom).

The runner INTERPRETS the plan: it can print it (the step list),
render the form from it, step it in the dock, validate every `Ref`
before execution, derive the dataflow DAG ("which params feed step
6" is a graph query), parallelize independent Data producers, and
serialize the plan as the run's provenance record.

```python
PARAMS = [
    Param("location",       door=doors.QUESTION, desc="River reach, as a place name."),
    Param("spill_fraction", door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
          desc="Initial plume span as a fraction of reach width."),
    Param("release_coords", door=doors.USER,
          desc="Where the substance enters the water."),
    Param("mesh_size_m",    door=doors.DERIVED, resolve="telemac.suggest_mesh_size",
          user_lever=True, desc="Target element edge length."),
]
DATA = [
    Data("terrain", Fetch.dem()),                       # reference: fetch-fresh, never byo
    Data("rivers",  Fetch.river_geometry()),
    Data("rain",    Fetch.rain().ladder("era5_domain_mean")),
    Data("mesh",    BuildMesh.channel(banks=Ref("banks"), size=Ref("mesh_size_m"))
                       .byo(validate=CoversAOI)),       # authored: byo-able, validated
]

def plan(p, d):
    return Workflow("telemac_river_dye", engine="telemac2d")[
        Geocode.river(p.location).named("reach"),
        When(p.delineate,
             Delineate.watershed(dem=d.terrain).overrides_domain()),
        BankReach(rivers=d.rivers, seed=Ref("reach.seed")).named("banks"),
        DrawGate(param="release_coords", geometry="point",
                 prompt="Click where the substance enters the river"),
        FormGate(),
        WriteDeck.telemac(mesh=d.mesh, forcing=d.rain,
                          substance=p.substance, release=p.release_coords),
        Solve(),
        Postprocess()
            .render(preset="dye_concentration")
            .chart("concentration_timeseries", builder="telemac.steps.dye_chart"),
    ]
```

`p.<name>` yields a LATE-BOUND `ParamRef`, not the value: the plan is built
once, before the gates it declares have run, so it must DESCRIBE the read and
let the interpreter perform it against the current sheet. That is what makes a
form-gate revision reach the run (what-was-approved == what-ran). A real
construction-time branch reads the value explicitly - `When(p.get("delineate"),
...)` - and `bool(ParamRef)` refuses rather than silently reading True.

Every CONCRETE read path on the sheet records the name it hands over -
`get`, `row`, `rows`, `values_dict`, and the `values_view()` view's attribute
access alike. That record is what lets the validator refuse a plan that declares
a `FormGate` and also branches on a value that gate can revise; a read path that
did not record was a side entrance to the same frozen branch. The set closes at
validation, so a sheet reused for a second run carries no run-time reads into it.

A `When` body is a SCOPE: a step named inside it is Ref-able only from inside it,
because the branch may not be taken and a Ref from outside would be a runtime
`REF_UNRESOLVED` waiting to happen. That has a consequence worth stating, since
it decides plan shapes (ADR 0307): an OPTIONAL VARIANT whose result the answer
step must read cannot be `When`-guarded. Either the variant is declared
unconditionally, or the branch and everything that reads it move inside one
composite - and hiding a solve inside a composite is what this library exists to
undo. Both SWMM wave-A templates chose to declare: the RDII cross-check and the
snowmelt plow variant are always-on plan nodes, and both were things the template
claimed to be about anyway.

A step that runs its OWN input review declares `self_gating=True`; a plan may
not put a `FormGate` in front of one, because the composite reads its own
resolved sheet and never sees the plan's. `self_gating` is also one of the two
REVIEW SURFACES the law-9 floor recognises (the other is a declared `FormGate`):
a plan with neither refuses an invented physics default in every mode, live
session or not. An emitter is where a card COULD be shown, never evidence that
one was.

## One declaration, three surfaces

The `Param` list is written once and rendered wherever someone has to
read it. Which VIEW a surface gets is the surface's job, not the
author's:

| surface | view | why |
|---|---|---|
| the model's tool docstring | `render_docstring(view="full")` | it fills the params, so it needs the sheet in prose |
| the catalog / choose-a-tool page | `render_docstring(view="routing")` (via `fn.routing_doc`) | it only helps someone PICK the tool, and it must fit the 1000-char truncation budget |
| the FORM CARD | the `ParamSheet` itself | an edit surface needs the declaration structurally - bounds to clamp to, units to label with, a badge saying where the value came from - not a paragraph about it |

## The Domain environment

The current spatial domain (AOI) is an ENVIRONMENT value, not a
threaded argument - spatial producers read it implicitly (no repeated
`aoi=Ref("aoi")`); a step that refines it declares so
(`.overrides_domain()` - delineation, clip-to-county, a drawn polygon,
a byo mesh's footprint). The Case camera follows the final domain.

## Param vs Data

Param = a VALUE (fits in a form cell; doors; bounds; form-editable).
Data = an ARTIFACT (object store; produced or BYO'd; ladders +
coverage validation; emitted to the canvas as it arrives). Boundary
rule for drawn geometry: a handful of vertices parameterizing the
question = Param (via draw gate); a feature layer participating as a
dataset (obstruction geometry, clip zone) = Data with the draw gate
as producer. Data producers consume params, which is what makes
dataflow tracing cross the boundary.

AN ARTIFACT IS DATA; A SCALAR THE PLAN CONSUMES IS A DERIVED PARAM
(ADR 0306). A `Data` producer may `Ref` a param or another `Data`,
never a step, so a fetch that depends on a resolved LOCATION cannot be
declared as `Data` - `Data` reads the DOMAIN environment, which carries
an extent, and that fits rasters and layers. The test is what the plan
CONSUMES, not how the derivation reached it: if the consumed value is a
scalar that fits a form cell - it wants bounds, wants to be editable,
wants to be on the card - declare it `door=DERIVED`. (A point sample is
the common case, not the rule: a basin mean, a class fraction or a
station statistic is the same shape.) Derivations run inside
`resolve_params`, BEFORE the plan is built and therefore before the form
gate, and they may be `async` - so a derivation can geocode and can
fetch, and the card shows the REAL derived number with its source badge
rather than an empty row the user pins by hand. A derivation that read
the world returns `Derived(value, note, real_source)` so its evidence
rides on the row.

RECORDED GAP, not blessed doctrine: what such a derivation FETCHES on
its way to the scalar sits outside the `Data` machinery. Those rasters
are not ledgered (a resume re-fetches rather than replays), they are not
artifacts the interpreter can evict on a form revision (the DERIVATION
re-runs instead, and its own memo decides whether the fetch repeats),
and they are not walked by the terminal leaked-ref scan. Today the cost
is small - the derivations that exist read one memoized point per run -
but the SWMM and MODFLOW engine campaigns are the place to decide
whether a derivation's world-reads become first-class `Data` or stay a
documented exception.

## Temporal transforms (the `Data` modifiers)

A `Data` declaration states the cadence and the units its artifact ARRIVES
in, because both are part of what the artifact IS:

```python
Data("rain", Fetch.tool("...resolve_rain_forcing", ...)
        .ladder("gridmet_domain_mean", "user_rate")
        .resample(to="1D", max_gap="native*3")
        .normalize(units="mm/day")),
```

pandas does the arithmetic (`workflows/lib/temporal.py`); the library is
the doctrine around it, and three rules decide every call:

- THE QUANTITY CLASS PICKS THE METHOD. A RATE resamples conservatively
  (mass-preserving: the interval mean going down, hold-the-interval going
  up), a STATE interpolates linearly, a CATEGORICAL value moves by
  nearest and by nothing else. The first two are overridable per
  declaration; averaging class labels is refused.
- INTERPOLATION IS DECLARED. A transform that ran leaves a provenance
  stamp ("resampled 6h->1h linear", "converted in/day->mm/day"), so a
  manufactured value is distinguishable from an observed one - and a
  payload with NO `.resample()` is never realigned behind the consumer's
  back. That is the wave-A clocks-align bug class, closed by declaration.
- A HOLE WIDER THAN `max_gap` REFUSES, typed. Within-cadence
  interpolation is refinement; bridging a hole in the record is
  invention. Default bound: three native intervals (`"native*3"`), where
  native is the record's own lower-median sample spacing.

Unit conversion rides an EXPLICIT table in `temporal.py`, not a units
engine: a conversion nobody declared is one nobody can check, and a
cross-dimension request refuses rather than guessing.

The declaration travels TO the producer (the same channel `.ladder()`
uses), because the producer is the only party that knows the payload's
quantity class and native cadence - the interpreter never reshapes a
payload it cannot read (the no-double-middleware law). A single-value
payload accepts `.normalize()` and a `.resample()` at its own cadence; a
`.resample()` to any OTHER cadence refuses, since one number carries no
time axis to redistribute.

Surfaces: the provenance stamp rides the run's `SyntheticInput` row (the
`event_time` pinning style), and the gap/shape/units refusals are typed.
The FORM BADGE is not wired: the form card is a param sheet, and a `Data`
row on it needs a `ParamSheet` contract plus a plugin change.

## The doors (Param resolution order)

1. BYO/USER - explicitly passed this invocation. NEVER ambient: no
   case-store lookup, ever (NATE ruling - kills the stale-DEM
   collision class at the root).
2. QUESTION - agent-filled from the ask (the user's words beat data).
3. FETCHED/DERIVED - real data or derivation seams; ladders apply.
4. SCENARIO - labeled default with declared bounds, surfaced at the gate.
5. CONSTANT - non-question physics; defaulted, never asked, inspectable.
6. GATE/REFUSE - asked for (form/draw) or refused typed. Never invented.

BYO data rule (NATE, from the declarative-UI mutable/immutable axis):
AUTHORED artifacts (meshes, networks, decks, edited layers, survey
rasters) are byo-able; REFERENCE data (DEM, landcover, canonical
rasters) is not - fetch-fresh for the domain - except the explicit
survey-grade user_supplied ladder rung. EVERY byo input is
coverage-validated against the domain at resolution (typed refusal on
mismatch).

## Rulings in force (NATE, 2026-08-21/23)

- GATE WAITS - HYBRID: v1 waits in-turn with TTL (timeout = typed
  refusal naming the unmet gate); the step ledger is resumable-shaped
  from day one.
- IMMUTABLE RECIPES: the agent fills params and answers gates, never
  alters the plan - structurally enforced (it only supplies `p`).
- FORM EDITS: every row editable; editing a derived value warns via
  its source badge and stamps basis=user. No locks.
- FAILURE/RERUN: RESUME FROM THE FAILED STEP - the ledger replays
  completed steps from cached artifacts and re-executes from the
  failure. Restart-clean is a flag, not the default.
- MIGRATION ACCEPTANCE: (a) every incident-hardening behavior in the
  old composer inventoried and consciously re-homed (bound, tool
  check, or documented deletion); (b) reference run old-vs-new, same
  question -> same physical answer within tolerance, QGIS-true renders
  side by side.
- NET-LOC LAW: rolling net LOC per landing; an engine family finishes
  NET-NEGATIVE (TELEMAC baseline: 12,139 lines / 8 templates).
- PURITY RULE (charter-grade): zero demo constants, zero baked decks
  in tool/workflow code; demos live in banner-labeled demo scripts.
  Mechanism-compare templates: per-engine fork at migration
  (deck-as-input vs demoted to demo script).

## Gates

All on the existing pending-confirmation spine; two modes preserved
(auto = labeled defaults / typed refusals, never hangs; user_gated =
waits per the hybrid rule).

- FORM GATE: the resolved param sheet as an editable form - name,
  value, unit, SOURCE BADGE ("geocoded from your prompt" / "derived
  from NLCD" / "default"), edit field; question-bearing on top,
  constants under an "advanced" fold; the submitted snapshot persists
  as the run's input record (and is what calibration will later read
  and write). The ModelMuse/SWMM-GUI property grid, pre-filled by a
  sentence. Wire: the OPTIONAL `param_sheet` field on the existing
  `tool-payload-warning`; edits ride back on `tool-payload-confirmation`
  (`narrow_scope` + `revised_args`). SUBMIT IS THE APPROVAL - the whole
  sheet was on screen, so the gate does not re-present it; the text card
  without a sheet keeps its adjust-and-re-present rounds.
- DRAW GATE: point | polyline | polygon | rectangle, prompt text. No
  ghost suggestions: user_gated waits; auto refuses typed. Extends the
  existing AOI-rectangle machinery. Wire: the existing
  `spatial-input-request` pair - `point`/`bbox` ride the stock pick
  tools, `polygon`/`polyline` ride purposes `aoi`/`line` and the
  plugin's vertex-capture tool. Draw-time constraints (within(reach),
  on-mesh) are still OUT - the geometry to constrain against is produced
  after the gates, so there is nothing to check at gate time and a
  declared-but-unread constraint is a dead promise.
- WHAT A GATE ASKS FOR, per mode: `auto` never shows a card - an
  OPTIONAL param's `derived_when_absent` describes its own absence, and
  a required one refuses typed. `user_gated` ASKS in both cases, because
  declaring the gate is the request to ask. The DECLINE is what differs:
  an optional param falls back to its declared absence, a required one
  refuses naming the unmet gate.
- SEATING: form edits and drawn values take the SAME path - re-seated
  through the GATE door (declared bounds still apply, `basis=user`),
  derivations re-run, dependent Data evicted, the ledger re-keyed. The
  cards are new FRONT ENDS to that machinery, not new semantics.

## Steps beyond fetch/solve

- PRE/POST/RENDER as declared steps. The render toolset promotes the
  single publish_layer styling seam into declared primitives;
  zero-as-transparent is a RENDERING choice only (rasters keep their
  zeros - law 9 applies to pixels) and arrives WITH that toolset, since
  publish_layer has no zero-handling knob to declare against today.
  Render steps are agent-callable conversationally. A render's SOURCE
  is not auxiliary: a step that declared a render and produced no
  raster failed, and says so.
- CHART STEPS: the chart SPEC (kind + data + axes) is the persisted
  product; the plugin chart dock is the ONE renderer. Closes the
  chart-restore gap; ends server-side figure generation (matplotlib
  retirement ledger row); specs are JSON, readable by MCP clients. The
  run carries its built specs out on `RunResult.charts` and writes them
  to its own object-store prefix beside the physical-answer metrics, so
  VERIFICATION cites the product's chart rather than rebuilding one.
- SENSOR EMISSION: station-shaped fetchers publish sensor POSITIONS
  as a context layer alongside their data.
- QGIS-TRUE PROOF RENDERER: PyQGIS headless rendering through QGIS's
  own engine + plugin presets + ESRI basemap - pixel-identical to the
  canvas; becomes the montage engine.

## Patterns in play (GoF, kept explicit so extension follows the grain)

- COMPOSITE: steps and named sub-plans form one tree; a step group
  (e.g. CoastalBed: fetch -> validate -> clip) is a value reusable
  across workflows.
- INTERPRETER: the runner walks the plan; plans never run themselves.
- BUILDER/FLUENT: modifiers (.byo(), .ladder(), .render(),
  .overrides_domain()), each returning a new value; modifier LEGALITY
  is the rule surface (reference fetchers simply lack .byo()).
- STRATEGY: doors and fallback ladders - interchangeable resolution
  policies.
- TEMPLATE METHOD: per-engine step families (WriteDeck.telemac /
  .swmm / .modflow) share the skeleton, override one serialization
  hook each - the generalization checkpoint made structural.
- MEMENTO: the step ledger (completed steps + resolved params +
  artifact URIs) - powers resume-from-failure now, full pause/resume
  later.

## Testing

Declarative: a test is a declared invocation (!run in the dock, a
plan stepped line by line, or the same over MCP). The plan validator
(Ref integrity, modifier legality, gate placement) runs before any
execution. Offline pytest remains for CI.

THREE PATHS, and the split is itself diagnostic (NATE, 2026-08-24):

- **A - the all-params-upfront `!run`.** Every unfilled param supplied
  on the call, so the gates are SATISFIED rather than skipped: each row
  arrives through the USER door and there is nothing left to ask. The
  mechanical contract/physics check, one declared line, run often. Demo
  VALUES live in the declaration - a demo script IS a saved,
  banner-labeled path-A invocation, never a constant in workflow code.
- **B - the gate-by-gate walkthrough**, over the real socket
  (`trid3nt_server/testing`, ADR 0305): the tool, its args, the answers
  its gates get, and the assertions - `LiveRun(tool, args, answers=
  GateAnswers(draw=..., form_edits=..., require_draw=True))`. The full
  product-path audit, run at wave acceptance. Three rules make it
  evidence rather than a script: a declared answer is also an
  EXPECTATION (a card that never fired is a failure, not a silent
  pass); the assertions read the run's OWN persisted products off its
  prefix rather than recomputing the answer; and the harness is product
  code beside the server, because drivers are.
- **C - NATE in QGIS.** Plugin-UI coverage only; A and B own the logic.

A-green with B-red isolates a fault to the interaction machinery.

## Migration order

1. Library v1: Param/Data/plan value types, the interpreter with
   ledger + resume, plan validator, form + draw cards (plugin), the
   Domain environment.
2. do_sag migration (314 lines - fast ergonomic feedback).
3. river_dye migration (the full-contact proof; R3 acceptance;
   net-LOC meter on).
4. GENERALIZATION CHECKPOINT: one SWMM and one MODFLOW template
   before any mass conversion. DONE (ADR 0306) - PASSED.
5. SWMM + MODFLOW engine-complete campaigns (purity + meshing + byo
   mesh adoption; row-19 river_dem_uri wiring lands here), TELEMAC
   family completion, HEC-RAS tail (skippable). Two items queued out
   of the checkpoint: the SWMM Green-Ampt trio derived from the same
   texture fit as the aquifer column (a physics change, NATE's), and
   river_dye's carrier discharge adopting the DERIVED door, which
   closes ADR 0305's delta 1 with no library change.
   SWMM waves, per ADR 0307's tranche plan: **A - the standalone solve
   templates (LANDED)**; B - the published-deck trio, where a fetched
   deck becomes the family's first `Data` (authored, therefore
   `.byo()`-able); C - the mechanism-comparison five, where the purity
   fork gets its per-engine answer over the SHARED deck-authoring core,
   all five at once; D - the AOI giants (`urban_flood`,
   `network_import`, `dual_drainage`), where the `Data` producers, the
   byo-mesh adoption and the declared render steps land and where the
   family's net-LOC return is realised.

## Deliberately not in v1

- Full pause/walk-away/resume persistence (the ledger enables it).
- Streaming emission over MCP; chart rendering beyond specs.
- The mechanism-template purity fork (per-engine call at migration).
- Calibration capability (separate design; consumes the form
  snapshot + basis machinery this campaign builds).

## The Workflow Skeleton (Template Method) - PROPOSED, FOR NATE REDLINE

Agreed in the 2026-08-24 architecture discussion (rulings recorded in
docs/IDEAS.md, 2026-08-24/25 entries); this section is the contract the
family campaign builds. Status: PROPOSED until NATE redlines.

### The placement rule

A capability lives at the highest layer where it needs no
specialization; it drops one layer only when variation is genuine; and
it drops all the way to the workflow only when the variation is
per-question. Every placement below is an application of this rule, and
any future placement argument is settled by it.

### The abstract Workflow class

Base class name RULED: `Workflow`. A template file DECLARES a workflow;
the class IS one - the apparent name collision with the species is
coherence, not conflict. Analysis-only templates ride the same skeleton
and simply leave the solve-family slots unfilled.

`Workflow` owns everything that never varies:

- the stage sequence: acquire -> prep -> mesh -> gates -> author ->
  solve -> post -> publish;
- gate mechanics (form/draw/select on the pending-confirmation spine);
- chart scaffolding (display/persist/emit - invisible to templates);
- the emission seam (automatic publish; see Emission unification);
- solve supervision (the shared solver supervisor);
- ledger + resume, provenance, the leak guard;
- the registration factory (see below).

Two slot kinds, distinguished per slot:

- **hooks** - SILENT defaults: charts, sensor/context layers,
  validation checks. Unfilled = nothing happens; no engine subtype ever
  restates them (chart scaffolding exists ONLY in `Workflow`).
- **abstract slots** - must-fill: physics and the EngineOps five. The
  library refuses to register a template that leaves one empty.

### EngineOps - the engine facade

Each engine subtype realizes exactly five abstract operations, and
nothing else:

    acquire_domain(p, d)          build_mesh(domain, policy)
    author(mesh, physics, forcing)  solver_spec() -> image/limits
    read_results(run) -> result

Facades are named by engine ONLY: `TelemacWorkflow`, `SwmmWorkflow`,
`ModflowWorkflow`. Domain qualifiers are BANNED ("Reach" rejected: it
welded a domain assumption into the engine facade; domain shape arrives
through `acquire_domain` slots and shared domain steps). The facade's
value is stability: the interface never changes while the mechanisms
behind it - meshers, writers, readers - evolve freely.

### Three step tiers

- `workflows/shared/` - DOMAIN steps, engine-agnostic: forcing
  resolvers, reach acquisition, soil/roughness derivations, temporal
  transforms. Unifies the world's interface. (Filed by what varies,
  never by who happened to build it - forcing is not TELEMAC mechanism.)
- `workflows/<engine>/steps/` - ENGINE steps: deck writers, meshers,
  result readers. The engine's weirdness ends here, normalized behind
  EngineOps.
- the skeleton + library plane both tiers plug into.

### Slots are value objects

Slot payloads (physics, forcing, mesh policy) are VALUE objects, never
kwarg chains. The do_sag composite - one plan step funneling seventeen
explicitly-named kwargs through three files - is the named disease
exhibit and DIES. Runners take the params view or a slot value; no
signature exists whose only job is forwarding.

### The chart contract

A chart is a plain, standalone-runnable function
`(result, params) -> spec`, COLOCATED in the template file beside
`plan()`, and referenced as a FUNCTION OBJECT - never a dotted string.
The skeleton hook owns display, persistence, and emission invisibly.
There is no Builder DSL (rejected twice); optional plain helpers only.

### The registration factory

The ~70-line `_normalize` / `_with_notes` / `_physical_answer` +
try/except tool-body tails repeated in do_sag.py and river_dye.py are
absorbed into library-generated registration:
`register_workflow(metadata, PARAMS, plan, answer=(...))`. Template
file end state: PARAMS + plan + answer + chart - four declarations and
a chart. The old tool bodies are deleted, not wrapped.

### Acceptance criterion (falsifiable)

The skeleton ADR must enumerate the workflow-only change list: every
meaning-level edit - values, bounds, doors, composition, gates, charts,
answer - achievable in the template file with ZERO other touches. A
meaning-level change that turns out to require touching steps/ is a
defect BY DEFINITION. Mechanism changes touch exactly one runner. The
skeleton wave demonstrates at least two workflow-only changes live.

### The demolition clause

This is a GENERALIZATION refactor: absorbed functionality is DELETED
outright - no backward compatibility, no dual paths, no deprecation
shims, no transition aliases. The composite dies; pass-through
signatures are deleted; old tool bodies are replaced by the factory and
removed; dotted-string chart builders become function refs with no
string fallback. DISTINCTION: interfaces and wiring owe nothing to the
past - PHYSICS ANSWERS still owe parity (R3 stands: same question ->
same answer; "no back compat" is about API shape, never about results
drifting). Every removal gets a DELETION_LEDGER row.

### The no-double-middleware law

Fetcher invocations are DATA. The fetcher router's middleware - cache,
fallback ladders, provenance, staleness, typed refusals - lives ONCE
and is authoritative. No step tier, engine facade, or skeleton stage
re-implements or re-wraps it at another level of abstraction: the
acquire stage INTERPRETS `Data` declarations; it never fetches.

### Emission unification (publish_layer dies)

Emission becomes automatic on ALL three paths: fetchers (already),
declarative workflows (the skeleton's publish stage), and
processing-primitive rasters (NEW - hillshade/NDVI/slope and playground
outputs auto-emit, intermediates included: they are useful input
checks, and the user hides what they don't want). The mechanism -
styling seam (`_resolve_qgis_style_params`), overview enforcement,
layer registration - moves OUT of the publish_layer tool file into
`emission/` as its single home. The registered `publish_layer` tool is
then DELETED (DELETION_LEDGER entry QUEUED 2026-08-24; condition: a
live case shows a processing raster on the map with zero publish call,
plus flood canary green).

### Mesh

Mesh is BYO-optional `Data`: AUTHORED (user-supplied - e.g. the 2dm
import path - the top ladder rung) or GENERATED (the shared front in
`workflows/mesh/`, the default). The default generation policy is
opinionated toward SPEED - a fast, normal-quality baseline, never a
slow optimized guess; the mesh-economy A/B calibrates it. Both paths
converge at the MESH GATE on the pending-confirmation spine, where
refinement is USER-DRIVEN via atomic mesh tools (refine-region,
densify-along-channel, coarsen) acting on the ENGINE-NEUTRAL mesh
artifact. The shared front is neutral artifact + thin per-solver
writers (the hecras_build pattern); cross-engine translation is a
writer WITHIN a mesh species (unstructured tri: TELEMAC / SCHISM /
HEC-RAS 2D) and a TYPED REFUSAL across species (MODFLOW structured
grids, SWMM node-link networks) - never a silent conversion.
`EngineOps.build_mesh(domain, policy)` is the frozen interface;
generation strategies and writers evolve behind it. The TELEMAC private
corridor mesher folds into the shared front as a generation strategy;
the private ladder dies (ledger row).

### Layout map and campaign sequencing

Destinations (extends the Migration order section above; where they
conflict, this table governs):

| today | destiny |
|---|---|
| `data/fetchers/` | `tools/fetchers/` (pure rename - the substrate) |
| `data/processing/` | `tools/processing/` + misdirection audit (workflow verbs -> `workflows/shared/`; dead web-era code -> delete) |
| `data/search/` | `tools/search/` |
| `data/publish_layer/` | `tools/publish_layer/` interim; dissolved by emission unification |
| `data/simulation/` engine shims | STAY PUT; die engine-by-engine as the factory absorbs them (moving a thing scheduled to die is double work) |
| `data/simulation/solver/` | `workflows/solver/` |
| `data/simulation/diagnostics/`, `_setter_envelope.py` | `workflows/solver/diagnostics/` (server runtime imports it - registered tool read_run_diagnostics; scripts/ routing was wrong); envelope helper -> `workflows/lib/` |
| `data/meta/`, `data/display/` | `tools/meta/`, `tools/display/` - NOT dead (meta holds 5 registered tools incl code_exec + spatial_input; display holds show_nexrad_radar) |
| `declarative/` | `workflows/lib/` |
| per-template `steps.py` | dissolves during skeleton migration |

Sequence: (1) the mechanical move wave - git mv + import rewrites, zero
behavior change, offline suite + flood canary green as the gate, so the
skeleton is born at its permanent address; (2) skeleton +
`TelemacWorkflow` on the new homes; (3) engine migration and shim
deaths, one ledger row each; (4) `rmdir data/`. Milestone, falsifiable:
**the campaign is done when `data/` does not exist.**
