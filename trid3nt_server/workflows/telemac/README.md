# `workflows/telemac/` - the TELEMAC engine

One door, four shared trees, one package per template. The shared trees are
named for what they produce - `authoring/` the run directory the box receives,
`solving/` the dispatched run, `products/` the answer that run is read into,
`helpers/` what a declaration summons on the way. A template package is the recipe
(`<name>.py`), its declarations (`declarations.py`), its routing phrasings
(`corpus.yaml`) and whatever one bespoke coercion its wire needs; everything
else it uses is the door's or the shared trees'.

A template writes no plan. It declares a STEERING body of the module's own raw
keywords, the parts it is made of, the data chain it consumes and the mesh recipe
it triangulates on, and hands them to the door - which fills the sheet, holds it
for review, runs it, and reads the solved file through the wrapper's own output
binding. Every question this engine answers is one of those, so the plan language
is gone from the tree and the worker authors nothing.

TOMAWAC has NO wrapper, and the reason is measurable rather than an oversight:
`catalog/tomawac.json` holds its 223 keywords and `entrypoint._MODULES` can solve
it, but the spectral tier has no publisher of the shape an `outputs(...)` binding
takes - `products/postprocess_telemac.postprocess_tomawac` is a raw postprocess
returning `(layers, metrics)`, and its tool is tombstoned. The wrapper was to be
built only if it fell out of this stage for free; it does not, so it is stated
absent here and rides with the rung-4 wave-field rebuild.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `result_reader.py` | `read_selafin` - a solved result's mesh and per-variable frames, read by the engine's own `TelemacFile` inside the TELEMAC image. |
| `release_layer.py` | The reach's release point published as a context layer on the canvas. |
| `release_point.py` | Where a release is allowed to be - inside the domain, on the river - the refusal when a supplied point is not, and where a derived one is settled inside the accepted mesh. |
| `results_mesh_seam.py` | Writes the results-mesh `outputs.json` and publishes it through the one emission seam. |
| `run_telemac.py` | The local-docker solve seam: five solver names, one image, one spec. |
| `streeter_phelps.py` | The Streeter-Phelps closed-form dissolved-oxygen sag, the WAQTEL O2 verification reference. |
| `workflow.py` | The fill/run door a template hands its declarations to, and `TelemacWorkflow` - the two facts the skeleton records a run of this engine under. |

## Subfolders

| folder | what it is |
| --- | --- |
| `catalog/` | The engine's own keyword dictionaries, one JSON per exposed module - generated in-image by `scripts/extract_telemac_catalog.py`, committed, never hand edited, compared back against the image by the suite. |
| `modules/` | One wrapper per exposed module - its catalog, its composites, its outputs, and nothing that opines - plus the sheet a body fills and the two acts on it, fill and run. See its own README. |
| `authoring/` | Everything the box receives: the ONE assembler, the steering-file writers it summons, the DAMOCLES parse that gates them. See its own README. |
| `solving/` | The run, dispatched: stage the manifest, hand it to the solve seam, wait, surface the gates. See its own README. |
| `products/` | What a solved run is answered with: the postprocessors, the reach and catchment deliverables, the run's own files read on the server. See its own README. |
| `helpers/` | What a declaration summons: the reach front, the catchment, the infiltration surface, the declared forcing, the substance class, the WAQTEL relations, the typed failures. See its own README. |
| `templates/` | One package per question, over the module wrappers, plus the shared bodies several of them list. See its own README. |
