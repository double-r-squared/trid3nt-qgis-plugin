# `workflows/telemac/` - the TELEMAC engine

One facade, one shared step family, one package per template. A template package
is the recipe (`<name>.py`), its declarations (`declarations.py`), its routing
phrasings (`corpus.yaml`) and whatever one bespoke coercion its wire needs;
everything else it uses is the facade's or the step family's.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `postprocess_telemac.py` | Turns a solved result's fields into the map products: the peak field, the rasterized grids, the animated frames. |
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
| `steps/` | The shared step families every TELEMAC template declares. See below. |
| `helpers/` | What a declaration summons: the reach front, the declared forcing, the substance class, the WAQTEL relations, the typed failures. See its own README. |
| `agitation/` | `artemis_harbor_agitation` - a swell at the harbour mouth to the agitation field inside it (ARTEMIS). |
| `do_sag/` | `telemac_do_sag` - an outfall's BOD load to the dissolved-oxygen sag downstream (TELEMAC-2D + WAQTEL). |
| `rain_on_grid/` | `telemac_rain_on_grid` - a storm over a catchment to the outlet hydrograph; `cn_infiltration.py` is its SCS curve-number infiltration. |
| `river_dye/` | `telemac_river_dye` - a spill in a reach to the downstream plume, or a flood to bed scour and deposition (TELEMAC-2D, GAIA, NESTOR); `coercions.py` is its wire policy for which point seeds the reach. |
| `stratified_flow/` | `telemac3d_stratified_flow` - a water column to its vertical structure (TELEMAC-3D). |

## `steps/` - the shared step families

| file | what it is |
| --- | --- |
| `__init__.py` | The step family's public surface: the step constructors, the runners and the typed errors a template imports. |
| `agitation.py` | The ARTEMIS deck writer and its deliverable. |
| `author.py` | The AUTHOR step: the accepted mesh plus the approved sheet to TELEMAC's own decks. |
| `cas_validate.py` | Every authored steering file, parsed by the engine's own reader against its own dictionary before anything is staged. |
| `deck.py` | The DECK step: params and forcing to the run's own record of what it solves, staged for the box. |
| `oil_templates/` | The user-fortran source an oil-class run compiles into its deck. |
| `open_water.py` | The open-water front of the AOI templates: stage, solve, read, surface. |
| `products.py` | The PRODUCTS step: a solved reach to the map layers, the scalars and the chart spec. |
| `rain_on_grid.py` | The rain-on-grid front: a catchment in, an outlet hydrograph out. |
| `run_reads.py` | What a solved run's own files say, read on the server. |
| `solve.py` | The SOLVE step: stage the manifest, dispatch the worker, wait, surface the gates. |
| `stratified.py` | The TELEMAC-3D deck writer and its deliverable. |
