# Workflows - a template over one engine module

A template is VALUES over one engine module: a `PARAMS` body, a `DATA` body, the
module's own keywords, the mesh ask, the placed reads, the outputs and their
captions. Nothing in a template names a function to run. A FILL puts a value
into each input, which ACCEPTS it or REJECTS it naming the remedy; a READY fill
launches on an explicit act, and the engine's run is straight-line code - write,
solve, read. The skeleton is `workflows/runtime/workflow.py`; the TELEMAC
facade is `workflows/telemac/workflow.py`.

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
  frame, an upload onto the store's lon/lat frame. Each is the slot's own
  coercion, never a row a template writes.
- A FETCH reading its source onto the asked window and spacing. The kernel a
  row states is how that source's pixels are read; a grid past the pixel
  budget refuses naming the spacing to re-ask at.
- A MODULE'S CAPABILITY - the mesh's own operations, engine keywords, engine
  actions, forcing composites and outputs - stated by the person or the model
  on any run, whether or not a template uses it.

## The two bodies

`PARAMS` (`workflows/runtime/params.py`) - one declared VALUE per row: its
default, its bounds and its unit. A value outside the bounds refuses by name.
Every row is on the model-facing wire. A row with no default that is not
optional is REQUIRED: left unstated, the fill refuses it in every mode, so a
world value nothing measures is stated by the caller and never started from.
The model reads the same fact twice off `Param.required`: the row is listed in
the tool schema's `required`, and its docstring line ends `required`. A `DATA`
row is never listed - one left unfilled is matched, measured or asked for on
the canvas before it refuses.
A seated value carries ONE origin - user, derived, producer or template
(`Origin`, in the contracts) - and the card row, the provenance entry and the
journal row all write that word.

`DATA` (`workflows/runtime/data.py`) - one declared ARTIFACT per row. A row
STATES its slot as its type - `Data(BED)`, one of the types in
`inputs/slots.py` - and is named freely; a row that states none is a plain one,
read by whatever declared it. A class the stated slot does not read refuses
where it is written. A row is filled one of three ways:

    bed       = Data(BED).need("bathymetry")              a class the match fills
    gauge     = Data(LEVEL).need("water level series").optional()
    rivers    = Data.need("hydrography", geometry="polyline")
    structure = Data.supplied(geometry="polyline")        the person hands it in
    rain      = Data(RAIN).need("precipitation series").context("...")

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
cadence; the sea state is a height, a period and the bearing the waves come
from. The engine keeps only its reading of them: which quantities its modules
read, the mnemonic or keyword each becomes, the one clock its table is written
on, and the refusal by name of a column it reads that the station is short of.

`.need` names a CLASS of the coverage vocabulary, never a fetcher; `geometry=`
is which SHAPE of that class is asked for, a filter on the match, never a
conversion. The match (`tools/search/match.py`) filters the rows on
class and place, holds a series source to the run's window, ranks on the cell
against the mesh, on recency and on the native datum, and calls the survivors
in rank order. The window comes off `event_time` and the frame off the vertical
lever, so a template states neither.

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

## The context row

A producer row whose absence is legal is declared `.context("...")`: the
source is asked, an empty answer continues the run, and the review card carries
the sentence the template stated about what is not there. A load-bearing
row stays hard and refuses. A producer-less slot says absence with
`.optional()` instead, because there is no source to have been empty.

## Degrading between sources

A row declares no fallback of its own. The MATCH ranks every source that
states coverage for the class here and the probe calls them in that order,
dropping one that held nothing and writing the substitution on the run's
own journal; when none of them answered, an optional row states its
absence and a load-bearing one refuses typed.

## Continuing a run - an input, not a derivation

A run that carries another on NAMES it: `continue_from=<run id>` is a control on
every template's wire, like `keywords` or `restart_clean`. The skeleton reads the
file that run's solve wrote off its own journal line (`solved`), and the step
that opens the water stages it as the module's previous computation; the fill
states it and the journal line says which run was continued. A run id whose line
names no solved result refuses by name (`CONTINUATION_UNSOLVED`). Changing a
value and running again is simply a new call with that value.

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

## Mesh

The mesh is supplied-optional `Data`: AUTHORED (the person's own mesh) or
GENERATED by the shared front in `tools/mesh/`, which knows no engine. The mesh
ask carries the mesher, the extent, the resolution and the mesh's own
operations (`mesh_op`), validated against the chosen mesher's namespaces. A
mesh of another species (a structured grid, a node-link network) is a typed
refusal, never a silent conversion.
