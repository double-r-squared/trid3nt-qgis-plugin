# ADR 0121 -- S-tier template wave 2 (hazard cluster) -- triage outcome

Status: accepted (2026-08-04, NATE S-tier wave 2 kickoff, run under the wave-1
lesson ADR 0120)
Follows: 0120 (wave-1 flood/hydraulics cluster + the template hygiene gate),
0107 (two-mode input gate), the physics_registry seam.

## Context

The S-tier candidate table (reports/design/template-candidates-2026-08-03.md)
proposed a hazard-cluster wave of thirteen templates, each labelled [S] on the
reading that it was a knob swap or copy-modify on an already-plumbed deck /
worker / library surface:

1. openquake_scenario_shaking
2. openquake_logic_tree_epistemic_uncertainty
3. swmm_wq_buildup_washoff_single
4. geoclaw_tsunami_gauge_timeseries (rename of gauge_timeseries_copalis)
5. fema_p58_component_assessment (Pelicun)
6. hazus_eq_building_assessment (Pelicun)
7. pelicun_loss_function_damage_state_aggregation
8. landlab_flow_accumulator
9. landlab_priorityflood_real_dem
10. landlab_green_ampt_overland_flow
11. elmfire_verification_elliptical_replication
12. swan_windsea_swell_partition_prvi
13. swan_official_regression_testcases

The wave-1 lesson (ADR 0120) was explicit: the candidate [S] labels are
optimistic where worker-side wiring is missing; exactly one of seven wave-1 rows
survived ground-truth as a true drop-in. Wave 2 was run under the same
discipline: ground-truth every row against the ACTUAL worker / deck / library
surfaces FIRST, land the true-S subset with live cheap-smoke proofs, STOP the
rest honestly with precise per-row blockers and re-scoped effort. Never force,
never fabricate evidence.

## The load-bearing execution-substrate fact

The wave-2 cluster splits cleanly by how each engine's deck is authored and run,
which decides live-verifiability far more than the nominal [S] label:

- **exec-mode / library engines** (openquake, swmm, landlab pip-only via
  exec_kind="exec"; pelicun library-driven in-process): worker code runs from
  the LOCAL checkout, no baked image -- a worker-code edit takes effect with no
  image rebuild. Packages confirmed installed in venvs/agent: landlab, pyswmm,
  openquake, pelicun. clawpack/geoclaw NOT installed (geoclaw is docker-mode).
- **docker-mode engines** (geoclaw, swan, elmfire, sfincs, hecras, telemac,
  schism -- images present locally): the deck is authored either INSIDE the
  container (setrun_builder / deck_builder baked by a Dockerfile COPY -- a change
  needs an image rebuild) or ON THE AGENT HOST (deck built server-side before
  staging -- a change is a live drop-in). Confirmed per engine:
    - geoclaw: setrun authored inside the container (baked) BUT the gauge block
      is already authored and active and gaugeNNNNN.txt is already uploaded;
    - swan: deck authored inside the container by deck_builder.py (baked);
    - elmfire: deck authored ON THE AGENT HOST (run_elmfire.load_deck_builder
      imports deck_builder.py by repo path; the image only runs the compiled
      binary on a ready deck).

## Decision -- triage verdicts (all thirteen)

Ground-truth (worker / deck / library code read directly, file:line evidence in
the wave-2 report) showed NONE of the thirteen is a pure-knob drop-in over an
already-plumbed PRODUCT surface with a byte-identical baseline -- the bar the one
wave-1 landing (sfincs_advanced_numerical_physics_knobs) met. Every row needs
new server-side postprocess/parser code, a new worker analysis mode, or new
library/deck-authoring. The verdicts:

### STOP -- needs new worker/library deck-authoring (M/L), NOT a knob

- **1 openquake_scenario_shaking [S -> M/L].** job_ini.py hardwires
  `calculation_mode = classical` (no build_spec passthrough); there is zero
  rupture_model.xml authoring; run_oq.py harvests only hazard-map/curve CSVs, no
  scenario GMF. Needs a render_rupture_model_xml + a scenario job.ini branch +
  GMF-output harvesting. New worker code.
- **2 openquake_logic_tree_epistemic_uncertainty [S -> M/L].** classical mode is
  reusable, but both logic-tree renderers are hardwired to exactly one branch,
  `OpenQuakeRunArgs.gmpe` is a single str (no source_models list), and
  `[output] quantiles =` is a hardcoded empty string. Needs parameterized
  multi-branch renderers + quantile wiring + quantile-curve CSV harvesting.
- **3 swmm_wq_buildup_washoff_single [S -> already-covered / M].** The
  buildup/washoff pollutograph CAPABILITY already ships in
  `swmm_urban_flood(pollutants=..., dry_buildup_days=, washoff_model=)` (#224):
  outfall pollutograph chart + cumulative load + peak washoff-concentration
  layer. A dedicated single-subcatchment textbook deck would be a NEW minimal
  .inp deck-authoring recipe (M) and largely duplicative of the shipped
  capability. Recommend NOT adding a near-duplicate template; the honest gap is
  a minimal-deck FIXTURE, not a new capability.
- **5 fema_p58_component_assessment [S -> L].** The pelicun template does not
  call pelicun's DL_calculation / Assessment / DamageModel / LossModel at all --
  it is a bespoke numpy Monte-Carlo interpolator over ONE bundled HAZUS-flood
  loss_repair.csv (`import pelicun` used only for `__file__`). FEMA P-58 needs
  the real pelicun component-fragility assessment engine (CMP_QNT.csv, demand
  EDPs, input.json, ComponentDatabase="FEMA P-58") -- none of that plumbing
  exists. New library wiring.
- **6 hazus_eq_building_assessment [S -> L].** Same structural blocker;
  `fragility_set="fema_hazus_eq_2020"` is a registered enum literal that is
  DELIBERATELY stubbed to raise before any I/O. No HAZUS-EQ curve loader, no
  seismic-demand-to-EDP path, no pelicun assessment call.
- **7 pelicun_loss_function_damage_state_aggregation [S -> L].** The tool
  supports one mode (loss-ratio-from-depth, binned post-hoc into damage states);
  there is no classical fragility model, no eco_scale / AcrossFloors /
  AcrossDamageStates aggregation, and no pelicun assessment object to configure.
- **12 swan_windsea_swell_partition_prvi [S -> M].** deck_builder.py runs INSIDE
  trid3nt-local/swan:latest; HSWELL is not in `_VALID_OUTPUT_QUANTITIES`, there
  is no QUANTITY-command render path at all, and postprocess_swan reads only
  Hsig. Adding HSWELL + a `QUANTITY ... [fswell]` line requires editing the
  baked deck_builder and rebuilding the image. Not a server-side drop-in.
- **13 swan_official_regression_testcases [S -> M/L].** entrypoint.py
  unconditionally re-renders swan_run.swn from the narrow parametric SwanBuildSpec
  every run; there is no raw-deck passthrough, and the authors' decks
  (curvilinear grids, OBSTACLE, non-parametric 2D spectra, nesting) use features
  the renderer has no commands for. Baked-image change, closer to new-feature
  engineering than a knob.

### STOP -- genuine server-side / exec-mode FEATURE builds (NOT knobs), live-verifiable next without an image rebuild

These four are the honest "true-S-in-the-narrow-sense" candidates: no image
rebuild, the image/worker already emits the raw output, and the remaining work
is server-side (or exec-mode worker) Python. They are NOT pure knobs -- each is a
contained feature build (a new parser + chart, a new worker analysis mode, a new
verification postprocess) -- so they are re-scoped as their own small jobs rather
than forced into this session half-verified. They are the recommended next build
subset, cheapest-first:

- **8 landlab_flow_accumulator + 9 landlab_priorityflood_real_dem [S -> M, exec-mode].**
  FOLD 9 into 8: both are flow accumulation on a real DEM with a routing /
  depression knob. The worker (component_chain.py) ALREADY runs FlowAccumulator
  with a selectable flow_director (D8/Dinf/MFD, wired through
  physics_registry["landlab"]) + DepressionFinderAndRouter, and already exposes
  drainage_area as a secondary field. PriorityFloodFlowRouter is available in the
  venv. Remaining work: a new `analysis="flow_accumulation"` branch in
  component_chain (primary output = drainage_area, log-scaled), a
  depression_handler knob (fill vs priority_flood), a new style preset + metrics
  in both postprocess sides, the analysis Literal + knobs on the existing landlab
  tool, corpus + tests. Exec-mode: a live cheap-smoke is a direct run_chain.py
  subprocess on a small synthetic DEM (seconds, no image, no MinIO). Cheapest
  landing; foundational (other landscape-evolution candidates reuse it).
- **10 landlab_green_ampt_overland_flow [S -> M, exec-mode].** The overland_flow
  chain exists but has no infiltration component; SoilInfiltrationGreenAmpt is in
  the venv. Adds infiltration-vs-runoff partitioning to the existing chain +
  an infiltration output field + interval-sampled depth (the animation norm).
  Exec-mode cheap smoke.
- **11 elmfire_verification_elliptical_replication [S -> M, server-side].** The
  strongest calibration anchor (published closed-form ellipse, <0.5% tolerance).
  Deck is authored ON THE AGENT HOST (run_elmfire/deck_builder), wind_speed_mph /
  wind_dir_deg / fuel_moisture presets are already server-side knobs, and the
  image already emits the time_of_arrival raster. Remaining server-side work: a
  fuel_model constant-raster override (extend the existing write_constant_raster
  pattern to force GR2/102), plus a NEW verification postprocess that vectorizes
  the ToA raster into isochrones, generates the closed-form ellipse, diffs them,
  and emits the comparison chart. Live smoke = the elmfire image on a small flat
  synthetic grid (fast). Highest-value; medium build.
- **4 geoclaw_tsunami_gauge_timeseries [S -> M, server-side].** setrun already
  authors a coastal gauge unconditionally and the image already uploads
  gaugeNNNNN.txt; the co-seismic subsidence is already in the dtopo-driven
  initial condition. Remaining server-side work: widen the
  `_download_batch_geoclaw_outputs` `fort.`-only key filter to include gauge
  files, and add a gauge-series parser + chart (postprocess_geoclaw has zero
  gauge references today). Live smoke needs a geoclaw docker run (Fortran,
  heavier than the exec-mode smokes).

### Landed this wave

- **None.** No row met the pure-knob drop-in bar, and the four genuine
  server-side / exec-mode candidates are contained FEATURE builds that cannot be
  taken to the full wave bar (charts copied from the cited example, deck /
  gate / postprocess-vs-fixture offline tests, live cheap-smoke, retrieval
  proof, hygiene, docs) inside this session without forcing. Per the close-out
  rule and the live-E2E-evidence rule, they are handed forward as scoped jobs
  rather than landed half-verified.

## Consequences

- Registry / templates / categories / retrieval index UNCHANGED (0 landed). No
  coded-tool delta. Offline baseline undisturbed (no code / schema / registry
  change): the documented exactly-9-failure set stands by construction.
- The wave-1 finding holds a second time and harder: on the hazard cluster the
  candidate [S] labels were optimistic across the board -- 7 of 13 are M/L needing
  new worker/library deck-authoring, and the remaining 4 (plus the folded 9) are
  contained server-side/exec-mode feature builds, not knobs.
- Recommended next-build order (cheapest, most-certain first, all no-image-rebuild):
  (a) landlab_flow_accumulation (folds 8+9, exec-mode, seconds-long smoke),
  (b) landlab_green_ampt (10, exec-mode),
  (c) elmfire_verification_elliptical (11, server-side, highest value),
  (d) geoclaw_tsunami_gauge_timeseries (4, server-side, docker smoke).
  The remaining rows (1,2,5,6,7,12,13) need new worker/library/deck code or an
  image rebuild and sequence after their named worker-side work.
- Row 3 (swmm WQ) is recorded as already-covered by swmm_urban_flood(pollutants)
  -- no new template recommended; only a minimal-deck regression fixture if a
  unit-level washoff-math check is wanted.
