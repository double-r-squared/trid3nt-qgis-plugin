# ADR 0184 - Landlab shortlist grind batch 2 (channel incision, chi-map, storm generator)

Status: Accepted
Date: 2026-08-08

## Context

The M/L sign-off shortlist (`docs/validation/ml-signoff-shortlist.md`) lists five
Landlab rows: three "ready NOW" (machinery in place, no front) and two
"front-away" (CAND-L). This batch grinds the three ready-now rows on the existing
Landlab surface (10 landed templates, exec-mode component-chain worker,
`_composer_common.py` boilerplate, Boulder CO smoke AOI), and triages the two
front-away rows.

Published-first sources (the board rows cite them): the official Landlab tutorial
notebooks - `shared_stream_power` / FastscapeEroder for incision, `chi_finder` for
the chi map, `generate_uniform_precip` (PrecipitationDistribution) for the storm
generator.

## Decision

### Landed (3 templates, registry 228 -> 231, EXPECTED_TEMPLATES +3)

1. **`landlab_channel_incision_steady_state`** (worker analysis `channel_incision`).
   FastscapeEroder (detachment-limited stream power E = K A^m S^n) + rock uplift
   stepped to steady state (implicit solver -> large stable dt -> quasi-steady
   state in a bounded step budget). V&V = the closed-form slope-area relation
   S = (U/K)^(1/n) A^(-m/n): the fitted concavity (negative log-log slope) is
   checked against m_sp/n_sp and K is back-solved from the intercept
   (K = U / exp(n * intercept)). Primary layer = the EVOLVED topography (a REAL
   AOI DEM evolved under a LABELED demo uplift/erodibility forcing -
   `SyntheticInput`: the terrain is real, the forcing is a scenario); secondary =
   channel steepness (ksn); chart = slope-area log-log with the analytical
   prediction line at the KNOWN forcing K.
   Live V&V (Boulder foothills, 90 m, U=1 mm/yr, K=1e-5, m=0.5, n=1, T=1 Myr):
   **fitted concavity 0.485 vs analytical 0.500 (delta 0.015), K recovered
   1.23e-5 vs input 1.0e-5 (ratio 1.23), R^2 = 0.972**, 73 channel nodes.

2. **`landlab_channel_steepness_chi_map`** (worker analysis `chi_map`).
   ChiFinder (chi integrated at a reference concavity) + SteepnessFinder (ksn) on
   the routed real DEM. Primary = chi index over the channel network; secondary =
   ksn raster + channel-network vector; chart = the chi-elevation profile
   (near-linear = uniform steepness, slope breaks = knickpoints). A diagnostic on
   the CURRENT terrain, no evolution.
   Live smoke (West Bijou Creek escarpment, CO, 30 m, theta=0.5): **max ksn
   133.2, mean ksn 12.5, max chi 6.93, 1476 channel nodes** - high-ksn reaches
   concentrated along the escarpment knickzone.

3. **`landlab_storm_sequence_generator`** (in-process; no worker analysis).
   A PrecipitationDistribution (Poisson storm/interstorm/depth) forcing generator.
   Disposition (below): a chart-led forcing UTILITY, run IN-PROCESS (no DEM, no
   grid solve), emitting an AOI point marker + the storm-sequence time series +
   the storm-depth distribution. Returns a `LandlabStormSequenceLayerURI`.
   Live smoke (Boulder, 5 yr, mean 15 mm): **865 storms, total 13305 mm, mean
   depth 15.4 mm, mean intensity 7.32 mm/hr, max 187 mm** (deterministic, seeded).

### Storm-generator disposition (honest)

The row asks for "a storm-sequence series + statistics chart" and the mission
flagged it as "a forcing knob/utility, maybe not a standalone template". The
generator is spatially-uniform POINT rainfall with no grid/DEM, so forcing it
through the DEM-centric `stage_solve_download` off-box path would be dishonest
ceremony (fetching a DEM to throw it away). It is landed as a **standalone
chart-led template** that runs PrecipitationDistribution IN-PROCESS (landlab is a
server dep; the draw is trivial deterministic CPU, wrapped in `asyncio.to_thread`)
and anchors to an AOI point marker (the disaggregation-template precedent for a
chart-led diagnostic). It is a reusable forcing surface: the same Poisson-draw
logic already feeds `landslide_storm_ensemble`, and the groundwater-seepage front
(below) will consume it.

### Front-away rows: honest STOPs

Both CAND-L rows have their landlab COMPONENT importable in the venv, but a
defensible landing needs more than the class existing:

- **`single_event_landslide_runout_validated` (MassWastingRunout) - STOP / DEFER.**
  MassWastingRunout requires `mass__wasting_id` (a mapped failure-scar cell
  labelling), `soil__thickness`, and `particle__diameter` as INPUT fields. There
  is no failure-scar input surface in TRID3NT, and the row's value rests on the
  published 2021 Cascade Mountains (US) scar + field DEM-of-Difference V&V - a
  specific dataset not reachable through our generic fetchers. A generic-AOI
  runout with a synthetic scar would be a demo without the paper-first V&V. This
  is the ~2-3 h front the shortlist already classes it as (#29). Defer.

- **`aquifer_storm_seepage_hydrograph` (GroundwaterDupuitPercolator) - STOP,
  recommended as the NEXT front.** The component exposes
  `surface_water__specific_discharge` (seepage) as an output and its
  `recharge_rate` composes cleanly with the storm generator landed here; the V&V
  is a self-contained mass-conservation gate (<1% cumulative-flux error, no
  external field data). BUT it is the FIRST groundwater solver chain: it needs a
  new adaptive-dt time-stepping loop (courant/vn coefficients), seepage-flux ->
  hydrograph aggregation, and the board's mandatory `constant_recharge_mass_
  balance_gate` V&V harness as a prerequisite. That is a genuine front (CAND-L,
  ~2-3 h); rushing a first-of-its-kind chain's V&V alongside three full templates
  would violate the fidelity-ladder / canonical-case-V&V doctrine. Land it as a
  dedicated next front (it composes with the storm generator; highest-value next
  Landlab row).

## Consequences

- New worker analyses `channel_incision` + `chi_map` (component_chain.py dispatch
  + `_JSON_SAFE_EXTRA_ANALYSES`); `storm_sequence` is composer-only (not a worker
  analysis).
- New contract knobs on `LandlabRunArgs` (k_bedrock/m_sp/n_sp/uplift/duration/
  steps/hillslope-diffusivity for incision; reference_concavity for chi;
  storm_total_years/random_seed for the generator) + normalizer aliases.
- Three new `LayerURI` carriers: `LandlabChannelIncisionLayerURI`,
  `LandlabChiMapLayerURI`, `LandlabStormSequenceLayerURI`.
- Style presets REUSED (NAMED RESIDUALS): evolved elevation -> `continuous_dem`;
  ksn -> `continuous_slope`; chi -> `continuous_drainage_area`. Dedicated chi /
  ksn ramps are named residuals, not new presets.
- Pins bumped: registry 228 -> 231 (test_catalog_surfacing), EXPECTED_TEMPLATES
  +3 (test_door_dissolution); categories.py PRIMARY + secondary entries added.
- Retrieval-corpus-first honoured: corpus.yaml queries for each; model-free
  `retrieve_visible_tools(q, None, 8)` surfaces all three (test_door_dissolution
  green).
- Proofs (default path, over Esri): docs/proof/templates/
  landlab_channel_incision_steady_state{,_chart}.png,
  landlab_channel_steepness_chi_map{,_chart}.png,
  landlab_storm_sequence_generator{,_chart}.png.
