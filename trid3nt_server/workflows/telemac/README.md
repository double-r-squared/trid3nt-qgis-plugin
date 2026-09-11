# `workflows/telemac/` - the TELEMAC engine

One door and seven trees. Nothing lives at this root but the door itself: the
trees are named for what they hold - `dictionary/` the engine's own dictionaries,
`modules/` the wrappers over them, `templates/` one package per question,
`authoring/` the run directory the box receives, `solving/` the dispatched run,
`products/` the answer that run is read into, `helpers/` what a declaration
summons on the way. A template package is the recipe (`<name>.py`), its
declarations (`declarations.py`) and its routing phrasings (`corpus.yaml`);
everything else it uses is the door's or the trees'.

A template writes no plan. It declares a STEERING body of the module's own raw
keywords, the data chain it consumes, the mesh recipe it triangulates on and the
outputs it reads off the solved run, and hands them to the door - which fills
the sheet, holds it for review, runs it, and publishes what the outputs list
names through the wrapper's own primitives. Every question this engine answers
is one of those, so the plan language is gone from the tree and the worker
authors nothing.

TOMAWAC has NO wrapper, and the reason is measurable rather than an oversight:
`dictionary/tomawac.json` holds its 223 keywords and `entrypoint._MODULES` can solve
it, but the spectral tier has no template over it - `products/postprocess_telemac.postprocess_tomawac`
is a raw postprocess returning `(layers, metrics)`, and its tool is tombstoned. The wrapper was to be
built only if it fell out of this stage for free; it does not, so it is stated
absent here and rides with the rung-4 wave-field rebuild.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `workflow.py` | The fill/run door a template hands its declarations to, and `TelemacWorkflow` - the two facts the skeleton records a run of this engine under. |

## Subfolders

| folder | what it is |
| --- | --- |
| `dictionary/` | The engine's own keyword dictionaries, one JSON per exposed module. See its own map. |
| `modules/` | One wrapper per exposed module - its dictionary, its composites, its outputs, and nothing that opines - plus the sheet a body fills and the two acts on it, fill and run. See its own map. |
| `templates/` | One package per question, over the module wrappers, plus the shared bodies several of them list. See its own map. |
| `authoring/` | Everything the box receives: the ONE assembler, the serializer that writes the steering format, the DAMOCLES parse that gates it. See its own map. |
| `solving/` | The run, dispatched: stage the manifest, hand it to the solve seam, wait, surface the gates. See its own map. |
| `products/` | What an open-water run is answered with: the free-surface, wave, agitation, 3D and coastal postprocessors, the catchment products, and the listing readers the primitives share. See its own map. |
| `helpers/` | What a declaration summons: the reach front, the catchment, the infiltration surface, the declared forcing, the dredge fields, the saturation relations, where a derived release settles, and the typed failures. See its own map. |
