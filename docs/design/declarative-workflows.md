# Declarative workflows - the plan-value architecture

NATE-shaped design (2026-08-21/23 discussion). V1 LANDED (ADR 0303) -
do_sag MIGRATED; plugin form/draw cards are wave 2. Proving order:
telemac do_sag (314 lines, fast feedback),
then telemac_river_dye (3,503 lines, the full-contact proof). Focus
engines: SWMM + MODFLOW (top priority, EPA/USGS), TELEMAC, HEC-RAS
(tail, skippable). One principle everywhere: DECLARE THE WHAT,
CENTRALIZE THE HOW - and the what is a VALUE.

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

A step that runs its OWN input review declares `self_gating=True`; a plan may
not put a `FormGate` in front of one, because the composite reads its own
resolved sheet and never sees the plan's. `self_gating` is also one of the two
REVIEW SURFACES the law-9 floor recognises (the other is a declared `FormGate`):
a plan with neither refuses an invented physics default in every mode, live
session or not. An emitter is where a card COULD be shown, never evidence that
one was.

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
  sentence.
- DRAW GATE: point | polyline | polygon | rectangle, prompt text. No
  ghost suggestions: user_gated waits; auto refuses typed. Extends the
  existing AOI-rectangle machinery. Plugin cost: two card types.
  Draw-time constraints (within(reach), on-mesh) land WITH the wave-2
  draw card that can enforce them - the geometry to constrain against
  is produced after the gates, so there is nothing to check at gate
  time and a declared-but-unread constraint is a dead promise.

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
  retirement ledger row); specs are JSON, readable by MCP clients.
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

## Migration order

1. Library v1: Param/Data/plan value types, the interpreter with
   ledger + resume, plan validator, form + draw cards (plugin), the
   Domain environment.
2. do_sag migration (314 lines - fast ergonomic feedback).
3. river_dye migration (the full-contact proof; R3 acceptance;
   net-LOC meter on).
4. GENERALIZATION CHECKPOINT: one SWMM and one MODFLOW template
   before any mass conversion.
5. SWMM + MODFLOW engine-complete campaigns (purity + meshing + byo
   mesh adoption; row-19 river_dem_uri wiring lands here), TELEMAC
   family completion, HEC-RAS tail (skippable).

## Deliberately not in v1

- Full pause/walk-away/resume persistence (the ledger enables it).
- Streaming emission over MCP; chart rendering beyond specs.
- The mechanism-template purity fork (per-engine call at migration).
- Calibration capability (separate design; consumes the form
  snapshot + basis machinery this campaign builds).
