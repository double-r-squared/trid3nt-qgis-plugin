# ADR 0204 - Rain-on-grid replication on the Ball Creek fork (Coweeta), executed

Date: 2026-08-09
Status: EXECUTED (the ADR 0202 path-1 unblock). The Godara et al. 2024 rain-on-grid
protocol is run computed-vs-observed against the real USFS/EDI Ball Creek weir #9
gauge, graded deterministically (NSE/R2 via `compute_skill_metrics`). Result: an
HONEST PARTIAL replication -- the flash-flood PEAK MAGNITUDE is reproduced and
calibratable, but the installed constant-rain engine + coarse mesh + static SCS-CN
land the hourly NSE well BELOW the paper's 0.70-0.90 (a documented screening-grade
finding, not the paper's refinement-grade skill).
Source: Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205.
Builds on: ADR 0202 (STOPPED on coverage gap), 0203 (AORC + LTER fetchers, LANDED),
0195/0196 (RoG machinery + TELEMAC RoG template), 0193 (Coweeta watershed mesh).

## 1. Domain re-cut: the Ball Creek weir #9 catchment (the 0202 spatial caveat)

ADR 0202 flagged that our old pour point (-83.40402) sits EAST of the Coweeta basin
and drains ~28.7 km2 of Coweeta Creek BELOW the lab, whereas the gauge (Ball Creek
weir #9, EDI knb-lter-cwt.3037/19) drains only the Ball Creek FORK. This wave re-cuts
the domain to that fork.

The EML carries only the whole-basin bounding box (CWTBASIN 21.85 km2), not the weir
point, so the pour point was located EMPIRICALLY from the conditioned Copernicus
GLO-30 flow network (`scripts/sandbox/replication/ballcreek_delineate_explore.py`):
the basin outlet (max flow accumulation) -> walk the main stem up to the first major
confluence (two inflows each >25% of basin accumulation) = the Ball Creek / Shope
Fork junction at (-83.429, 35.060).

Fork identity (decisive, not assumed): NHD/GNIS named reaches place **'Ball Creek'**
centroid at (-83.4451, 35.0466) = the SOUTH fork, and **'Shope Fork'** at
(-83.4496, 35.0589) = the NORTH fork. The EML centroids of the two sibling gauged
watersheds WS18 (Grady Branch, knb-lter-cwt.3033) and WS27 (3034) both fall INSIDE
the south-fork polygon, confirming Ball Creek = the south fork.

- Weir pour point: **(-83.43131, 35.05701)** -- a channel cell +12 D8-cells up the
  Ball Creek fork so the mesh acquisition's 8-cell max-accumulation snap resolves to
  the fork outlet **(-83.42971, 35.05921)** rather than jumping to the higher-accumulation
  Coweeta Creek merged stem (which sits <8 cells away near the confluence).
- Delineated area: **7.24 km2** (Copernicus GLO-30). Shope Fork = 8.41 km2.
- Reconciliation (honest): Ball(7.24) + Shope(8.41) + intervening = CWTBASIN. On
  GLO-30 the whole basin delineates to 18.06 km2, UNDER-capturing the surveyed
  21.85 km2 by ~17%. Ball Creek is ~40% of the GLO-30 basin (the smaller of the two
  comparable forks), ~33% of the documented 21.85 km2; scaled for the DEM
  under-capture the true fork is ~8.7 km2. "Roughly half-ish of 21.85" reads
  honestly as "the smaller of the two roughly-equal main forks."
- Mesh (`acquire_watershed_mesh`): 1834 nodes / 3535 triangles, EPSG:32617, 3DEP
  bare-earth bed, NLCD-distributed CN 75-89 + Manning 0.05-0.20. Provenance:
  `scripts/sandbox/replication/ballcreek_delineation_provenance.json`.

## 2. Events (2014-2019 gauge x 1979-present AORC overlap)

Forcing is the AORC AOI-mean hyetograph (`fetch_aorc_precip`, ADR 0203) over the
Ball Creek catchment bbox (-83.4733, 35.0281, -83.4219, 35.0601); observed discharge
is `fetch_lter_records` (Ball Creek weir #9, hourly m3/s). All windows and baseflow
are pinned in `scripts/sandbox/replication/rog_ballcreek_events.py`.

| Event | Role | t_rise | Obs peak | AORC 24h-burst intensity | Pre-event baseflow | 5-day antecedent |
| --- | --- | --- | --- | --- | --- | --- |
| Dec 2015 | CALIBRATION (single-storm) | 2015-12-23 18:00 | 8.60 m3/s @ Dec-24 07:00 | 8.48 mm/hr | 0.484 m3/s | 52.6 mm |
| Feb 2018 | VALIDATION (single-storm, split-sample) | 2018-02-10 19:00 | 5.67 m3/s @ Feb-11 11:00 | 3.73 mm/hr | 0.550 m3/s | 41.3 mm |
| Dec 2015 (full) | MULTI-PEAK negative control | 2015-12-23 18:00 | 8.60 (h13) + 5.99 (h126) | 8.48 mm/hr | 0.484 m3/s | 52.6 mm |

Dec 2015 is the record's ONLY large flash flood; every other single-storm event in
2014-2019 has a low 24h-burst intensity (Aug 2018 2.56, May 2018 3.29, Feb 2016 3.51
mm/hr) -- a data constraint on the split-sample that is itself a finding.

## 3. Installed-engine + mesh constraints (the physics that bounds the result)

1. **Constant rain (RAINDEF=1, ADR 0195/0196).** TELEMAC v9.0.0 ingests only a
   constant rain intensity; a time-varying hyetograph needs a `user_rain.f`
   recompile. Each event is driven as a constant design storm = the AORC 24h
   max-burst mean intensity, for a rain window (native keyword DURATION OF RAIN OR
   EVAPORATION IN HOURS / RAIN_HDUR -- wired this wave), after which rain stops and
   the catchment drains (the recession limb).
2. **Mass-balance peak cap.** Constant-rain outlet runoff cannot exceed
   excess_rate x area, so a flashy sub-daily peak driven by a burst above the
   multi-hour-mean intensity is structurally under-representable. The AORC precip
   peak (Dec-24 11:00) LAGS the gauge peak (07:00) by ~4 h -- a forcing timing
   inconsistency at this ~7 km2 scale that no calibration can remove.
3. **Coarse-mesh low-flow conveyance threshold.** On the 30-200 m TIN a thin
   low-intensity overland sheet (~0.15-0.26 m) never establishes drainage to the
   outlet -- it ponds mid-hillslope. Verified on Feb 2018 (0.15 m sheet, outlet
   peak 0). Stream-burning the channel (4 m incision, 60 m radius) was tried and
   did NOT resolve it (peak still ~0) -- the binding limit is the sub-storm runoff
   VOLUME, not just channel definition. Only the high-intensity Dec 2015 event
   builds enough depth to drain on this mesh.
4. **Static SCS-CN across antecedent regimes.** A single calibrated CN gives very
   different runoff coefficients by total rain depth (SCS nonlinearity), so it does
   NOT transfer from the big Dec storm to the saturated-basin Feb event (section 5).

## 4. Worker changes (image rebuilt + behavior smoke, this wave)

Both are in `services/workers/telemac/rog_build.py`; the `trid3nt-local/telemac:latest`
image was rebuilt (new id, `docker history` GRACE-2 refs = 0, keyword baked) and
behavior-smoked THROUGH the image.

1. **RAIN_HDUR emission.** `author_rog_deck` now emits `DURATION OF RAIN OR
   EVAPORATION IN HOURS = rain_duration_s/3600` when `rain_duration_s < duration_s`
   (both native + preprocessing paths), so rain stops and a recession develops.
   `rain_duration_s` was already a `ReachConfig` field -> no parser bump. Smoke:
   rain stops at the window and Q recedes (6.3 -> 2.5 -> 1.4 -> 0.64 m3/s); the
   Manning lever is confirmed effective (0.5x peak 0.09 vs 2.0x 0.0007).
2. **`classify_outlet` contiguity fix (latent bug).** The outlet segment was the k
   GLOBALLY-nearest ring nodes, which need not be contiguous on the boundary -> an
   isolated liquid node between two walls aborts the solver (`FRONT2: LIQUID POINT
   BETWEEN TWO SOLID POINTS`) on any mesh where the Coweeta accident of contiguity
   does not hold (hit on Ball Creek). Now a contiguous ring-arc centred on the
   nearest node. 7/7 pure `test_rog_build` tests pass.

## 5. Results (deterministic; computed vs observed, hourly)

Calibration levers on Dec 2015: uniform CN2 = 55, AMC II, Manning scale 1.0, initial
abstraction ratio 0.2 (a bounded search; trials below). Baseflow handling: the RoG
is an event model with no subsurface return flow, so the observed pre-event baseflow
is added to the computed runoff as a constant (total-vs-total); a peak-aligned NSE
(computed shifted so its peak coincides with the observed peak) is also reported to
isolate hydrograph SHAPE skill from the constant-rain + AORC-lag timing offset.

Calibration trials (Dec 2015, Manning 1.0, all through the rebuilt image):

| AMC | CN2 | raw NSE | R2 | peak-aligned NSE | aligned R2 | peak comp | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| II | 55 | -1.41 | 0.04 | +0.04 | 0.67 | 8.46 | peak match -1.7%, BEST |
| II | 60 | -1.48 | 0.09 | -0.20 | 0.55 | 9.67 | over-peaks |
| II | 65 | -1.75 | 0.16 | -0.75 | 0.47 | 10.4 | over-peaks |
| II | 70 | -2.32 | 0.27 | -1.87 | 0.38 | 14.3 | over |
| III | 50 | -2.31 | 0.26 | -1.81 | 0.39 | 14.3 | AMC III saturates runoff |
| III | 80 | -6.17 | 0.69 | -8.5 | 0.22 | 17.0 | good rising-limb corr, 2x over |

Final calibrated params: **CN2 = 55, AMC II, Manning x1.0, Ia 0.2** (best raw NSE +
near-exact peak).

Results table (calibrated params UNCHANGED across events):

| Event | NSE (raw) | R2 | peak-aligned NSE / R2 | peak comp / obs (err) | runoff vol comp / obs (err) | timing lag |
| --- | --- | --- | --- | --- | --- | --- |
| Dec 2015 (calibration) | -1.41 | 0.036 | +0.036 / 0.675 | 8.46 / 8.60 (-1.7%) | 312 / 651 x10^3 m3 (-52%) | +11 h |
| Feb 2018 (validation, CN 55) | -1.90 | 0.32 | -- | 0.55 / 5.67 (-100%) | ~0 / -- | -- (ponds) |
| Feb 2018 (standalone, CN 90) | -0.87 | -- | +0.44 / 0.84 | 5.91 / 5.67 (+4%) | -- | +8 h |
| Dec 2015 (multi-peak) | -1.38 | 0.089 | -0.47 / 0.57 | 8.46 / 8.60 (-1.7%) | 330 / 1257 x10^3 m3 (-74%) | +11 h |

Continuity (relative volume error) was O(1e-15) on every solve -- mass is conserved
exactly; the skill gap is physics/forcing fidelity, not numerics.

### Calibration event (Dec 2015)
The constant design storm reproduces the flash-flood PEAK MAGNITUDE (8.46 vs 8.60
m3/s, -1.7%) and, with the +11 h timing offset removed, the hydrograph SHAPE
(peak-aligned R2 0.675). Raw NSE is -1.41 because (a) constant rain places the
modelled peak at rain-end (~h22) while the gauge peaks at h13 -- an ~11 h offset the
AORC precip-peak-lags-gauge-peak inconsistency compounds -- and (b) the RoG produces
no subsurface return flow, so its recession falls below the observed baseflow-supported
tail (computed event volume is 52% of observed; the missing half IS that tail).

### Validation event (Feb 2018) -- split-sample
With the Dec-calibrated CN 55 applied UNCHANGED, Feb 2018 produces essentially NO
modelled outlet flow (peak 0.55 = the baseflow constant; a 0.15 m sheet ponds
mid-hillslope) against an observed 5.67 m3/s -- the split-sample FAILS. The cause
is diagnostic, not a solver error: Feb 2018's real runoff coefficient is ~75% (a
saturated basin producing 5.67 m3/s from 3.73 mm/hr), which static SCS-CN at CN 55
cannot generate from only 89 mm of cumulative rain (~10% runoff). Run STANDALONE at
CN 90 the same event is reproduced well -- peak 5.91 vs 5.67 (+4%), peak-aligned
NSE +0.44 / R2 0.84 -- so the event IS modellable; it is the single-CN TRANSFER
that fails. Both single storms are individually peak-matchable (Dec at CN 55, Feb at
CN 90); a static curve number does not transfer across their different
antecedent-saturation states, and the coarse mesh cannot convey Feb's thin
low-intensity sheet even where runoff is generated. An honest split-sample limit.

### Multi-peak negative control (Dec 2015 full)
The paper's structural failure is reproduced and QUANTIFIED. The observed hydrograph
has two peaks -- 8.60 m3/s at h13 and 5.99 m3/s at h126 (Dec 29, a second frontal
burst) -- with sustained baseflow-supported flow between them (inter-peak observed
mean 2.454 m3/s). The RoG, driven by the single design-storm pulse and calibrated CN,
produces ONE hump (peak 8.46, matching peak 1) and then drains toward baseflow:

- the SECOND peak is NOT reproduced (`comp_second_peak_reproduced = false`) -- a
  single constant-rain pulse has no second burst, and even if forced the model has
  no soil store to carry antecedent wetness into it;
- the inter-peak flow is UNDER-estimated by 48% (computed mean 1.275 vs observed
  2.454 m3/s) -- the "infiltrated water permanently lost, no subsurface return flow"
  signature: between storms the real catchment stays baseflow/interflow-supported
  while the RoG recedes to near baseflow;
- over the full 8-day window the computed event volume is only 26% of observed
  (-74%), the missing three-quarters being exactly the sustained interflow the
  event model cannot generate.

This is the applicability-envelope boundary the ADR 0195/0196 docstring warns about,
now measured against a real multi-peak gauge record.

## 6. Honest comparison to the paper

Godara et al. report single-storm NSE 0.70-0.90 / R2 0.93-0.95, achieved with a
time-varying hyetograph on a fine channel-resolving mesh. Our installed
constant-rain build lands the raw single-storm NSE at -1.41 (peak-aligned +0.04),
i.e. SCREENING-GRADE: it estimates the flash-flood peak magnitude and general shape
but not the hourly hydrograph. The gap is fully attributable to documented
limitations, not calibration:

- constant rain (RAINDEF=1) cannot represent the sub-daily intensity structure that
  sets the peak timing -> the ~11 h lag (a `user_rain.f` recompile is the fix);
- no subsurface return flow -> the sustained recession tail is missed (a continuous
  soil-moisture model, vs static SCS-CN, is the fix);
- a 30-200 m mesh cannot convey thin low-intensity overland flow -> lower-intensity
  events pond (a finer channel-resolving mesh is the fix);
- static SCS-CN does not transfer a single CN across antecedent-saturation regimes.

This is the fidelity-ladder verdict for rain-on-grid on the installed stack.

## 7. Consequences

- +0 registered tools; two worker functions changed + image rebuilt + smoke; the
  RoG flood seam was exercised end-to-end (peak-depth field + outlet hydrograph),
  the flood-canary spirit met on the Ball Creek catchment.
- Drivers (all `py_compile` clean) in `scripts/sandbox/replication/`:
  `ballcreek_delineate_explore.py`, `ballcreek_delineation_provenance.json`,
  `rog_ballcreek_live.py`, `rog_ballcreek_events.py`, `rog_ballcreek_calib.py`,
  `rog_ballcreek_final.py`, `rog_ballcreek_proofs.py`, `ballcreek_events.json`.
- Proofs (`docs/proof/templates/`): `telemac_rain_on_grid_replication_chart.png`
  (validation), `telemac_rain_on_grid_multipeak_chart.png` (negative control),
  `telemac_rain_on_grid_calibration_chart.png` (calibration).
- The ADR 0195/0196 constant-design-storm template proofs remain valid; this ADR
  adds the graded computed-vs-observed replication the template smoke was not.
