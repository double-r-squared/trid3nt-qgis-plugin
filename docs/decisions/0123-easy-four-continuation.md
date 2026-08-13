# ADR 0123 -- Hazard easy-four continuation (rows 2-4 of ADR 0122)

Status: LANDED (2026-08-04) -- all three continuation rows landed with live
cheap-smoke evidence.
Follows: 0122 (easy-four wave, row #1 landlab_flow_accumulation LANDED + rows 2-4
scoped-ready), 0121 (S-tier wave-2 triage), 0120 (the template hygiene gate), 0107
(two-mode input gate), the physics_registry seam.

## Context

ADR 0122 landed row #1 (landlab_flow_accumulation) and handed forward rows 2-4 at a
clean boundary with precise build recipes. This ADR records taking all three to the
full wave bar (one-file capability-named composer; gates; synthetic_inputs;
fidelity/off-scope; corpus + model-free retrieval proof; offline tests; LIVE
cheap-smoke with numbers + URIs; hygiene lint; board flip; metrics row).

## Decision -- per-row outcomes

### #1 landlab_green_ampt_overland_flow -- LANDED (exec-mode)

A NEW capability-named template (a distinct question class from
landlab_susceptibility / landlab_flow_accumulation per the capability-naming rule):
how a design storm PARTITIONS into infiltration vs runoff over a watershed DEM.

- **Worker** (`services/workers/landlab/component_chain.py`): a new
  `analysis="green_ampt_overland_flow"` branch. `OverlandFlow` (de Almeida) stepped
  over a design storm while `SoilInfiltrationGreenAmpt` removes infiltrated water
  each step (dt capped to resolve >= 40 substeps). PRIMARY output =
  `soil_water_infiltration__depth` (m); SECONDARY = `runoff_depth` (rainfall excess
  = total rainfall - infiltration, clamped >= 0). Typed partition scalars
  (infiltrated/runoff fraction + means + storm total) travel in `extra`, folded into
  the worker result block `green_ampt` (entrypoint + run_chain).
- **Contracts** (`landlab_contracts.py`): `green_ampt_overland_flow` added to
  `LandlabAnalysis` (+ synonyms); Green-Ampt run-args
  (`soil_hydraulic_conductivity_m_s` / `initial_soil_moisture_content` /
  `green_ampt_soil_type`, demo defaults); new `LandlabGreenAmptLayerURI`
  (infiltrated_fraction / runoff_fraction / mean_infiltration_mm / mean_runoff_mm /
  total_rainfall_mm).
- **Composer** (`workflows/landlab/green_ampt/green_ampt.py`): the
  `landlab_green_ampt_overland_flow` tool + composer, reusing the susceptibility
  DEM-fetch / download / AOI-floor helpers AND the Atlas-14 design-storm seam (the
  0102 seam via `_atlas14_design_storm_mm`). The DEM is REAL; the triggering
  rainfall is the real NOAA Atlas-14 design storm (a failed lookup STOPS with a
  typed gate); only the SOIL hydraulic block is demo-labeled synthetic_inputs.
- **Postprocess** (`postprocess_landlab.py`): `postprocess_landlab_green_ampt`
  reprojects the infiltration + runoff COGs to 4326 (reusing `continuous_flood_depth`
  -- a dedicated infiltration ramp is a NAMED RESIDUAL) and
  `build_infiltration_partition_chart_spec` builds the infiltration-vs-runoff
  partition Vega-Lite chart.

Live cheap-smoke (exec, `run_chain.py`, synthetic UTM DEM, seconds, no MinIO):
infiltration COG + runoff COG + `landlab_result.json` green_ampt block. Numbers
(45.0 mm 100-yr/0.5-hr storm proxy, K=1e-5): infiltrated_fraction 0.79,
runoff_fraction 0.21, mean_infiltration 35.5 mm; determinism verified (byte-identical
field on re-run); higher K infiltrates more (monotonicity). Model-free retrieval:
`landlab_green_ampt_overland_flow` in the top-8 for all 5 partition phrasings.

### #2 elmfire_verification_elliptical_replication -- LANDED (docker)

The calibration anchor: does the level-set solver reproduce the closed-form
elliptical solution on a controlled constant-fuel/uniform-wind/flat-terrain deck.

- **Deck override** (`deck_builder.write_constant_raster_typed` +
  `run_elmfire.build_constant_verification_deck`): an ALL-CONSTANT flat-grid deck
  authored AGENT-SIDE (no LANDFIRE/DEM fetch, no warp) -- GR2 (FBFM 102) uniform
  grass fuel, zero canopy, flat terrain (constant elevation, slope 0, aspect 0),
  uniform constant wind. Reuses the deck_builder grid / namelist / grid-identity
  assert / ignition projection / manifest verbatim.
- **Verifier** (`postprocess_elmfire.verify_elliptical_replication`): extracts the
  numerical ToA perimeter (max-radius burned cell per angular bin about the
  ignition), rotates into the wind-aligned frame, builds the Richards (1990) ellipse
  from the observed head/back/flank extents, and returns the verification triple
  (rmse_m / err_fraction / correlation + graded corr_class) +
  length_to_width_ratio + passed (err_fraction <= tolerance AND perimeter did not
  touch the domain edge). `build_ellipse_overlay_chart_spec` builds the
  numerical-vs-ellipse overlay chart.
- **Contract** (`elmfire_contracts.ElmfireEllipseVerificationLayerURI`): a
  `FireSpreadLayerURI` subtype adding the verification triple + ellipse geometry.
- **Composer** (`workflows/elmfire/verification/verification.py`): the
  `elmfire_verification_elliptical_replication` tool + composer (constant deck ->
  stage -> run_solver -> ToA read -> verify -> postprocess ToA COG -> publish ->
  ellipse chart). NOT confirm-gated (a fast controlled verification, no user inputs).

TOLERANCE: `ELLIPSE_VERIFICATION_TOLERANCE = 0.08` is the COARSE-grid shape-agreement
tolerance (RMSE / ellipse semi-major), explicitly NOT the published fine-grid <0.5%
Verification-01 gate (which needs a fine grid + a full Rothermel-rate cross-check) --
a named residual. The image `trid3nt/elmfire:dev` (bash-compatible) is used, not
`trid3nt-local/elmfire:trid3nt-verify` (that image has a python entrypoint
incompatible with the local-docker `bash -c` spec -- a documented substrate fact).

Live cheap-smoke (docker, constant deck, 10 km domain @30 m, 15 mph/270deg, 1.5 h,
GR2): err_fraction 0.0375 (< 0.08 tolerance), correlation 0.986 (corr_class "good"),
length_to_width_ratio 3.41, passed=True; real ToA COG s3 URI. Model-free retrieval:
top-8 for all 5 verification phrasings.

### #3 geoclaw_tsunami_gauge_timeseries -- LANDED (docker)

A capability-named gauge template: the coastal water-level TIME SERIES from a
tsunami (waveform + co-seismic subsidence), riding the existing inundation deck.

- **Download filter widened** (`inundation._is_geoclaw_output_key`): the composer's
  `fort.*`-only download filter now ALSO pulls `gaugeNNNNN.txt` (the worker already
  writes one coastal gauge; the filter dropped it). The plain inundation path
  ignores gauges; the gauge template reads them.
- **Gauge parser + chart** (`postprocess_geoclaw`): `parse_geoclaw_gauge_series`
  parses the standard GeoClaw gauge file (`[level, t, h, hu, hv, eta]`, eta = surface
  elevation last column) into the surface-elevation series + typed scalars
  (max/min surface elevation, max amplitude, co-seismic offset = eta at t0, max
  depth). `build_gauge_timeseries_chart_spec` builds the surface-elevation
  time-series chart.
- **Contract** (`geoclaw_contracts.GeoClawDepthLayerURI`): OPTIONAL gauge scalars
  (default None, additive) so the plain inundation path is unchanged.
- **Composer** (`inundation.model_geoclaw_inundation`): a new `emit_gauge_series`
  flag parses the gauge before out_dir cleanup, attaches the scalars to the peak
  layer, and emits the gauge chart. New template
  `workflows/geoclaw/gauge_timeseries/gauge_timeseries.py` rides it (scenario
  forced tsunami, `emit_gauge_series=True`). Confirm-gated (reuses the geoclaw card).

Live cheap-smoke (docker, small Crescent City CA AOI, amr=2, 600 s, gauge recorded):
real tsunami solve -> gauge file downloaded (filter widening works) -> parsed ->
typed scalars on the layer -> real s3 ToA COG URI. Numbers: gauge_max_surface_elevation
3.64 m, gauge_max_amplitude 0.43 m, gauge_coseismic_offset 3.64 m. (In this cheap
config the gauge landed on high dry ground so the wave signal is small; a production
run places the gauge in the water column -- the CAPABILITY pipeline is proven.)
Model-free retrieval: top-8 for all 5 gauge phrasings.

## Consequences

- Registered 182 -> 185 (+3 coded templates: `landlab_green_ampt_overland_flow` +
  `geoclaw_tsunami_gauge_timeseries` + `elmfire_verification_elliptical_replication`),
  templates 24 -> 27, coded tools 84 -> 87. test_catalog_surfacing pins 182 -> 185
  (x4 + header). No coded-fetcher / spec-served change (no data source touched).
- Category `hazard_modeling`. No flood-seam touched (grep-verified; the geoclaw
  change is additive gauge parsing + a widened output filter, not a flood physics
  seam; no canary mandated).
- Board rows `infiltration_coupled_runoff_generation`,
  `constant_wind_elliptical_spread_regression_gate` /
  `elliptical_exact_solution_regression_gate`, and the geoclaw gauge row flip
  CAND -> LANDED.
- Named residuals: the fine-grid <0.5% Verification-01 gate (Rothermel-rate
  cross-check); dedicated infiltration / drainage-area log-domain TiTiler ramps.
