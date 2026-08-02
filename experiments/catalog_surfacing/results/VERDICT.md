# Catalog-surfacing -- Design 3 (stratified data pool) VERDICT

**Winner: NO_ADVANCE**

Arm 3 = auto-trigger composed declaration (docs/specs/stratified-pools.md): harness-side source-stratum retrieval declares ONE generic fetcher whose `source` enum is the matched candidates in rank order, full source cards in context; the model NEVER initiates discovery. Deterministic grading (selected `source` vs acceptable set + router.validate_params), N=1, temp 0.

## Headline metrics (arm3 vs arm0 baseline)

| Arm | Selection | First-attempt | One-retry | Graded | Sel-correct | Upstream excl | Ambient declarable |
|-----|-----------|---------------|-----------|--------|-------------|---------------|--------------------|
| 0 baseline | 60.58 | 80.95 | 100.0 | 104 | 63 | 0 | 170 |
| 3 Design 3 | 57.69 | 100.0 | 100.0 | 104 | 60 | 0 | 156 |

PASS bars (recomputed from arm0): selection >= 60.58, first-attempt >= 75.95, one-retry >= 100.0, controls valid.

Beat-baseline hypothesis: arm3 selection 57.69 <= arm0 60.58 -> does NOT beat baseline (hypothesis, not a criterion).

Diagnostics: 97/104 source asks activated the stratum (8 via escalation); 28 NO_CALL (weak-model, no tool emitted).

## Reachability precondition (model-free, source stratum)

- recall 1.0 (104/104)

## Controls gate (amended: leakage invalidates; jitter -> N=3 majority)

- controls: 19 | identical to arm0: 16 | leakage: 0 | passes: True
- JITTER resolved by N=3 majority (non-leakage, non-invalidating):
  - control#0 (acc=fetch_nws_alerts_conus): arm3=None majority=None trials=[None, None, None]
  - control#1 (acc=fetch_fema_nfhl_zones): arm3=None majority=fetch_fema_nfhl_zones trials=['fetch_fema_nfhl_zones', 'fetch_fema_nfhl_zones', 'fetch_fema_nfhl_zones']
  - control#4 (acc=run_sfincs): arm3=None majority=geocode_location trials=['geocode_location', None, 'fetch_gridmet']

## Per-source selection accuracy (arm0 -> arm3)

| Source | n | arm0 | arm3 |
|--------|---|------|------|
| fetch_cdc_svi | 5 | 80.0 | 60.0 |
| fetch_census_acs | 12 | 58.33 | 66.67 |
| fetch_esri_landcover_10m | 12 | 41.67 | 75.0 |
| fetch_gridmet | 12 | 25.0 | 25.0 |
| fetch_hifld_critical_infrastructure | 12 | 75.0 | 50.0 |
| fetch_hifld_transmission_lines | 5 | 100.0 | 80.0 |
| fetch_mtbs_burn_severity | 5 | 100.0 | 40.0 |
| fetch_nhd_waterbodies | 5 | 100.0 | 80.0 |
| fetch_nhdplus_nldi_navigate | 5 | 20.0 | 40.0 |
| fetch_nifc_fire_perimeters | 5 | 60.0 | 60.0 |
| fetch_noaa_coops_currents | 5 | 100.0 | 60.0 |
| fetch_noaa_coops_tides | 11 | 27.27 | 27.27 |
| fetch_us_drought_monitor | 5 | 80.0 | 100.0 |
| fetch_usgs_water_quality | 5 | 80.0 | 100.0 |

