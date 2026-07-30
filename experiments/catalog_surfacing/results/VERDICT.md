# Catalog-surfacing experiment -- VERDICT

**Winner: INVALID**

Control identity check FAILED -- a surfacing change leaked into unrelated routing; the run is INVALID (fix + re-run).

Deterministic grading (selected NAME vs acceptable set + router.validate_params pass/fail); model-in-the-loop selection/param formation; N=1, temperature 0. Pre-registered criteria + NATE 2026-07-30 favored-arm rule (Design 2 favored).

## Headline metrics

| Arm | Selection acc | First-attempt validity | One-retry validity | Graded (den) | Sel-correct | Upstream excl | Ambient declarable |
|-----|---------------|------------------------|--------------------|--------------|-------------|---------------|--------------------|
| 0 | 60.58 | 80.95 | 100.0 | 104 | 63 | 0 | 170 |  <- baseline
| 1 | 0.0 | 0.0 | 0.0 | 104 | 0 | 0 | 156 |
| 2 | 0.96 | 100.0 | 100.0 | 104 | 1 | 0 | 156 |

Arm 0 = baseline (14 ambient tier=general). Arm 1 = Design 1 (card-carried). Arm 2 = Design 2 (discovery-expands-declaration).

## Reachability precondition (model-free)

| Arm | Recall (sources) |
|-----|------------------|
| 0 | 0.9904 (103/104) |
| 1 | 0.9904 (103/104) |
| 2 | 0.9904 (103/104) |

## Controls (route-identity across arms)

- Controls: 19 | identical fired name across all 3 arms: 16 | passes: False
- Divergences (arm0/arm1/arm2 fired):
  - control#11 (acceptable=run_model_groundwater_contamination_scenario): ['run_modflow', None, 'run_modflow']
  - control#3 (acceptable=fetch_dem): ['geocode_location', 'fetch_dem', 'fetch_dem']
  - control#7 (acceptable=geocode_location): ['geocode_location', None, 'geocode_location']

## Advancement

- Design 1 advances vs baseline: False (blocked: ['selection 0.0 < baseline 60.58', 'first-attempt 0.0 < baseline 80.95 - 5.0', 'one-retry 0.0 < baseline 100.0', 'controls identity check FAILED'])
- Design 2 advances vs baseline: False (blocked: ['selection 0.96 < baseline 60.58', 'controls identity check FAILED'])

## Per-source selection accuracy (arm0 baseline -> arm1 / arm2)

| Source | n | arm0 | arm1 | arm2 |
|--------|---|------|------|------|
| fetch_cdc_svi | 5 | 80.0 | 0.0 | 0.0 |
| fetch_census_acs | 12 | 58.33 | 0.0 | 0.0 |
| fetch_esri_landcover_10m | 12 | 41.67 | 0.0 | 0.0 |
| fetch_gridmet | 12 | 25.0 | 0.0 | 0.0 |
| fetch_hifld_critical_infrastructure | 12 | 75.0 | 0.0 | 0.0 |
| fetch_hifld_transmission_lines | 5 | 100.0 | 0.0 | 0.0 |
| fetch_mtbs_burn_severity | 5 | 100.0 | 0.0 | 0.0 |
| fetch_nhd_waterbodies | 5 | 100.0 | 0.0 | 0.0 |
| fetch_nhdplus_nldi_navigate | 5 | 20.0 | 0.0 | 0.0 |
| fetch_nifc_fire_perimeters | 5 | 60.0 | 0.0 | 0.0 |
| fetch_noaa_coops_currents | 5 | 100.0 | 0.0 | 0.0 |
| fetch_noaa_coops_tides | 11 | 27.27 | 0.0 | 0.0 |
| fetch_us_drought_monitor | 5 | 80.0 | 0.0 | 0.0 |
| fetch_usgs_water_quality | 5 | 80.0 | 0.0 | 20.0 |

## Diagnosis (honest read)

Verdict: **INVALID / INCONCLUSIVE -- NEITHER design advances, and the registry-shrink
is NOT decided by this run.** Two independent reasons, plus the mechanism status:

1. Control gate tripped on MODEL NON-DETERMINISM, not a surfacing leak. The 3
   divergences are: control#3 (baseline itself mis-fired geocode_location instead of
   fetch_dem; both design arms fired fetch_dem correctly), control#7 + control#11
   (arm1 produced NO_CALL where arm0/arm2 fired the same tool). All three are the
   weak free model emitting a different/no tool call at temperature 0 -- NOT the
   surfacing change re-routing an unrelated tool. Default config is byte-identical
   (registry 190, declarable 170, fetch_from_catalog signature/docstring unchanged),
   so there is no actual architecture leak; the pre-registered control-identity gate
   is simply over-sensitive to a non-deterministic model.

2. Source selection collapsed in BOTH designs (D1 0.0%, D2 0.96% vs baseline 60.6%)
   because the model almost never invokes the discovery surface -- NOT because the
   mechanisms fail. In arm1 the model called search_data_catalog 11/104 times and
   fetch_from_catalog 0/104 (it fired ambient sibling tools like fetch_raws_weather /
   fetch_hrrr_forecast, or nothing). In arm2 it called search_tools 1/104 -- and that
   ONE time the discovery-expands-declaration seam worked end-to-end
   (search_tools -> fetch_usgs_water_quality declared-by-expansion and fired,
   first-attempt params valid). The Arm-1 2-hop was likewise validated in isolation
   (search_data_catalog card -> fetch_from_catalog(source=...) -> router-validated).

Mechanism status: BOTH arm prerequisites are BUILT + VALIDATED (offline suite green,
9-baseline unchanged; unit tests pass; the one live invocation of each discovery
path succeeded). The blocker is empirical, not structural: the stack's DEFAULT free
model (nvidia/nemotron-3-super-120b:free) will not spontaneously route data needs
through a discovery hop when semantically-adjacent ambient sibling tools are declared.

Recommendation: RE-RUN with a capable model (and/or a system-prompt that steers data
needs to the discovery surface) before deciding the registry-shrink. The favored-arm
tie-break (Design 2) is moot here -- neither design meets the advancement thresholds,
so the pre-registered outcome is NEITHER, run flagged INVALID for the control gate.
The surface-stability principle behind favoring Design 2 is undisturbed by this run.
