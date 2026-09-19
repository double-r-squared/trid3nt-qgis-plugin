# `workflows/telemac/modules/` - the module surface

A workflow is a WRAPPER around a TELEMAC module, exposing the module's full
keyword surface. The engine already publishes that surface: every module's
dictionary names each keyword, its type, its allowed values, its default and its
help. The wrapper is that dictionary, and nothing else that opines - it is the
analog of the engine's own defaults. Variance lives in templates, which stay
Python because people read them.

A class body extending a wrapper asserts raw keywords under the identifiers the
image itself spells them by. A keyword the module does not have, and a value the
dictionary does not take, refuse BY NAME at import; the sheet the body fills
records per slot WHERE its value came from, so nothing composed or derived hides
behind a number. That record is a closed vocabulary - `template`, `user`,
`model`, `producer`, `derived`, `calibrated` - with a detail beside it naming
the body, the producer or the slot it points at; the engine default answers by
ABSENCE and carries no row at all. Nothing branches on it: it is what a reader
overrides with confidence, or does not.

A COMPOSITE expands one value into the keywords that value IS: the arming
keyword its input implies, the slots its value literally fills, the file it
writes. It states nothing else. A choice among the alternatives the dictionary
offers - which wind option, which restart format, which runoff model - is the
TEMPLATE's assertion where the template needs a non-default, and the engine's own
default by omission where it does not. Nor does an ARGUMENT re-name a keyword the
dictionary carries: what the composite takes is what the dictionary LACKS - a
file, a placement, a gradation, a series - and every keyword beside it stands on
the body under its own name. The one argument that survives a keyword is a
COERCION the ingestion owes its reader: a common alternate unit the question is
asked in - microns for a diameter, milligrams per litre for a source load, a
compass bearing for a wind - converted at the line that writes the engine's own.

Beside the dictionary's own rows sits `module_bounds.json`, the PLAUSIBILITY
bounds each keyword's values are taken inside: one `[lo, hi]` for every value a
keyword holds, or one pair per element in the keyword's own order. It is a
sidecar rather than a column, because the dictionary itself is re-extracted from
the image and compared to it byte for byte. A value outside refuses by the
keyword's name at fill, and a keyword the file rows nothing for is bounded by the
engine alone. This is the one table a calibration reads its ranges from.

A body extends its module's wrapper and nothing else: a value two templates
share is restated in each, under the template that states it, so a keyword that
means something else in a new setting is seen at the use site. Bodies are
STATIC: every assertion is data fixed at import - a literal, or a description of
a read the fill substitutes - so a body never branches on a value a run
resolved.

Two acts, and only two. `fill` is repeatable and decides nothing. `run` is
explicit, on a complete sheet, and it is where execution stops being held.

A wrapper's MODULE_OUTPUT is the table of what the module WRITES: one row per
engine variable, keyed by the mnemonic the module's own printouts keyword
spells (ARTEMIS `ZS`, not `S`), carrying the name the result file gives it, its
unit, the style row it draws under and whether it varies in time. The table is
the whole statement - a variable the dictionary offers and the table does not
row is not written - and `table(stated)` is that statement AS ONE DECK CARRIES
IT: the rows whose switch the deck leaves on, and, where the engine numbers a
row per class (KHIONE's frazil), one row per class the deck itself counts. A
switch the deck does not state is read at the dictionary's own default, because
that is the value the engine then reads. The printouts keyword is GENERATED
from that table as the deck is serialized, for the host and for every coupled module, with every token
checked against the keyword's own choices. A run publishes every row its result
carries as ONE layer, styled from the row: the temporal layer where the row
varies in time - ranged over every frame of the record - and the final frame
where it does not. A row the result does not carry is skipped. A row the module PRINTS in its listing (TELEMAC-2D's `FLUX`) or
DERIVES over the variables its result carries (ARTEMIS's `KD`) is published or
read but never asked of the engine. The `TRACER` row is the run's tracers - one
per NAMES OF TRACERS entry, read by position as `T<n>` - and a module that
appends tracers to its carrier states those rows and their styles itself
through one hook (WAQTEL by process, GAIA per suspended class, KHIONE by the
branch its own Fortran adds each tracer in), over rows it
declares in APPENDABLE so what it may append is readable without a body to run
the hook against; the carrier never counts them. A row whose name a carrier
tracer already carries is ADOPTED rather than appended, which is the match the
engine itself makes on the first sixteen characters of the name.

A wrapper's READS are the PRIMITIVE SET over that output, named from the
module's own variables: `field(name, t)`, `series(name, at)`,
`max_over_time(name)`, `profile(name, along, t)`, `extent()`, `mesh()`,
`mass_balance()`, plus the reads past the set a module carries for itself -
TELEMAC-2D's `drogues()`, the particle track it writes, and TELEMAC-3D's
`column(name, at, t)`, a variable down the planes its result stacks. A field of
a 3D result reads one plane (`plane=`, bottom first; unstated, the surface) and
is named by it. A primitive names the coupled module whose own result it reads
(`module=`). A series of a printed token is read off the listing at the
boundary the Point lies on. A profile names the line it runs along and, where
the line is a transect rather than the domain's own axis, how far off it a node
still counts (`within_m=`).

A template lists only the reads it PLACES - a series at a point the user gives,
a profile along a line, the track a module writes - with how each is published
(`.chart(reference=)`, `.station()`, and `.layer()` for the track alone) and a
caption for each; it states no style, no printout list and no caption for a
variable, and it paints nothing over the domain: a field, an envelope, the
extent or the mesh published as a layer or an animation is what the module's own
table already states, and the grammar lint refuses it. It names its answer
as measures of the primitives, each held against a sheet value
where a verdict needs one (`.over(P.x)` the ratio, `.below(P.x)` the
comparison) and against the lever a question needs where the run may never have
been asked it (`.needs(P.x, without=...)`). A chart's reference is a callable
computing lines beside the read or another primitive drawn as one. An answer
over a variable the run did not write states the REASON and the delivery
refuses it; one over a lever nobody supplied says the question was not asked and
the delivery passes. A placed read that is missing refuses. The wrapper binds no
reader that knows a question.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the wrappers, the two acts, and the primitive set. |
| `module.py` | What a slot, a wrapper, a composite and an output row ARE, the loader that makes a wrapper out of `module_input/<module>.json`, and the generation of a module's printouts keyword from its own table. |
| `sheet.py` | The sheet - filled slots with their provenance, the files a composite named, the slots still open, the coupled bodies it couples, the tracers its result carries and every variable it publishes - and `fill` / `run`. |
| `outputs.py` | The primitive set - `field`, `series`, `max_over_time`, `profile`, `extent`, `mesh`, `mass_balance` - and `drogues` and `column`, with the read of each off a solved run through `read_selafin`, the engine's own reader inside the image, and the `Measure` a template names an answer by, with the two sentences a measure answers with when it cannot be read or was never asked. |
| `listing.py` | What a solved run's own listing says, read on the server: the engine's demand, GAIA's closure, the water-volume closure per period and whole, and the flux across a liquid boundary. |
| `coupling.py` | The `coupling` composite both hydrodynamic carriers register: the three keywords a carrier states about a coupled body, that body's own slots handed to its steering file, and the refusal a carrier that solves no water column gives the keywords a module has only under a three-dimensional host. |
| `describe.py` | `describe_keywords` - the read over a module's dictionary, which is how the whole keyword surface is reached rather than carried in a docstring. |
| `corpus.yaml` | The routing phrasings that reach `describe_keywords`. |
| `telemac2d.py` | The TELEMAC-2D wrapper: the releases, wind, rain, atmosphere, oil, friction, runoff, infiltration (the curve-number and roughness surface read off the land cover at the fill), rating, hyetograph, time-origin and coupling groups, the module's output table with the tracer row and the flux it prints rather than writes, and the drogues track it writes. |
| `telemac3d.py` | The TELEMAC-3D wrapper: the vertical grid keyword pair and its refusal, the water column a run is initialized from, the wind, the atmosphere it reads the same file for as TELEMAC-2D does, and the output table its 3D printouts keyword is written from. |
| `artemis.py` | The ARTEMIS wrapper: the incident wave, which the module reads out of the boundary file rather than the deck, so the composite restamps the pair the mesh recipe wrote; the wave output table; and the KD coefficient it derives over that table. |
| `waqtel.py` | The WAQTEL wrapper: the thermal, O2, micropollutant, eutrophication and degradation processes a carrier names as coupled bodies, each carrying its process number and the keywords stated under their own names, whose slots serialize into WAQTEL's own steering file while the coupling keywords land on the carrier's sheet; a degradation given nothing couples nothing; and the tracers each process appends to its carrier's result. |
| `gaia.py` | The GAIA wrapper: the bed and the suspension a carrier names as coupled bodies, expanded from a gradation or a class and its concentration, the DREDGE the engine offers as keywords on this deck rather than as a module of its own - its action values, and the three files it names - its output table and the class a suspension appends to its carrier's tracers, and its primitives over the module's own result file. |
| `khione.py` | The KHIONE wrapper: the ice a host couples - the body carrying the host's own mesh and boundary file and a result file of its own, the variables the engine indexes with a name of their own plus everything the thermal budget allocates past them (four rows per frazil class the deck counts, the temperature, the salinity and the dynamic cover), the tracers it appends to its host under the branch its own Fortran adds each in, the keywords only a three-dimensional carrier states, and the variables keyword written in the engine's own wildcard where the literal table would run past the line DAMOCLES reads. |
| `tomawac.py` | The TOMAWAC wrapper: the wave field a template solves STANDALONE on its own mesh and boundary walk, or a host couples through its own keywords - the forty variables the engine indexes less the private table nothing in the distributed code writes, the bottom velocity a deck over infinite depth takes off the table because the engine leaves that row untouched rather than clearing it, the spectra kept as result files no primitive of the geographic mesh reads, and the host switch a coupling arms, the module appending no tracer and its forces reaching the host only through it. |

## Subfolders

| folder | what it is |
| --- | --- |
| `module_input/` | The module's input vocabulary: one JSON per exposed module, as the engine's own dictionary publishes it. See its own map. |
