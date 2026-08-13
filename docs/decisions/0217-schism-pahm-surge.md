# ADR 0217 - SCHISM parametric-hurricane storm surge (standalone Holland-1980 sflux)

Date: 2026-08-11
Status: accepted

## Context

Module-coverage board PaHM rows (docs/validation/module-coverage-board.md,
"### PaHM (Parametric Hurricane Model)"). PaHM is NOAA/CSDL's parametric
tropical-cyclone wind/pressure generator (GAHM + symmetric-vortex) that forces
SCHISM from best-track data; it is operational in STOFS-3D-Atlantic. The two
US-applicable board rows:

- [CAND-L] `besttrack_parametric_hurricane_wind_forcing` - given a historical
  Atlantic hurricane's best track, what storm-surge water-level response results
  from parametric-vortex winds alone?
- [CAND-L] `stofs3d_operational_pahm_forcing_pattern` - the STOFS-3D-Atlantic
  operational pattern of a PaHM-forced SCHISM surge nowcast for a US Atlantic
  storm.

(The third board row, `hurricane_niran_gahm_vs_regional_wx_blend_replication`,
is non-US -- excluded by the US-only doctrine, kept only as a mechanical PaHM
smoke idea.)

## Decision: standalone Holland-1980 sflux, NOT the baked USE_PAHM binary

The image (`trid3nt-local/schism:latest`) DOES carry a `USE_PAHM`-compiled
binary -- the full-monty `pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_`
`PAHM_HA_MARSH_GEN_AGE_TVD-VL`. But ADR 0115 documented that a full-monty run
UNCONDITIONALLY initializes every compiled tracer module and so demands every
module's namelist (icm.nml, sediment.nml, cosine.nml, fib.nml, marsh, ...) --
the exact friction that drove the targeted-binary posture. There is NO targeted
PaHM-only binary in the image, and building a fourth binary is a full
from-source SCHISM recompile.

The honest, no-rebuild route: author STANDALONE parametric Holland-1980
wind/pressure fields as SCHISM `sflux/` atmospheric inputs (`nws=2`) consumed by
the CLEAN hydro-core binary `pschism_TVD-VL`. `nws=2` sflux is a SCHISM CORE
feature (not a compiled module), so the existing image runs it with NO rebuild
-- proven live (below). This is a SYMMETRIC Holland vortex (no GAHM
forward-motion asymmetry) -- the honest screening scope; native GAHM via a
targeted `USE_PAHM`-only binary is the documented upgrade.

Citations: Holland, G.J. (1980), "An Analytic Model of the Wind and Pressure
Profiles in Hurricanes", Mon. Wea. Rev. 108, 1212-1218 (the wind/pressure
profile); SCHISM sflux format (src/Hydro/sflux_9c.F90 header + sample_inputs/
sflux_inputs.txt, verified in-source at the pinned v5.11.0 commit); the board
rows' PaHM/STOFS citations (schism-dev PaHM docs; NOAA STOFS-3D-Atlantic).

## Physics + deck

`holland_sflux.py` (pure numpy + netCDF4, offline-testable): radial pressure
`P(r)=Pc+dP*exp(-(Rmw/r)^B)`, gradient wind `Vg=sqrt((Rmw/r)^B*(B/rho)*dP*`
`exp(-(Rmw/r)^B)+(rf/2)^2)-rf/2`, `B=rho*e*Vmax^2/dP` clamped [1.0,2.5];
gradient wind reduced to 10 m (0.9) and rotated cyclonically with a 20-deg inflow
angle; track center/Pc/Vmax/Rmw time-interpolated; written to a structured
lon/lat `sflux_air_1.0001.nc` (uwind/vwind/prmsl/stmp/spfh, base_date = run
start).

`deck_authoring.author_pahm_surge_deck`: the georeferenced solve uses the PROVEN
`ics=1` Cartesian path -- the lon/lat TIN is PROJECTED to local metres for
`hgrid.gr3`, and `hgrid.ll` reconstructs lon/lat node-for-node (the invertible
equirectangular map) for the sflux geographic interpolation. Still-water open
boundary (`iettype=2`, no tidal constituents) so the surge is PURELY
wind/pressure driven -- the behavior-proving criterion. Coriolis OFF (`ncor=0`):
wind-setup + inverse-barometer are the dominant screening drivers.

Four SCHISM input requirements discovered + baked (each was a real abort, fixed
against the in-source v5.11.0 truth, not guessed):
1. `nrampwind` is NOT a valid &OPT namelist member (the QA fixture had it
   commented) -> aborts init; the valid wind members are
   nws/wtiminc/iwind_form/drampwind/iwindoff.
2. `ncor=1` with `ics=1` needs a beta-plane setup SCHISM did not resolve ->
   dropped (Coriolis-off screening).
3. `nws=2` requires `windrot_geo2proj.gr3` (wind rotation geo->projected) ->
   authored, 0.0 everywhere (equirectangular keeps north aligned).
4. sflux MUST extend PAST the run end (a `tail_hours` buffer) or SCHISM aborts
   the FINAL step with no forward record to interpolate into.

## Consequence

- Template `schism_pahm_surge` (engine=schism, tier=template): best track (the
  published Hurricane Ike 2008 by default, published-first; a named storm via
  `fetch_storm_tracks`/IBTrACS otherwise) -> Holland sflux -> barotropic surge on
  an internal graded coastal TIN (bathymetry from fetch_topobathy/fetch_dem, else
  a synthetic sloping shelf) -> peak-surge COG + best-track overlay + coastal
  gauge surge hydrograph. Precondition mesh gate (ADR 0212) consumes a case mesh.
- Solver `schism_pahm_surge` shares the ONE worker image (variant=hydro); NO
  image rebuild, NO parser bump (sflux rides the existing `inputs[]` with nested
  `dest`, which the launcher already stages; the entrypoint is sflux-agnostic).
  Parser stays `schism-manifest-2`.
- Board rows `besttrack_parametric_hurricane_wind_forcing` +
  `stofs3d_operational_pahm_forcing_pattern` LANDED (screening scope; the
  GAHM-asymmetry / native-PaHM-binary + tide co-forcing + Coriolis remain the
  documented refinement follow-ups).

Superseded by nothing. The native-GAHM targeted-binary build is a future ADR.
