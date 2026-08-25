# TELEMAC family migration - the inventory

Taken at `04ab6b1c`, BEFORE any migration in this wave, so the wave is measured
against a stated starting point rather than a remembered one. Counting rule is
the LOC ledger's: `wc -l`, physical lines, product `.py` only.

## What is already on the skeleton

| template | tool name | file | LOC |
|---|---|---|---|
| do_sag | `telemac_do_sag` | `telemac/do_sag/do_sag.py` | 329 |
| river_dye | `telemac_river_dye` | `telemac/river_dye/river_dye.py` | 535 |

Both ride `TelemacWorkflow` (`telemac/workflow.py`, 242) over the shared family
`telemac/steps/*` (9 files, 2653). They are the cohort NATE LGTM'd; they are the
reference shape for everything below.

## What is NOT on the skeleton

| template | tool name | files | LOC | consumes (legacy top-level) | driver today |
|---|---|---|---|---|---|
| coastal_tidal_surge | `coastal_tidal_surge` | `coastal_tidal_surge.py` + `__init__.py` | 848 | `postprocess_telemac` (`postprocess_coastal`), `run_telemac` (`TELEMAC_COASTAL_SOLVER_NAME`), `results_mesh_seam`, `_bed_input`, `_template_card` | NO LiveRun. Two bespoke render scripts (`scripts/proof_coastal_tidal_surge.py`, `..._registered.py`) that read a pre-staged run dir |
| wave_field | `tomawac_wave_field` | `wave_field.py` + `__init__.py` | 695 | `postprocess_telemac` (`postprocess_tomawac`), `run_telemac` (`TOMAWAC_SOLVER_NAME`), `_bed_input`, `_template_card` | NO LiveRun, NO driver |
| agitation | `artemis_harbor_agitation` | `agitation.py` + `__init__.py` | 840 | `postprocess_telemac` (`postprocess_artemis`), `run_telemac` (`ARTEMIS_SOLVER_NAME`), `_bed_input`, `_template_card` | NO LiveRun. `scripts/proof_artemis_composer_live.py` direct-calls the registered tool |
| stratified_flow | `telemac3d_stratified_flow` | `stratified_flow.py` + `__init__.py` | 737 | `postprocess_telemac` (`postprocess_telemac3d`), `run_telemac` (`TELEMAC3D_SOLVER_NAME`), `_template_card` | NO LiveRun, NO driver |
| rain_on_grid | `telemac_rain_on_grid` | `rain_on_grid.py` + `mesh_acquisition.py` + `cn_infiltration.py` + `__init__.py` | 1835 | `postprocess_telemac` (`postprocess_telemac`), `run_telemac` (`TELEMAC_SOLVER_NAME`), `results_mesh_seam`, `workflows/mesh/precondition_gate` | NO LiveRun. Sandbox drivers `scripts/sandbox/telemac/rog_coweeta_live.py`, `rog_render_proofs.py`, `scripts/sandbox/replication/rog_ballcreek*.py` |

Total un-migrated template surface: **4955 lines** across 11 files.

## The legacy top-level files, and who reads them

| file | LOC | what it IS | readers after this wave |
|---|---|---|---|
| `postprocess_telemac.py` | 2401 | POST-STAGE MECHANISM: `read_selafin`, the node->grid rasterizer, and eight per-deliverable postprocessors (`postprocess_telemac`, `_deposition`, `_wse`, `_do`, `postprocess_tomawac`, `postprocess_artemis`, `postprocess_telemac3d`, `postprocess_coastal`) | SURVIVES. `steps/products.py` already reads it; every migration adds a reader rather than removing one. Not a deletion candidate - it is the mechanism the steps tier calls |
| `run_telemac.py` | 591 | SOLVER REGISTRY: five `register_*_solver()` / `*_local_spec()` pairs (telemac2d, tomawac, artemis, telemac3d, coastal) plus their exit classifiers | SURVIVES. Imported at server start by `tools/__init__.py` and `workflows/__init__.py`; `steps/solve.py` reads the solver names. Infrastructure, not template code |
| `results_mesh_seam.py` | 194 | the ONE native-mesh emission seam (ADR 0283/0286) | SURVIVES - `steps/products.py` is its main reader |
| `release_layer.py` | 89 | the release/outfall point as a context vector layer | SURVIVES - `steps/deck.py` reads it |
| `streeter_phelps.py` | 92 | the CLOSED-FORM DO-sag V&V oracle (pure arithmetic) | SURVIVES. Its only reader is `tests/test_telemac_do_sag.py`, which is exactly what an analytical reference is for - deleting it would delete the check, not the duplication |
| `_bed_input.py` | 62 | publishes the worker's `bed_bathymetry.tif` as a context layer | DIES when coastal + wave_field + agitation migrate: the mechanism moves into the steps tier beside the other product publishers. Ledger row owed |
| `_template_card.py` | 30 | `TemplateCard` - the dissolved engine door's listing card | ZERO real readers already (DELETION_LEDGER rows 208 + 332). Every template still declares a `TEMPLATE_CARD` nothing reads |

## What the facade does NOT yet cover

`TelemacWorkflow` today is REACH-shaped end to end, and that is the real size of
this wave:

- `acquire_domain` = geocode-reach -> mid-reach seed -> carrier discharge at the
  seed. The four AOI templates want a bbox/place AOI and a bathymetry source; the
  rain template wants a pour point and a delineated catchment.
- `author` = `write_reach_deck` only, and `_refuse_uncovered_deck_fields` checks
  the declaration against THAT signature. Each of the five needs its own deck
  authoring hook (coastal liquid boundaries, TOMAWAC wave spectrum, ARTEMIS
  monochromatic wave + reflection, TELEMAC-3D nplan/thermocline, rain hyetograph
  + CN infiltration fields).
- `solver_spec` = `Solve.telemac`, one solver name. Four more names exist.
- `read_results` = `_READERS` maps three physics processes to two publishers.
  Five more deliverables exist.

The four AOI templates are visibly ONE family: each carries its own
`_bbox_center`, `_stage_<x>_manifest`, `_download_<x>_result`, `_geo_field`,
`_slug`, `_great_lake_for`, `_classify_mode` and an `async model_<x>` composer of
the same shape. That repetition is where the family's net-LOC return has to come
from, and it is why the order below leads with coastal (the one that establishes
the shared AOI/bathymetry/manifest front) rather than with the largest file.

## Migration order for this wave

Sequential - the four AOI templates share the front they are about to create, so
there is no parallel lane.

1. `coastal_tidal_surge` - establishes the AOI + bathymetry + manifest + solve +
   read front the other three ride.
2. `tomawac_wave_field` - the first reuse; proves the front generalizes.
3. `artemis_harbor_agitation` - adds the OSM breakwater `Data` producer.
4. `telemac3d_stratified_flow` - adds the 3D reader.
5. `rain_on_grid` - the outlier (catchment delineation, in-container mesher, CN
   infiltration). Its own risk, taken last.
