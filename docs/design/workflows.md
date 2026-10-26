# Workflows - a template over one engine module

A template is VALUES over one engine module: one `INPUTS` body, the
module's own keywords, the mesh ask, the placed reads, the outputs and their
captions. Nothing in a template names a function to run. A FILL puts a value
into each input, which ACCEPTS it or REJECTS it naming the remedy; a READY fill
launches on an explicit act, and the engine's run is straight-line code in one order - full
fill, mesh, settle, one review, solve. The skeleton is `workflows/runtime/workflow.py`; the TELEMAC
facade is `workflows/telemac/workflow.py`, the MODFLOW one `workflows/modflow/workflow.py` and the GeoClaw one `workflows/geoclaw/workflow.py`. The
publish is the runtime's (`publish_reads`, `workflows/runtime/primitives.py`): each engine hands it
how a solved module is opened, its module table of what the run wrote, and the run's record.

## No declared data operations

The system performs no resample, reproject, clip, aggregate or unit conversion
because an input or a row DECLARED it. QGIS, GRASS and Python are in the
person's hands, so data that does not fit a slot is REFUSED by name with the
step that makes it fit named as the remedy, and the person or the model runs
that step first and hands the slot the layer it produced. A bed with water
nothing measured refuses naming `merge_rasters` and `fill_nodata`; a layer of
soundings refuses naming the derive that grids it; a place name handed to a
Point or an Extent refuses naming `geocode_location`.

Three things are not operations and stay:

- READING ONTO THE RUN'S OWN FRAME at ingestion - a record onto the engine's
  time step and the unit its slot reads, a surface onto the run's vertical
  frame, an upload onto the store's lon/lat frame, a MODFLOW raster onto the
  sheet's length and time units (a raster publishing no unit refuses). Each is
  the slot's own coercion, never a row a template writes.
- A FETCH reading its source onto the asked window and spacing. The kernel a
  row states is how that source's pixels are read; a grid past the pixel
  budget refuses naming the spacing to re-ask at.
- A MODULE'S CAPABILITY - the mesh's own operations, engine keywords, engine
  actions, forcing composites and outputs - stated by the person or the model
  on any run, whether or not a template uses it.

## The one body

`INPUTS` - one class body of `Input` (`inputs/input.py`), the one declaration a
run is handed anything through. The attribute a row is written under is its
name. An `Input` is a VALUE or a ROW, told apart by what it states:

    spill_duration_s = Input()                      a value on TELEMAC: its
                                                     schema and prose are the
                                                     module input JSON's row -
                                       {"bounds": [1.0, 86400.0], "default": 300.0,
                                        "units": "s", "desc": "Finite pulse injection window"}
    bed       = Input(BED).need("bathymetry")              a class the match fills
    gauge     = Input(LEVEL).need("water level series")    the match fills it, and
                                                           nothing measures it it REFUSES
    rivers    = Input().need("hydrography", geometry="polyline")
    dredge    = Input().need(geometry="polygon")    the user-prompt fetch: the
                                                    person draws it or hands it in
    mesh      = Input(MESH).stated()                the user MAY state one;
                                                    unstated the run's own
                                                    machinery answers it

A VALUE states its default, its bounds and its unit, and a value outside the
bounds refuses by name (`Input.accept`). Where the engine keeps a module input
dictionary, that state and the value's prose are its `inputs` section - one
entry per workflow, named by the workflow's own name beside the engine's
keyword rows - and registration takes them onto the body before the body is
declared (`hydrate`, the engine naming its dictionary through
`Workflow.input_schema`; the STEERING module's own on TELEMAC). A template
therefore writes bare declarations and chains, and the section is the only
place its default, bounds, unit, type and description appear; an engine with no
dictionary takes the fields on the declaration itself, as a test body does. A
value outside its bounds refuses by name wherever it was stated. One with no
default that is not stated-only is REQUIRED: left unstated, the fill refuses it
in every mode, so a world value nothing measures is stated by the caller and
never started from. The model reads the same fact twice off `Input.required`:
the input is listed in the tool schema's `required`, and its docstring line ends
`required`. A ROW is never listed - one left unfilled is matched, drawn or
handed in before it refuses, and the refusal is the honest end: there is no
absence, no authored sentence about what the run did without, and no eager
asking. A row STATES its slot as its type - `Input(BED)`, one of the types in
`inputs/slots.py` - or the producer that fills it
(`Input(tool(...))`; the producer is the runtime's, `workflows/runtime/data.py`)
and is named freely; a row that states neither is a plain one, read by whatever
declared it. A class the stated slot does not read refuses where it is written.
`need(geometry=...)` with no class is the USER-PROMPT FETCH - the shape the
person draws or hands in, absorbing what they supply (`need` with a class is the
catalog match, and a geometry with it is the shape the matched source is read
as). `stated()` is the ONE standing of "the user may state one; unstated the
run's own machinery answers" - the supplied mesh the builder replaces, the
mesher's own edge, the moment a dated source asks for at its own parameter -
and carries no verdict, no refusal and no sentence.

`optional_need()` (after `need`) is the standing of "fill this where a source covers, else go on without
it": a need the match answers with no source leaves the row unfilled (no verdict against the run; the
probe's own journal line, "nothing measures ... here", says its absence once) instead of rejecting it, and
the user can still state it. A source that matched and then failed is not an absence and still refuses.
`optional_need(instead_of="row")` is the either-or: the row stands in for another. It is not read when the
run states that other row (a supplied value or a picked source), and the other row is not asked for when
the stand-in fills, each said once in the journal - so the composite that reads both never sees both.

A stated-only input may also be the one a PART of the template is composed on,
and the part is composed only when the input is stated - there is no
conditional primitive, only the seams the parts already ride. A coupled body
whose `given` inputs all hold nothing states nothing (an unstated decaying
substance leaves the dye conservative); a world measurement (`Measured`) whose
`value` input holds nothing is not taken, and what is set on it is not composed
either - the mesh op whose `geometry` names it, the profile read `along` it (an
unstated structure leaves the harbour free-field: no obstacle, no reflecting
face, no transect). A value whose meaning is the part's - a structure's
reflection coefficient - is stated with that part and only with it, and the
author that reads both refuses either alone by name. Composing a part from an
ad hoc script that edits the composition, beyond the parts a template declares,
is a stated requirement no seam carries yet.

A POINT IS NEVER INVENTED AND NEVER MOVED. A point is stated, picked on the canvas, or
supplied by data (a matched gauge, a measured site); nothing derives one - no centroid,
no box centre, no fixed fraction along a reach. A placed point (`Placed`) whose input
holds nothing places nothing, and nothing read at it is composed. Before an engine uses
a stated point, its placement calls the one shared check (`inputs/point.py`
`require_viable`) with the engine's own reading of where the point lands - TELEMAC's
nearest mesh node, which must hold water at t0 off the rim, inside the domain polygon;
MODFLOW's containing cell, which must be one the run solves (`DISV_IDOMAIN` not zero) -
and a point that misses refuses `POINT_NOT_VIABLE`, naming what it missed and carrying
the nearest viable site as a hint. The point itself is never relocated: the deck states
it where it was stated. A pure seed - a point read only to locate a fetched domain - is
`stated()`: a supplied domain never reads it, and a domain the match must locate with
no seed refuses `DATA_NEED_UNPLACED` naming what to pick or draw.

TWO READS OF A POINT CARRY DIFFERENT WEIGHT. A SOURCE point - a release, an outfall -
changes the physics, so it goes through the check above and refuses where it misses. A
PROBE is a read AFTER the run: it never changes the solve, it is never a template
input, and a click replaces it - the dock's Probe click has QGIS read every visible
mesh layer's painted group at the point over every saved time, as the map draws it,
and charts the series; a point inside no face reads "outside the mesh". Within a run,
a station is the DATA'S SITE: a template's grounded series is read where the record it
compares against was measured (the matched gauge's own `lon`/`lat`), never at a stated
override and never at the site whose record forced the run. There the check is
containment, not viability - a site the mesh or grid does not hold composes no series
and the journal says so with its site and distance; an observation stated as a number,
which stands at no site, composes nothing and the journal says that; neither refuses
the run. A wave run's station is matched as its own OBSERVE row, one buoy nearest the
window and never the boundary's own; when the only buoy the row matches is the one the
sea state came from, the observation is taken out of the run's reads before the sheet
fills or any output answers at it, and the journal says there is no independent check.

AN OBSERVE ROW IS READ AT AN INSTANT AND MAY TAKE A SET. `need(..., when="snapshot")`
names the run input holding the instant the record is asked and read at, apart from the
moment the run opens at; unstated, the row reads at the run's moment as before, and a
stated instant spans no forcing window. An unparseable instant refuses by its input's
name. The reading keeps the instant it was asked at, and a check reads the saved time
the reading's own window holds (thirty days back, a day ahead), nearest its sample; with
no saved time in that window it reads the nearest saved time and the journal names it
and how many days away it is - never the nearest silently.
`need(..., every=True)` takes every site the window holds rather than the nearest, and
a read placed at the set is each site through the engine's own one-site read: a site
the mesh does not hold is left out and journalled, never moved, and the checks publish
as ONE comparison chart (measured across, modelled up, the line of agreement) through
the chart format. A read that checks nothing at a site has no comparison and refuses.
`need(..., measured="solute")` names the run input stating WHAT is measured in the
source's own word ("Boron"): the source is asked for that word in place of its
vocabulary's word for `of`, and a row that cannot be told it (no `need:of` ask) leaves
the match. `of` still names the published variable and its unit.

Values lead the wire and rows follow, each in body order, and the generated
docstring documents both in ONE section: required values, defaulted ones, then
the stated-only ones and the rows.

Prose has exactly one home each and the template .py carries none of it: how
the question is found - summary, routing, what it is not for, what it returns -
is the package's `routing.yaml`, read at registration and refused when absent
or short a required key, and its front block must fit the 1000-character
truncation budget every registered template is measured against; a value's
description, unit and range are the module input JSON's, hydrated onto the
declaration; the run controls' one description each is the framework's, beside
the wire signature they ride on (`_CONTROL_DOCS`); a ROW may state its own
words beside the slot it needs (`Input(EXTENT, desc=...)`) - the template's
judgment about this use, read in place of the slot's generic description. The
template-grammar lint refuses a DOC name, a controls= argument and a desc= on a
value input.

The runtime declares no input of its own. A template carries, without restating
them, the inputs whoever reads them declares (`Workflow.seated`):
`mesh_resolution_m`, `mesh_grade` and `mesh_max_edge_m` are the mesh ask's
(`RESOLUTION`, `GRADE`, `MAX_EDGE` in `tools/mesh/recipe.py`), each seated only
where the ask names it, and the two sizing inputs carry no default - a template
that names one restates it with its own number or the run asks for it; `event_time` is the engine's - the workflow names it as its `moment` and the fill reads the moment under that name - read through the
engine's own coercion, and each module writes it into its deck in its own date
keywords, in UTC, with the meridian and latitude a solar clock reads taken off
the domain (`MOMENT` in a module's `FILLED_BY`; a stated keyword wins, no moment
writes none). The ice module tests its solar hour unfolded, so a dated ice run
stages the engine's thermal module with that hour folded (`authoring/solar.py`,
`Module.CODE_ON_HOST`), compiled as the host's user Fortran; `vertical_frame`
is the bed's, an `Input` stated on its type (`Slot.states`). A template's own
input of the same name wins and keeps its place. How many cores a solve is
partitioned across is no input of the body at all: it is the module's own
PARALLEL PROCESSORS keyword, stated on the call like any other, the carrier's
on a coupled run, and a count past the box's cores refuses by name.

## One lookup, one fill, one record

`Workflow.input(name)` is the ONE lookup: the template's own input, then one
the engine or a row's type declares, then - on an engine - the keyword the name
spells on whichever of the run's bodies carries it. A keyword keeps its
dictionary-backed row and is answered as an `Input` is (`Keyword`,
`telemac/modules/sheet.py`: its name, the one range that bounds it, the
module's accept rule). The `keywords={...}` container stays on the wire and the
two doors keep their own sets: BESIDE it a name is a declared input, INSIDE it
an engine keyword or composite. A keyword stated beside it refuses naming the
container, and a declared input stated inside it refuses naming where it
belongs.

ONE floor routes every name inside it across the run's bodies
(`keyword_floor`, `runtime/module.py`; each engine's `_floor` hands it its
`run_bodies(steering)`): a qualifier names its body alone, a bare name resolves
where exactly one body spells it and two refuse naming both, the partition
keyword and the carrier's `CARRIER_OWNED` family - a clock or grid the whole
run shares - resolve as the carrier's wherever they are spelled, and an unknown
module word refuses naming the bodies this run runs. On MODFLOW a qualifier may
also ADD the body it names where the carrier couples it (`gwt`/`gwe`: the run's
qualified names join `run_bodies`, and the carrier's composite creates the deck
the template did not state); on TELEMAC a body's presence is the template's
`coupling` declaration alone, and a qualifier naming outside it refuses. The floor returns the
carrier's values by identifier beside each coupled module's: the carrier's fill
last over the template, and a coupled module's compose through the carrier's
own coupling composite and merge onto the template's statement of that deck -
one run statement beats the template's value and never replaces the template's
statement of the body.

The fill (`workflows/runtime/fill.py`) answers every input in one loop and
leaves one `Verdict` each: the state, the value taken, and where it came from -
the origin (user, template, producer, derived; `Origin`, in the contracts) and
the source or producer that names it. A row is fetched or read and ingested
before it answers, and a row first read by a stage of the launch is stamped the
same way. A row handed a case layer by its handle - bare or as `{"layer": id}` -
is read at that layer's uri, and nothing judges a workflow's input before the
fill does: a direct run reaches it with its args as typed.

The fill produces every row a stage reads without the mesh (`named_rows(held)`: the domain, the named rows, the mesh ask's rows unless a mesh was supplied, the level, the inflow, a measurement's line, the rows a module fills from), and only a row asked near a placed point waits for the settle. `Workflow.judged` answers before any unstated row is fetched: `NO_SESSION` for an engine run with no session and, on TELEMAC, `TELEMAC_CONTINUATION_REFUSED` and `TELEMAC_SHEET_INCOMPLETE` judged from the deck's assertions and the stated keywords. The sheet stage writes the deck and runs the staged check before every card it draws, so a proceed is followed by the box alone. The run holds one tag from its first staging to dispatch: a card edit re-stages into the same local directory and the same prefix, each emptied first, and a run that does not reach the box (the review cancelled, the staged check or completeness refusing, a raise before dispatch) removes both.

A row resolves in ONE order (pinned by `tests/runtime/test_resolution_order.py`):
what the caller supplied, then the row's one source - its producer, or the
match its need asks; a row declares one or neither - then, for a row that
declares neither, the canvas, and nothing answering refuses by name
(`DATA_SLOT_UNSATISFIED`). A need nothing measures refuses
(`DATA_NEED_UNMATCHED`) and is never drawn instead. A stated-only row nothing
supplied stops after the first rung with no verdict. The keyword sheet has its
own three tiers: the engine's dictionary default, under the template's
statement, under the fill's.

An argument that is no declared input is refused by name
(`INPUT_UNNAMED`, with the nearest declared names), and a name inside the
container the module has no keyword for refuses naming the nearest spelling
(`KEYWORD_REFUSED`); the generated signature's `**` is how it reaches the fill.
What an accept rule has to say of the run - the engine's own check being out of
reach at the fill - rides a run that launches and never a failure's text.

Everything the run holds is keyed by NAME. A module that fills keywords from a
row reads it by the name of the row's type (`FILLED_BY = {"wave": ...}`), so no
other input of a template takes a type's name.

The run journal records ONE map, `inputs`: every keyword of the solved deck and
every input the fill answered, by name, each with its `value`, its `origin` and
its `source`; a keyword stated on a coupled body keeps the name it was stated
under beside the carrier's own. A supplied row's value is what the caller handed in; a matched
one holds none and names the source that answered. A stated-only input nobody
stated holds no verdict at all: it is not in the map. The map is the line's
only record of an input.

A run solves over exactly one `DOMAIN` row and one `BED` row; a record type
(`LEVEL`, `OBSERVE`) may be stated more than once, and a stage that reads one
by type reads the first. The `MESH` row is the engine's: every template of the
engine carries `mesh` without writing it, a supplied mesh is held to the kind
the engine states once and becomes the run's mesh in place of the build. Its
ingestion refuses one with no bed painted on it (`MESH_BED_UNPAINTED`) and one
whose own extent shares no ground with the run's domain
(`SUPPLIED_COVERAGE_MISMATCH`); the launch journals one note naming each input
the caller stated that only the mesh build reads, since it went unused.
A row's value that is a mapping naming no `layer` and no `source` is the value
itself - a drawn geometry is taken whole.

`WEATHER`, `RAIN` and `WAVE` are ingested as WHAT WAS MEASURED, in words no
engine owns. The weather is one station - the nearest whose record brackets the
run's window, ranked from the point the row names or else the middle of the
domain - with a series per quantity its network reports and the reason beside
any it is short of; the rain is the depth per interval at the record's own
cadence - the median gap between its stamps - and a gap of one and a half
cadences or more is a hole the slot refuses, naming the stamps either side; the
sea state is a height, a period and the bearing the waves come
from. The engine keeps only its reading of them: which quantities its modules
read, the mnemonic or keyword each becomes, the one clock its table is written
on, and the refusal by name of a column it reads that the station is short of.

`.need` names a CLASS of the coverage vocabulary, never a fetcher; `geometry=`
is which SHAPE of that class is asked for, a filter on the match, never a
conversion. A slot whose ingestion reads one shape states it once (`reads=`):
the weather reads station points and the rain a record, so a gridded product
serving the same class is excluded before it is fetched. The match (`tools/search/match.py`) filters the rows on
class and place, holds a series source to the run's window, ranks on the cell
against the mesh, on recency and on the native datum, and calls the survivors
in rank order. The window comes off `event_time`, which the engine declares,
and the frame off `vertical_frame`, which the bed declares, so a template states
neither.

`of=` is WHAT of the class a row asks for, and it is read one of two ways. Where
some row of the class states that word as the quantity its source publishes
(`normalize.quantity` in the fetcher's `source.yaml`, read where the row is
loaded), the match keeps only the rows publishing it: the three CONUS aquifer
rasters share "aquifer property", so `need("aquifer property",
of="aquifer_transmissivity")` takes the transmissivity grid and drops the
thickness and the recharge by name. Anywhere else `of` is a COLUMN word - the
published variable a record carries (`of="TEMPERATURE"` on a water quality
sample) or the feature a hydrography source publishes - and a row with no word
for it in its `vocabulary` leaves the list. A word neither reading finds refuses
`DATA_NEED_UNMATCHED` naming it.

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

Unit conversion rides an EXPLICIT table in `inputs/temporal.py`, not
a units engine: a conversion nobody declared is one nobody can check, and a
cross-dimension request refuses rather than guessing.

Four callers, and no fifth: the atmosphere composite holding each column of a
station's record to the gap one day's cycle survives, the observation coercion reading a gauge in the unit its
slot reads, the model-vs-observation pairing reading the model AT the moment the
gauge reported, and the liquid-boundaries author writing a window at the
engine's own time step.


## The vertical frame

A run counts every elevation it ingests from ONE zero: `vertical_frame`,
which the bed's type declares beside its row, NAVD88 by default, never a
row a question writes; the level and every matched need read the bed's. The two ELEVATION slots - the bed under the water and the level
over it - are read on it. A source published on another frame reaches it
through a shift somebody MEASURED, in one order: the shift the source
publishes about itself (a district's project datum is stated in the
survey's own metadata, and no service serves it), else the offset the
RUNTIME declares as a row on `fetch_vertical_datum_offset`, between
the two frames and at a point the OWING SOURCE has data at: the point of
that source's own footprint nearest the question's seed, the seed itself
where it lies inside that footprint, never a box's centre or a polygon's
centroid. One point stands for the whole source, because a datum relation
is a shift and not a field, and the row states WHICH point it was asked
at. That row is journaled like any other producer, and the slot's
coercion reads its value and fetches nothing. A pair nothing measures
refuses naming BOTH frames rather than laying one over the other, and a
footprint the service reaches nowhere refuses naming the point. A
refusal that is coverage - no region stands over the point, VDatum's
grids leave a gap, VDatum refuses the pairing - also names the frame
pairs the local grids convert AT that point, so a run can state a
frame whose offset is measured; nothing states it for the run, and a
template names no fallback frame order.

## Degrading between sources

A row declares no fallback of its own. The MATCH ranks every source that
states coverage for the class here and the probe calls them in that order,
dropping one that held nothing and writing the substitution on the run's
own journal; when none of them answered, the row refuses typed.

## Continuing a run - an input, not a derivation

A run that carries another on NAMES the file it picks up from, as a keyword on the module whose engine restarts: TELEMAC's
`PREVIOUS_COMPUTATION_FILE` holds the URI of the previous run's result file, GeoClaw's `CLAW_RESTART_FILE` the URI of one
checkpoint file (`fort.chkNNNNN`, `fort.chkaaaaa` or `fort.chkbbbbb`) in the previous run's folder of the runs bucket. The
workflow takes the keyword off the floor, stages the file in the run directory and states the engine's own keywords with the
staged name; the journal says which file was continued. Changing a value and running again is simply a new call with that value.

GeoClaw restarts only from a checkpoint a run wrote because it stated `CLAW_CHECKPT_STYLE` (the worker then uploads
`fort.chk*`, `fort.tck*` and the `.data` set beside the products). The named run must be a GeoClaw run (its folder holds
`geoclaw_metrics.json`) that ended at the checkpoint's instant, since the engine appends to the gauge records and the fixed-grid
records, which are staged with the checkpoint; the new set is held to the previous run's wherever the engine restores from the
checkpoint (grid, ratios, clock, bed, seafloor motion, constants, storm) and the final time, output times and checkpoints may
change. Each refusal is typed (`GEOCLAW_RESTART_*`). The fixed-grid maxima are restored from the checkpoint, so a continued run's
`fgmax` covers the whole history. A restart prints no initial-mass line, so the worker demands one only of a run whose claw.data
states `restart` false.

A failure NAMES why. Every typed step failure carries a code and a sentence, and
an exception that stringifies to nothing is named by its type (`said`) rather
than reaching the envelope as an empty message; the emitter refuses to mark a
step failed without both. A run that stops in silence leaves the reader a red
card and nothing to act on, which is the fault the refusal exists to make
impossible.

## The emission contract - one page

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

## MODFLOW - a sheet over the accepted mesh

The engine's own page - the simulation, the three module wrappers, the
coupling, the publish transform and the worker - is `docs/design/modflow.md`;
this section is the workflows-runtime view.

A MODFLOW run is the GWF module's sheet solved on a mesh the shared front built
or the caller supplied (`workflows/modflow/workflow.py`). `launch` runs one
order: the mesh, the sheet filled under its one review - each fill writes the
simulation, runs mf6's own validate over it and stages it with the staged check
(`authoring/staged_check.py`) into the run's one tag - the solve on the box, and
the publish.

THE DISV IS THE ACCEPTED MESH'S. The vertices and cells are the mesh's own
display face; the sheet states the layering (`DISV_NLAY`, `DISV_TOP`,
`DISV_BOTM`) and no sheet may state the mesh's tables. An unstated top is the
mean of each cell's painted bed, and a cell touching a node the bed does not
paint refuses. The file is written once per mesh and layering and staged beside
the files that name it (`authoring/mesh_files.py`).

A RASTER IS READ ONTO THE MODEL'S FRAME at the sampler (`authoring/griddata.py`):
each cell takes the raster's value at its centre, and the unit the raster
publishes (`LayerURI.units`, which the fetcher's `normalize.units` stamps) is
converted through the declared unit table onto `DISV_LENGTH_UNITS` and
`TDIS_TIME_UNITS` by the dimension the keyword or stress column states. A
raster publishing no unit, a frame stating none, a unit of another dimension and
a cell the raster measures nothing at each refuse by name. A stated number is
already in the model's units and holds at every cell.

THE AQUIFER IS A COERCION of the GWF wrapper (`modules/gwf.py`):
`Aquifer(thickness | saturated_thickness, water_table_depth, conductivity |
transmissivity [, layers])` states an aquifer as cell expressions against the
accepted mesh - the bottom a thickness below the top, or a saturated thickness
below the water table (the published saturated thickness runs down from the
water table, so it is never read from the land surface), the starting head a
water-table depth below the top, the conductivity stated or the transmissivity
over the thickness - every argument required, exactly one of each pair, and a
thickness at or under zero refused. `layers` (an integer from 1, unstated one)
splits the saturated thickness under the water table into that many equal layers: K is the same in
each, only layer 1 is convertible (unconfined) and layers below it are
confined. The stress composites
(`Recharge`, `Drains`, `GeneralHead`, `ConstantHead`, `Wells`) place a geometry
on the cells (`authoring/stress.py`) and state their package's period data.

THE PUBLISH is the runtime's `publish_reads` over the engine's own reading of a
solved run (`modules/outputs.py`): every output the output control saved is one
temporal mesh layer per model layer, each a dataset group of its own CF-UGRID
file of the accepted mesh valued per cell (`modules/ugrid.py`), timed in seconds
since the run's reference instant. A series at a measured well is checked
against the reading in the reading's own quantity, in the model's length unit,
at the saved time nearest the moment it was sampled: a depth below the land
surface against the cell's top less its head, an elevation against the head on
the run's own vertical frame (`vertical_frame`, carried on the run's handle). A
reading on another frame, of another quantity, or stated as a number standing at
no site refuses by name.

A COUPLED RUN's fill composes the transport body through the GWF wrapper's
`Transport` composite (`modules/gwf.py`), and the staged run's manifest takes
the coupled body's module word as its `module` - transport answers, flow is its
machinery. The publish walks both models' output rows: the carrier's read off
the run's own module, a coupled body's tagged with its own word, each body
reading its filerecord from its own slots over the carrier's sheet (the clock
and units the simulation owns are one for both models). The engine page
(`docs/design/modflow.md`) holds the coupling itself.

## Mesh

The mesh is a stated() row: AUTHORED (the person's own mesh, which the builder
replaces when unstated) or
GENERATED by the shared front in `tools/mesh/`, which knows no engine. The mesh
ask carries the mesher, the extent, the resolution and the mesh's own
operations (`mesh_op`), validated against the chosen mesher's namespaces. A
mesh of another species (a structured grid, a node-link network) is a typed
refusal, never a silent conversion.

GeoClaw has no mesh; its grid is a RECIPE on the same front. The `claw_grid`
mesher (`tools/mesh/meshers/claw_grid.py`) takes the lon/lat level-1 box, the
level-1 cell edge along a meridian as `resolution_m` (fitted to the box as a whole
number of cells, and said so), the integer `refinement_ratios` as a build
parameter (one list for x, y and t), and `flagregion` ops, each one Clawpack
FlagRegion over a lon/lat rectangle. It builds the level-1 lattice; its finest
cell (metres and arc-seconds) and the cells each level is forced and allowed
by the flagregions ride the one mesh gate card as read-only rows, with a wall-time
estimate per simulated hour that states its measured basis; the sheet card states the
run's total (that rate times the stated `CLAW_TFINAL`, redrawn when either is edited) as a
read-only row off the same probe fact; no budget is enforced, the resolution being the
user's lever. Refinement is stated as flagregions only:
old-style `REGION_REGIONS` refuses by name pointing at `FLAGREGION_FLAGREGIONS`. A
flagregion level above the levels the ratios give refuses by name. Authoring
(`workflows/geoclaw/authoring/grid.py`) lands the accepted grid on `CLAW_LOWER`,
`CLAW_UPPER`, `CLAW_NUM_CELLS`, `AMR_AMR_LEVELS_MAX`, `AMR_REFINEMENT_RATIOS_X/Y/T`
and `FLAGREGION_FLAGREGIONS`, and a template stating any of them beside the recipe
refuses by name. A flagregion op that states `with_rupture` ends at the run's stated
rupture when that is later than its own `t2` (a static rupture's last dtopo instant, a kinematic
one's largest rupture_time + rise_time), computed once the finite-fault record is fetched; a run
that restates the op without it keeps its window, and a run stating no rupture keeps `t2`. The
template states `RISE_SHAPE` ('quadratic'), which a kinematic Fault or FiniteFault naming no
`rise_shape` takes. A topofile header is checked against the level-1 box by the
engine's own coverage rule (topo node extent against the domain's area), not an
alignment rule: the engine integrates topo over each cell and stops only when the
topo does not cover the domain - when the overlap differs from the domain's area by
more than 1 percent of it. The check refuses exactly where the engine would stop, never
more strictly. The level-1 cell is stated in metres along a meridian and fitted to a
whole number of cells over the box, the card saying when the fit moved it; one
refinement-ratio list applies to x, y and t.

The engine's exit verdict (`classify_exit`) reads the worker's metrics: a run whose
`Total mass at initial time` is zero (no wet cell) refuses first, naming it; any other
mass is reported in the journal and on the record for the user to read, since a small
domain is a legitimate run.

The bed is the engine's own list of topofiles, coarse to fine. The `Topography`
composite on the wrapper takes the surfaces in that order, each a bed (a raster or
a case layer, with the measured `offset` onto the run's `vertical_frame` where its
zero differs; a pair nothing measures refuses by name), and states one
`TOPO_TOPOFILES` row per surface. Authoring (`workflows/geoclaw/authoring/topo.py`)
writes each as topotype 3, centre-registered, in EPSG:4326 with Clawpack's own
`Topography.write`; a projected surface is reprojected and trimmed of the corner
wedges the reprojection leaves unmeasured, and a node nothing measured refuses.
Nothing is merged or filled: GeoClaw takes the finest file at each point, and the
coverage rule above is applied to the UNION of the files. `TOPO_TOPOFILES` stated
beside the composite refuses by name.

The rupture is the `Fault` composite: `Fault({subfaults, x, y, times})` states every
value of each subfault under Clawpack's own `SubFault` names (location, depth, strike,
dip, rake, slip, length, width, `coordinate_specification`) and the lon/lat grid the
Okada displacement is summed on. A value left out refuses by name; nothing is scaled
from a magnitude or defaulted, and the magnitude is Clawpack's output, logged with the
rigidity it used. Authoring (`workflows/geoclaw/authoring/dtopo.py`) writes one
topotype-3 dtopo with `DTopography.write` and states one `DTOPO_DTOPOFILES` row. Every
subfault's surface projection must lie in the level-1 box, and where the run states
topofiles at least one grid node under the projection must stand on water (finest
file at the node, below `GEO_SEA_LEVEL`); a rupture over land or outside the box refuses
before the solve, naming the subfault. `DTOPO_DTOPOFILES` stated beside the composite
refuses by name.

What a solved GeoClaw run publishes is its fixed grids, never the AMR frames: the
fgmax grid (`FGMAX_FGMAX_GRIDS`, a uniform grid whose geometry the record's `points`
states under `FGmaxGrid`'s own names `x1, y1, x2, y2, nx, ny`), the fgout grid
(`FGOUT_FGOUT_GRIDS`) and the gauges (`GAUGE_GAUGES`). `GEO.MODULE_OUTPUT` rows the maximum
depth, speed and surface elevation and the arrival time (`max_over_time`), the depth,
surface elevation and overland depth per frame (`field`), and a gauge's surface elevation
and depth (`series`); the reads (`modules/outputs.py`) go through Clawpack's own
`FGmaxGrid.read_output`, `FGoutGrid.read_frame` and `GaugeSolution`, and return each raster
as a `Rasters` read (north-up, cell edges from the grid's nodes, nodata NaN); `deliver`
writes the COGs under the run prefix and a `Raster` carries them: an fgmax product is one
untimed layer, the fgout frames one layer per frame, each stamped with its window
(`valid_from` to the next frame's instant, the last to the run's `CLAW_TFINAL`; an instant
is the run's reference time plus the frame's seconds; a run states its own `event_time`, else its
reference time is the solve's start, said on the record as defaulted, and only a record carrying
neither refuses `RASTER_UNANCHORED`), which the plugin's Temporal Controller plays as it does a fetcher's
frames. A read of an output whose keyword the run did not state, a speed with
`FGMAX_NUM_FGMAX_VAL` below 2, a grid that is not lon/lat (`GEO_COORDINATE_SYSTEM` not 2)
or a file the run did not write refuses by name. Depth is TOTAL depth, drawn where the
engine calls the cell wet (`GEO_DRY_TOLERANCE`, read off the run); the maximum surface is
Clawpack's own `h + B` at the fgmax point; the arrival time keeps Clawpack's nodata where
no wave was flagged. The overland rows (`OVERLAND_DEPTH`, `MAX_OVERLAND_DEPTH`) also drop
the cells wet in fgout frame 0, which must be written at t = 0 - never the cells below
zero elevation; the fgmax one needs the fgout nodes on the fgmax nodes and refuses
otherwise. A series is read at the run's one gauge, a gauge number, or a Point standing on
a gauge (a Point off every gauge refuses with the nearest). A run stating more than one
grid of a kind has no one grid to read and refuses. A template's own `OUTPUTS` and `CAPTIONS` publish beside
the module table through the same `publish_outputs` shape as MODFLOW's: a placed read is a
`series` at a gauge, a `field` over the fgout grid or a `max_over_time` over the fgmax grid.

### The GeoClaw template: `geoclaw_tsunami_inundation`

The first GeoClaw template (`workflows/geoclaw/templates/tsunami_inundation/`) takes R. J. LeVeque's Copalis Beach tutorial
(`CopalisBeach/example2/setrun.py`) as its values. The caller states the places: `extent` (the level-1 box), `source` and
`approach` (the rupture's sea and the sea to the coast), `domain` (the coast) and `seed` (a point on the water), plus
`event_time`, the label of the rupture's t = 0 - a scenario has no date, the solve's start is never one, and a run that
writes fgout frames without it refuses before the solve. The grid recipe is the tutorial's seven levels (ratios
`[2,5,2,2,2,3]`) with its three regions over the stated boxes and one more holding the `domain` at the finest level from t = 0,
so fgout frame 0 is the initial sea at the resolution the overland products are read at. The `FixedGrids` composite puts the
fgmax and fgout grids on the finest cells that cover the `domain`, on the same nodes.

The bed is the fetched finest surface at the `domain` (the BED row) over a deep rung over the whole grid (ETOPO 2022, or
`deep_bed`), two topofiles, nothing merged; the deep rung's zero reaches the run's frame by the offset the workflow asks at the seed (`fetch_vertical_datum_offset`:
PROJ's local geoid grids for NAVD88, NGVD 29 and EGM2008 over CONUS and Alaska, VDatum elsewhere and for every tidal frame). `GEO_SEA_LEVEL` is the offset of the template's tidal datum (`SEA_LEVEL_DATUM`, MHW, as in the tutorial) on that
frame, measured by the same row at the seed and never moved, unless the run states it. The rupture is the `SiftFault`
composite: NOAA PMEL SIFT unit sources (`fetch_sift_unit_sources`) slipped by the tutorial's ASCE_SIFT_Region2 slips, each a
Fault subfault, the dtopo grid being the one Clawpack's `create_dtopo_xy` lays (10 arcsec, 1 degree margin); no slip anywhere is
no rupture. `FiniteFault` is the rupture of a real earthquake: the record `fetch_finite_fault` reads from the event's USGS
finite-fault product (ComCat names the `complete_inversion.fsp`; the parse takes segment strike and dip, Dx and Dz and the
row count from the file's own headers) becomes one Fault subfault per row, centred (`centroid`), at the inversion's slip with
no rigidity or scaling. It refuses beside any other rupture (`Fault`, `SiftFault`, the keyword). A rupture is static (one
final dtopo, Clawpack's two-instant limit) unless the statement says `rupture_type: 'kinematic'` (Clawpack's own word, on
`Fault` and `FiniteFault`): each subfault then carries `rupture_time` and `rise_time` (a `FiniteFault` takes the record's
TRUP and RISE), the statement states `rise_shape` ('linear' or 'quadratic', the two Clawpack's `rise_fraction` offers; the
FSP's asymmetric cosine is neither, so none is defaulted and an unstated or unknown one refuses), and Clawpack writes the
dtopo at the instants `times` lists, each subfault's share of its slip at that instant. A `FiniteFault` may state `times` as
one interval in seconds, the instants then running from 0 to the first multiple at or past the largest TRUP + RISE; a `Fault`
whose last instant precedes the rupture's end refuses (`GEOCLAW_FAULT_KINEMATIC_UNFINISHED`), since the engine holds the last
frame. A `FiniteFault` stating `event_id` (and a `finite_fault` name) has the workflow fetch the record as a declared row, the
way it fetches SIFT unit sources; a record the run already holds under that name wins and nothing is fetched. `SlabFault` is a rupture the user states on the real subduction interface: the record `fetch_slab_geometry` reads from USGS Slab2 (one tarball downloaded once through the cache; depth, strike and dip per zone, positive-down depth) and a rectangle - an anchor point (top-edge midpoint or centre), `length` along strike, `width` down dip, the subfault size, `rake` and `slip` - none defaulted, the lengths required to divide into whole subfaults. Subfaults are walked along the interface in half-subfault steps and each centre reads depth, strike and dip from the slab at that centre, so a planar subfault is an approximation of a curved surface; a place Slab2 masks or two zones hold refuses (the nearest node named, the zone to state). It is static, and refuses beside any other rupture. `MomentTensorFault` is a magnitude-only source: the record `fetch_moment_tensor` reads (every ComCat moment-tensor product of the event, none preferred; the centroid is the product's derived origin) and four picks the user states - the solution by code, the nodal plane (1 or 2), the published scaling relation by name and the placement (the published centroid, or the depth `fetch_slab_geometry` reads at that centroid). The relation is a pure helper (`workflows/geoclaw/helpers/scaling.py`), one paper table each, cited by table and page; only Wells and Coppersmith 1994 Table 2A (subsurface length and downdip width, by slip type) is built, since the Strasser 2010 and Blaser 2010 papers were not obtainable. Length and width come from Mw at the scalar moment; a magnitude outside the regression's fitted range refuses. The one subfault carries the plane's strike, dip and rake and slip M0 / (mu L W) with Clawpack's SubFault rigidity. It is static and refuses beside any other rupture. `InitialSurface` opens the sea at a stated raster instead (a qinit file of type 4). The workflow stages only the
case section the worker reads (`stage_run(case=)`), publishes the module table through `publish_reads`, and its record is the
outline of the fixed grids. The verification twin (`geoclaw_tsunami_zero_fetch`) runs Thacker's oscillating paraboloid, stated
and synthetic, against its analytic solution; `geoclaw_tsunami_continued` runs it in two legs, the second restarted from the first's checkpoint.

### The GeoClaw storm-surge template: `geoclaw_storm_surge`

The second GeoClaw template (`workflows/geoclaw/templates/storm_surge/`) takes Clawpack's `storm-surge/ike` setrun for its
values and a real storm for its forcing. `Storm` is a composite beside the faults: it reads the points `fetch_storm_tracks`
returns for ONE storm (several refuse naming them) and writes the fixes spanning CLAW_T0 to CLAW_TFINAL as GeoClaw's storm
file through Clawpack's `Storm` writer, in SI units (`workflows/geoclaw/authoring/storm.py`), setting `SURGE_STORM_FILE`. The
wind model, drag law and refinement are the body's own keywords (`SURGE_STORM_SPECIFICATION_TYPE` takes any model the engine
reads a track for, by its integer); a kept fix without the radius of maximum wind or the outer radius refuses naming the fix
and the IBTrACS field, a track that begins after the run or ends before it refuses, and Clawpack's fill never fires. The
workflow fetches the track (`storm_name` and `season` over the level-1 box) and, with no stated `event_time`, counts the run
from the storm's last landfall fix (`USA_RECORD` L). The run opens three days before landfall, so FixedGrids and the overland
mask start at CLAW_T0, and the seed gauge listens over the whole run. Friction is the example's domain-wide Manning law as
`GEO_MANNING_*`; the sea stands at the MHW offset NOAA VDatum measures at the seed. Outputs are the fixed grids and gauges.

### The GeoClaw Boussinesq module: `geoclaw_bouss`

GeoClaw has two modules. `geoclaw` is the shallow-water body; `geoclaw_bouss` is the same rundata plus Clawpack's `BoussData` group
(`bouss.data`: `BOUSS_BOUSS_EQUATIONS` 0 SWE, 1 Madsen-Sorensen, 2 SGN; `_MIN_LEVEL`, `_MAX_LEVEL`, `_MIN_DEPTH`, `_SOLVER` 1 GMRES,
2 Pardiso, 3 PETSc; `_TSTART`), its dictionary extracted by the same extractor, its composites, outputs and reads the shallow-water
body's. The worker image carries a second executable beside `xgeoclaw`: `xgeoclaw_bouss`, compiled once at build from
`examples/bouss/radial_flat`'s own Makefile against PETSc (built from its release tarball, 3.23 or later is what Clawpack 5.14
needs) and MPICH, with GeoClaw's standard `qinit.f90` where radial_flat carries its own Gaussian hump. A run states
`module: geoclaw_bouss` on any GeoClaw template's values (the template's STEERING body is rebased onto the module's wrapper, same
assertions) and `cores`, the MPI process count (unstated 1, at most this box's cores; shallow-water runs take none); the staged
case names the module and the worker runs `xgeoclaw`, or `mpiexec -n <cores> xgeoclaw_bouss` with Clawpack's own
`petscMPIoptions`. A module the worker has no executable for refuses by name (`GEOCLAW_CASE_MODULE_UNKNOWN`), as does a BOUSS_
keyword on a shallow-water run. The template's shallow-water numerics (`CLAW_NUM_EQN` 3 and the aux layout) are the template's;
a Boussinesq run states the five-equation numerics Clawpack's own setrun states.
