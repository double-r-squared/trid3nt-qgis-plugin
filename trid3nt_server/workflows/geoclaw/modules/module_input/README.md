# `modules/module_input/` - the module's input vocabulary

One JSON per exposed body, its `keywords` extracted from Clawpack 5.14.0's own
Data classes by `scripts/extract_geoclaw_dictionary.py` and committed. The
keyword rows are never hand edited: Clawpack publishes no DFN or dico, so its
classes ARE the surface, and a transcribed table would be a second answer to a
question the engine already answers. The extractor refuses a default it cannot
type rather than guessing one, and refuses a group Clawpack grows or drops
without re-adjudication. The `inputs` section is authored - each template's
values by name, with their prose, type and unit - and the extractor writes it
back beside the fresh rows. Run the extractor with the spike venv interpreter
(`scratchpad/geoclaw_spike/venv/bin/python`): Clawpack has no linux wheel on
PyPI and never enters `venvs/agent`.

A row carries what the Data class says about one keyword - the identifier
`<GROUP>_<ATTRIBUTE>`, its type (INTEGER, REAL, LOGICAL, STRING, ARRAY for a
value or a list of values, STRUCT for a record table like `gauges`), its
record fields as the shape, its size, its help as `write()` states it, the
data file it is written into as the rubrique, its engine default when the
class has one, the spellings `write()` validates where they are enumerable,
and whether it is mandatory. Twelve keywords the classes default to `None`
carry their type from the reader that consumes them; two of those - `tfinal`
and `num_output_times` in `claw.data` - the engine reads unconditionally, so
they are the only required ones. `modules/module.py` reads a file here into
the wrapper's slots.

## Files

| file | keywords | what it is |
| --- | --- | --- |
| `geoclaw.json` | 159 | The whole rundata surface of a 2D run: claw, geoclaw, gauges, topo, dtopo, amr, adjoint, region, flagregion, refinement, fgout, fgmax, qinit, surge, friction and multilayer. |
