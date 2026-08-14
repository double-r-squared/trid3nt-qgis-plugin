# ADR 0259 - TELEMAC-2D coastal tidal/surge substrate (LIQUID BOUNDARIES FILE + open-water domain)

Date: 2026-08-14
Status: accepted

## Context

ADR 0245 STOP-RECIPE'd the `tidal_storm_surge_boundary_forcing` row and named
three shared substrate blockers behind the whole open TELEMAC-2D misc cluster.
Blocker #3 was: a LIQUID BOUNDARIES FILE time-series author + a coastal/estuary
domain archetype with an ocean open boundary + a coastal water-level data source.

Since ADR 0245 the data-source half dissolved: `fetch_noaa_coops_tides`
(observed/predicted US tide-gauge series) AND `fetch_gtsm_tide_surge` (Deltares
GTSM v3.0 reanalysis) already ship under `fetchers/ocean/`, each emitting an
inline `time_series_csv` water-level series. The remaining gap was purely the
TELEMAC substrate: a coastal domain mesher + a LIQUID BOUNDARIES FILE author.

The river-reach worker (`telemac_river_dye_build`) prescribes only CONSTANT
`PRESCRIBED ELEVATIONS` at a channel outlet and meshes a single-valued NHDPlus
flowline TIN - no ocean boundary, no time-varying stage. The wave modules
(`tomawac_build` / `artemis_build`) DO build a real-bathymetry open-water grid
the family way (`build_grid` + node-sampled NOAA bathy + `_bed_cog`), which is
exactly the coastal-domain shape the tidal/surge row needs.

## Format authority (pinned in-image v9.0, never guessed)

Docker `trid3nt-local/telemac:latest`, `/opt/conda/opentelemac/sources`:

- `telemac2d.dico`: `NOM1 = 'LIQUID BOUNDARIES FILE'` (INDEX 38, file slot
  T2DIMP, `SUBMIT = ...;ASC;LIT;PARAL`) + `OPTION FOR LIQUID BOUNDARIES`
  (INDEX 47, 1 = classical).
- `telemac2d/read_fic_frliq.f` (the reader): first non-`#` line is the column
  NAMES; the FIRST name MUST be `T` (else PLANTE); the SECOND line (units/names)
  is skipped; then free-format `T value...` rows with STRICTLY increasing time;
  `#` comment lines allowed anywhere; a `#REFDATE` line needs ORIGINAL DATE OF
  TIME in the deck; a solver time OUTSIDE the series range aborts, so the file
  must bracket [0, DURATION]; some compilers require a trailing blank line.
- `telemac2d/sl.f`: for liquid-boundary index I with an imposed free surface,
  builds `FCT='SL(<i>)'` (e.g. `SL(1)`) and looks that column up in the file;
  if absent it falls back to `PRESCRIBED ELEVATIONS`. Discharge would be
  `Q(<i>)` (`telemac2d/q.f`). A single ocean boundary => column `SL(1)`.

## Decision

`tidal_storm_surge_boundary_forcing` -> SUBSTRATE-LANDED / PROTOTYPE-PROVEN. Two
new worker payload pieces + the entrypoint dispatch were built and PROVEN LIVE
through the baked telemac2d binary. The registered AI-drivable composer template
+ agent-side postprocess/contract is the scoped productionization follow-on
(same staging as ADR 0251/0258: seam proven, product surface deferred).

Built (`services/workers/telemac/telemac_coastal_build.py`, parser marker
`coastal-tidal-1`):

1. COASTAL DOMAIN (`build_coastal_mesh`): a regular UTM grid over a coastal bbox
   (the `tomawac_build.build_grid` family), real NOAA DEM_all topobathy sampled
   at node lon/lat (`fetch_demall_bed`, the SAME ImageServer the TOMAWAC lake
   path uses; negative = bathymetry, positive = land). ONE seaward edge (auto =
   deepest-mean bbox edge, or explicit N/S/E/W) coded LIHBOR=5 (free-surface
   imposed) + LIUBOR/LIVBOR=4 (free velocity); all other land edges solid
   (LIHBOR=2). `write_slf`/`write_cli` reuse the river_dye SELAFIN + CONLIM
   grammar. TIDAL FLATS wetting/drying floods the low coast as the boundary
   stage rises. A synthetic plane-beach bed (`bathy_source="synthetic"`) gives a
   deterministic offline path for CI.

2. LIQUID BOUNDARIES FILE author (`write_liquid_boundaries_file`): emits the
   `T SL(1)` grammar exactly as `read_fic_frliq.f` reads it, from a
   `water_level_series` [[t_s, sl_m], ...] authored from the CO-OPS/GTSM
   `time_series_csv`. Strict normalize (`_normalize_series`): finite-only,
   sorted, strictly-increasing time (equal-time keep-last), t=0 anchor, a
   flat-hold row past DURATION so the reader never runs off the end, a labeled
   `datum_offset_m` to reconcile the MLLW tide datum with the DEM (sea-level)
   datum.

3. DECK (`author_deck`): `INITIAL CONDITIONS='CONSTANT ELEVATION'` +
   `LIQUID BOUNDARIES FILE` + `OPTION FOR LIQUID BOUNDARIES=1` +
   `PRESCRIBED ELEVATIONS`=init (SL(1) fallback/boundary count) + SAINT-VENANT FE
   + TIDAL FLATS (option 1) + negative-depth treatment 2. Optional constant WIND.

4. Entrypoint dispatch: a `manifest['coastal']` block routes to
   `run_coastal_pipeline` -> `CoastalConfig` (strict-unknown-field gate, ADR
   0158) -> `telemac_coastal_build.solve`, exactly mirroring the wave / 3D legs.

## Live evidence (through the image)

Apalachicola Bay, FL, bbox (-85.02,29.69,-84.90,29.80), real NOAA DEM_all bed
(-6.7..+6.0 m), 4480 nodes @ 180 m, ocean edge auto-picked E. A/B discriminating
pair from REAL NOAA CO-OPS station 8728690, a 30 h window around the Hurricane
Michael (2018-10-10) surge peak, driven through the LIQUID BOUNDARIES FILE:

- A = OBSERVED water level (surge): SL(1) peak 2.65 m -> flooded LAND 3.59 km^2
  (256 newly-inundated nodes). CORRECT END OF RUN, wall 18.3 s.
- B = astronomical PREDICTION (calm tide, same domain): SL(1) peak 0.40 m ->
  flooded LAND 0.016 km^2 (66 nodes). CORRECT END OF RUN, wall 19.4 s.

The surge floods ~220x more land than the calm tide. The solver listing confirms
`THERE IS 1 LIQUID BOUNDARIES` and time-varying interpolated `SL(1)` reads
(1.129 -> 1.390 -> 1.510 ... m), empirically pinning the file format. Proof:
`docs/proof/templates/coastal_tidal_surge_boundary_forcing.png` (ESRI World
Imagery + mesh wireframe + filled peak-inundation-depth cells, A vs B).

## Consequence

Blocker #3 of ADR 0245 is dissolved: the coastal open-water domain + LIQUID
BOUNDARIES FILE time-series author now exist and are proven. This also clears ONE
of the three blockers on `okada_fault_source_tsunami_propagation` (the coastal
ocean-domain archetype); that row STAYS STOP for its remaining blocker (the
Okada -> CONDIN initial-free-surface user-fortran author, shared with the
dam-break rows).

Scoped follow-on (productionization): a registered `coastal_tidal_surge`
composer template (question class: how far does a storm tide/surge series flood
this coast) that fetches CO-OPS/GTSM via the emit-on-fetch seam, builds the
`manifest['coastal']`, dispatches the LOCAL-DOCKER solve, and rasterizes the
peak-inundation field to a COG through the postprocess/publish_layer path
(+ the `_bed_input` in-worker bed COG surface) - plus the registry pin-chase
(categories.py, tools/__init__.py, catalog_surfacing/door_dissolution pins,
corpus.yaml, retrieve_visible_tools) and the four-slice law. The harmonic-tidal
path (TPXO/FES constituents) remains STOP (no constituent fetcher); the CO-OPS /
GTSM stage-series path is the shipped substrate.
