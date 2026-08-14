# ADR 0253 - Mixed-clusters triage sweep (NESTOR / ELMFIRE Suppression / SFINCS Wavemaker / HEC-RAS Water Quality). Four families adjudicated knob-or-STOP IN-IMAGE. 0 landings, 0 new tools, 0 image rebuild.

Date: 2026-08-13
Status: accepted
Continues: ADR 0157 (HEC-RAS WQ engine absent), ADR 0238/0243 (SnapWave held), ADR 0239 (ELMFIRE spotting SET_* overwrite precedent), ADR 0240 (GAIA sediment), ADR 0245/0248 (TELEMAC in-image dico triage).

## Context

Triage sweep over four small, deliberately-mixed board clusters, each a
knob-or-STOP boundary case. Engine family confirmed per cluster from the board's
own src links + surrounding roster notes, NEVER the section title. Every verdict
verified IN-IMAGE (docker probe of the baked binary/library) or against the
compiled fortran source the image is built from.

- "### NESTOR" -> ogoe/OpenTelemac sisyphe examples -> TELEMAC (telemac:latest).
- "### Suppression" -> elmfire.io -> ELMFIRE (elmfire:trid3nt-verify, bin 2025.0526).
- "### Wavemaker (infragravity boundary forcing)" -> sfincs.readthedocs.io -> SFINCS (sfincs:latest, sfincs-v2.3.3).
- "### Water Quality / Temperature" -> usgs Mohawk / hecras WQ test PDFs, sits above "### Breach" + "Roster gaps (hecras)" -> HEC-RAS (hecras2025-authoring:latest + hecras:latest). NOT WAQTEL, NOT SWMM, NOT MODFLOW-GWE.

## Decision

Net: 0 landings, 0 new tools, 0 image rebuild, registry unchanged at 254,
EXPECTED_TEMPLATES unchanged. Every row STOPs; recipes below.

### Cluster 1 - NESTOR (TELEMAC dredging), 3 rows -> ONE STOP-RECIPE

The board flagged NESTOR's compiled-library status as unverified (only WAQTEL +
GAIA .so confirmed precompiled). RESOLVED - NESTOR IS precompiled in
telemac:latest: `libnestor4api.so`, `libnestor4telemac2d.so`,
`libnestor4telemac3d.so` under builds/gnu.shared/lib (mirrored in the conda pkg
tree). The keyword family is in the baked v9.0 dicos: gaia.dico (`NESTOR = YES`,
`NESTOR ACTION FILE`, `NESTOR POLYGON FILE`, `NESTOR RESTART FILE`) +
telemac2d.dico (`NESTOR INFO` coupling toggle, `NESTOR ACTION FILE`, `ZRL`
reference level). So the "if .so absent -> one image-rebuild STOP" branch does
NOT apply - the image is ready.

The rows STOP for a different reason: NO NESTOR deck-authoring machinery exists.
The telemac worker authors GAIA v1 (supply-limited) + v2 (erodible-bed) sediment
decks (write_gaia_deck, COUPLING WITH 'GAIA'), but there is ZERO NESTOR code in
server/ or services/workers/telemac/ (grep = clean; only a fixture listing shows
nestor as an available module). Driving NESTOR is a new deck-authoring family,
not a knob: a NESTOR action file (dredge/dump schedule + critical-elevation
dig/dump rules), a NESTOR polygon file (dredge/dump zone geometry), the NESTOR
coupling block layered onto a GAIA base run, plus a multi-year simulation for the
closed-loop critical-elevation case (heavy compute). All 3 rows
(channel_maintenance_dredging, dredge_spoil_disposal_placement,
critical_elevation_triggered_dig_dump) collapse into this one STOP.

RECIPE (NOT an image rebuild): author a NESTOR deck writer on the GAIA v1 base -
action-file + polygon-file emitters + the telemac2d/gaia NESTOR coupling
keywords, dico-pinned against the in-image v9.0 gaia.dico; dredge/dump zone
geometry via the input-review gate (labeled default zones, real US federal
navigation channel AOI); source real bathy/sediment from 3DEP + reach data; a
GAIA sediment base supplies bed evolution. New template family, own build job.

### Cluster 2 - ELMFIRE Suppression (extended attack / SDI), 2 open rows -> STOP-RECIPE

The &SUPPRESSION namelist family is REAL and fully WIRED - verified against the
compiled source the image builds from (third_party/elmfire/build/source,
VERSIONSTRING 'ELMFIRE 2025.0526' == baked binary). elmfire_namelists.f90:769
READ_SUPPRESSION reads /SUPPRESSION/ (ENABLE_EXTENDED_ATTACK, USE_SDI,
USE_SDI_LOG_FUNCTION, SDI_FACTOR, B_SDI, DT_EXTENDED_ATTACK,
MAX_CONTAINMENT_PER_DAY, AREA_NO_CONTAINMENT_CHANGE). elmfire_level_set.f90:1069
gates the extended-attack step, computes DC_PER_DAY (lines 1086-1096) from
MAX_CONTAINMENT_PER_DAY / AREA_NO_CONTAINMENT_CHANGE / log-vs-linear / B_SDI *
SDIBAR, grows TARGET_CONTAINMENT, and CALLs CENTROID + CONTAINMENT
(elmfire_suppression.f90). NOT dead knobs.

SET_* overwrite-trap check (the ADR 0239 spotting precedent - SET_* silently
overwriting scalars): FOUND at elmfire_level_set.f90:185-187 (B_SDI,
MAX_CONTAINMENT_PER_DAY, AREA_NO_CONTAINMENT_CHANGE <- *_PYROME calibration
arrays) - but GUARDED by `IF (RANDOM_IGNITIONS .AND. USE_PYROMES .AND.
CALIBRATION_CONSTANTS_BY_PYROME)`. A single deterministic ignition (our normal
template path) does NOT enter that branch, so the namelist scalars ARE honored.
The trap is real but avoidable - unlike the ungated spotting bounds overwrite.

The rows STOP anyway:
- `extended_attack_suppression_difficulty_index` [CAND-M]: the row's discriminating
  question is SDI-vs-default. USE_SDI=.TRUE. requires an SDI_FILENAME raster
  (elmfire_io.f90:351 READ_BSQ_RASTER SDI from FUELS_AND_TOPOGRAPHY_DIRECTORY) -
  a Suppression Difficulty Index field that is NOT fetchable (no fetcher; a
  derived analytic product; fabricating it = inventing physics, out of scope for
  the input-review gate). The default areal-growth half (USE_SDI=.FALSE.) IS
  namelist-only, but is not a knob either: the deck_builder emits ZERO
  &SUPPRESSION (grep = 0), so it needs a new namelist-authoring leg + a multi-day
  simulation (DC_PER_DAY is per-DAY; containment only bites over days, not the
  usual hours-long point-ignition run) + a new containment-curve output. A
  template build, not a sweep knob.
- `combined_initial_extended_attack_pipeline` [CAND-L]: chains the in-engine
  ENABLE_INITIAL_ATTACK stage (distinct from the shipped closed-form Hirsch tool,
  ADR 0190) + extended attack over a multi-day sim. Board's own note: "inferred
  composite, not directly evidenced" - no published worked example chains both.
  Un-evidenced composite + heavy -> defer (paper-first-replication norm).

RECIPE: add &SUPPRESSION authoring to deck_builder (ENABLE_EXTENDED_ATTACK +
MAX_CONTAINMENT_PER_DAY / AREA_NO_CONTAINMENT_CHANGE / USE_SDI_LOG_FUNCTION,
single-ignition deterministic to dodge the pyrome overwrite) + a multi-day
weather stream (RAWS/ASOS fetchable) + a with-vs-without containment/burned-area
output. The SDI comparison stays blocked until an SDI raster source lands.

### Cluster 3 - SFINCS Wavemaker (infragravity forcing), 2 rows -> STOP

The wavemaker module IS baked (sfincs_wavemaker.f90 compiled into
/usr/local/bin/sfincs, sfincs-v2.3.3). TWO feed paths are visible in the binary:
(a) a standalone external path - "Info: reading FEWS compatible netcdf ...
boundaries (bnd, bzs, bzi)", "Reading wavemaker polyline file ...", "Boundary
infragravity time series bzi file" - distinct from (b) the SnapWave-coupled path
("DEBUG SFINCS_SnapWave - incoming tp for IG wave at wavemaker", snapwave IG
input block "bnd, bhs, btp, bwd, bds"). So a non-SnapWave wavemaker path DOES
exist in the baked binary.

- `snapwave_ig_energy_coupled_wavemaker` [CAND-L]: explicitly SnapWave-driven ->
  inherits the SnapWave HELD/STOP (ADR 0238/0243). STOP.
- `infragravity_wavemaker_boundary_signal` [CAND-M] (bzifile): does NOT inherit
  the SnapWave STOP on binary grounds (the bzi path is independent). STOPs on two
  other grounds: (1) NO wavemaker/bzi deck-authoring exists anywhere - SnapWave
  itself is still an unwired documented follow-up (flood.py:435), so wavemaker is
  further downstream; a new wave-boundary deck-authoring leg, not a knob. (2) The
  zero-mean IG water-level time series is not directly fetchable - it must be
  synthesized from an offshore spectrum via bound-IG-wave theory (new physics
  prep) or supplied as a labeled-demo signal; the physically-grounded IG source
  is SnapWave, which is HELD.

RECIPE: land SnapWave forcing first (the wired, physical IG source) OR build a
standalone bound-IG bzi generator (offshore Hm0/Tp/dir from NDBC/WW3 -> zero-mean
IG water-level series) + a wavemaker-polyline + bzi deck-authoring leg on the
sfincs builder + a wavemaker-on-vs-flat-boundary discriminating pair on a coastal
subgrid case. Blocked while SnapWave is held.

### Cluster 4 - HEC-RAS Water Quality / Temperature, 3 rows -> STOP (extends ADR 0157 to the 2025 engine)

Family = HEC-RAS. `wq_module_smoke_test_suite` already carried the ADR 0157 STOP
(6.x image = RasUnsteady/RasGeomPreprocess/RasSteady only, no WQ engine, no
bundled WQ datasets). This sweep verified BOTH images and EXTENDS the STOP to the
2025 managed engine:
- hecras:latest (6.x): /opt/hecras/bin = RasUnsteady, RasGeomPreprocess,
  RasSteady only. No WQ/temperature/nutrient binary.
- hecras2025-authoring:latest (2025 .NET managed engine): Ras.Core.dll +
  Ras.Engine.dll carry hydraulic transport-mixing scalars (Advection,
  TransMixCoeff, UseAdvection, AdvectionMethod) and a single bare "WaterQuality"
  enum/label stub - but ZERO WQ solver methods (no ComputeWaterQuality, no
  temperature heat-budget, no NSM I nutrient cycle, no GCSM constituent module).
  Probe validated against known-present tokens (Sediment/Unsteady/Engine hit).

So all 3 rows STOP on the same missing engine:
- `water_temperature_heat_budget_advection_dispersion` [CAND-M] - STOP (no
  temperature/heat-budget engine in either image).
- `nutrient_simulation_module_I_algae_do_cbod` [CAND-L] - STOP (no NSM I nutrient
  engine; even with a binary it is a NEW solver capability, not a knob).
- `wq_module_smoke_test_suite` [S] - STOP (already ADR 0157; extended here).

RECIPE (unchanged from ADR 0157, extended): obtain the HEC-RAS Linux
water-quality engine (temperature/GCSM/NSM I) + the bundled WQ test datasets, add
to the image (rebuild + staleness discipline), wire a WQ solve leg after the
hydraulic solve, then run the toy cases to the manual's expected output before
attempting the USGS Mohawk-scale calibration. New engine + data, not a knob.

## Consequence

Registry stays 254 (no landings). All in-image verification is captured above so
a future wave does not re-probe: NESTOR .so present (deck-authoring is the gap);
ELMFIRE &SUPPRESSION wired + pyrome overwrite guarded (scalars trustworthy in
single-ignition mode) + SDI raster is the blocker; SFINCS bzi wavemaker path
exists independent of SnapWave (data + deck-authoring are the gap); HEC-RAS has
NO WQ solver in 6.x OR the 2025 managed engine. Board rows and the rolling metric
updated. No proofs (physics-visible landings only; this sweep landed nothing).
