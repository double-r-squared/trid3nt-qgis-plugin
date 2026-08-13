# SFINCS workflow-folder audit

Read-only audit of `server/src/trid3nt_server/agent/workflows/sfincs/` and its
coupled seams. Branch `refactor/engine-doors`. No code changed.

Scope: NATE's smell-test on `workflows/sfincs/` -- reference-scenario-era remnants,
non-conforming folder layout, and the 5,637-line `flood/flood.py` mini-monolith.

---

## 0. Verdicts at a glance

| Axis | Verdict |
|---|---|
| Layout conformance | **PARTIAL** -- template shells conform; engine root is a support-module dumping ground with mis-named + mis-located shared hubs and NO `run_sfincs.py` (solve dispatch is buried inside `flood.py`). |
| reference scenario remnant count | **~22 remnant sites** across 6 files (docstrings/comments). Most ride ON load-bearing engineering prose (constraint-keep the module, cut the demo framing); a handful are pure narrative that dies; a few are stale refs to a **culled** composer. |
| `flood.py` split | 5,637 lines = **~1,100 lines dead Batch-era scaffold (delete)** + a forcing-autowire library (~1,200 lines -> engine-support module) + a solve/progress/telemetry layer (~600 -> `run_sfincs.py` + `shared`) + the ~2,000-line orchestrator + the thin wrapper. |
| Per-path | pluvial **LIVE**; coastal-surge / spiderweb / river / compound / breach / tsunami **DEGRADED-LIVE** (regular-grid local-docker deck emits the forcing; no waves); quadtree + SnapWave waves **DEAD** (Batch arm removed); build-offload **DEAD**; habitat **DEAD** (culled, stale refs only). |
| Coupling | **`postprocess_flood.py` is a 5-engine shared hub** (ELMFIRE/GeoClaw/TELEMAC/SWAN/SWMM import it) + `sfincs_builder.load_manning_mapping` + `manning_mapping.csv` shared into SWMM. The shared-helper-hub problem NATE flagged is REAL and unresolved. |
| Biggest surprise | **~1,100 lines of `flood.py` (roughly lines 679-1935) are unreachable dead code** -- two composer functions (`_compose_and_upload_deckbuild_spec`, `_compose_and_upload_flood_build_spec`) and the entire wave-boundary / S3-staging subtree they root are **never called** since the AWS Batch arm was removed. The `services/workers/sfincs_deckbuilder/` worker (with Dockerfile + tests) is orphaned against a dispatch that no longer exists. |

---

## 1. Layout conformance

### 1.1 The pinned template layout (from ADR 0034 + 0025, and the conforming engines)

Every conforming engine (`modflow`, `swmm`, `landlab`, `geoclaw`, `telemac`,
`swan`, `elmfire`, `openquake`) uses this shape under `workflows/<engine>/`:

```
<engine>/
  __init__.py
  _template_card.py                 # shared TemplateCard dataclass
  run_<engine>.py                   # workflow-side solve dispatch / door body
  postprocess_<engine>.py           # engine-named postprocess
  <engine>_mesh.py | <engine>_builder.py | <engine>_hyetograph.py   # engine support
  <template>/                       # folder-per-template
    __init__.py
    <template>.py                   # SAME-NAMED module (the @register_tool wrapper)
    corpus.yaml                     # retrieval phrasings (template tier)
  model_<case>_scenario/            # OPTIONAL Case composer (no corpus.yaml; matches modflow)
    __init__.py
    model_<case>_scenario.py
```

Confirmed reference: `modflow/` root = `modflow_mesh.py`, `postprocess_modflow.py`,
`run_modflow.py`, `_template_card.py`; `swmm/` root adds `swmm_hyetograph.py` +
`swmm_mesh_builder.py`. Composer folders (`modflow/model_groundwater_contamination_scenario/`)
carry NO `corpus.yaml` -- so a composer without a corpus is CONVENTIONAL, not a defect.

### 1.2 SFINCS folder mapped against it

| File | Lines | Conforms? | Note |
|---|---:|---|---|
| `__init__.py` | 0 | yes | empty package marker (matches others) |
| `_template_card.py` | 30 | yes | correct engine-support placement |
| `flood/__init__.py` | 0 | yes | template package marker |
| `flood/flood.py` | 5,637 | **shape yes / size NO** | folder-per-template + same-named + corpus present, but a 5.6k-line monolith |
| `flood/corpus.yaml` | 6 | yes | template-tier phrasings, correct |
| `sfincs_builder.py` | 2,851 | name yes / **size NO** | `<engine>_builder` naming OK; second mini-monolith |
| `sfincs_forcing_adapter.py` | 1,322 | yes | `<engine>_`-prefixed engine support, correct placement |
| `sfincs_spiderweb.py` | 771 | yes | `<engine>_`-prefixed engine support, correct placement |
| `postprocess_flood.py` | 1,141 | **NO** | mis-named (should be `postprocess_sfincs.py`) AND is a cross-engine shared hub (belongs in `shared/`) |
| `postprocess_waves.py` | 328 | **NO** | hazard-named not engine-named; body is DEAD (SnapWave path removed) -- see 4 |
| `manning_mapping.csv` | 85 | **NO** | data asset at engine root, shared into SWMM; belongs in `shared/` (or a `data/` dir) |
| `model_nws_flood_event_scenario/` | 966 | mostly | composer folder, no `corpus.yaml` (conventional for composers) BUT it registers an LLM tool `run_model_nws_flood_event_scenario`, so it arguably wants retrieval phrasings; self-labeled "Case 3 DEMO composer" |
| (missing) `run_sfincs.py` | -- | **NO** | every other engine factors solve dispatch into `run_<engine>.py`; SFINCS buries it inside `flood.py` |

### 1.3 Conformance verdict

The **template shell conforms** (folder-per-template, same-named module,
`corpus.yaml`, `_template_card.py`). The **engine root does not**: it is a flat
pile of oversized support modules where (a) the solve-dispatch layer that other
engines isolate in `run_<engine>.py` is fused into the template body, (b) the
postprocess module is hazard-named and is actually a shared 5-engine hub, and
(c) a shared data asset sits at engine root. A conforming reorg extracts the
shared hub to `shared/`, renames the engine postprocess, adds `run_sfincs.py`,
and breaks the two monoliths (`flood.py`, `sfincs_builder.py`) into cohesive
modules -- see 3 and 6.

---

## 2. reference scenario remnant census

Three-way verdict per NATE's frame: **constraint-keep** (demo framing rides on
load-bearing engineering prose -- cut the framing sentence, keep the module),
**dies** (pure demo narrative, no engineering content), **genuine-decision**
(names a real design constraint that happens to cite the demo -- keep).

The recall pass held tool/workflow ROUTING docstrings sacred; these are the
NARRATIVE sections it left behind. All are comments/docstrings -- **none affects
runtime**.

| # | Site | Text (abridged) | Verdict |
|---|---|---|---|
| 1 | `sfincs_forcing_adapter.py:3` | "THE GAP THIS FILLS (COASTAL SFINCS reference scenario, Mexico Beach / Hurricane Michael):" | **constraint-keep** -- the sentence dies; the rest of the docstring (the hydromt-sfincs 1.2.2 CSV/locations format contract, time re-anchoring) is load-bearing and stays |
| 2 | `sfincs_forcing_adapter.py:59` | "the fetchers carry REAL event timestamps (Hurricane Michael = Oct 2018)" | **constraint-keep** -- reword to a generic example; the re-anchoring rule is real |
| 3 | `sfincs_spiderweb.py:3` | "The COASTAL SFINCS wind track:" (+ zone-16n / Mexico Beach de-risk log) | **genuine-decision** -- the de-risk block (exact `sfincs.inp` lines the binary accepted) is real evidence; drop "reference scenario", keep the contract |
| 4 | `postprocess_waves.py:17` | "the visibly-animating SnapWave wave field on the Mexico Beach (Hurricane Michael) coastal reference scenario demo" | **dies** -- pure demo framing on a module whose runtime path is DEAD (see 4) |
| 5 | `sfincs_builder.py:179` | "exactly the forcing path the COASTAL SFINCS reference scenario needs" | **constraint-keep** -- the pandas `is_integer` shim it justifies is real; reword the "why" |
| 6 | `sfincs_builder.py:305,372,594` | "COASTAL SFINCS reference scenario: the surge / tide hydrograph ..." | **constraint-keep** -- `WaterlevelForcing`/`WindForcing` dataclass docs; strip the epithet |
| 7 | `sfincs_builder.py:2282,2357` | "advanced / demo-default", "demonstrates the ..." | **constraint-keep** -- reword; the constitutive-physics lever is real |
| 8 | `flood.py:2103` | "The COASTAL SFINCS reference scenario couples surge / tide / discharge / wind / pressure" | **constraint-keep** -- `_build_surge_forcing_members` docstring; the dict-shape contract stays |
| 9 | `flood.py:2320-2321` | "Anchored to published Gulf-coast storm-tide observations (Hurricane Michael at Mexico Beach peaked near 4 m NAVD88)" | **genuine-decision** -- the parametric surge-scaling anchor is a real calibration citation; keep as a numeric provenance note, drop "reference scenario" |
| 10 | `flood.py:1354,3350,3377,3473,3481,3665,4762,4916,5087` | recurring "SFINCS reference scenario P1", "coastal reference scenario", "Mexico Beach ... reference scenario" in `model_flood_scenario` param docs + inline comments | **mostly dies / some constraint-keep** -- the epithet dies everywhere; where it labels a still-live branch (coastal-AOI detection, cadence) keep the branch note without the demo name |
| 11 | `flood.py:2320`, `postprocess_waves`, `sfincs_spiderweb` "Michael offshore Hs ~ 8-10 m" (`flood.py:808`) | parametric wave magnitudes cited from Michael | **constraint-keep on live, dies on dead** -- wave magnitudes feed the DEAD wave-boundary synth (delete with it); surge magnitudes feed the LIVE surge scaling (keep, reword) |
| 12 | `model_nws_flood_event_scenario.py:3` | "The **Case 3 demo composer**" | **genuine-decision (reword)** -- it is a registered, LLM-reachable capability (NWS alert -> MRMS -> flood); drop "demo", it is a real tool |
| 13 | `flood.py:379` | "THE FIX for the demo-breaking 'run hangs / goes dark' symptom" | **constraint-keep** -- the pre-solver timeout is real hardening; reword "demo-breaking" |
| 14 | `flood.py:3767` | "the v0.1 HUC4 heuristic only covers a few demo areas" | **constraint-keep** -- documents a real degrade (river-fetch optional); reword |
| 15 | stale refs to `run_model_flood_habitat_scenario` in `flood.py:5570`, `model_nws...:942`, `publish_layer.py:2007`, `model_conservation_priority.py:473`, `modflow ...:5` | cross-references to a composer that was **culled** | **dies** -- the composer no longer exists (see 4); these are stale doc pointers to a dead symbol |

Remnant total: **~22 comment/docstring sites** (grouped rows 10-11 expand to
~10 individual lines). The dominant pattern is #1/#8-style: a live engineering
contract wearing a demo-era epithet -- **cut the epithet, keep the module.**

---

## 3. `flood.py` decomposition map (5,637 lines)

Composition stages, top to bottom, with live/dead status and split target:

| Lines | Stage | Contents | Status | Target |
|---:|---|---|---|---|
| 166-426 | **live-solve progress / cadence** | `_emit_presolver_progress`, `_resolve_output_interval_min`, `_estimate_frame_count`, `_extract_solve_autoscale`, `_drive_live_solve_progress`, `_drive_presolver_phase_progress` | LIVE | mostly `shared/solve_progress.py` (exists) + `run_sfincs.py`; cadence stays SFINCS-support |
| 428-676 | **errors / envelope / telemetry** | `WorkflowError`, `_resolve_bbox`, `_build_failed_envelope`, `_bbox_area_km2`, `_emit_flood_solve_telemetry`, `_record_flood_batch_solve_telemetry`, `_default_runs_prefix` | LIVE | `run_sfincs.py` (dispatch/telemetry) + a small `flood/_envelope.py` |
| **679-1273** | **DEAD: S3 forcing upload + parametric wave boundary** | `_is_remote_object_uri`, `_upload_local_forcing_files_to_s3`, `_sample_dem_depth_m`, `_wave_storm_envelope_factor`, `_parametric_wave_hs_m`, `WaveBoundaryError`, `_depth_aware_offshore_points`, `_synthesize_parametric_wave_boundary` | **DEAD** (only reachable from the never-called deckbuild composer) | **DELETE** (or quarantine into a dormant coastal-quadtree module if the template is planned) |
| **1274-1635** | **DEAD: quadtree deckbuild spec compose+upload** | `_compose_and_upload_deckbuild_spec` (+ `_pick_time`) | **DEAD** -- **never called**; targets removed `build_sfincs_quadtree_deck` | **DELETE** |
| **1636-1935** | **DEAD: regular-grid build-offload spec** | `_sfincs_build_offload_enabled`, `_forcing_member_to_dict`, `_forcing_spec_to_dict`, `_build_options_to_dict`, `_stage_local_forcing_files_full`, `_compose_and_upload_flood_build_spec` | **DEAD** -- `_compose_and_upload_flood_build_spec` **never called**; gated by unset `TRID3NT_SFINCS_BUILD_OFFLOAD` + removed Batch arm | **DELETE** (keep the `*_to_dict` serializers only if a live caller emerges) |
| 1936-2087 | **precip forcing** | `PrecipForcingError`, `compute_precip_area_mean_mm_per_hr` | LIVE (observed-raster branch) | engine-support `sfincs_forcing_*` |
| 2088-2311 | **surge member construction** | `_build_surge_forcing_members`, `_resolve_surge_forcing_from_fetchers` | LIVE (emitted on regular deck) | engine-support (fold into `sfincs_forcing_adapter.py`) |
| 2312-3243 | **forcing synthesis + autowire** | `_parametric_surge_peak_m`, `_synthesize_parametric_surge_forcing`, `_autowire_coastal_surge_forcing`, `_synthesize_tide_base_forcing`, `_resolve_spiderweb_forcing`, `_resolve_building_obstacle_uri`, `_autowire_river_discharge_forcing`, `_resolve_infiltration_uri`, `_synthesize_breach_discharge_forcing`, `_synthesize_tsunami_waterlevel_forcing`, `_resolve_quadtree_rivers_uri` | LIVE except `_resolve_quadtree_rivers_uri` (dead) | **new `sfincs_forcing_autowire.py`** engine-support module (the single biggest cohesive extraction) |
| 3244-5292 | **the orchestrator** | `model_flood_scenario` (~2,050 lines) | LIVE (core) | `flood/flood.py` keeps this (slimmed) OR `flood/_orchestrator.py` |
| 5293-5637 | **LLM wrapper** | `TEMPLATE_CARD`, `sfincs_flood` (`@register_tool`) | LIVE | `flood/flood.py` (the template surface) |

### One-line split proposal

> Delete ~1,100 lines of never-called Batch-era deckbuild/offload/wave-boundary
> scaffold (lines ~679-1935); lift the ~1,200-line forcing-autowire/synthesis
> library into a new engine-support `sfincs_forcing_autowire.py`; move the
> solve-dispatch/telemetry/progress layer into a new `run_sfincs.py` (+ the
> existing `shared/solve_progress.py`); leaving `flood/flood.py` as the ~2,300-line
> `model_flood_scenario` orchestrator + `sfincs_flood` wrapper.

Template-specific vs engine-support vs shared:
- **Template-specific** (stays in `flood/`): `sfincs_flood` wrapper, `TEMPLATE_CARD`, the `model_flood_scenario` orchestrator body.
- **Engine-support** (SFINCS root): all forcing synthesis/autowire, surge member construction, precip area-mean, `run_sfincs.py` dispatch.
- **Shared** (`workflows/shared/`): progress/telemetry primitives (partly already there), bbox/area helpers.

---

## 4. Dead / forced-path analysis

Load-bearing facts:
- `solver_backend()` is **hardwired** to `local-docker`; the AWS Batch arm was
  removed (`agent/tools/simulation/solver/solver.py:355-360` -- "The AWS Batch
  arm has been removed (local-only slim)").
- `build_sfincs_quadtree_deck` / `run_sfincs_quadtree` **no longer exist** in the
  server package (grep: zero defs).
- In `flood.py`, `quadtree_run_result` and `build_solve_run_result` are declared
  `None` and **never reassigned** (`flood.py:4525-4534`, "quadtree combined-Batch
  path removed ... sentinels stay wired (always None here)").
- Surge/discharge/wind/pressure/spiderweb blocks ARE emitted into the regular
  deck: `sfincs_builder._emit_surge_forcing_blocks` (called at `sfincs_builder.py:2336`)
  and `_emit_spiderweb_config` (called at `:2217`) both fire inside the regular
  `build_sfincs_model` deck-compose path.

| Path | Trigger | Live behavior | Verdict |
|---|---|---|---|
| **Pluvial** (design-storm / observed raster) | default; `forcing_raster_uri` for observed | full 9-step fetch -> regular deck -> local-docker solve -> `postprocess_flood` | **LIVE** (primary, ~50 test files reference it) |
| **Coastal surge** (`coastal=True` / `surge_forcing` / topobathy) | `is_coastal` at `flood.py:3496` | routes DEM through `fetch_topobathy`, autowires a CO-OPS/GTSM/parametric water-level boundary (`_autowire_coastal_surge_forcing`, `flood.py:4168`), emits `setup_waterlevel_forcing` on the **regular grid**, solves local-docker | **DEGRADED-LIVE** -- surge forcing runs; the wave coupling it advertises does NOT (see quadtree). A coastal run yields a surge-forced depth flood, no waves. Tests: `test_coastal_forcing_offloop.py`, `test_sfincs_builder_surge_forcing.py` (forcing construction, not full solve) |
| **Spiderweb** (`storm_name`+`storm_season` / `storm_track_uri`) | `storm_requested` at `flood.py:3493` | resolves IBTrACS track, writes a Holland `.spw`, `_emit_spiderweb_config` emits `spwfile/utmzone/baro` into the regular `sfincs.inp`; de-risk log (`sfincs_spiderweb.py:20-27`) shows the `deltares/sfincs-cpu` binary accepting it with nonzero response | **DEGRADED-LIVE** -- reachable on the regular-grid local-docker deck; not gated on Batch. Test: `test_fetch_storm_tracks.py` |
| **River / fluvial** (`river=True`) | `flood.py:3495` | autowires NWM->NWIS discharge boundary, emits `setup_discharge_forcing` | **LIVE** |
| **Compound** (`compound=True`) | lifts `coastal`+`river` | surge + discharge + precip on one regular deck | **DEGRADED-LIVE** (no waves) |
| **Levee-breach** (`breach_point`+`breach_peak_discharge_m3s`) | user-gated | synthesizes triangular interior jet, emits discharge | **LIVE-conditional** (typed `USER_INPUT_REQUIRED` without magnitude) |
| **Tsunami** (`tsunami=True`+`tsunami_wave_height_m`) | user-gated; implies coastal | N-wave water-level boundary on regular deck | **LIVE-conditional**. Test: `test_fetch_tsunami_events.py` |
| **Infiltration** (`infiltration`) | best-effort GCN250 | emits infiltration loss | **LIVE** |
| **Building obstacles** (`building_obstacles`) | OSM footprints burned to grid | exclude-mask / subgrid-raise | **LIVE** |
| **Quadtree + SnapWave waves** (`quadtree=True`; auto-forced by coastal in the docstring) | -- | `quadtree_run_result` is hardwired `None`; the combined deck-build+solve was REMOVED; `postprocess_waves` call at `flood.py:5097` is gated on `quadtree_run_result is not None` and therefore **never runs** | **DEAD** -- DEMO-SCAFFOLD. `_compose_and_upload_deckbuild_spec` + wave-boundary synth (`flood.py:679-1635`) + `postprocess_waves.py` (328 lines) + `services/workers/sfincs_deckbuilder/` (worker + Dockerfile + tests) are all orphaned. **Candidate: a real `sfincs_coastal_surge` / wave template someday** (the hydromt-sfincs source NATE provided covers it), or delete until then |
| **Build-offload** (`TRID3NT_SFINCS_BUILD_OFFLOAD`) | env flag, unset | `_compose_and_upload_flood_build_spec` **never called**; needs removed Batch arm | **DEAD** |
| **Habitat** (`run_model_flood_habitat_scenario`) | -- | composer **culled**; survives only as stale docstring cross-refs in 5 files | **DEAD** (post-cull remnant) |

Net: the coastal/surge/spiderweb machinery NATE quoted is **not** pure
Mexico-Beach scaffolding -- the FORCING half is degraded-LIVE on the regular
local-docker deck. It is only the **quadtree grid + SnapWave wave** half that is
dead demo-scaffold, and it drags ~1,100 lines of `flood.py` + an orphaned worker
with it.

---

## 5. Coupling (the shared-helper-hub problem)

What OUTSIDE `workflows/sfincs/` imports from it:

| Importer | Imports | Symbol(s) |
|---|---|---|
| `workflows/elmfire/postprocess_elmfire.py:60` | `sfincs.postprocess_flood` | `RUNS_BUCKET_DEFAULT` |
| `workflows/geoclaw/postprocess_geoclaw.py:61` | `sfincs.postprocess_flood` | raster/COG helpers |
| `workflows/telemac/postprocess_telemac.py:52` | `sfincs.postprocess_flood` | `RUNS_BUCKET_DEFAULT` |
| `workflows/swan/postprocess_swan.py:56,61` | `sfincs.postprocess_flood` + `sfincs.postprocess_waves` | COG helpers + `NODATA_WAVE_M` |
| `workflows/swmm/postprocess_swmm.py:63` | `sfincs.postprocess_flood` | COG helpers |
| `workflows/swmm/swmm_mesh_builder.py:119` | `sfincs.sfincs_builder` | `load_manning_mapping` (+ `manning_mapping.csv`) |
| `workflows/shared/frames.py:48,121` | -- | names itself with the `sfincs.postprocess_flood` logger; the frame-select helper was extracted TO shared but still points back |
| `agent/gates/cards/solver_confirm.py:277-282` | `sfincs.flood.flood` + `sfincs.postprocess_flood` + `sfincs.sfincs_builder` | `MAX_FLOOD_FRAMES`, builder consts |
| `agent/tools/meta/open_case_in_qgis/...:303` | `sfincs.postprocess_flood` | `_read_crs_from_dataset` |
| `main.py:67` | `workflows.sfincs.flood.flood` | import-for-registration only |

**Verdict: the shared-helper-hub problem is REAL and unresolved.**
`postprocess_flood.py` is imported by **five sibling engines** for
`RUNS_BUCKET_DEFAULT`, the COG-write/read seams, and `MAX_FLOOD_FRAMES`;
`postprocess_waves.py` leaks `NODATA_WAVE_M` to SWAN; `sfincs_builder` +
`manning_mapping.csv` are the shared NLCD->Manning table for SWMM. A partial
extraction already happened (`shared/frames.py`, `shared/cog_io.py`,
`shared/publish_quantities.py`, `shared/solve_progress.py`,
`shared/register_published_manifest.py`), but the raster/bucket seam and the
Manning table were left behind under `sfincs/`. Sibling engines therefore take a
hard dependency on a SFINCS-named module -- the exact coupling smell NATE
recalled from the earlier `postprocess_flood` restructure.

Fix direction: hoist the cross-engine surface (`RUNS_BUCKET_DEFAULT`, COG
read/write, `_read_crs_from_dataset`, `NODATA_WAVE_M`, `MAX_FLOOD_FRAMES`, the
Manning table + CSV) into `workflows/shared/` (extend `cog_io.py` /
`publish_quantities.py`, add `shared/manning.py` + `shared/data/manning_mapping.csv`),
leaving `postprocess_flood.py` (renamed `postprocess_sfincs.py`) importing FROM
shared like every other engine does.

---

## 6. Proposed target layout

```
workflows/sfincs/
  __init__.py
  _template_card.py
  run_sfincs.py                       # NEW: solve dispatch + telemetry + progress
                                      #      (lifted from flood.py 428-676 + parts of 166-426)
  sfincs_builder.py                   # (still to be broken down -- secondary monolith, 2,851 ln)
  sfincs_forcing_adapter.py           # keep (engine support)
  sfincs_forcing_autowire.py          # NEW: forcing synthesis + autowire
                                      #      (lifted from flood.py 1936-3243, minus dead bits)
  sfincs_spiderweb.py                 # keep (engine support)
  postprocess_sfincs.py               # RENAMED from postprocess_flood.py; imports raster
                                      #   seams FROM shared instead of owning them
  flood/
    __init__.py
    flood.py                          # ~2,300 ln: model_flood_scenario + sfincs_flood wrapper
    corpus.yaml
  model_nws_flood_event_scenario/
    __init__.py
    model_nws_flood_event_scenario.py
    corpus.yaml                       # ADD (it registers an LLM tool)

workflows/shared/                     # hoisted cross-engine surface
  cog_io.py                           # + RUNS_BUCKET_DEFAULT, _read_crs_from_dataset, MAX_FLOOD_FRAMES, NODATA_WAVE_M
  manning.py                          # + load_manning_mapping
  data/manning_mapping.csv            # moved from sfincs/

DELETE (dead Batch-era scaffold):
  flood.py lines ~679-1935            # deckbuild/offload/wave-boundary composers (never called)
  postprocess_waves.py                # SnapWave path removed (or quarantine if wave template is planned)
  services/workers/sfincs_deckbuilder/# orphaned GPL worker + Dockerfile + tests
```

---

## 7. Cut / keep / template-ize -- action lists (with evidence)

### CUT (dead code, safe to delete)
- `flood.py` `_compose_and_upload_deckbuild_spec` (1274) -- **never called** (1 mention = its def).
- `flood.py` `_compose_and_upload_flood_build_spec` (1795) -- **never called**.
- `flood.py` `_synthesize_parametric_wave_boundary` (1076), `_depth_aware_offshore_points` (979), `_wave_storm_envelope_factor` (932), `_parametric_wave_hs_m` (949), `WaveBoundaryError` (965), `_sample_dem_depth_m` (845), `_upload_local_forcing_files_to_s3` (718), `_is_remote_object_uri` (706) -- reachable ONLY from the two never-called composers.
- `flood.py` `_stage_local_forcing_files_full` (1737), `_resolve_quadtree_rivers_uri` (3204) -- Batch/quadtree-only.
- `postprocess_waves.py` (328) -- call site gated on always-`None` `quadtree_run_result` (`flood.py:5097`).
- `services/workers/sfincs_deckbuilder/` -- dispatch removed from server; `build_sfincs_quadtree_deck` gone.
- Stale `run_model_flood_habitat_scenario` docstring refs in `flood.py:5570`, `model_nws...:942`, `publish_layer.py:2007`, `model_conservation_priority.py:473`, `modflow...:5` -- symbol is culled.

### KEEP (constraint-keep -- strip demo epithet, retain engineering content)
- `sfincs_forcing_adapter.py` whole module (hydromt-sfincs format contract) -- cut line 3's "THE GAP THIS FILLS ... Mexico Beach / Hurricane Michael" header only.
- `sfincs_spiderweb.py` (Holland `.spw` + the byte-exact `sfincs.inp` de-risk log) -- keep; drop "COASTAL SFINCS ... reference scenario".
- `sfincs_builder.py` surge/wind dataclasses, the pandas `is_integer` shim, constitutive-physics levers -- reword the reference-scenario/demo asides.
- `flood.py` surge scaling anchored to "Hurricane Michael at Mexico Beach ~4 m NAVD88" (2320-2321) -- keep as a numeric provenance note (it calibrates the LIVE `_parametric_surge_peak_m`), drop the epithet.
- The whole coastal-surge / spiderweb / river / compound / breach / tsunami FORCING path -- degraded-LIVE, keep.

### TEMPLATE-IZE (promote demo-scaffold to a real template someday)
- Quadtree + SnapWave wave coupling -> a solve-gated `sfincs_coastal_surge` (or `sfincs_coastal_waves`) TEMPLATE per ADR 0025 template-sourcing, grounded in the hydromt-sfincs example NATE provided. Until authored, CUT the scaffold (above) rather than carry dead code.
- `model_nws_flood_event_scenario` -> keep as a registered composer but (a) drop the "demo composer" self-label, (b) add a `corpus.yaml` so its `run_model_nws_flood_event_scenario` tool earns retrieval phrasings like every template.

### SECONDARY (out of this audit's headline but noted)
- `sfincs_builder.py` (2,851 ln) is a second mini-monolith (deck-compose emitters + Manning loader + build orchestration + surge/spiderweb emit). Same decomposition discipline applies; break the emitters out from `build_sfincs_model`.
