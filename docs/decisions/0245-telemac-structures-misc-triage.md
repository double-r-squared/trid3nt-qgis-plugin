# ADR 0245 - TELEMAC-2D Structures + misc triage sweep (all STOP-RECIPE)

Date: 2026-08-13
Status: accepted

## Context

The first board triage sweep over the open TELEMAC-2D rows in
docs/validation/module-coverage-board.md: the Structures cluster (main pass,
### TELEMAC-2D) and the misc cluster (supplementary mining, ### TELEMAC-2D).
Six open CAND rows:

Structures:
- `weir_controlled_discharge_staging` [CAND-M] - weir/low-head-dam discharge
  regulation + upstream backwater staging.
- `culvert_siphon_flow_singularity` [CAND-L] - culvert/siphon under a
  road/levee embankment, choked/submerged-orifice regime.

Misc (supplementary):
- `unstructured_mesh_dam_break_validation` [CAND-L] - Malpasset dam-break V&V.
- `tsunami_runup_benchmark_monai_valley` [CAND-M] - Monai runup benchmark.
- `okada_fault_source_tsunami_propagation` [CAND-L] - Okada finite-fault ->
  tsunami propagation.
- `tidal_storm_surge_boundary_forcing` [CAND-M] - tidal/surge open-boundary
  forcing for an estuary/coastal domain.

The knob-or-STOP rule: LAND only a cheap knob/parameter/template on machinery
that already exists; STOP-RECIPE anything needing a binary variant, a missing
data source, or heavy new machinery.

Ground truth against the worker: services/workers/telemac/telemac_river_dye_build.py
`author_deck` emits a river-reach deck with CONSTANT `PRESCRIBED FLOWRATES`
(inflow) + CONSTANT `PRESCRIBED ELEVATIONS` (fixed downstream stage), friction,
the wind/rain knobs, tracer + oil/decay/O2/GAIA coupling blocks, and
`INITIAL CONDITIONS = 'CONSTANT DEPTH'`. There is NO weir, culvert, Coriolis,
time-varying-boundary, tidal-database, or initial-discontinuity path. The mesher
is an NHDPlus river-reach flowline TIN (single-valued open channel, no embedded
internal crest line, no embankment, no ocean boundary).

Dico families verified in-image (docker trid3nt-local/telemac:latest, sha
9f5696b6b8b3, /opt/conda/opentelemac/sources/telemac2d/telemac2d.dico v9.0):
- Weirs: NUMBER OF WEIRS (MNEMO NWEIRS) + WEIRS DATA FILE (T2DSEU) + TYPE OF
  WEIRS (INDEX 87; 1 = BC-treated coincident-node-pair singularity, 2 =
  section-based) + WEIRS DISCHARGE OUTPUT FILE.
- Culverts: NUMBER OF CULVERTS (INDEX 89) + CULVERTS DATA FILE (INDEX 90).
- Coriolis: CORIOLIS + CORIOLIS COEFFICIENT (keyword only).
- Tidal: OPTION FOR TIDAL BOUNDARY CONDITIONS + TIDAL DATA BASE + BINARY
  DATABASE 1/2 FOR TIDE + ASCII DATABASE FOR TIDE + TIDAL MODEL FILE (all
  require an external tidal-constituent database).
- Discontinuity IC: INITIAL CONDITIONS = 'PARTICULAR' + FORTRAN FILE (CONDIN).

## Decision

All six rows STOP-RECIPE. Zero landings, zero new tools, zero worker/deck
change, zero image rebuild. Each row genuinely falls in the STOP category
(new machinery / missing data source / non-US geometry), not the cheap-knob
category:

- `weir_controlled_discharge_staging` -> STOP. TYPE 1 weirs need the crest line
  embedded as PAIRED COINCIDENT NODES across the channel (mesh-authoring the
  reach mesher does not do) + a WEIRS DATA FILE author + a US low-head-dam
  source (NID). The backwater-staging half is separately reachable as a cheap
  tailwater knob on the existing PRESCRIBED ELEVATIONS BC, but that BC proxy is
  not a weir singularity and labelling it "a weir" fails the honesty floor -
  recorded as the cheap partial, not landed.
- `culvert_siphon_flow_singularity` -> STOP. New CULVERTS DATA FILE author
  (2-node linkage + invert/diameter/loss + orifice/weir regime) AND the
  physical premise (flow under an embankment) is absent from the open-channel
  archetype - no natural inlet/outlet node pair exists to link.
- `unstructured_mesh_dam_break_validation` -> STOP. Duplicate of the already-
  STOP'd `instantaneous_dam_breach_flood_wave` (ADR 0154): non-US geometry
  (Malpasset) + a CONDIN two-level-discontinuity fortran the worker neither
  authors nor compiles.
- `tsunami_runup_benchmark_monai_valley` -> STOP. Non-US lab geometry +
  LIQUID BOUNDARIES FILE time-series author (incident-wave forcing) the worker
  lacks.
- `okada_fault_source_tsunami_propagation` -> STOP. US-relevant but heavy:
  Okada displacement -> CONDIN initial-free-surface author + a coastal ocean-
  domain mesher (not the river-reach builder). Recipe reuses the GeoClaw
  worker's Okada dtopo as the source field.
- `tidal_storm_surge_boundary_forcing` -> STOP. Missing external tidal-
  constituent DB (TPXO/FES) or NOAA CO-OPS surge-stage fetcher + a coastal
  domain with an ocean boundary + a LIQUID BOUNDARIES FILE time-series author.
  Coriolis alone is a free keyword but physically invisible on a narrow river
  reach, so not a standalone landing.

Full recipes are written on each board row.

## Consequence

The open TELEMAC-2D Structures + misc rows share three recurring blockers that a
future build wave should attack as SHARED substrate rather than per-row:
1. an internal-singularity mesh author (embed a crest/embankment polyline,
   duplicate coincident nodes) - unblocks weirs + culverts;
2. a CONDIN user-fortran authoring + compile path (initial free-surface
   discontinuity / Okada-derived IC) - unblocks dam-break + Okada tsunami;
3. a LIQUID BOUNDARIES FILE time-series author + a coastal/estuary domain
   archetype + a coastal-water-level data source (NOAA CO-OPS / TPXO) -
   unblocks tidal/surge + wave-time-series tsunami forcing.

US-only doctrine keeps Malpasset and Monai off the roster; the Okada tsunami and
tidal/surge rows are the US-compliant members of the misc cluster and are the
better targets if a coastal-TELEMAC wave is ever funded. No physics-visible
landing was in reach without one of the three substrates above, so the sweep is
correctly a clean all-STOP with dico-exact recipes and no build.
