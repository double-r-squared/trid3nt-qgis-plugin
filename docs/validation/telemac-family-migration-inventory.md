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

---

## Close-out: what the wave actually did

| template | migrated | template LOC | parity | proofs |
|---|---|---|---|---|
| `telemac_do_sag` | wave 2 (cohort) | 329 -> 316 | canary re-verified: DO min 9.0099 mg/L @ 158.8 m, unchanged | post-migration + refined-10 m sets |
| `telemac_river_dye` | wave 3 (cohort) | 535 -> 520 | canary re-verified: cmax 4.878571510314941 mg/L, peak 200 s, reach 472.7 m, unchanged | refined-10 m set |
| `coastal_tidal_surge` | THIS WAVE | 843 -> 301 | deck BYTE-IDENTICAL, worker metrics identical bar run tag + wall_s | coarse + refined-50 m sets |
| `tomawac_wave_field` | THIS WAVE | 690 -> 288 | deck BYTE-IDENTICAL, worker metrics identical bar run tag | coarse + refined-500 m sets |
| `artemis_harbor_agitation` | THIS WAVE | 835 -> 289 | deck BYTE-IDENTICAL, worker metrics identical bar run tag + wall_s | coarse + refined-20 m sets |
| `telemac3d_stratified_flow` | THIS WAVE | 732 -> 292 | manifest BYTE-IDENTICAL bar run tag, worker metrics identical bar run tag + wall_s | coarse + refined-1150 m sets |
| `telemac_rain_on_grid` | **NO** | 1835, untouched | n/a - not migrated | its live canary run is captured as the next wave's baseline |

`telemac_rain_on_grid` was left DELIBERATELY un-migrated and verified UNBROKEN by
a live run through the registered tool (`01M0VZ7XCBD74RVXWAKKBFWX0J`: Coweeta
Creek delineated, meshed, infiltrated and solved, six layers on the canvas). The
reason it is the monster is principled rather than a matter of size: it is the
only TELEMAC template whose migration lands inside the MESH-GATE wave's
territory. It carries a mesh PRECONDITION GATE (a gate species the declarative
library does not have), a BYO mesh (`mesh_uri`, which is the mesh-as-`Data`
ruling), and an in-container mesher - all three explicitly deferred by NATE to a
later wave. Migrating it now would either invent a mesh-gate species ahead of the
wave designed to rule on it, or keep the gate imperative inside a composite,
which is the exact disease the skeleton exists to remove.

## What happened to the legacy files

| file | predicted | actual |
|---|---|---|
| `postprocess_telemac.py` | survives | SURVIVES, +44 (the shared `_local_mesh_origin` + four call sites) |
| `run_telemac.py` | survives | SURVIVES, untouched - it is the solver registry |
| `results_mesh_seam.py` | survives | SURVIVES, untouched |
| `release_layer.py` | survives | SURVIVES, untouched |
| `streeter_phelps.py` | survives (a V&V oracle) | SURVIVES, untouched |
| `_bed_input.py` | dies with the three wave-module templates | DELETED, mechanism in `steps/open_water.py` |
| `_template_card.py` | zero readers already | DELETED - TELEMAC is the first engine to clear the species |

## Wave A - the static plan + the style contract (representation only)

The six migrated templates were REWRITTEN, not re-tuned: `plan(ops)` with
module-level binding blocks, all reads through the `P` / `D` namespaces, PARAMS
and DOC moved to a `declarations.py` sibling. Physics answers owe parity, so
every coarse canary was re-run through the product path and its metrics compared
field-for-field against the evidence the migration wave recorded.

| canary | headline | verdict |
|---|---|---|
| `telemac_do_sag` (coarse) | DO min **9.0099 mg/L @ 158.8 m**, discharge 60 m3/s user-supplied | 15/15 metric keys IDENTICAL |
| `telemac_river_dye` (coarse) | cmax **4.878571510314941 mg/L**, peak **200 s**, reach **472.7 m**, mesh 30 m / 155 nodes | IDENTICAL |
| `coastal_tidal_surge` | peak WL 3.4863 m, datum offset -0.232 m (Apalachicola 8728690) | 16/16 IDENTICAL |
| `tomawac_wave_field` | Hs field over Superior, 3000 m grid | 15/15 IDENTICAL |
| `artemis_harbor_agitation` | Kd over the real surveyed Marquette breakwater, 30 m | IDENTICAL bar `target_resolution_m` / `_note`, which were ABSENT in the recorded evidence and are present now |
| `telemac3d_stratified_flow` | surface temperature profile, calm column, 3000 m | 16/16 IDENTICAL |

The one artemis difference is a STALE EVIDENCE FILE, not a drift. That evidence
was written at `fbd013aa` (the artemis migration), and the
`("target_resolution_m", "target_resolution_note")` provenance row was added
afterwards by `ee86785c` - the change that made a grid spacing the WORKER moved
narrate itself. The row is now populated with the canary's own supplied 30 m,
which is the later commit working, and the file is refreshed here.

Run ids are excluded from the comparison for the obvious reason, and so are
`layer_uri` and wall times.

---

## Wave C - `telemac_rain_on_grid`, the seventh (2026-08-26, ADR 0316)

The one wave B left. Its reason for being last held: it was the only template
whose migration sat inside the mesh-gate wave's territory. What made it
migratable was that ADR 0315 had already ruled the shape - mesh is
SUPPLIED-optional `Data`, a producer-less context slot with no baked source - so
the byo path needed no new species.

| template | migrated | template LOC | parity | proofs |
|---|---|---|---|---|
| `telemac_rain_on_grid` | THIS WAVE | 1835 -> 952 (template folder), the mesher re-homed | manifest BYTE-IDENTICAL bar run tag; `watershed.slf` / `node_cn2.txt` / `node_manning.txt` sha256 IDENTICAL; worker metrics 31/31 IDENTICAL | coarse packet PASS, 12 deliverables incl. the 13-frame GIF |

The family is 7 of 7.

### Where the composer's contents went

| was | is | why |
|---|---|---|
| `rain_on_grid.py` composer body | `telemac/steps/rain_on_grid.py` | the TELEMAC mechanism: deck, solve, publish, provenance |
| `mesh_acquisition.py` generation | `mesh/watershed.py` | a catchment is a domain SHAPE, not a TELEMAC fact |
| `mesh_acquisition._write_bottom_selafin` | `mesh/telemac_build.py` | the front's thin per-solver writer, beside `hecras_build` |
| `cn_infiltration.py` | UNCHANGED, template sibling | SCS curve numbers vary per QUESTION, so they drop all the way |
| PARAMS + DOC | `rain_on_grid/declarations.py` | the uniform sibling norm |

### The legacy top-level files, re-checked

Nothing this migration touched leaves a legacy file readerless.
`postprocess_telemac.py` gains a reader (the rain-on-grid publisher calls
`postprocess_telemac_wse`), `results_mesh_seam.py` keeps both its readers,
`run_telemac.py` is still the solver registry, and `release_layer.py` and
`streeter_phelps.py` are untouched. `_bed_input.py` and `_template_card.py` died
in wave B and stay dead.

`workflows/mesh/precondition_gate.py` was checked for the opposite reason - the
context slot could have orphaned it - and keeps three readers: this template,
`hecras_flood_2d` and the three SCHISM templates.

### What the audit's class-C set turned into

The steps audit parked rain_on_grid's whole set behind this migration. Row by row:

- **the 17 bare signature defaults** - now 24 declared `Param` rows plus three
  labeled module constants (`POUR_POINT_BUFFER_DEG`, `NLCD_NATIVE_RESOLUTION_M`,
  and the mesher's own default band in the shared front).
- **the invented AOI-centroid pour point** - DELETED; a required USER param behind
  a `DrawGate`, refused typed in auto mode.
- **the unreachable Huang slope correction** - now a declared param, wired to node
  slopes read off the mesh's own piecewise-linear bed.
- **the 24-hour solve timeout** - a labeled `_SOLVE_TIMEOUT_S` beside the
  open-water front's, with the reason it is an order of magnitude larger stated.
- **the duplicated UTM-zone formula** - one `utm_epsg_for(lon, lat)` in the mesh
  front; both former copies read it.

---

## Wave D - the FETCH migration (2026-08-26, ADR 0317)

Waves B and C moved the templates onto the skeleton. This one moves the DATA:
the bed four of the seven families solve on was fetched from inside the solver
container, and is now a declared producer staged into the run directory.

| family | bed before | bed after | `--network none` | parity |
|---|---|---|---|---|
| `coastal_tidal_surge` | in-worker `requests.get` NOAA DEM_all, 1800 px/deg | `Data("bed")` -> `fetch_ncei_dem_mosaic`, staged as `bed_source.tif` | YES | 19/19 composer metrics + all worker metrics IDENTICAL |
| `tomawac_wave_field` | same fetch, 1200 px/deg | same, 1200 px/deg | YES | IDENTICAL |
| `telemac3d_stratified_flow` | same fetch, 1200 px/deg, NO bed layer at all | same, 1200 px/deg, bed layer for the first time | YES | IDENTICAL |
| `artemis_harbor_agitation` | same fetch, 3000 px/deg | same, 3000 px/deg | YES | MOVED - the `demo_bw` chop, by design |
| `telemac_river_dye` | 6 in-worker fetches | UNCHANGED | no | IDENTICAL (refined) |
| `telemac_do_sag` | shares river_dye's | UNCHANGED | no | IDENTICAL on re-run; a pre-existing FLAKE found |
| `telemac_rain_on_grid` | agent-side already | UNCHANGED | no (shares the reach solver) | IDENTICAL |

Byte-parity was PROVEN before anything was migrated, not argued: the router's
`fetch_ncei_dem_mosaic` request over the coastal canary's own bbox returns a
262982-byte body with sha256 `ffa0579fbf84d7d9...`, and so does the worker's own
`requests.get` of the same endpoint. That is why the executor gained a declared
`px_per_deg` sizing rather than reusing its metric `native_cell_m` one - the four
builders' lattices are ANGULAR and differ per builder, and a re-grid would have
moved every sampled node.

### What died

| file / seam | LOC | why |
|---|---|---|
| `workers/telemac/_bed_cog.py` | 109 | the node-lattice bed COG; the staged SOURCE raster is continuous and IS what the nodes were sampled from |
| 4x `fetch_greatlakes_bathy` / `fetch_demall_bed` | ~80 | one 55-line `_staged_bed.sample_staged_bed` reads the file instead |
| `steps/open_water.py::surface_in_worker_bed_input` | 26 | emit-on-fetch surfaces the producer's own result |
| `artemis_build.py`'s `demo_bw` branch | ~30 | the deck is the only authority on a structure |
| 5x hand-copied `build_argv` closures | ~40 | one `_telemac_build_argv` factory |
| 4x `test_write_bed_cog*` / `test_surface_in_worker_*` | ~90 | they covered the deleted seams |

`workers/telemac/` product 9318 -> 9132, test 2383 -> 2336.

### What did NOT die, and why

`telemac_river_dye_build.py::write_bed_cog` survives. It is the same shape as the
one that died, and it dies the same way - but only once its bed is a declared
producer too, and that is blocked on a NATE ruling and a measured parity question
(ADR 0317, "What this ADR deliberately does not do").

### Gate results, verbatim

| gate | result |
|---|---|
| image provenance | every product `.py` in `trid3nt-local/telemac:latest` sha256-identical to `workers/telemac/`; only `test_entrypoint.py` differs, trimmed by `.dockerignore` |
| network denial | `docker run --network none trid3nt-local/telemac:latest` -> `socket.create_connection('gis.ngdc.noaa.gov', 443)` raises `gaierror [Errno -3] Temporary failure in name resolution` |
| byte parity of the migrated fetch | router body 262982 bytes sha256 `ffa0579f...` == worker body 262982 bytes sha256 `ffa0579f...` (coastal canary bbox) |
| family canaries | 8 runs. coastal / tomawac / telemac3d / river_dye-refined / rain_on_grid / artemis-resonance metric-IDENTICAL; artemis-agitation MOVED by the demo_bw chop; do_sag-refined identical on re-run (flake, see ADR 0317) |
| artemis refined flagship packet | PASS, 8 deliverables, 2 layers |
| offline suite | 12370 passed, 17 skipped, 1 xfailed, **0 failed** (the documented baseline was 6: fetch_resolution x4 + river_dye x2) |
| contracts | 789 passed |
| ws_smoke | all_passed=True |
| retrieval, `retrieve_visible_tools(prompt, None, 8)` | `fetch_ncei_dem_mosaic` 6/6 HIT, top-2 on every query |

BLOCKED, upstream, not by this wave: the REFINED coastal flagship packet. NOAA
CO-OPS returned HTTP 504 on both `mdapi/.../stations.json` and
`mdapi/.../stations/8728690/datums.json` for over half an hour. The refusal is the
correct one and reads verbatim: "CO-OPS station 8728690 published datums could not
be read (HTTP Error 504: Gateway Timeout); the MLLW series cannot be reconciled
with the [NAVD88 bed]" - a typed `TIDE_SERIES_UNAVAILABLE`, never an assumed zero
offset. The datum numbers the flagship pins are carried by the COARSE pin, which
ran green and identical: `datum_offset_m -0.232`, `peak_wl_m 3.4863`.
