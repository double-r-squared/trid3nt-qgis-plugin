# `workflows/telemac/` - the TELEMAC engine

One facade, four shared trees, one package per template. The shared trees are
named for what they produce - `authoring/` the deck the box receives, `solving/`
the dispatched run, `products/` the answer that run is read into, `helpers/`
what a declaration summons on the way. A template package is the recipe
(`<name>.py`), its declarations (`declarations.py`), its routing phrasings
(`corpus.yaml`) and whatever one bespoke coercion its wire needs; everything
else it uses is the facade's or the shared trees'.

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
| `workflow.py` | `TelemacWorkflow` - the engine facade. Realizes `acquire_domain`, `author`, `solve` and `read` for every TELEMAC physics process. |

## Subfolders

| folder | what it is |
| --- | --- |
| `authoring/` | Everything the box receives: the deck writers, the fronts that stage them, the DAMOCLES parse that gates them. See its own README. |
| `solving/` | The run, dispatched: stage the manifest, hand it to the solve seam, wait, surface the gates. See its own README. |
| `products/` | What a solved run is answered with: the postprocessors, the reach deliverables, the run's own files read on the server. See its own README. |
| `helpers/` | What a declaration summons: the reach front, the declared forcing, the substance class, the WAQTEL relations, the typed failures. See its own README. |
| `agitation/` | `artemis_harbor_agitation` - a swell at the harbour mouth to the agitation field inside it (ARTEMIS). |
| `do_sag/` | `telemac_do_sag` - an outfall's BOD load to the dissolved-oxygen sag downstream (TELEMAC-2D + WAQTEL). |
| `rain_on_grid/` | `telemac_rain_on_grid` - a storm over a catchment to the outlet hydrograph; `cn_infiltration.py` is its SCS curve-number infiltration. |
| `river_dye/` | `telemac_river_dye` - a spill in a reach to the downstream plume, or a flood to bed scour and deposition (TELEMAC-2D, GAIA, NESTOR); `coercions.py` is its wire policy for which point seeds the reach. |
| `stratified_flow/` | `telemac3d_stratified_flow` - a water column to its vertical structure (TELEMAC-3D). |
