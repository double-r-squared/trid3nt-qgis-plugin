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
default by omission where it does not.

A body extends its module's wrapper and nothing else: a value two templates
share is restated in each, under the template that states it, so a keyword that
means something else in a new setting is seen at the use site. Bodies are
STATIC: every assertion is data fixed at import - a literal, or a description of
a read the fill substitutes - so a body never branches on a value a run
resolved.

Two acts, and only two. `fill` is repeatable and decides nothing. `run` is
explicit, on a complete sheet, and it is where execution stops being held.

A wrapper's OUTPUTS are the PRIMITIVE SET, named from the module's own variable
vocabulary: `field(name, t)`, `series(name, at)`, `max_over_time(name)`,
`profile(name, along, t)`, `extent()`, `mesh()`, `mass_balance()`, plus the
outputs past the set a module carries for itself - TELEMAC-2D's `drogues()`,
the particle track it writes, and TELEMAC-3D's `column(name, at, t)`, a
variable down the planes its result stacks. A field of a 3D result reads one
plane (`plane=`, bottom first; unstated, the surface) and is named by it. A
module may DEFINE a token over the variables its result carries - ARTEMIS's
`KD`, the wave height over the incident height its boundary file stamps - and
the primitives read it as any other. A primitive names the coupled module whose
own result it reads (`module=`), and a tracer a coupled process appended behind
the carrier's declared ones is the carrier's `T<n>` by position. A series of a token the module PRINTS rather than writes - TELEMAC-2D's `FLUX`,
the discharge across a liquid boundary - is read off the listing at the boundary
the Point lies on. A profile names the line it runs along and, where the line is
a transect rather than the domain's own axis, how far off it a node still counts
(`within_m=`). A template lists primitives with how each is published -
`.layer()`, `.chart(reference=)`, `.animate()`, `.station()` - and names its
answer as measures of them, each held against a sheet value where a verdict
needs one (`.over(P.x)` the ratio, `.below(P.x)` the comparison); a chart's
reference is a callable computing lines beside the read or another primitive
drawn as one. An answer over a variable the run did not write is nothing rather
than a refusal; a listed output that is missing refuses. The wrapper binds no
reader that knows a question.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the wrappers, the two acts, and the primitive set. |
| `module.py` | What a slot, a wrapper, a composite, an output and a defined token ARE, and the loader that makes a wrapper out of `module_input/<module>.json`. |
| `sheet.py` | The sheet - filled slots with their provenance, the files a composite named, the slots still open - and `fill` / `run`. |
| `outputs.py` | The primitive set - `field`, `series`, `max_over_time`, `profile`, `extent`, `mesh`, `mass_balance` - and `drogues` and `column`, with the read of each off a solved run through `read_selafin`, the engine's own reader inside the image, and the `Measure` a template names an answer by. |
| `listing.py` | What a solved run's own listing says, read on the server: the engine's demand, GAIA's closure, the water-volume closure per period and whole, and the flux across a liquid boundary. |
| `describe.py` | `describe_keywords` - the read over a module's dictionary, which is how the whole keyword surface is reached rather than carried in a docstring. |
| `corpus.yaml` | The routing phrasings that reach `describe_keywords`. |
| `telemac2d.py` | The TELEMAC-2D wrapper: the releases, wind, rain, oil, friction, runoff, infiltration (the curve-number and roughness surface read off the land cover at the fill), rating, hyetograph, time-origin and coupling groups, the module's variable vocabulary with the flux it prints rather than writes, and the drogues track it writes. |
| `telemac3d.py` | The TELEMAC-3D wrapper: the vertical grid keyword pair and its refusal, the water column a run is initialized from, and the wind. |
| `artemis.py` | The ARTEMIS wrapper: the incident wave, which the module reads out of the boundary file rather than the deck, so the composite restamps the pair the mesh recipe wrote; and the wave vocabulary. |
| `waqtel.py` | The WAQTEL wrapper: the O2 process and the degradation a carrier names as coupled bodies, whose slots serialize into WAQTEL's own steering file while the coupling keywords land on the carrier's sheet; a degradation given nothing couples nothing. |
| `gaia.py` | The GAIA wrapper: the bed and the suspension a carrier names as coupled bodies, expanded from a gradation or a class and its concentration, the NESTOR dredging composite, and its primitives over the module's own result file. |

## Subfolders

| folder | what it is |
| --- | --- |
| `module_input/` | The module's input vocabulary: one JSON per exposed module, as the engine's own dictionary publishes it. See its own map. |
