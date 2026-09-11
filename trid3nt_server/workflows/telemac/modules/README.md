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

A body reuses another body by COMPOSITION - `parts = [RIVER, TRACER]` - never by
extending it. Parts merge in the listed order, the body's own assertion beats
every part, and a keyword two parts both set refuses by name unless the body
settles it. Provenance names the part, so a keyword that means something else in
a new setting is seen at the use site. Bodies are STATIC: every assertion is data
fixed at import - a literal, or a description of a read the fill substitutes -
so a body never branches on a value a run resolved.

Two acts, and only two. `fill` is repeatable and decides nothing. `run` is
explicit, on a complete sheet, and it is where execution stops being held.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The door: the wrappers, and the two acts. |
| `module.py` | What a slot, a wrapper, a composite and an output ARE, and the dictionary loader that makes a wrapper out of `dictionary/<module>.json`. |
| `sheet.py` | The sheet - filled slots with their provenance, the files a composite named, the slots still open - and `fill` / `run`. |
| `describe.py` | `describe_keywords` - the read over a module's dictionary, which is how the whole keyword surface is reached rather than carried in a docstring. |
| `corpus.yaml` | The routing phrasings that reach `describe_keywords`. |
| `telemac2d.py` | The TELEMAC-2D wrapper: the releases, wind, rain, friction, rating, hyetograph, time-origin and coupling groups, and the fields and mass-balance outputs. |
| `telemac3d.py` | The TELEMAC-3D wrapper: the vertical grid keyword pair and its refusal, the water column a stratified run is initialized from, and the wind. |
| `artemis.py` | The ARTEMIS wrapper: the incident wave, which the module reads out of the boundary file rather than the deck, so the composite restamps the pair the mesh recipe wrote. |
| `waqtel.py` | The WAQTEL wrapper: the coupled bodies a carrier names, whose slots serialize into WAQTEL's own steering file while the coupling keywords land on the carrier's sheet. |
| `gaia.py` | The GAIA wrapper: the sediment bodies a carrier names and the NESTOR dredging composite, coupled the same way. |
