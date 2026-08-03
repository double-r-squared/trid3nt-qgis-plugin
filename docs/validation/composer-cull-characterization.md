# Composer cull characterization (report-only)

Post-engine-door-dissolution ground truth: the workflows surface is now **20
registered atom templates** (`tier="template"`, gate-expanded behind the
`run_<engine>` doors, excluded from the default retrieval pool) --
`sfincs_flood`; the 11 MODFLOW templates (`asr`, `capture_zone`,
`contaminant_plume`, `managed_recharge`, `mine_dewatering`,
`regional_water_budget`, `river_seepage`, `saltwater_intrusion`,
`sustainable_yield`, `wellhead_protection`, `wetland_hydroperiod`);
`elmfire_fire_spread`; `geoclaw_inundation`; `landlab_susceptibility`;
`openquake_psha`; `pelicun_damage_assessment`; `swan_wave_field`;
`swmm_urban_flood`; `telemac_river_dye` -- **plus 3 general composers**
(`run_model_nws_flood_event_scenario`,
`run_model_groundwater_contamination_scenario`, `compute_impact_envelope`;
registered + retrievable) **plus 7 internal `model_*` siblings** (unregistered,
not retrievable).

The load-bearing finding of this characterization: **the 7 internal `model_*`
siblings are NOT dead and NOT overfit duplicates of some other atom -- each is
the 1:1 private orchestration BODY of exactly one registered engine template**
(the "door"). The door (e.g. `elmfire_fire_spread` in
`elmfire/fire_spread/fire_spread.py`) is a thin surface: it holds the
LLM-facing docstring + `@register_tool` + the `TemplateCard`, coerces loose
kwargs into the engine `RunArgs` contract, maps typed errors to a status dict,
and then `await`s the `model_*` body which owns the real fetch -> deck/stage ->
`run_solver` -> `wait_for_completion` -> postprocess -> publish chain. This is a
door/body SPLIT, confirmed verbatim by the import seams in
`server/src/trid3nt_server/agent/tools/__init__.py:600-640` ("`model_X` stays
the internal engine surface the template calls"). So the only "overfit-era"
residue on the 7 is their scenario-specific NAMING (dambreak, landslide,
urban_flood, river_dye, seismic_hazard, wave, fire_spread) surviving under a
now-generic engine template -- not a redundant capability. "Cull" for these
therefore means **fold the body into its door file (or rename it generically)**,
never delete the engine run.

The 3 registered general composers are genuinely distinct question-archetypes
that no atom template + knobs reproduces: a live-alert-driven multi-source chain,
a news-prose extraction + confirmation chain, and a cross-tool aggregation.

## Characterization table

| Composer | Registered? | Question archetype | Engine atom sibling(s) | Atom+knobs covers it? | Real usage | Recommendation |
|---|---|---|---|---|---|---|
| `run_model_nws_flood_event_scenario` | Yes (retrievable) | "Model the flood that is happening" from a LIVE NWS flood/flash-flood warning -> observed MRMS precip -> SFINCS; returns a 3-layer accumulation (warning polygon + precip raster + flood depth) | `sfincs_flood` (`sfincs/flood/flood.py`) | **No.** Structurally irreducible: CONUS alert sweep, severity selection + polygon-bbox extraction (`select_flood_warning`/`extract_polygon_bbox`), live raw-GeoJSON read, MRMS binding, and a `no_active_flood_warning` structured degrade. The atom takes a bbox + return period; it cannot ingest/select a live warning. Calls `model_flood_scenario` as its final step (true higher-order composer). | Registered via `AtomicToolMetadata(name="run_model_nws_flood_event_scenario")` (line 909); wrapper `run_model_nws_flood_event_scenario` at line 917; `compute_impact_envelope` docstring names it as an upstream flood-layer source (`compute_impact_envelope.py:275`). | **KEEP** -- distinct live-data archetype the SFINCS atom cannot express. |
| `run_model_groundwater_contamination_scenario` | Yes (retrievable) | Turn a news article about a chemical/solvent spill into a MODFLOW-GWT plume: extract contaminant/location/amount/duration from prose, derive forcing (mass via density, rate = mass/duration) with clamps, CONFIRM, then run | `modflow_contaminant_plume` (`modflow/contaminant_plume/contaminant_plume.py`) | **No.** The atom needs explicit numeric forcing already in hand; this composer's entire value is the prose extraction (`extract_spill_parameters`, solvent/duration regex bags, unit conversions, plausibility clamps) + Invariant-9 confirmation gate. It then delegates the solve to `model_contaminant_plume` (the atom's composer) at line 880-900 -- a genuine higher-order composer over the atom. | Registered `AtomicToolMetadata(name="run_model_groundwater_contamination_scenario")` (line 975); wrapper at line 992; imported into `tools/__init__.py` is via the general-composer path. | **KEEP** -- distinct news-ingest + confirmation archetype above the atom. |
| `compute_impact_envelope` | Yes (retrievable) | "How much damage / how many structures / expected loss / displaced population" from an existing flood layer -> one aggregate `ImpactEnvelope` | `pelicun_damage_assessment` (`pelicun/damage_assessment/damage_assessment.py`) + `postprocess_pelicun` | **Partly.** The 4-step chain (geocode -> NSI/MS inventory -> `pelicun_damage_assessment` -> `postprocess_pelicun`) IS composable from registered atoms; `pelicun_damage_assessment` gives per-feature damage, this collapses to portfolio totals. Distinct as an AGGREGATE archetype + single narratable sentence, but sits close to the "analysis is playground, not tools" doctrine (a composed analysis). | Registered `AtomicToolMetadata(name="compute_impact_envelope")` (line 186); imported/registered in `tools/__init__.py:580`; chains `fetch_usace_nsi`, `pelicun_damage_assessment`, `postprocess_pelicun`. | **KEEP** (flagged) -- distinct aggregate impact archetype + one load-bearing narration; note the playground-doctrine tension for NATE. |
| `model_fire_spread_scenario` | No (internal, not retrievable) | ELMFIRE wildfire spread from a point ignition (same question as its door) | Door: `elmfire_fire_spread` (`elmfire/fire_spread/fire_spread.py`) | **N/A -- it IS the door's body.** Standard fetch fuels/DEM -> build deck -> `run_solver('elmfire')` -> postprocess -> publish; no multi-engine chaining, no novel post-proc. `elmfire_fire_spread` expresses exactly this run and calls it. | Called only by `fire_spread/fire_spread.py:205`; 1 test file (`test_model_fire_spread_chain.py`). Not registered, not retrievable. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body, overfit scenario name; fold into the door or rename generically. Engine run must survive. |
| `model_dambreak_geoclaw_scenario` | No (internal) | GeoClaw shallow-water inundation (dam-break / tsunami / surge) -- same question as its door | Door: `geoclaw_inundation` (`geoclaw/inundation/inundation.py`) | **N/A -- door body.** Standard fetch topobathy -> stage -> `run_solver('geoclaw')` Batch -> download fort.q -> postprocess -> publish (+ offshore-domain planning, overland mask). Engine-run internals, not a distinct archetype; `geoclaw_inundation` calls it. | Called only by `geoclaw/inundation/inundation.py:262`; 3 test files (`test_run_geoclaw_chain.py`, `test_worker_postprocess_offload.py`, `test_always_offload_heavy_tools.py`). Not registered. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body, overfit "dambreak" name. Engine run survives. |
| `model_landslide_scenario` | No (internal) | Landlab landslide-susceptibility field -- same question as its door | Door: `landlab_susceptibility` (`landlab/susceptibility/susceptibility.py`) | **N/A -- door body.** Standard fetch DEM -> stage -> `run_solver('landlab')` Batch -> download field COG -> postprocess -> publish; `landlab_susceptibility` calls it. | Called only by `landlab/susceptibility/susceptibility.py:231`; 2 test files (`test_landlab_engine.py`, `test_worker_postprocess_offload.py`). Not registered. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body, overfit "landslide" name. Engine run survives. |
| `model_seismic_hazard_scenario` | No (internal) | OpenQuake PSHA hazard map -- same question as its door | Door: `openquake_psha` (`openquake/psha/psha.py`) | **N/A -- door body.** Assemble job.ini build_spec -> stage -> `run_solver('openquake')` Batch -> download hazard CSV -> postprocess -> publish; `openquake_psha` calls it. | Called only by `openquake/psha/psha.py:226`; 7 test files (most-tested internal body: `test_openquake_engine.py`, `test_seismic_real_fault_wiring.py`, `test_engine_chart_emission.py`, etc.). Not registered. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body; heavy test coupling means the fold must re-point ~7 test imports. Engine run survives. |
| `model_wave_scenario` | No (internal) | SWAN standalone nearshore wave field (Hs/Tp/Dir) -- same question as its door | Door: `swan_wave_field` (`swan/wave_field/wave_field.py`) | **N/A -- door body.** Standard fetch topobathy -> parametric boundary -> stage -> `run_solver('swan')` Batch -> download swan_out.mat -> postprocess -> publish; `swan_wave_field` calls it. | Called only by `swan/wave_field/wave_field.py:312`; 2 test files (`test_run_swan_chain.py`, `test_publish_manifest_register_only_phase4.py`). Not registered. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body, overfit "wave scenario" name. Engine run survives. |
| `model_urban_flood_swmm` | No (internal) | PySWMM quasi-2D urban flood -- same question as its door | Door: `swmm_urban_flood` (`swmm/urban_flood/urban_flood.py`) | **N/A -- door body.** Standard fetch DEM/buildings/precip -> build mesh -> `run_swmm_local` (in-process) -> postprocess -> publish; `swmm_urban_flood` calls it. | Called by `swmm/urban_flood/urban_flood.py:297` AND imported by the solver-confirm gate `gates/cards/solver_confirm.py:952` (pure-extraction preview); 9 test files (most-coupled internal body). Not registered. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body; fold must re-point the gate import + ~9 test imports. Engine run survives. |
| `model_river_dye_release_scenario` | No (internal) | TELEMAC-2D river-dye release (animated SELAFIN mesh + peak COG) -- same question as its door | Door: `telemac_river_dye` (`telemac/river_dye/river_dye.py`) | **N/A -- door body.** geocode -> `fetch_river_geometry` -> stage -> `run_solver('telemac_river_dye')` -> download SELAFIN -> postprocess -> publish (native-mesh deliverable, no per-frame COGs). `telemac_river_dye` calls it. | Called only by `telemac/river_dye/river_dye.py:512`; 1 test file (`test_run_river_dye_scenario.py`). Not registered. | **CULL-CANDIDATE (fold/rename)** -- 1:1 door body, overfit "river dye release" name. Engine run survives. |

### Note on "CULL" for the 7 internal siblings

These are load-bearing bodies, not dead code. "CULL-CANDIDATE" here is a
**module-consolidation** recommendation (fold the body into its door file, or
rename it to a generic `model_<engine>_<template>` matching the door), NOT a
capability deletion. Deleting the function outright would break the door. The
counter-argument to folding is modularity: each body is 250-1000 LOC of
orchestration cleanly separated from a thin surface door, and folding produces
one large file. If NATE prefers to keep the split, the minimal cleanup is the
**rename** (drop the overfit scenario name) so the module name matches the
now-generic template. NATE decides fold-vs-rename-vs-leave.

## Ledger rows (QUEUED)

Draft rows for `docs/DELETION_LEDGER.md` (NOT written here -- report-only). Each
condition is a FOLD/RENAME, never an engine-run deletion.

```
model_fire_spread_scenario | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the elmfire_fire_spread door; overfit scenario name) | CONDITION-to-delete: fold the body into elmfire/fire_spread/fire_spread.py (or rename to model_elmfire_fire_spread) and re-point the 1 test import (test_model_fire_spread_chain.py) | reopen: a 2nd elmfire template needs to share this exact orchestration body (then keep it as a generically-named shared helper)
model_dambreak_geoclaw_scenario | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the geoclaw_inundation door; overfit "dambreak" name) | CONDITION-to-delete: fold into geoclaw/inundation/inundation.py (or rename to model_geoclaw_inundation) and re-point 3 test imports | reopen: a 2nd geoclaw template needs this exact body
model_landslide_scenario | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the landlab_susceptibility door; overfit "landslide" name) | CONDITION-to-delete: fold into landlab/susceptibility/susceptibility.py (or rename to model_landlab_susceptibility) and re-point 2 test imports | reopen: a 2nd landlab template needs this exact body
model_seismic_hazard_scenario | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the openquake_psha door; overfit "seismic hazard" name) | CONDITION-to-delete: fold into openquake/psha/psha.py (or rename to model_openquake_psha) and re-point ~7 test imports (heaviest coupling) | reopen: a 2nd openquake template needs this exact body
model_wave_scenario | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the swan_wave_field door; overfit "wave scenario" name) | CONDITION-to-delete: fold into swan/wave_field/wave_field.py (or rename to model_swan_wave_field) and re-point 2 test imports (incl. test_publish_manifest_register_only_phase4 source-inspection) | reopen: a 2nd swan template needs this exact body
model_urban_flood_swmm | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the swmm_urban_flood door; overfit scenario name) | CONDITION-to-delete: fold into swmm/urban_flood/urban_flood.py (or rename to model_swmm_urban_flood) and re-point the solver_confirm gate import + ~9 test imports | reopen: a 2nd swmm template needs this exact body
model_river_dye_release_scenario | QUEUED | CULL-CANDIDATE per composer characterization (1:1 private body of the telemac_river_dye door; overfit "river dye release" name) | CONDITION-to-delete: fold into telemac/river_dye/river_dye.py (or rename to model_telemac_river_dye) and re-point 1 test import | reopen: a 2nd telemac template needs this exact body
```

## Summary count

**3 KEEP, 7 CULL-CANDIDATE (fold/rename, not capability deletion).**
