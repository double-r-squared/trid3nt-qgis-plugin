# V&V primitive tool list (THINKING - for NATE review, nothing built)

Atomic primitives that VERIFY and CALIBRATE. Loop/orchestration/subagents are
FROZEN - these are the building blocks the loop would later compose. Build
discipline: each tool wraps a REAL package function, usage verified against
that package's API at build time; then a live-data test harness.

Legend: [now] = atomic, buildable now  [loop] = embodies a loop, frozen
        [exists] = we already have it (extend/reuse, don't duplicate)

============================================================
VERIFY - does the run report itself healthy, and match observations
============================================================

-- A. Engine diagnostic readers (tier-1, "always runs" on a sim) [now] --
   One reader per engine -> one NORMALIZED envelope
   {engine, mass_balance_pct|derived, max_instability, nonconverged_pct,
    dry_cells, warnings[]}. (May surface as one read_run_diagnostics(run)
    dispatcher - decide at build.)

1. read_sfincs_diagnostics   - sfincs_map.nc + log: DERIVE mass balance
     (cumprcp vs cuminf + boundary flux + storage delta), max water depth
     (instability), timestep_analysis CFL-limiting cell, runtime. (SFINCS has
     no explicit continuity field - derived + labeled.)
2. read_swmm_diagnostics     - .rpt: runoff + flow-routing continuity %, node
     flooding/surcharge summary, per-link Flow Instability Index,
     non-converging-step %.
3. read_modflow_diagnostics  - .lst: volumetric budget % discrepancy per
     stress-period, convergence-failure count, dry-cell count. (Confirm exact
     LST field names vs mf6io before hardcoding.)
4. read_geoclaw_diagnostics  - gauge output + conservation (total mass initial
     vs final - the known geoclaw sanity signal).
5. read_telemac_diagnostics  - listing mass balance (we already parse some in
     postprocess_telemac - reuse).

-- B. Skill-metric primitives (tier-2 math, obs vs model) [now] --
   Wrap spotpy.objectivefunctions (NSE/KGE/PBIAS/RMSE native) - no bespoke math.

6. compute_skill_metrics     - time-series obs vs model: NSE, KGE, PBIAS, RSR,
     RMSE, R2, peak error, peak-timing error. Returns values + Moriasi
     acceptance bands + a SUGGESTED verdict (heuristic, not a gate).
7. compute_flood_extent_skill- raster wet/dry confusion vs a benchmark extent:
     Hit rate H, False Alarm Ratio F, Critical Success Index CSI.
8. compute_head_calibration_stats - groundwater: scaled RMS (head-residual
     RMSE / observed head range) + residual summary. (Could fold into #6.)
   NOTE: compute_model_residuals [exists] already does observed-vs-modeled
   residuals - #6-#8 should EXTEND/consume it, not duplicate. Reconcile at build.

-- C. Observation ingestion + pairing (feeds B) --
9.  extract_model_at_observations [now] - sample model output (raster or
     timeseries) at observation points/times -> the aligned pairs #6/#7 need.
     The crucial alignment primitive.
10. fetch_high_water_marks   [now] - USGS STN flood-event high-water marks
     (level/extent validation). Confirm the STN API.
11. fetch_flood_extent_observation [now/heavy] - satellite-derived flood
     extent (Sentinel-1 SAR) as the benchmark raster for #7. May be
     catalog/search territory rather than a dedicated fetcher.
   [exists] fetch_usgs_nwis_gauges, fetch_noaa_coops_tides already supply the
   gauge/tide observation series #6 consumes.

============================================================
CALIBRATE - adjust parameters until the model matches observations
============================================================

-- D. Parameter-write primitives (atomic) [now] --
   One per engine, wraps the package's param-setting API.

12. set_sfincs_parameters    - write manning/infiltration/subgrid params into
     the SFINCS deck (hydromt-sfincs setup_* API).
13. set_swmm_parameters      - inject imperviousness/depression-storage/
     manning/infiltration into the .inp (swmm-api / PySWMM).
14. set_modflow_parameters   - parameterize K-field (pilot points)/recharge/
     storage via pyEMU PstFrom + flopy.

-- E. Optimizer drivers (THESE ARE LOOPS - frozen) [loop] --
   Listed for completeness; they embody the sample->run->metric->repeat loop
   we are deferring. Built AFTER the primitives + orchestration decision.

15. run_spotpy_calibration   - SPOTPY sampler (SCE-UA/DDS) over a param space
     + objective function; surface engines (SWMM/SFINCS). In-process,
     Python-native. [loop]
16. run_pest_calibration     - pyEMU builds the .pst + runs PEST++ (ies/glm);
     MODFLOW industry standard. ~100-250 runs - cost-gated. [loop]
   The "run" primitive they compose = run_solver [exists]; the "metric" =
   #6 [now].

============================================================
BUILD ORDER (proposal)
============================================================
1. Group A (diagnostics readers) - cheapest, highest-trust, would have caught
   the Landlab lie; one engine at a time (hydrology first: SFINCS, SWMM, MODFLOW).
2. Group B + C (metrics + pairing) - the observed-vs-model half; reconcile
   with compute_model_residuals.
3. Group D (param setters) - atomic, sets up calibration without the loop.
4. Group E (optimizer drivers) - FROZEN with the orchestration/loop layer.
Each tool: verify package usage -> unit-correct -> live-data test harness.

============================================================
F. Webinar-derived review primitives (NATE - the AWS flood-review lecture)
============================================================
These come from PRACTICE (the review webinar), NOT the calibration packages.
DEDUP RULE (NATE): before making a tool, confirm the check has NO home in the
engine module / calibration package - only build where there's a real gap.
Verdict per item: [pkg] already covered -> reuse; [gap] genuinely new tool;
[data] a cross-check against data we already fetch; [human] not a tool.

17. check_peak_in_window     [gap] - duration sufficiency: did max stage/flow
     occur in the final time-slice? -> "peak likely truncated, extend duration"
     (webinar 12:00). No package does this; reads the engine timeseries.
18. check_trapped_water      [gap] - boundary-failure symptom: water still
     rising / not receding at sim end with no downstream BC (webinar 38:28).
     New; reads the last output frames + the deck's BC config.
19. check_volume_vs_forcing  [gap] - volume sanity: total modeled water volume
     / catchment area = implied rainfall depth; compare to the forcing WE
     supplied (webinar 49:55). Arithmetic; new.
20. check_parameter_vs_landcover [data] - assigned imperviousness/roughness
     vs the NLCD landcover we ALREADY fetch for the same AOI (industrial site
     at 100% pervious = catchable) (webinar 14:42). Cross-check, not a package.
21. check_obstructions_present [data] - urban domain + zero building
     obstructions in the mesh -> warn (webinar 41:26). We already fetch
     footprints + treat them as obstructions; this is a count check.
22. check_lidar_artifacts    [gap/hard] - culverts modeled "in the air" from
     unstripped vegetation in LiDAR (webinar 20:46). HARD - may be human-only;
     research whether any DEM-hydro-conditioning check applies. Flag, don't
     assume a tool.

ALREADY COVERED BY THE ENGINE MODULE (do NOT build - webinar item has a home):
- Mass balance / continuity (webinar 19:10) -> read_*_diagnostics (A). [pkg]
- Numerical stability / instability (webinar) -> Flow Instability Index in
  read_swmm_diagnostics; CFL cell in read_sfincs_diagnostics. [pkg]
- First-principle metrics (Q=VA, structure capacity) -> compute_skill_metrics
  (B) where observations exist; manual nomograph cross-ref is [human].

HUMAN-ONLY (webinar - never a tool, only a review-card prompt):
- Dynamic visual review / vector arrows (33:47) - one-click animation is a UX
  affordance, the SEEING is human.
- Local knowledge / flow-path reasonableness / event selection (23:05) -
  the tier-3 checklist. [human]
- Collaborative walkthrough (43:32) - a workflow norm, not a tool.
