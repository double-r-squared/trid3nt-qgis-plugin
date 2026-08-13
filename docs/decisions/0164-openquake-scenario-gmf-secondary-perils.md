# ADR 0164 - OpenQuake scenario ground-motion field + earthquake secondary-perils front

Date: 2026-08-06

Status: accepted

## Context

The M/L sign-off shortlist ranked an OpenQuake secondary-perils front (front #14,
~6 rows: scenario-liquefaction, event-based-liquefaction, newmark-landslide,
probabilistic-landslide-susceptibility + adjacent site-model rows) that "feeds off
the signed scenario GMF". ADR 0149 had deferred the scenario-rupture GMF
realization set (board row 4) as a validated STOP with a deck-confirmed recipe:
`calculation_mode = scenario` is a distinct calculator, not a classical-PSHA deck
extension. Building the secondary perils therefore requires building the scenario
GMF machinery FIRST.

Triage against the installed engine (oq 3.25.1):
- The scenario deck (region grid + `rupture_model_file` simpleFaultRupture +
  `ground_motion_correlation_model = JB2009` + `number_of_ground_motion_fields`)
  runs verbatim and exports `avg_gmf`, giving per site the mean `gmv_<IMT>` and
  the across-realization geometric std `gsd_<IMT>` (re-confirmed on a Bay-Area
  grid: 55 sites, 100 GMFs, ~8 s).
- `openquake.sep` IS importable and rich: liquefaction (Zhu 2015/2017, HAZUS,
  Rashidian-Baise, Allstadt) + landslide (Jibson 2007/2000 Newmark,
  Nowicki-Jessee, infinite-slope FS). All pure-numpy - drivable as playground
  math over the exported GMF field, no engine sec_peril datastore wiring needed.
- `pysheds` 0.4 is BROKEN in this env (a NEP-50 nodata cast TypeError); `richdem`
  works and computes the compound topographic index (D-infinity accumulation).

## Decision

Land two additive engine templates (registry 222 -> 224, EXPECTED_TEMPLATES
64 -> 66); both run the OpenQuake engine IN-PROCESS (the installed `oq` CLI as a
subprocess of the composer - NO container image, NO Batch dispatch, NO reuse of
the classical-PSHA `run_solver`/`job_ini.py` worker path, so that byte-identical
classical deck stays untouched and its logic-tree-fold test unaffected).

- `openquake_scenario_gmf` (`workflows/openquake/scenario_gmf/`): renders a
  self-contained scenario `job.ini` + `rupture_model.xml`, runs `oq engine --run`
  off the event loop, parses `avg_gmf`, and maps the MEAN motion (primary COG,
  PGA magma preset) + the across-realization SPREAD `gsd` (context COG, new
  `continuous_gmf_spread` viridis preset), plus a mean +- spread realization
  chart. Returns `ScenarioGmfLayerURI`. The reusable `run_scenario_gmf()` is the
  shared spine both templates call.
- `openquake_secondary_perils` (`workflows/openquake/secondary_perils/`): rides
  `run_scenario_gmf`, fetches the AOI DEM (`fetch_copernicus_dem`), derives the
  terrain covariates, and applies the `openquake.sep` models per site:
  - Liquefaction: Zhu et al. (2015) global model from scenario PGA + magnitude +
    Vs30 (slope-derived, Wald and Allen 2007 active-tectonic table) + compound
    topographic index (richdem, LOUD typed fallback to a labelled default CTI).
  - Landslide: Newmark screen - infinite-slope factor of safety from DEM slope +
    labelled shallow-soil strength -> yield acceleration; Jibson (2007)
    displacement; Jibson et al. (2000) probability.
  A coarse scenario cell is sampled by a half-site-spacing WINDOW and the
  governing sub-cell condition per peril (85th-pct slope for landslide; Vs30 of
  the 15th-pct gradient + 85th-pct CTI for liquefaction). Publishes the
  liquefaction probability COG (primary, new `continuous_liquefaction_probability`
  ylgnbu preset) + a landslide probability COG (context, existing
  `continuous_landslide_susceptibility`) + a per-site peril-distribution chart.
  Returns `SecondaryPerilLayerURI`.

Rupture geometry is prompt-interpreted and gated: an explicit caller
`rupture_trace` wins; else the longest real GEM Global Active Fault trace in the
AOI (`rupture_kind="real-fault"`, best-effort fetch); else a synthetic demo fault
through the AOI centre. Magnitude + rupture geometry ride the ADR-0107
input-review gate (labelled in auto mode, reviewable in user_gated). Both
templates join `SOLVER_CONFIRM_TOOLS` with an inline proceed/cancel card
(Invariant 9). Site-data provenance is stated honestly on every layer
(`site_data_note`): PGA/PGV + magnitude are engine output; Vs30 + CTI are
DEM-derived; soil strength parameters are labelled screening defaults.

## Consequence

- Two new contract types (`ScenarioGmfLayerURI`, `SecondaryPerilLayerURI`,
  both `LayerURI` subclasses) and two style presets. All additive.
- Live evidence (East Bay AOI, M6.9 on the fetched Hayward-fault trace,
  real-fault rupture): scenario mean PGA peaks along the trace (max 0.59 g,
  median spread factor 1.69, 55 sites); liquefaction fires in the soft bayshore
  flats (max prob 0.79) and vanishes in the hills; Newmark landslide fires on the
  steep hill sub-slopes (max prob 0.14). Six proofs under
  `docs/proof/templates/openquake_scenario_gmf_*` /
  `openquake_secondary_perils_*`.
- Rows unblocked (shortlist #23 + front #14): scenario-liquefaction-probability,
  newmark-landslide, probabilistic-landslide-susceptibility (as a scenario
  screen), event-based-liquefaction (mechanism shared), plus the scenario-rupture
  GMF realization-set row (ADR 0149 row 4) itself.
- The classical-PSHA worker (`services/workers/openquake/job_ini.py`) is NOT
  touched - the scenario deck is self-contained in the template module, so this
  front adds zero regression surface to `openquake_psha`.
- Compute lane: on-box `oq` only. A cloud/Batch deployment would need a distinct
  scenario worker path (out of scope for the offline build).
