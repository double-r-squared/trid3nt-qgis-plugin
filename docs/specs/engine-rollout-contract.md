# Engine-rollout contract - uniform slice plan for the remaining 9 engines

Status: FOR NATE REVIEW - CONTRACT LANE PIN. Nothing builds until the go.
Date: 2026-07-27. Branch: refactor/engine-doors.
Authority: docs/specs/engine-door-refactor.md (the CORRECTED 2026-07-26
terminology block is binding) + docs/specs/modflow-pilot-contract.md (the
reference implementation - every mechanism below is REUSED from it, not
redesigned). This document pins the exact per-engine slice plan for the nine
engines that follow the MODFLOW pilot, grounded in the LIVE registry
(`trid3nt_server.main._import_tools_registry()` -> `TOOL_REGISTRY`, 203
registered tools on this branch) and the `workflows/<engine>/` trees.

Binding terminology (restated from the corrected block, unchanged from the
pilot):

- DOOR = `run_<engine>`, a read-only CONCIERGE (tier=door). It (1) lists its
  engine's tier=template tools from the registry, (2) expands the turn's
  retrieval gate with those templates, (3) fidelity-briefs incl. mismatch
  redirection. It EXECUTES NOTHING. The reference implementation is
  `tools/simulation/modflow/run_modflow/` - every door below is a copy of that
  pattern with its own engine slug, corpus, fidelity brief, and redirect map.
- TEMPLATE = an individual REGISTERED TOOL tagged `engine=<engine>,
  tier=template`. It keeps its own schema / envelope / telemetry / direct-call
  testability / bench grading. EXCLUDED from the default retrieval pool,
  surfaced only by the door's gate expansion. SELECT-THEN-CALL. Registered
  names carry the engine (`<engine>_<question>`).
- Renames REPLACE old registered names (NO aliases). Fold ONLY on functional
  sameness; when in doubt, separate.

---

## 0. Shared machinery is DONE - reused as-is, not rebuilt

The MODFLOW pilot already landed every cross-engine seam. Each engine slice
below is a MECHANICAL application; NO engine re-invents any of these. Verified
present on this branch:

| seam | location | status |
|------|----------|--------|
| `engine` + `tier` metadata on `AtomicToolMetadata` | `contracts/src/trid3nt_contracts/tool_registry.py` | landed, backward-compatible defaults |
| tier=template EXCLUDED from the default pool | `tools/discovery/search_tools/search_tools.py` (index build) | landed |
| fail-open floor also filters tier=template | `tools/discovery/tool_retrieval.py` `_full_registry_floor` | landed |
| door recognition (registry-driven, ANY tier=door) | `server.py:1381 _gate_expander_tool_names()` | landed - a new door is picked up automatically |
| door expand cap (uncapped vs the 8-cap open-ended discovery) | `server.py:1335 _DOOR_EXPAND_CAP = 24` | landed |
| terminal-deliverable latch widened for tier=template | `server.py:1443 _is_terminal_composer` (`... or is_template`) | landed - covers EVERY non-`run_` template name below (RISK-1 from the pilot is CLOSED) |
| setter relocation precedent | `tools/simulation/modflow/set_modflow_parameters/` | landed - the template for the setter moves below |
| door concierge reference impl | `tools/simulation/modflow/run_modflow/` | landed - copy per engine |
| folder-per-template + co-located corpus.yaml | `workflows/modflow/<template>/` | landed - the tree shape below |

Consequence: this contract adds NO new shared machinery. It is renames +
re-tiers + one new door folder + one corpus split + confirm-hook re-keys per
engine. The only NON-mechanical decisions are the two flagged in section 3
(pelicun fold, engine-ambiguous tools).

---

## 1. The live engine families today (exact, from the registry)

MODFLOW is DONE (`run_modflow` door + 11 `modflow_*` templates registered
tier=template on this branch). The nine engines to roll:

| engine | registered engine tool(s) today | source_class | workflow body folder | param setter |
|--------|--------------------------------|--------------|----------------------|--------------|
| sfincs | `run_model_flood_scenario` | workflow_dispatch | `workflows/sfincs/model_flood_scenario/` | `set_sfincs_parameters` |
| telemac | `run_telemac` | workflow_dispatch | `workflows/telemac/model_river_dye_release_scenario/` | `set_telemac_parameters` |
| swmm | `run_swmm_urban_flood` | workflow_dispatch | `workflows/swmm/model_urban_flood_swmm/` | `set_swmm_parameters` |
| geoclaw | `run_geoclaw_inundation` | workflow_dispatch | `workflows/geoclaw/model_dambreak_geoclaw_scenario/` | none |
| swan | `run_swan_waves` | workflow_dispatch | `workflows/swan/model_wave_scenario/` | none |
| landlab | `run_landlab_susceptibility` | workflow_dispatch | `workflows/landlab/model_landslide_scenario/` | none |
| openquake | `run_seismic_hazard_psha` | workflow_dispatch | `workflows/openquake/model_seismic_hazard_scenario/` | none |
| elmfire | `model_fire_spread` | workflow_dispatch | `workflows/elmfire/model_fire_spread_scenario/` | none |
| pelicun | `run_pelicun_damage_assessment` + `run_pelicun_with_buildings` | pelicun_damage / workflow_dispatch | `workflows/pelicun/pelicun_damage_with_buildings/` | none |

Architecture note: today the registered engine tool lives in
`tools/simulation/run_<engine>_tool.py` (a thin wrapper) and the workflow BODY
lives in `workflows/<engine>/model_*_scenario/`. The rollout MOVES the
registration into the folder-per-template body (`workflows/<engine>/<template>/`,
re-tagged `engine=<engine>, tier=template`) exactly as the pilot did, and adds
the NEW door under `tools/simulation/<engine>/run_<engine>/`.

Composers that STAY general (consumers of an engine template, NOT templates -
direct precedent = MODFLOW's `run_model_groundwater_contamination_scenario`
news composer, which stayed registered general): `run_model_flood_habitat_scenario`
(sfincs flood + habitat overlay; internally calls `run_model_flood_scenario`),
`run_model_nws_flood_event_scenario` (NWS alert -> MRMS -> `model_flood_scenario`;
news/forecast-driven), `compute_impact_envelope` (flood -> inventory -> Pelicun
-> envelope composition, under `workflows/pelicun/`).

---

## 2. Per-engine slice pins

Uniform naming (the pilot's rule): folders imply the engine (no prefix stutter);
the registered door name carries the engine (`run_<engine>`); the template name
says the question (`<engine>_<question>`); the file is named after its folder;
`corpus.yaml` co-located at every level; template corpus is TEMPLATE tier (NOT
merged into the main index - it stays under `workflows/`, which the index walk
does not reach).

### 2.1 SFINCS - door `run_sfincs`

- Fidelity brief: reduced-physics fast SCREENING flood (SFINCS; coastal /
  riverine / watershed inundation; subgrid + quadtree; planning-grade, not a
  calibrated regulatory model). Mismatch redirect: urban storm-sewer flooding
  -> `run_swmm`; groundwater plume -> `run_modflow`; tsunami / dam-break run-up
  -> `run_geoclaw`; coastal spectral wave field -> `run_swan`; river dye /
  tracer transport -> `run_telemac`.
- Template map: `run_model_flood_scenario` -> `sfincs_flood`
  (`workflows/sfincs/flood/flood.py`). The large parameter surface (surge,
  coastal, quadtree, compound, wind, breach, infiltration, building_obstacles)
  rides through unchanged.
- Setter relocation: `set_sfincs_parameters` ->
  `tools/simulation/sfincs/set_sfincs_parameters/` (stays tier=general).
- STAYS general (NOT templates): `run_model_flood_habitat_scenario`,
  `run_model_nws_flood_event_scenario` - both consume `sfincs_flood`; migrate
  their internal dispatch name reference only (below).
- Special case (HOT_SET): SFINCS is the ONLY engine in `HOT_SET_TOOLS`. Replace
  `run_model_flood_scenario` -> `run_sfincs` (the door) so the always-on flood
  hot-path survives the pool exclusion of `sfincs_flood`.
  `run_model_flood_habitat_scenario` stays general and MAY remain in HOT_SET or
  be dropped in favor of the door (low-stakes, NATE call - flagged OPEN-C).

### 2.2 TELEMAC - door `run_telemac` (name flip)

- Fidelity brief: full-physics 2D depth-averaged shallow water (TELEMAC-2D;
  unstructured mesh; a river DYE / TRACER / CONTAMINANT plume that travels
  downstream in surface water). Mismatch redirect: groundwater plume ->
  `run_modflow`; surface inundation flooding -> `run_sfincs`; tsunami /
  dam-break -> `run_geoclaw`.
- SPECIAL CASE (name flip, task-pinned): the current `run_telemac` IS the
  river-dye scenario. Its BODY re-tiers to the template
  `telemac_river_dye` (`workflows/telemac/river_dye/river_dye.py`,
  `engine=telemac, tier=template`); the DOOR takes over the `run_telemac` name
  (tier=door, executes nothing). The confirm gate that keys on `run_telemac`
  today (BK-3b mesh-approve) MUST re-key to `telemac_river_dye` (the template
  is what submits the solver; the door runs no solve) - see section 4.
- Setter relocation: `set_telemac_parameters` ->
  `tools/simulation/telemac/set_telemac_parameters/`.

### 2.3 SWMM - door `run_swmm`

- Fidelity brief: 1D / quasi-2D urban drainage network (PySWMM; node-link storm
  sewer + pluvial overland; buildings as obstruction, walls as blocked links,
  flap gates native). Mismatch redirect: coastal / riverine inundation ->
  `run_sfincs`; groundwater -> `run_modflow`.
- Template map: `run_swmm_urban_flood` -> `swmm_urban_flood`
  (`workflows/swmm/urban_flood/urban_flood.py`). The #154 granularity gate +
  the `barriers=` round-trip re-key to `swmm_urban_flood` (section 4).
- Setter relocation: `set_swmm_parameters` ->
  `tools/simulation/swmm/set_swmm_parameters/`.

### 2.4 GEOCLAW - door `run_geoclaw`

- Fidelity brief: adaptive-mesh finite-volume shallow water (GeoClaw / Clawpack;
  TSUNAMI / DAM-BREAK / SURGE run-up inundation; AMR). Mismatch redirect: pluvial
  / riverine flooding -> `run_sfincs`; urban storm sewer -> `run_swmm`; coastal
  spectral waves -> `run_swan`.
- Template map: `run_geoclaw_inundation` -> `geoclaw_inundation`
  (`workflows/geoclaw/inundation/inundation.py`). ONE template - the tool
  already covers tsunami / dam-break / surge via its scenario knob (functional
  sameness: one adaptive-FV shallow-water solve, one envelope); do NOT split.
- No param setter.

### 2.5 SWAN - door `run_swan`

- Fidelity brief: spectral (phase-averaged) nearshore wave field (SWAN;
  significant wave height / period / direction; standalone or SFINCS-coupled).
  Mismatch redirect: inundation flooding -> `run_sfincs`; tsunami run-up ->
  `run_geoclaw`.
- Template map: `run_swan_waves` -> `swan_wave_field`
  (`workflows/swan/wave_field/wave_field.py`). (Alt name `swan_waves` reads as
  engine-stutter; `swan_wave_field` states the question - pinned, NATE may
  override.)
- No param setter.

### 2.6 LANDLAB - door `run_landlab`

- Fidelity brief: landscape-process / susceptibility models (Landlab component
  grids; landslide susceptibility factor-of-safety OR overland-flow routing).
  Mismatch redirect: channel / riverine flooding -> `run_sfincs`; post-fire
  debris-flow hazard -> `model_debris_flow` (a general pfdf tool, not an engine
  door - section 3.2); seismic hazard -> `run_openquake`.
- Template map: `run_landlab_susceptibility` -> `landlab_susceptibility`
  (`workflows/landlab/susceptibility/susceptibility.py`). ONE template today
  (landslide susceptibility + overland flow via a mode knob). Latent split:
  overland-flow is a DIFFERENT question and could become a second folder
  `landlab_overland_flow` later (door discovers it free) - NOT this slice
  (flagged OPEN-D).
- No param setter.

### 2.7 OPENQUAKE - door `run_openquake`

- Fidelity brief: probabilistic seismic hazard (OpenQuake classical PSHA;
  area-source, GEM/USGS source models; hazard curves + maps at return periods).
  Mismatch redirect: landslide / ground-failure susceptibility -> `run_landlab`;
  structural damage / loss from a hazard -> `run_pelicun`.
- Template map: `run_seismic_hazard_psha` -> `openquake_psha`
  (`workflows/openquake/psha/psha.py`). The door is engine-named `run_openquake`
  (NOT `run_seismic_*`).
- No param setter.

### 2.8 ELMFIRE - door `run_elmfire`

- Fidelity brief: wildfire spread (ELMFIRE level-set / Eulerian; LANDFIRE fuels
  + weather; point-ignition perimeter growth). Mismatch redirect: post-fire
  debris-flow hazard -> `model_debris_flow`; fire ANIMATION (satellite / GOES
  observed, not a spread solve) -> `run_model_goes_fire_animation` /
  `run_model_satellite_fire_animation` (general animation composers).
- Template map: `model_fire_spread` -> `elmfire_fire_spread`
  (`workflows/elmfire/fire_spread/fire_spread.py`). (Alt `elmfire_spread`; pinned
  `elmfire_fire_spread` for clarity.) The fire confirm card re-keys (section 4).
- No param setter.

### 2.9 PELICUN - door `run_pelicun`

- Fidelity brief: damage / loss assessment (Pelicun; HAZUS v6.1 fragility /
  loss functions; per-asset damage -> ImpactEnvelope; consumes a hazard raster
  + a structure inventory). Mismatch redirect: the hazard itself (flood ->
  `run_sfincs`, seismic -> `run_openquake`, fire -> `run_elmfire`) must run
  FIRST; Pelicun assesses damage FROM a hazard layer.
- Template map (see section 3.1 for the fold decision):
  `run_pelicun_damage_assessment` -> `pelicun_damage_assessment`
  (`workflows/pelicun/damage_assessment/damage_assessment.py`);
  `run_pelicun_with_buildings` -> `pelicun_damage_with_buildings`
  (`workflows/pelicun/damage_with_buildings/damage_with_buildings.py`).
- STAYS general (support / composition, NOT templates): `postprocess_pelicun`
  (aggregates per-asset FlatGeobuf -> ImpactEnvelope; support),
  `compute_impact_envelope` (full flood -> inventory -> Pelicun -> envelope
  composition; a `compute_*` composer).
- No param setter.

Door name summary: `run_sfincs`, `run_telemac`, `run_swmm`, `run_geoclaw`,
`run_swan`, `run_landlab`, `run_openquake`, `run_elmfire`, `run_pelicun` (nine
new doors; `run_telemac` reuses the freed name).

Template name summary: `sfincs_flood`, `telemac_river_dye`, `swmm_urban_flood`,
`geoclaw_inundation`, `swan_wave_field`, `landlab_susceptibility`,
`openquake_psha`, `elmfire_fire_spread`, `pelicun_damage_assessment`,
`pelicun_damage_with_buildings` (ten templates; pelicun contributes two pending
the fold decision).

---

## 3. Fold + engine-ambiguous decisions (the only non-mechanical calls)

### 3.1 PELICUN fold candidate - `run_pelicun_damage_assessment` vs `run_pelicun_with_buildings`

Evidence (verified in code):
- `run_pelicun_damage_assessment(hazard_raster_uri, assets_uri, fragility_set,
  component_types, realization_count)` - the CORE atomic assessment; takes an
  explicit VECTOR asset inventory (FlatGeobuf).
- `run_pelicun_with_buildings(hazard_raster_uri, bbox, cell_size_m,
  fragility_set, realization_count)` - a COMPOSER that fetches a building
  DENSITY GRID by bbox, rasterizes/adapts it into the asset representation,
  then internally calls `TOOL_REGISTRY["run_pelicun_damage_assessment"].fn`.

PINNED DISPOSITION: KEEP SEPARATE - two templates
(`pelicun_damage_assessment` + `pelicun_damage_with_buildings`). Rationale: they
share the engine and the ImpactEnvelope OUTPUT but differ in the INVENTORY
acquisition + representation pathway (explicit vector assets vs auto-fetched
building-density grid). Under the HARD RULE "fold ONLY on functional sameness;
when in doubt, separate," the distinct input contract is enough to keep them
apart - this is NOT the contaminant_plume case (there, single vs multi species
was the SAME input type shortened by a convenience field; here the two inputs
are genuinely different acquisition modes). The `with_buildings` template
calling the core template internally is acceptable (a template may call a
registered sibling, as MODFLOW templates call the shared archetype job).

DOCUMENTED ALTERNATIVE (if NATE judges inventory-source a mere knob): FOLD into
one `pelicun_damage_assessment` with an inventory knob - `assets_uri` supplied
directly, ELSE auto-fetch buildings from `bbox` + `cell_size_m` (the
density-grid adapter becomes an internal branch selected by which input is
present, mirroring contaminant_plume's convenience normalization). Flagged
OPEN-A for NATE.

### 3.2 Engine-ambiguous tools assigned by import evidence

- `model_debris_flow` - "USGS post-fire debris-flow hazard assessment over a
  burned AOI (pfdf: Staley/Gartner/Cannon models)." Import evidence: it uses the
  vendored `pfdf` library (watershed conditioning + `staley2017` / `gartner2014`
  / `cannon2010` empirical models); it does NOT import `run_landlab` or
  `run_geoclaw` or any physics solver. It is engine-ambiguous by NAME only.
  DISPOSITION: STAYS GENERAL - a standalone empirical-hazard tool, NOT one of
  the nine physics engines, NOT folded under any door. A future USGS-pfdf /
  debris-flow door is a separate later slice (flagged OPEN-B). The Landlab and
  Elmfire doors' mismatch-redirect prose points debris-flow asks at it by name.

- `run_model_conservation_priority`, `run_model_glm_lightning_animation`,
  `run_model_goes_fire_animation`, `run_model_satellite_fire_animation`,
  `run_model_news_event_ingest` - COMPOSERS / ANIMATIONS under
  `workflows/shared/`, not engine templates. STAY GENERAL (unchanged).

---

## 4. Cross-cutting reference-site handling (uniform table)

Every old registered name below is REPLACED (no aliases). Frozen dated evidence
under `docs/reports/*`, `docs/site/*` snapshots, `experiments/bench/*/data/*`,
`experiments/embedding-*/data/*` is NOT edited (dated run outputs; a rename does
not rewrite history). Load-bearing sites, uniform across the nine slices:

| reference site | file(s) | per-engine handling |
|----------------|---------|---------------------|
| Registration / import | `tools/__init__.py`, `workflows/__init__.py`, `workflows/<engine>/__init__.py` | re-point the engine tool import to the folder-per-template body; add the door import; drop the old `tools/simulation/run_<engine>_tool.py` wrapper registration. |
| Category membership | `categories.py` PRIMARY_CATEGORY (lines 246-339) + SECONDARY_CATEGORIES (653-721) | replace each old engine-tool key with the DOOR `run_<engine>` -> `hazard_modeling` (+ secondary: pelicun -> damage_assessment, swan -> coastal, elmfire -> fire); templates get NO category membership (pool-excluded, door-surfaced) - do NOT add them or opened-category widening re-leaks them. |
| HOT_SET | `categories.py:832 HOT_SET_TOOLS` | SFINCS ONLY: `run_model_flood_scenario` -> `run_sfincs`; `run_model_flood_habitat_scenario` stays general (keep or drop, OPEN-C). No other engine is in HOT_SET. |
| Solver confirm gate | `server.py:1100-1146 SOLVER_CONFIRM_TOOLS` | RE-KEY preserving parity (RISK-8): `run_model_flood_scenario`->`sfincs_flood`; `run_swmm_urban_flood`->`swmm_urban_flood`; `run_seismic_hazard_psha`->`openquake_psha`; `run_telemac`->`telemac_river_dye` (the TEMPLATE, not the door - the door runs no solve); `model_fire_spread`->`elmfire_fire_spread`; `run_model_flood_habitat_scenario` stays (general). geoclaw / swan / landlab / pelicun are NOT gated today - do NOT add (parity); geoclaw's heavy AMR solve being ungated is a latent Invariant-9 gap flagged OPEN-E, out of this rename's scope. |
| Confirm-card builders + dispatch branches | `server.py` (BK-3b mesh `run_telemac` ~8439/8539; #154 granularity `run_swmm_urban_flood` ~8574/8630/8681; seismic `run_seismic_hazard_psha` ~9284; fire `model_fire_spread` ~9384; dispatch branches ~9535-9609; barriers round-trip `run_swmm_urban_flood` ~10814/10967/11044) | re-key each `tool_name=`/branch guard to the TEMPLATE name (`telemac_river_dye`, `swmm_urban_flood`, `openquake_psha`, `elmfire_fire_spread`, `sfincs_flood`). |
| System-prompt routing | `adapter.py:393-646` (flood -> `run_model_flood_scenario`; urban -> `run_swmm_urban_flood`; the routing block) | rewrite to the door model: flood -> `run_sfincs`, urban -> `run_swmm`, etc.; each door -> select-then-call the template. Load-bearing LLM routing (re-baselines `test_system_prompt.py`). |
| Retrieval corpus | `data/tool_query_corpus.yaml` | remove the migrated names' residual entries; door phrasings -> the door's co-located `run_<engine>/corpus.yaml`; template phrasings -> `workflows/<engine>/<template>/corpus.yaml` (TEMPLATE tier, NOT merged into the main index). Canonical-query ranking tests re-baseline to expect the DOOR. |
| Persistence / reuse | `scenario_reuse.py` `_SCENARIO_TOOL_MAP` | re-key any `run_<engine>_*` scenario key to the template name; the signature matcher reads params (unaffected by the rename). |
| Consumer composers | `run_model_flood_habitat_scenario`, `run_model_nws_flood_event_scenario` (sfincs consumers), `compute_impact_envelope` (pelicun consumer) | migrate the internal dispatched name `run_model_flood_scenario` -> `sfincs_flood` / pelicun name -> template; envelope unchanged (they surface one deliverable). |
| Cross-tool prose | sibling-tool docstrings citing an old engine name as an example | rename the referenced name; no behavior change. |
| Docs (living) | `docs/site/tool-support.md`, `docs/validation/engine-coverage-inventory.md`, `docs/validation/build-report.md` | update the living inventories (not the frozen dated reports). |
| Bench INPUT fixtures | `experiments/bench/routing_sweep/inputs/*.json`, `experiments/bench/retrieval_probe/inputs/*.json` | re-baseline the acceptable-tool-name SETs to doors/templates (grading = fired names vs catalog-validated set; NATE-first methodology). Dated OUTPUT data under `.../data/*` stays FROZEN. |

RISK-1 is CLOSED: `_is_terminal_composer` (`server.py:1443`) already latches
tier=template, so every non-`run_` template name above (all of them) still
latches as a terminal deliverable - no further widening needed.

---

## 5. Cheap-smoke spec per engine

Uniform floor (EVERY engine): DOOR LISTING smoke - direct-call the door on the
offline stub, assert `kind == "engine_door"`, `templates[]` non-empty,
`fidelity_brief` + `mismatch_redirect` present, and every `templates[].tool_name`
matches a registered `engine=<engine>, tier=template` name (registry-derived, no
fabricated template). Plus the retrieval assertion: the door is retrievable
(tier=door in the pool); each template is ABSENT from `retrieve_visible_tools` /
index `tool_names`; a door dispatch gate-expands with all of its templates
(reuse the pilot's discovery-expand test pattern). No solve.

Solve-side smoke, added only where cheap (do NOT run heavy solvers in the smoke):

| engine | solve-side smoke | rationale |
|--------|------------------|-----------|
| sfincs | NONE beyond the door smoke - covered by the END flood canary (standing rule): direct-call `sfincs_flood` -> status ok + depth COG + envelope. | canary already exercises the SFINCS solve; no duplicate. |
| swmm | fixture SOLVE on a tiny AOI (small node-link mesh). | PySWMM small solves are cheap. |
| landlab | fixture SOLVE on a tiny AOI (`landlab_susceptibility`). | raster factor-of-safety is a fast component grid, no batch solver. |
| pelicun | fixture SOLVE on a small assets set (`pelicun_damage_assessment` + `pelicun_damage_with_buildings`). | fragility-curve assessment is pure-python, fast. |
| telemac | DRY-STAGE: the BK-3b mesh-only build (gmsh, ~10-25 s, no DEM, no solve) - assert node/element counts. | full TELEMAC-2D solve is expensive; the mesh build is the cheap validation. |
| geoclaw | DRY-STAGE: deck build/stage validation (no AMR solve); assert the "Total mass at initial time" mass-balance diagnostic when a fixture deck is available. | adaptive-FV solve is expensive. |
| swan | DRY-STAGE: deck build/stage validation (no spectral solve). | full spectral solve is moderate-to-expensive. |
| openquake | DRY-STAGE: area-source deck build validation (no PSHA run) OR a minimal single-site PSHA only if it stays under a few seconds. | classical PSHA over an AOI is a Batch job. |
| elmfire | DRY-STAGE: LANDFIRE fetch + config/deck build validation (no containerized level-set solve). | ELMFIRE solve is a heavy containerized run. |

Committed tests stay OFFLINE (stub_server.py / mocked publish); the cheap
fixture solves run locally where a local backend exists.

---

## 6. Acceptance (per slice) + closure canary

Per engine slice:
1. Registry membership: `run_<engine>` registered tier=door; each template
   registered `engine=<engine>, tier=template`; every old engine-tool name GONE.
2. Retrieval: canonical-query suite green (door-baselined); NO template in any
   default-pool ranking; door dispatch expands the gate with all its templates.
3. Offline suites green (per-engine test migration below).
4. The engine's cheap-smoke (section 5) passes.

Per-engine test migration (re-key old name -> template/door; add the door
registration + gate-expansion assertions mirroring the pilot):
`test_model_flood_scenario_coastal.py`, `test_sfincs_solve_domain_aoi_guard.py`,
`test_job0327_flood_honesty_floor.py`, `test_duplicate_flood_layer_fix.py`,
`test_flood_frame_sequence.py` (sfincs); `test_run_river_dye_scenario.py`,
`test_run_telemac_chain.py` (telemac); `test_run_geoclaw_chain.py` (geoclaw);
`test_model_fire_spread_chain.py` (elmfire);
`test_run_pelicun_damage_assessment.py`,
`workflows/test_pelicun_damage_with_buildings.py` (pelicun);
`test_solver_confirm_gate.py`, `test_combined_run_settings_gate.py` (confirm
parity - assert the re-keyed template names gate identically);
`test_categories.py` (HOT_SET + category re-baseline); `test_system_prompt.py`
(adapter door-model re-baseline); `test_tool_retrieval.py`,
`test_search_tools.py`, `eval_routing_live.py` (canonical-query / routing
re-baseline to doors); `test_scenario_reuse_job0326.py` (reuse-map re-key);
`test_tool_arg_normalizer.py`, `test_pipeline_emitter.py`,
`test_allowed_set.py` (incidental name re-keys). Engine-scoped test files stay
in their slice; shared-file tests (categories, system_prompt, tool_retrieval,
solver_confirm_gate) are touched by MULTIPLE slices -> land those slices
serially or coordinate the shared-file edits (feature-pipelining: the
shared-file edge is the only inter-slice collision).

Closure (after the nine slices land): FLOOD CANARY (standing rule - registry +
corpus seams touched): direct-call flood run (status ok + depth COG + envelope)
+ WS turn smoke (door -> select -> template) + NATE visual pass in QGIS = the
rendering acceptance.

---

## 7. Execution order + shared-file coordination

The nine slices are independent EXCEPT for four shared files every slice edits:
`categories.py` (PRIMARY/SECONDARY/HOT_SET), `server.py` (SOLVER_CONFIRM_TOOLS +
confirm hooks), `adapter.py` (routing), `data/tool_query_corpus.yaml` (+ the
canonical-query tests). Pin: run the nine slices such that these four files are
edited under a single owner per pass (serialize the shared-file edits, or batch
all nine shared-file re-keys in one coordinated commit after the per-engine
folder moves land) - this is the only inter-slice hazard (shared-DATA collision;
the folder-per-template moves are collision-free). Suggested order by
solve-cheapness for fast smoke feedback: pelicun, landlab, swmm (cheap fixture
solves) -> telemac, swan, geoclaw, openquake, elmfire (dry-stage) -> sfincs last
(its HOT_SET + flood canary + the highest shared-file blast radius).

---

## 8. Open decisions for NATE

- OPEN-A (pelicun fold): pinned KEEP SEPARATE (two templates) on the
  inventory-pathway difference; documented alternative = fold to one template
  with an `assets_uri` OR `bbox` inventory knob. NATE picks.
- OPEN-B (model_debris_flow): pinned STAYS GENERAL (pfdf empirical tool, not an
  engine); confirm no debris-flow door is wanted this rollout.
- OPEN-C (HOT_SET flood_habitat): `run_model_flood_habitat_scenario` stays
  general - keep it in HOT_SET or drop it now that `run_sfincs` is the always-on
  flood entry? Low-stakes.
- OPEN-D (landlab split): `landlab_susceptibility` folds landslide + overland
  flow behind one mode knob today; a later `landlab_overland_flow` template is a
  free folder-add. Leave as one template this slice - confirm.
- OPEN-E (geoclaw confirm gate): geoclaw's heavy AMR solve is NOT in
  SOLVER_CONFIRM_TOOLS today; the rename preserves parity (does not add it), but
  the ungated heavy solve is a latent Invariant-9 gap worth a follow-up job.
- OPEN-F (swan template name): `swan_wave_field` vs `swan_waves` - pinned
  `swan_wave_field` for clarity; NATE may override.
