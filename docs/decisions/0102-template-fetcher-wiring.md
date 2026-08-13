# ADR 0102 -- Template input-provenance: wire the have-but-not-wired fetchers

Status: accepted (2026-08-03)
Follows: the template input-provenance audit
(`docs/validation/template-input-provenance-audit.md`) MISSING-FETCHER queue
("have-but-not-wired" half) + ADR 0091 (the gated-fallback pattern). Provenance
chain WAVE 1 (NATE-approved).

## Context

The audit found ~six engines whose PHYSICALLY DOMINANT model inputs were baked
demo constants (or an AOI-centroid) even though a REGISTERED fetcher already
existed to source the real value. This wave wires those fetchers so the constant
is replaced by real data, with a typed input gate as the fallback (never a silent
revert to the constant, per ADR 0091). Structured per-input provenance lists are
WAVE 2; here the provenance is honest prose in the result envelope + docstrings.

## Decision -- per target

Every wired fetch resolves the fetcher via seam-1
(`TOOL_REGISTRY[name].fn`), never a module internal. Every wired path keeps a
typed failure that names the explicit manual-param retry.

1. **GeoClaw dam-break** -- `geoclaw_inundation`. Dam location + released-column
   height were invented (AOI centroid + baked 10 m). Now resolved from the USACE
   National Inventory of Dams (`fetch_usace_dams`, seam-1) via a new
   `nid_dams.resolve_nid_dam` helper: by `dam_name` when supplied, else the NID
   dam nearest the AOI centroid; `DAM_HEIGHT` (feet) -> metres, `NID_STORAGE`
   surfaced. No NID dam for the AOI (or a named dam not found) AND the user did
   not supply both `source_lonlat` + `dam_break_depth_m` -> typed
   `GEOCLAW_DAM_INPUT_REQUIRED` gate. The dam provenance rides the new additive
   `GeoClawDepthLayerURI.source_note` into narration (moving the
   invented-source-vs-real-dam honesty off stdout onto the envelope). RESIDUAL:
   the tsunami synthetic-Okada stdout banner (worker `setrun_builder.py`) is a
   separate worker-stdout concern -> wave 2 (named, not touched).

2. **SWMM urban_flood** -- `model_urban_flood_swmm`. The Atlas-14 design-storm
   lookup was already wired but SILENTLY fell back to a baked 120 mm on failure.
   Now: seam-1 for the lookup + on failure a typed `SWMM_PRECIP_LOOKUP_FAILED`
   gate naming `total_rain_depth_mm` (ADR 0091). The 120 mm literal in
   `raster_cell_mesh.build_swmm_mesh` is relabeled as a mechanical last-resort for
   direct builder callers ONLY (the composer never reaches it).

3. **Landlab landslide/overland** -- `landlab_susceptibility`. The triggering
   rainfall is now sourced from the NOAA Atlas-14 design storm (seam-1) for the
   AOI (`rainfall_return_period_yr` / `storm_duration_hr`): overland-flow
   `rainfall_intensity_mm_hr = depth / duration` (clean unit conversion),
   landslide `recharge_mm_day = design-storm total depth as a 1-day pulse` (a
   defensible triggering-scenario proxy; the burst-intensity extrapolation
   `depth*24/duration` was REJECTED -- it over-saturates the steady-state wetness
   index, ~900 mm/day for a 2-hr 100-yr storm). Failed lookup -> typed
   `LANDLAB_RAINFALL_INPUT_REQUIRED`. The SOIL block STAYS demo-defaulted
   (no SSURGO/POLARIS fetcher yet) and is labeled in the additive
   `LandlabSusceptibilityLayerURI.source_note`. RESIDUAL: the soil block is the
   missing-fetcher half (future spec).

4. **TELEMAC river dye** -- `model_river_dye_release_scenario`. The carrier
   discharge that governs dilution was a hidden worker constant (250 m3/s). Now:
   a new `discharge_m3s` param + `_resolve_reach_discharge` fetches NOAA NWM
   streamflow (`fetch_noaa_nwm_streamflow`, seam-1) at the reach seed and picks
   the nearest reach's `streamflow_cms`, set into `reach["inflow_q_m3s"]`. A miss
   -> typed `TELEMAC_DISCHARGE_INPUT_REQUIRED` naming `discharge_m3s`. This is a
   boundary-condition seam INDEPENDENT of the fresh bank_source work at HEAD
   (the worker width-heuristic only fired on the 250 default, which a resolved
   value now supersedes). Not disturbed.

5. **SFINCS nws-event composer** -- `model_nws_flood_event_scenario`.
   CHARACTERIZE + SKIP (no code). The composer handles only "Flood Warning" /
   "Flash Flood Warning" (pluvial/fluvial) and forces the SFINCS run with REAL
   OBSERVED forcing: `fetch_mrms_qpe` gauge-corrected radar QPE (via the delegated
   `model_flood_scenario` `forcing_raster_uri` path). CO-OPS tide/surge is
   genuinely out of scope -- there is no coastal-flood/storm-surge warning type in
   the pipeline, so wiring CO-OPS would be a NEW coastal capability, not a
   fetcher-wiring gap-fill. `model_flood_scenario` already owns the full
   CO-OPS->GTSM->parametric surge auto-wire for a coastal/compound run. No
   flood-seam file touched -> flood canary NOT mandated.

## Consequences

- Two additive contract fields: `GeoClawDepthLayerURI.source_note`,
  `LandlabSusceptibilityLayerURI.source_note` (both default None, backward
  compatible). No enum grown.
- New typed gates: `GEOCLAW_DAM_INPUT_REQUIRED`, `SWMM_PRECIP_LOOKUP_FAILED`,
  `LANDLAB_RAINFALL_INPUT_REQUIRED`, `TELEMAC_DISCHARGE_INPUT_REQUIRED`.
- New tool params: `geoclaw_inundation.dam_name` (+ `dam_break_depth_m` nullable),
  `landlab_susceptibility.rainfall_return_period_yr`,
  `telemac_river_dye.discharge_m3s` (+ composer `discharge_m3s`).
- New component: `workflows/geoclaw/nid_dams.py` (lazy geopandas/boto3).
- Registry UNCHANGED at 175 (no new registered tool; params added to existing
  tools). Coded-tool / coded-fetcher counts UNCHANGED.
- Offline suite baseline preserved at EXACTLY 9 (`test_fetch_resolution_gate` x4
  + `test_run_river_dye_scenario` x5). The river_dye members are identical-in-kind
  after the change (4 byte-identical; `test_tool_rejects_both_location_and_bbox`
  shifts its unexpected downstream code
  `TELEMAC_DYE_STAGING_FAILED` -> `TELEMAC_DISCHARGE_INPUT_REQUIRED` -- still a
  failing broken-input-validation test, count preserved). The composer test mock
  (`_install_composer_mocks`) gained a `_resolve_reach_discharge` fake so the 3
  composer tests reach their pre-existing stale-`_fake_publish` failure unchanged.
- LIVE fetch-level proofs (MinIO env): GeoClaw resolved "Folsom Left Wing"
  (44.2 m / 145 ft, 1.12M acre-ft, American River) + ocean-bbox gate; SWMM
  Atlas-14 289.6 mm (Houston 100yr/6hr); Landlab Atlas-14 74.9 mm ->
  overland 37.5 mm/hr / landslide recharge 75 mm/day; TELEMAC NWM 230.6 m3/s at
  the Snake reach + explicit override. Full solver solves not run this wave
  (manifest/fetch-level provenance proof; the engines' own solve paths unchanged).
