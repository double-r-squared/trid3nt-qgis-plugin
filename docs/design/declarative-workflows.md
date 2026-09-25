# Declarative workflows - the plan-value architecture

One principle everywhere: DECLARE THE WHAT, CENTRALIZE THE HOW - and the what is
a VALUE. A template is inputs and declarations, both frozen at import; the plan a
run executes is assembled by the WORKFLOW CLASS out of them, and nothing in a
template names a function to call.

## The form: workflow = inputs + plan, both values

A workflow file has three parts, none of which execute anything:

1. PARAMS - frozen `Param` declarations (values: numbers, strings,
   flags, drawn coordinates). Resolve through the doors; render as the
   form; refuse a value outside the declared bounds.
2. DATA - a CLASS BODY, one row per artifact (rasters, layers, meshes,
   decks). The attribute NAME is the row name; the value is the row's
   PRODUCER description, written with the one author word `tool(...)`,
   which the runner executes - or the artifact the caller SUPPLIED
   satisfies it instead. Class-body ORDER is the row order.
3. `plan(ops)` - a PURE function returning the step tree. No awaits, no side
   effects, and NO SHEET: it reads no concrete value, so it is built ONCE at
   registration and the interpreter walks the same value on every run.

The runner INTERPRETS the plan: it can print it (the step list),
render the form from it, step it in the dock, validate every `Ref`
before execution, derive the dataflow DAG ("which params feed step
6" is a graph query), parallelize independent Data producers, and
serialize the plan as the run's provenance record.

```python
# declarations.py, one file over
class PARAMS:
    location = Param(door=doors.QUESTION, desc="River reach, as a place name.")
    spill_fraction = Param(
        door=doors.SCENARIO, default=0.25, bounds=(0.05, 0.9),
        desc="Initial plume span as a fraction of reach width.")
    release = Param(
        door=doors.USER, type=Point, desc="Where the substance enters the water.")
    mesh_size_m = Param(
        door=doors.DERIVED, resolve="telemac.suggest_mesh_size",
        user_lever=True, desc="Target element edge length.")

# the template file: `from .declarations import PARAMS as P`
class DATA:
    domain = Data.need("hydrography", at=P.release)
    bed = Data.need("terrain")
    rain = Data.need("precipitation series").context(
        "no hourly rainfall record over this catchment for that window")
    # row-to-row dataflow inside the body is the plain identifier
    basin = tool("delineate_watershed", dem_uri=bed)
    breakwaters = Data.supplied(geometry="polyline").optional()   # a context SLOT


# -- the binding blocks --------------------------------------------------- #
PHYSICS = Physics("tracer", substance=P.substance, release=P.release)
FORCING = Forcing(carrier=Ref("carrier_discharge"), rain=DATA.rain)
MESH    = tool.build_mesh(mesher="om2d", kind="unstructured_tri",
                          extent=Ref("reach_polygon"),
                          resolution_m=P.mesh_resolution_m,
                          ops=[mesh_op("laplacian2"),
                               mesh_op("set_bed", source=DATA.dem)])


def plan(ops):
    return [
        FormGate(),
        DrawGate(param="release", geometry="point",
                 prompt="Click where the substance enters the river"),
        Geocode.river(P.location).named("reach"),
        When(P.delineate,
             Delineate.watershed(dem=DATA.terrain).overrides_domain()),
        ops.author(mesh=MESH, physics=PHYSICS, forcing=FORCING),
        ops.solve(cores=P.cores, physics=PHYSICS),
        ops.read(Ref("solve"), physics=PHYSICS, forcing=FORCING)
           .chart("concentration_timeseries", builder=dye_chart),
    ]
```

The plan returns the STEP SEQUENCE. The skeleton names and engines it (from the
registration metadata and the facade), and `ops` is the engine facade whose four
operations the mechanism steps are reached through. A chart's `builder` is the
FUNCTION, colocated in the template file.

### The static-plan rule

`P.<name>` yields a LATE-BOUND `ParamRef` and `DATA.<row>` a `DataRef`, never the
value. BOTH are the template's OWN class bodies - `P` is the import alias of its
`PARAMS` - which is the whole point: a binding block can sit above `plan()` as a
plain frozen value, and the plan becomes a pure assembly of blocks rather than a
function that has to be called with a sheet before it means anything. A
misspelled row or param is an `AttributeError` at the line that wrote it, because
the body is a real class, and a name from another template's sheet is unwritable.
A ref built from a STRING still reaches the validator, which refuses it at
registration with the nearest declared spellings.

A REF TAIL BINDS OR REFUSES. `Ref("centerline.bbox")` naming a field the result
does not define - or one that is there and empty - is a typed `REF_FIELD_MISSING`
at BINDING, naming the ref and the field. No silent `None` reaches a step: the
ParamRef-leak law reaches attribute tails for the same reason it reaches refs.

The blocks are DEEP-frozen. They live at module scope for the life of the process
and every run reads the same object, so a nested mapping or list inside one would
be a cross-run channel: a step that popped a key out of a declared dict would
change what the next run declares.

The plan reads NO concrete value, which has three consequences:

- it is built ONCE, at registration, and validated there - an
  unreachable `Ref`, a misplaced gate or a physics process the facade does not
  model is an AUTHORING error that never reaches a caller as a run failure;
- there is no read-recording machinery, because there are no construction-time
  reads to record. `ResolvedParams.get` is gone; the concrete read is
  `value_of`, and it belongs to the interpreter and to code running WITH a sheet;
- EVERY conditional is a `When`, and the interpreter decides it AFTER the gates.

### `When` - the one conditional

A `When` condition is a late-bound read (`P.<param>`, `DATA.<data>`, or
`Ref("step.field")`); a concrete value is refused, because a branch decided while
the plan value is being built is decided before anything the user could approve.
The interpreter binds the condition against the CURRENT sheet at the moment the
branch is reached, so an approved form-gate revision decides which body runs -
the same what-was-approved-is-what-ran promise the late-bound reads give a step's
arguments.

A guarded body is also a SCOPE: a step named inside it is Ref-able only from
inside it, because the branch may not fire and a Ref from outside would be a
runtime `REF_UNRESOLVED` waiting to happen. That decides plan shapes: an OPTIONAL
VARIANT whose result the answer step must read cannot be `When`-guarded - either
the variant is declared unconditionally, or the branch and everything that reads
it move inside one composite, and hiding a solve inside a composite is what this
library exists to undo. A template declares the variant.

Guarded steps still carry stable ledger indices (every declared node is numbered,
fired or not), so an attempt that took a different branch than the one before it
can still replay what it shares.

### The declarations sibling

Every template folder carries `declarations.py`, holding exactly PARAMS and DOC.
The template file keeps the QUESTION docstring, DATA, the binding blocks, `plan`,
ANSWER, the chart function, the metadata and the registration - so the RECIPE
reads on one page while the CONTRACT, which runs to forty rows, is one file over.
Python rather than JSON, because types, `resolve=` hooks and the three render
surfaces all keep working.

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
| the FORM CARD | the `ParamSheet` itself | an edit surface needs the declaration structurally - the bounds a value is held to, units to label with, a badge saying where the value came from - not a paragraph about it |

## The Domain environment

The current spatial domain (AOI) is an ENVIRONMENT value, not a
threaded argument - spatial producers read it implicitly (no repeated
`aoi=Ref("aoi")`); a step that refines it declares so
(`.overrides_domain()` - delineation, clip-to-county, a drawn polygon,
a byo mesh's footprint). The Case camera follows the final domain.

## Param vs Data

Param = a VALUE (fits in a form cell; doors; bounds; form-editable).
Data = an ARTIFACT (object store; matched, supplied, or absent;
coverage validation; emitted to the canvas as it arrives).

Producers are DEMAND-PULLED: one runs when a step that `Ref`s it executes, which
is what makes a `When`-guarded consumer whose branch does not fire cost no fetch.

A RUNTIME ROW STATES THE NEED IT HAS, written
`bed = Data.need("bathymetry")`. The need is one class of a coarse
vocabulary (`trid3nt_contracts.coverage.DATA_CLASSES`); every fetcher states what
it covers on its own `source.yaml`, and the match
(`tools/search/match.py`) filters those rows on class and place, holds a
SERIES source to the run's window, sorts on the cell against the mesh, on
recency and on the native datum, then calls the survivors in rank order and
drops one that held nothing over this domain. A row NEVER names a fetcher: the
facts that decide whether a source can carry this solve are read at run time off
coverage rows, and a row that states both a need and a producer is refused at
declaration.
What the match produces is ONE ranked list in three views: the card renders it
with the pick highlighted, the tool result carries the rows only on a tie, and
the run record stores the pick and its reason - all reading the one sentence the
model was given. The window comes off the deck's own duration from `event_time` and the frame off
the vertical lever, so a question states neither; the place is the domain's
centre unless the row states the point it is asked at, `Data.need("...", at=...)`.

A row may declare NO producer and no need at all - a CONTEXT SLOT, written
`structure = Data.supplied(geometry="polyline")`. The template names the SHAPE it
accepts and says nothing about where the thing comes from, because naming a
default fetcher for a breakwater or a clip zone is an opinion the question does
not carry. What satisfies one arrives from outside (a layer the user already has,
a file uri, a gate's answer) or nothing does, and `.optional()` says that absence
is legal - and LABELLED: the run reports which slot went unfilled, because it
answered a slightly different question than one that had the layer. An
unsatisfied REQUIRED slot refuses typed. Boundary
rule for drawn geometry: a handful of vertices parameterizing the
question = Param (via draw gate); a feature layer participating as a
dataset (obstruction geometry, clip zone) = Data with the draw gate
as producer. Data producers consume params, which is what makes
dataflow tracing cross the boundary.

AN ARTIFACT IS DATA; A SCALAR THE PLAN CONSUMES IS A DERIVED PARAM
(the generalization checkpoint). A `Data` producer may `Ref` a param or another `Data`,
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

## The alignment - one record onto a reader's clock

A measured record reaches the runtime as a `Series`: instants on the run's own
clock, values, and the unit they were measured in, frozen. Moving one onto the
clock a reader reads it on is `inputs/series.py::align`, and it is the only
place a record moves:

```python
found = align(record, onto=step_s, quantity=RATE, units="m3/s",
              max_gap_s=21_600.0, opening_at=start_s)
```

Four rules decide every call:

- THE QUANTITY CLASS PICKS THE METHOD, and the class rides on the row rather
  than being stated by an author: `quantity_class(coverage.data_class)` reads it
  off the source that answered, and a slot's role is the fallback where no row
  states one. A RATE moves conservatively (the time-weighted interval mean, so
  the total it reports is the total it keeps), a STATE interpolates between the
  two rows bracketing the instant - the engine's own reading of its own table -
  and a CATEGORICAL value moves to its nearest neighbour and nowhere else,
  class labels having no average and no slope.
- A CLOCK NO COARSER THAN THE RECORD ASKS FOR NOTHING THE RECORD DOES NOT
  ALREADY ANSWER, so the record stands exactly as measured.
- A HOLE WIDER THAN THE BOUND REFUSES, typed. Within-cadence interpolation is
  refinement; bridging a hole in the record is invention. The bound defaults to
  three native intervals, and a caller who means to admit a hole states one.
- WHAT WAS DONE COMES BACK AS A STAMP ("1h->90min conservative", "converted
  mm/h->mm/day", "native 1h kept"), so a manufactured value is distinguishable
  from an observed one.

Unit conversion rides an EXPLICIT table in `workflows/runtime/temporal.py`, not
a units engine: a conversion nobody declared is one nobody can check, and a
cross-dimension request refuses rather than guessing.

Four callers, and no fifth: the Atmosphere ingestion putting a station's report
on the run's clock, the observation coercion reading a gauge in the unit its
slot reads, the model-vs-observation pairing reading the model AT the moment the
gauge reported, and the liquid-boundaries author writing a window at the
engine's own time step.

## The doors (Param resolution order)

1. SUPPLIED/USER - explicitly passed this invocation. NEVER ambient: no
   case-store lookup, ever - that is the stale-DEM collision class at
   the root.
2. QUESTION - agent-filled from the ask (the user's words beat data).
3. FETCHED/DERIVED - real data or derivation seams; ladders apply.
4. SCENARIO - labeled default with declared bounds, surfaced at the gate.
5. CONSTANT - non-question physics; defaulted, never asked, inspectable.
6. GATE/REFUSE - asked for (form/draw) or refused typed. Never invented.

SUPPLIED data rule (from the declarative-UI mutable/immutable axis):
AUTHORED artifacts (meshes, networks, decks, edited layers, survey
rasters) are byo-able; REFERENCE data (DEM, landcover, canonical
rasters) is not - fetch-fresh for the domain - except the explicit
survey-grade user_supplied ladder rung. EVERY byo input is
coverage-validated against the domain at resolution (typed refusal on
mismatch).

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
  plugin's vertex-capture tool. A picked POINT carries a NAME: the
  plugin's pick card offers a field, defaulted to `point-<n>`, the reply
  carries it as `name`, and the Point slot that asked carries it on - a
  river dye run calls its tracer by it. A Point slot is a `Param` typed
  `Point` (`inputs/point.py`); its coercion `point_arg` reads
  the wire value in any of the ingestion's forms, and in a live
  `user_gated` session with nothing on the wire asks the canvas. Draw-time constraints (within(reach),
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
  cards are front ends to that machinery and carry no semantics of their own.

## Steps beyond fetch/solve

- PRESENTATION IS NOT IN A DECLARATION. Emission is automatic on every surface,
  and how a product is drawn follows from what it IS: a fetcher's `style:` row,
  a solved output's kind and quantity. A workflow declares no ramp, no range and
  no title, because none of them change the simulation. Everything ad hoc lives
  on the ONE presentation surface, `restyle_layer`, at runtime. A declaration
  carries no render verb and no style modifier: renders are the plugin's job,
  and workflows describe products.
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
- BUILDER/FLUENT: modifiers (.supplied(), .optional(), .context(),
  .style(), .overrides_domain()), each returning a new value; modifier
  LEGALITY is the rule surface (reference fetchers lack .supplied()).
- STRATEGY: doors and fallback ladders - interchangeable resolution
  policies.
- TEMPLATE METHOD: per-engine step families (Assemble.reach /
  .rain_on_grid) share the skeleton, override one serialization
  hook each - the generalization checkpoint made structural.
- MEMENTO: the step ledger (completed steps + resolved params +
  artifact URIs) - powers the work a DERIVED rerun inherits from its
  parent now, full pause/resume later. Every terminal state, failure
  included, tombstones it.

## Testing

Declarative: a test is a declared invocation (!run in the dock, a
plan stepped line by line, or the same over MCP). The plan validator
(Ref integrity, modifier legality, gate placement) runs before any
execution. Offline pytest remains for CI.

THREE PATHS, and the split is itself diagnostic:

- **A - the all-params-upfront `!run`.** Every unfilled param supplied
  on the call, so the gates are SATISFIED rather than skipped: each row
  arrives through the USER door and there is nothing left to ask. The
  mechanical contract/physics check, one declared line, run often. Demo
  VALUES live in the declaration - a demo script IS a saved,
  banner-labeled path-A invocation, never a constant in workflow code.
- **B - the gate-by-gate walkthrough**, over the real socket
  (`dev/testing`): the tool, its args, the answers
  its gates get, and the assertions - `LiveRun(tool, args, answers=
  GateAnswers(draw=..., form_edits=..., require_draw=True))`. The full
  product-path audit, run when a landing is accepted. Three rules make it
  evidence rather than a script: a declared answer is also an
  EXPECTATION (a card that never fired is a failure, not a silent
  pass); the assertions read the run's OWN persisted products off its
  prefix rather than recomputing the answer; and the harness is product
  code beside the server, because drivers are.
- **C - the dock itself.** Plugin-UI coverage only; A and B own the logic.

A-green with B-red isolates a fault to the interaction machinery.

## The workflow skeleton (Template Method)

The base class lives at `workflows/runtime/workflow.py`. An ENGINE's own class -
`TelemacWorkflow` - reads the template module by its own names and turns those
declarations into the plan a run executes, so the plan is assembled once, at
registration, from what the template states and from nothing else.

The mesh ask stands OUTSIDE any one engine's facade: the op router validates
every op against the chosen mesher's own namespaces and the author reads the one
agnostic size word off it, because one mesher's build feeds several engines.

### The stages are the workflow's

A template declares WHAT its question is about; every stage that answers it is
built by the workflow class off those declarations, and a template names no
runner. The class reads:

- THE SLOT ROLES on the DATA rows - the domain, the bed, the runs, the level,
  the discharge - and builds the mesh, the open-channel inflow where a discharge
  carries one, and the settle the water stands on;
- THE PLACEMENTS a declaration names. A `Placed` reads as the `Ref` it is and
  carries what settling a point takes: the point the user gave, the fraction
  along the domain's own centerline where none was, the label its marker is
  published under. Whoever READS a placement states it - a source composite its
  own discharge point, a weather record its station, a chart's anchor its
  monitoring point - and the class settles each onto a node of the accepted
  mesh;
- THE MEASUREMENTS a composite asks for. A `Measured` reads as the `Ref` it is
  and says which measurement it wants; the mesh, the line, the domain and the
  settled run are handed over by the class, and only what the question alone can
  state rides on the value. A dredger asks for its areas and its reference
  surface, an outlet for its rating curve, an incident wave for what the
  accepted harbour mesh measures - and that last one IS the settle, because a
  harbour is settled by the wave that enters it rather than by a level;
- THE LEVERS a row reads. A row asking a dated source for a calendar day gets
  one, read off the `event_time` lever; a run that reads no dated source never
  pays for it.

The template-grammar lint refuses a module path inside a recipe file, so a
template cannot quietly take a stage back.

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
- chart scaffolding, invisible to templates: the HOOK is `Workflow`'s
  (a step's declared `ChartSpec`, its builder, and the persist call in the
  publish stage), while the build/emit/display mechanics live in the
  interpreter with the rest of the plan walk - one home each, no
  re-implementation;
- the emission seam (automatic publish; see Emission unification);
- solve supervision (the shared solver supervisor);
- ledger + resume, provenance, the leak guard;
- the registration factory (see below).

Two slot kinds, distinguished per slot:

- **hooks** - SILENT defaults: charts and validation checks. Unfilled =
  nothing happens; no engine subtype ever restates them. There is no
  sensor/context-LAYER hook: the steps that fetch inputs already emit through
  the one emission seam, so a skeleton-level hook would be a SECOND
  input-emission site - exactly the double emission the single-seam guard exists
  to catch. A test pins that the skeleton emits no input layer of its own.
- **abstract slots** - must-fill: the physics and the four operations the
  engine facade realizes. The library refuses to register a template that
  leaves one empty.

### The slots a run stands on

THE ROW'S NAME IS ITS SLOT. The reserved names are the runtime slot
registry's keys (`trid3nt_server/inputs/slots.py`) - one list - and a row
under one of them plays that role however it is filled: a drawing on the
canvas, the user's own layer or file, or the source the match picked.
Nothing downstream branches on which:

    domain = Data.need("hydrography", at=P.release)   the closed polygon
    bed    = Data.need("bathymetry")                  what every node carries

`domain` is geometry only. The boundary RUNS are no row: zero or more
stretches of the domain's edge, each two points and a type (wall by
default, inflow, outflow, open, rating_curve), they ride on the polygon
the domain's producer cut and the mesh reads them off it. `bed` resolves
to ONE surface - a DEM, a bathymetry or survey raster, a layer of
soundings, or a depth in metres below the free surface - and its own
ingestion grids soundings and lays a measurement covering part of the
domain over the wider surface under it.

Three more slots are declared the same way where a question needs them:
`line`, the polyline a placed read is measured along (unfilled, the
domain's producer answers with the centerline it measured); `extent`,
the lon/lat rectangle a question is asked inside, which is not a domain
because it has no shoreline; and `observation`, one measured value the
run opens on, read by the roles `level` and `discharge` where the
workflow has to know which row is which.

Every slot's ingestion runs wherever the value entered
(`inputs/slots.py`), so what fills it reads the same afterwards.

### The vertical frame

A run counts every elevation it ingests from ONE zero: a runtime lever
beside the compute class, NAVD88 by default, never a row a question
writes. The two ELEVATION slots - the bed under the water and the level
over it - are read on it. A source published on another frame reaches it
through a shift somebody MEASURED, in one order: the shift the source
publishes about itself (a district's project datum is stated in the
survey's own metadata, and no service serves it), else the offset the
RUNTIME declares as a DATA row on `fetch_vertical_datum_offset`, between
the two frames and at a point the OWING SOURCE has data at: the point of
that source's own footprint nearest the question's seed, the seed itself
where it lies inside that footprint, never a box's centre or a polygon's
centroid. One point stands for the whole source, because a datum relation
is a shift and not a field, and the row states WHICH point it was asked
at. That row is journaled like any other producer, and the slot's
coercion reads its value and fetches nothing. A pair nothing measures
refuses naming BOTH frames rather than laying one over the other, and a
footprint the service reaches nowhere refuses naming the point.

### The context row

A producer row whose absence is legal is declared `.context("...")`: the
source is asked, an empty answer continues the run, and the sheet carries
the sentence the template stated about what is not there. A load-bearing
row stays hard and refuses. A producer-less slot says absence with
`.optional()` instead, because there is no source to have been empty.

### Degrading between sources

A row declares no fallback of its own. The MATCH ranks every source that
states coverage for the class here and the probe calls them in that order,
dropping one that held nothing and writing the substitution on the run's
own journal; when none of them answered, an optional row states its
absence and a load-bearing one refuses typed.

### The engine facade

A `Workflow` subclass is the engine's facade: it declares its solver
family and the name of its solve step, and it OWNS the stages a run of
that engine walks. A template states only what differs - its domain
producer, its keywords, its placed reads, its answer - and hands them
over as one declaration the facade builds the plan from.

Facades are named by engine ONLY: `TelemacWorkflow`, `SwmmWorkflow`,
`ModflowWorkflow`. Domain qualifiers are BANNED: a domain is a slot, so
its shape is never a property of the engine that solves over it. The
facade's value is stability - the interface holds while the mechanisms
behind it, meshers and writers and readers, evolve freely.

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

Every template registers through one factory, which synthesizes the
model-facing signature from the declared params so the schema and the run read
the same declaration:

    register_workflow(facade, metadata, sys.modules[__name__],
                      provenance=(...), sensitivity=(...), coerce=(...))

The TEMPLATE MODULE is the third argument, and PARAMS, DATA, ACCEPTS, ANSWER and
DOC are read off its own names; what stays a keyword is what a template writes
inline. The FACADE class comes first - it is what makes the generated tool an
engine's workflow rather than a bare skeleton, and it is checked for must-fill
holes before anything is registered. A template file holds its declarations and
nothing else: no tool body sits beside them.

CONSTANT-DOOR WIRE ENFORCEMENT: a CONSTANT-door param is NOT on the
model-facing wire. The factory
computes the wire set ONCE (`_wire_params`) and both the synthesized
signature and the rendered docstring read it, so the schema and the prose
cannot drift. The door stops being documentation and becomes a BINDING
AUTHORITY contract: a constant is non-question physics, so the model is
not offered it, and a template that decides one deserves model access
re-doors it in a one-line edit (`mesh_resolution_m` per template, where
warranted).

What the exclusion is NOT: a deletion. The row keeps its full life on the
`ParamSheet` - the form card's advanced fold is where a user changes one -
and the generated body takes `**wire` filtered by DECLARED name, so a
value that arrives from a non-model lane is seated through the USER door
with `basis=user`. That is what keeps the Tier-A `!run` all-params
invocation working, and a canary that has to pin a 600 s window instead of
three hours is exactly the case that needs it. The exclusion is about who
the SCHEMA invites, and it invites the user, never the model.

### Rerun-with-overrides - the recalibration interface

**A run derives from a run.** `rerun_workflow(run_id, overrides={...})` is the one
way any question gets asked again with something moved, and it serves three
consumers with one implementation: failure recovery, manual what-if, and
calibration loops (a loop is this primitive driven by a proposer, never a second
re-run path).

The sheet comes from the PARENT, not from the wire - a re-invocation would
re-resolve every door and the two runs would differ in more than the value named.
Overrides seat through the USER door labelled `override of run <parent_id>`,
dependent derivations re-derive, and a row the user pinned keeps precedence.

Reuse is read off the PLAN, by the same `declared_reads` walk the validator and
the binder use (`plan.py` - one definition, three readers). The first node an
override reaches is a CUT: work before it is inherited, work from it on is
re-done. A PREFIX, deliberately - a step also reads the domain the steps before
it bound, and no declaration names that.

Inheritance is the LEDGER, not a copy: the parent's own records are planted under
the child's invocation key (`StepLedger.seed`) and the ordinary resume path
replays them, so the child never asks for the artifacts it reuses. They are the
parent's objects at the parent's URIs.

The completion TOMBSTONE is what keeps a `live-no-cache` tool from becoming a
result cache. A finished run's records are
copied out to a RUN SNAPSHOT keyed by run id (`snapshot.py`), reachable only by a
caller that NAMES that run. A failed attempt is recorded the same way under a
fresh id the error envelope names, which is what makes failure recovery reuse the
work that already succeeded.

A failure NAMES why. Every typed step failure carries a code and a sentence, and
an exception that stringifies to nothing is named by its type (`said`) rather
than reaching the envelope as an empty message; the emitter refuses to mark a
step failed without both, and the snapshot refuses to read `failed` off a step
that names no cause. A run that stops in silence leaves the reader a red card and
nothing to act on, which is the fault the refusal exists to make impossible.

CONSTANT-door params ARE overridable here. The door governs what the MODEL's plan
schema offers; naming a value explicitly, having seen an answer, is the
sanctioned way a fixed quantity moves.

### Coupled validity - rules one Param cannot express

A bound is a statement about ONE value. `Validity(name, reads, holds, message)`
declares a cross-param rule: a predicate over the resolved sheet plus the message
it refuses with, checked at resolve time on BOTH lanes. The library owns the
mechanism (`validity.py`); the engine or template owns the rule, because only
they know what their params mean together. A rule that reads an undeclared param
refuses at REGISTRATION - a guard that can never fire is worse than none.

The reference rule is `friction_coefficient_matches_law` on
`coastal_tidal_surge`: TELEMAC's friction law fixes whether the coefficient is a
Strickler Ks or its reciprocal, a Manning n. It refuses on the CROSSOVER, not on
each law's plausible band - an atypical value the caller means still proceeds; a
value on the wrong side is not atypical, it is the other quantity.

### The no-double-middleware law

Fetcher invocations are DATA. The fetcher router's middleware - cache,
fallback ladders, provenance, staleness, typed refusals - lives ONCE
and is authoritative. No step tier, engine facade, or skeleton stage
re-implements or re-wraps it at another level of abstraction: the
acquire stage INTERPRETS `Data` declarations; it never fetches.

### The emission contract - one page

PRODUCERS DECLARE PRODUCTS; EMISSION PERFORMS THEM; NOTHING ELSE MAY PUBLISH.
That is the whole contract, and the rest of this section is what each clause
costs.

**A producer declares a QUANTITY, and presentation is declared where the DATA
is.** A step that computed a depth field says `flood_depth`; a fetcher carries a
`style:` row in its own `source.yaml`; a solved output derives its row from the
manifest entry's kind, quantity and units. There are no preset NAMES, so there
is no table a quantity can be missing from and nothing to drift between.

**One family, four kinds.** `trid3nt_server/render/presets.py` holds the four
renderer shapes (continuous raster, classed, reference, mesh) and everything a
quantity contributes is a PARAMETER of one of them. It resolves a row plus a
raster into a concrete scale, into the sentence the legend says about it, and
into the `.qml` the map loads. The data-driven rescale LOGIC is code and stays
code (reading band statistics is not a declaration); the POLICY is declared.
`render/publish.py` keeps only the two FILE guards - an embedded palette and
an RGB(A) composite - because those are facts about the file, not about the
style, and each is a way a ramp would corrupt an already-painted image.

**Scale vocabulary, one schema.** `policy` (data | fixed), `range`, `transform`
(linear | log | sqrt | percentile), `clip`. It arrives from the declared row or
from `restyle_layer`'s arguments; the later one overrides the earlier field by
field, every override is labelled, and the data underneath never changes.

**`policy: data` is the default for model output**, because a hardcoded scale
makes an output less informative the moment a run leaves the range somebody
guessed. Two boundaries make it honest rather than merely nicer:

- the SCOPE of "data" is the RUN, never the frame. The range is computed once over
  the whole time axis and every frame and legend uses that one range. Per-frame
  scoping is what made the same colour mean different values in successive
  animation frames;
- a COMPARISON set shares ONE range - before/after, coarse-versus-refined,
  calibration iterations - because those layers are read against each other.

Fixed ranges remain for domain-standard bounded quantities (a probability, a PGA
in g, a temperature in K). LEGENDS ALWAYS STATE WHICH POLICY RAN AND OVER WHAT
RANGE, because the colours cannot.

**Presentation is DISPLAY STATE.** Restyling recomputes nothing and changes no
number, so all of it is available after the fact: `restyle_layer` re-paints,
RENAMES, rescales or HIDES an already-published layer, and takes several layer
ids plus `shared_scale` for an honest comparison. A `title` renames the layer
where the reader meets it: the new name rides the layer row the canvas is
rebuilt from, so the map, the case and the agent call it one thing. `hide=True`
is the un-emit and `hide=False` puts the layer back. It deliberately cannot
CREATE a layer - a uri nothing published is a typed refusal.

**Charts read the same vocabulary.** A chart's axis title and a layer's legend
take their units from one place, so the picture and the map cannot disagree about
what a field is measured in. What the field is CALLED is the layer's own name -
stated once, on the row, never a second time beside the colours.

**Coherence.** A published raster's maximum and the run's headline scalar for the
same quantity agree within a stated tolerance, and the resolved range CONTAINS the
headline - otherwise the caption states the gap. A number in prose that the
picture cannot show is the dishonest case this pins.

### Mesh

Mesh is SUPPLIED-optional `Data`: AUTHORED (user-supplied - e.g. the 2dm
import path - the top ladder rung) or GENERATED (the shared front in
`mesh/`, the default). The default generation policy is
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
`EngineOps.build_mesh(domain, policy, **slots)` is the frozen interface;
generation strategies and writers evolve behind it. The TELEMAC private
corridor mesher folds into the shared front as a generation strategy;
the private ladder dies (ledger row).

