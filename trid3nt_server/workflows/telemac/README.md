# `workflows/telemac/` - the TELEMAC engine

One door and six trees. Nothing lives at this root but the door and the engine's
typed failures: the trees are named for what they hold - `dictionary/` the
engine's own dictionaries, `modules/` the wrappers over them and the primitive
set their outputs are, `templates/` one package per question, `authoring/` the
run directory the box receives, `solving/` the dispatched run, `helpers/` the
pure physics a declaration derives a number from. A template package is the
recipe (`<name>.py`), its declarations (`declarations.py`) and its routing
phrasings (`corpus.yaml`); everything else it uses is the door's or the trees'.

A template writes no plan. It declares a STEERING body of the module's own raw
keywords, the data chain it consumes, the mesh recipe it triangulates on and the
outputs it reads off the solved run, and hands them to the door - which fills
the sheet, holds it for review, runs it, and publishes what the outputs list
names through the wrapper's own primitives. Every question this engine answers
is one of those, so the plan language is gone from the tree and the worker
authors nothing.

TOMAWAC has NO wrapper, and the reason is measurable rather than an oversight:
`dictionary/tomawac.json` holds its 223 keywords and `entrypoint._MODULES` can solve
it, but the spectral tier has no template over it. The wrapper is built when a
question needs it, not before.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `errors.py` | The engine's typed failures - a run that could not be acquired, settled, staged, solved or read, an input it cannot model, a reach nothing maps or the mesh does not hold - each carrying the code the envelope renders and none named for a question. |
| `workflow.py` | The fill/run door a template hands its declarations to, and `TelemacWorkflow` - the two facts the skeleton records a run of this engine under. |

## Subfolders

| folder | what it is |
| --- | --- |
| `dictionary/` | The engine's own keyword dictionaries, one JSON per exposed module. See its own map. |
| `modules/` | One wrapper per exposed module - its dictionary, its composites, its outputs as the primitive set, and nothing that opines - plus the sheet a body fills, the two acts on it, fill and run, and the listing reads the primitives share. See its own map. |
| `templates/` | One package per question, over the module wrappers, and the one shared DATA row module the river templates read. See its own map. |
| `authoring/` | Everything the box receives: the ONE assembler, the serializer that writes the steering format, the DAMOCLES parse that gates it, and the engine input files a run authors - the oil module's and NESTOR's. See its own map. |
| `solving/` | The run, dispatched: stage the manifest, hand it to the solve seam, wait, surface the gates. See its own map. |
| `helpers/` | The pure physics and numerics: the CFL step and the solve-time estimate, uniform flow, the saturation relations. See its own map. |
