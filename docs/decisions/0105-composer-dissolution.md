# ADR 0105 -- Composer dissolution wave

Status: accepted (2026-08-04, NATE-decided)

Supersedes the overfit-era scenario-wrapper pattern for good: after ADR 0094
dissolved the 10 engine "doors", this wave dissolves the last overfit-era
scenario composers. Two moves, one wave.

## Context

The workflow surface still carried overfit-era residue from the North-Star /
scenario era (the original overfit mistake, ADR 0024):

- **7 internal `model_*` scenario BODIES** (`model_fire_spread_scenario`,
  `model_dambreak_geoclaw_scenario`, `model_landslide_scenario`,
  `model_seismic_hazard_scenario`, `model_wave_scenario`,
  `model_urban_flood_swmm`, `model_river_dye_release_scenario`) -- each the 1:1
  private orchestration body of exactly one registered engine template (the
  "door/body split" documented in `docs/validation/composer-cull-characterization.md`).
  Not dead, not redundant -- just a scenario-era NAME surviving under a
  now-generic engine template, plus an extra module boundary.

- **3 registered standalone composers** (`run_model_nws_flood_event_scenario`,
  `run_model_groundwater_contamination_scenario`, `compute_impact_envelope`) --
  a live-alert flood chain, a news-article spill-ingest chain, and a
  cross-tool damage-aggregation. Genuinely distinct question-archetypes, but
  archetypes the MODEL can compose itself from the surviving templates + the
  fetchers. NATE: the aggregation layer is the user's + the model's common
  sense, not a hand-written tool.

## Decision

### Part A -- FOLD the 7 bodies into their templates (one honest file each)

Each `model_*` body is inlined into its engine template module (the template
file becomes the whole pipeline), the scenario-era module + name is deleted,
and the composer is renamed to the generic engine-template name. Registered
tool names / docstrings / params stay byte-identical; the envelope shape is
unchanged (each engine's existing test set re-run):

| former body | folded into (template module) | new composer name |
|---|---|---|
| `model_fire_spread_scenario` | `elmfire/fire_spread/fire_spread.py` | `model_elmfire_fire_spread` |
| `model_dambreak_geoclaw_scenario` | `geoclaw/inundation/inundation.py` | `model_geoclaw_inundation` |
| `model_landslide_scenario` | `landlab/susceptibility/susceptibility.py` | `model_landlab_susceptibility` |
| `model_seismic_hazard_scenario` | `openquake/psha/psha.py` | `model_openquake_psha` |
| `model_wave_scenario` | `swan/wave_field/wave_field.py` | `model_swan_wave_field` |
| `model_urban_flood_swmm` | `swmm/urban_flood/urban_flood.py` | `model_swmm_urban_flood` |
| `model_river_dye_release_scenario` | `telemac/river_dye/river_dye.py` | `model_telemac_river_dye` |

Importers re-pointed: the `gates/cards/solver_confirm.py` SWMM + TELEMAC preview
imports, the shared test files (`test_worker_postprocess_offload`,
`test_input_layer_surfacing`, `test_live_drive_fixes_0104`), each engine's own
tests, the `SOLVER_WORKFLOW_REGISTRY` display labels in `solver.py`, and the
`scripts/run_*_direct.py` dev drivers. No engine run changed; registry count
UNCHANGED by the folds.

### Part B -- DELETE the 3 standalone composers outright

`run_model_nws_flood_event_scenario`, `run_model_groundwater_contamination_scenario`,
and `compute_impact_envelope` are removed: modules, registrations, corpus
entries, categories, the server confirm-gate branch + the ImpactPanel WS
emitter (`_maybe_emit_impact_envelope`), docstring cross-references, and tests
(deleted or migrated to surviving machinery). Registry 175 -> 172 (in-process;
daemon-startup 179 -> 176), coded tools -3.

**Judgment relocation** (the knowledge the composers encoded must survive where
the model sees it):

- *NWS alert-type -> flood-mode routing* (filter to Flood / Flash Flood
  Warning, take the highest-severity warning polygon as the AOI, pull observed
  MRMS precip, run `sfincs_flood` -- observed event, not a design storm; honest
  no-warning degrade) now lives in the system prompt (`adapter.py`, "LIVE NWS
  FLOOD WARNING" block) + a bullet in the `fetch_nws_alerts_conus` docstring.
- *Invariant-9 never-invent-contamination-params* (extract spill
  contaminant/location/amount/duration from the article or the user, derive the
  forcing, NEVER fabricate a release rate; ask when missing) now lives in the
  system prompt (`adapter.py` groundwater-routing block) and remains fully
  stated in the `modflow_contaminant_plume` docstring.

## Consequences

- The overfit-era scenario-wrapper pattern is fully retired. Engine templates
  are the atomic simulation surface; multi-source / news-ingest / aggregation
  archetypes are MODEL-composed from templates + fetchers.
- No engine capability was removed; the 7 folds are module consolidations.
- Retrieval: the 3 deleted names are gone from the index; the 20 templates stay
  rank-stable (fold docstrings byte-identical; `modflow_contaminant_plume`
  surfaces #1 for the spill-plume query; the live-flood query surfaces
  `fetch_nws_alerts_conus` + `sfincs_flood`).
- Offline suite baseline preserved (the documented failures unchanged in kind).
