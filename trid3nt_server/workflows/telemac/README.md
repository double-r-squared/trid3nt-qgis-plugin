# `workflows/telemac/` - the TELEMAC engine

One facade, one shared step family, one package per template. A template package
is the recipe (`<name>.py`), its declarations (`declarations.py`), its routing
phrasings (`corpus.yaml`) and whatever one bespoke coercion its wire needs;
everything else it uses is the facade's or the step family's.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The package door. |
| `postprocess_telemac.py` | Reads a solved SELAFIN into the map products: the peak field, the rasterized grids, the animated frames. |
| `release_layer.py` | The reach's release point published as a context layer on the canvas. |
| `release_point.py` | Where a release is allowed to be - inside the domain, on the river - and the refusal when it is not. |
| `results_mesh_seam.py` | Writes the results-mesh `outputs.json` and publishes it through the one emission seam. |
| `run_telemac.py` | The local-docker solve seam: five solver names, one image, one spec. |
| `streeter_phelps.py` | The Streeter-Phelps closed-form dissolved-oxygen sag, the WAQTEL O2 verification reference. |
| `workflow.py` | `TelemacWorkflow` - the engine facade. Realizes `acquire_domain`, `author`, `solve` and `read` for every TELEMAC physics process. |

## Subfolders

| folder | what it is |
| --- | --- |
| `steps/` | The shared step families every TELEMAC template declares. See below. |
| `agitation/` | `artemis_harbor_agitation` - a swell at the harbour mouth to the agitation field inside it (ARTEMIS). |
| `coastal_tidal_surge/` | `coastal_tidal_surge` - a gauge stage series to coastal inundation. PARKED. |
| `do_sag/` | `telemac_do_sag` - an outfall's BOD load to the dissolved-oxygen sag downstream (TELEMAC-2D + WAQTEL). |
| `rain_on_grid/` | `telemac_rain_on_grid` - a storm over a catchment to the outlet hydrograph; `cn_infiltration.py` is its SCS curve-number infiltration. |
| `river_dye/` | `telemac_river_dye` - a spill in a reach to the downstream plume, or a flood to bed scour and deposition (TELEMAC-2D, GAIA, NESTOR); `coercions.py` is its wire policy for which point seeds the reach. |
| `stratified_flow/` | `telemac3d_stratified_flow` - a water column to its vertical structure (TELEMAC-3D). |
| `wave_field/` | `tomawac_wave_field` - a storm over a fetch to the wave field (TOMAWAC). PARKED. |

## `steps/` - the shared step families

| file | what it is |
| --- | --- |
| `__init__.py` | The step family's public surface: the step constructors, the runners and the typed errors a template imports. |
| `agitation.py` | The ARTEMIS deck writer and its deliverable. |
| `author.py` | The AUTHOR step: the accepted mesh plus the approved sheet to TELEMAC's own decks. |
| `cas_validate.py` | Every authored steering file, parsed by the engine's own reader against its own dictionary before anything is staged. |
| `coastal.py` | The coastal deck writer and the inundation deliverable. |
| `deck.py` | The DECK step: params and forcing to the run's own record of what it solves, staged for the box. |
| `errors.py` | The reach pipeline's typed failures, each with the code the envelope carries. |
| `forcing.py` | Declared forcing DATA: net rain and evaporation, and the carrier discharge resolved at the reach. |
| `oil_templates/` | The user-fortran source an oil-class run compiles into its deck. |
| `open_water.py` | The open-water front of the AOI templates: stage, solve, read, surface. |
| `products.py` | The PRODUCTS step: a solved reach to the map layers, the scalars and the chart spec. |
| `rain_on_grid.py` | The rain-on-grid front: a catchment in, an outlet hydrograph out. |
| `reach.py` | The reach front of every river plan: geocode, seed, flowline, banks coverage, mesh coverage, the CFL timestep law. |
| `run_reads.py` | What a solved run's own files say, read on the server. |
| `solve.py` | The SOLVE step: stage the manifest, dispatch the worker, wait, surface the gates. |
| `stratified.py` | The TELEMAC-3D deck writer and its deliverable. |
| `substance.py` | What was spilled: the substance CLASS and the modules that class arms. |
| `water_quality.py` | The WAQTEL steps: the documented relations and the O2 process block. |
| `wave.py` | The TOMAWAC deck writer and its deliverable. |
