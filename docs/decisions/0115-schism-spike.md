# ADR 0115 -- SCHISM feasibility spike (build + verification + mesh bridge + landing map)

Status: accepted (2026-08-04)
Spike: THE SCHISM SPIKE -- NATE-approved full-module integration, feasibility-first
(standing decision 2026-08-04: SCHISM lands WHOLE -- the semi-implicit cross-scale
core plus ALL modules per the full-engine-control doctrine, ADR 0024; redundancy
with SWAN/GAIA/WAQTEL accepted as cross-validation). This spike PROVES the build
+ one verification run + maps the landing. It is NOT the engine landing (no
registered tool, no contract, no template registration this wave).
Follows: ADR 0100 (HEC-RAS worker image template), 0101 (mesh worker GPL isolation
+ oceanmesh coastal_tin -- the TIN this bridge consumes).

## Context

SCHISM (schism-dev, github.com/schism-dev/schism) is the semi-implicit
cross-scale unstructured-grid hydrodynamic model behind NOAA's operational
STOFS-3D. It is the highest-leverage coastal engine we do not yet have: one
solver spanning tides, surge, waves (WWM-III), sediment (SED3D), water quality
(ICM/CoSiNE), and tracers on a single unstructured mesh. The make-or-break
question was whether we can BUILD it (gfortran + MPI + netCDF, all modules) and
RUN a verifiable case, and whether our oceanmesh `coastal_tin` (ADR 0101) can
feed it. All three are answered YES below.

## 1. THE BUILD -- the module matrix (what ONE executable can carry)

Version: **v5.11.0**, pinned to commit `4d350e49481c625002ee2bf7d7fca32777f53c65`
(the latest stable release, 2025-02-07; a moving tag is not reproducible).
Toolchain: **gfortran 12.2** (Debian bookworm), **OpenMPI 4.1**, **netCDF-Fortran
4.5.4 / netCDF-C 4.9**, cmake 3.25. ParMETIS 4.0.3 is bundled in the source tree;
netCDF + MPI are auto-discovered (nf-config on PATH + `find_package(MPI)`), so no
netCDF/HDF5 source build is needed -- the apt `-dev` packages suffice.

**A single "full-monty" executable compiles and links** carrying the hydro core
plus every module that can coexist in one binary:

    pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL

= WWM-III (wind waves) + SED3D (sediment) + ICM + CoSiNE + FIB (water quality) +
GEN + AGE (tracers) + PaHM (parametric hurricane) + MARSH + HA (harmonic
analysis) + ANALYSIS + PREC_EVAP, with the VL (Van Leer) TVD limiter. 7.2 MB
executable, links cleanly against system netCDF/MPI/HDF5, launches under MPI.

**What CANNOT join that single gfortran executable (characterized honestly):**

| module | status | why / what it needs |
| --- | --- | --- |
| EcoSim (USE_ECO) | EXCLUDED | `EcoSim/ecosim.F90` declares many automatic arrays (`real(r8),dimension(nvrt,Nphy)::...`). Under `-finit-local-zero` (the SCHISM gfortran SAFETY default -- uninitialized locals -> 0, which the hydro core relies on) gfortran errors "Automatic array cannot have an initializer" (93 arrays). Needs ifort (accepts it) or a per-module flag exception (drop `-finit-local-zero` for the EcoSim lib only). Redundant with ICM/CoSiNE, so excluded for the spike. |
| MARSH (USE_MARSH) | PATCHED (2 lines) | v5.11.0 `schism_init.F90:6463` and `schism_step.F90:9660` both write `if(iof_marsh(1)==1)` with **no `then`** (an upstream typo only reachable under `#ifdef USE_MARSH`); gfortran rejects it. Two documented one-line sed fixes carry it (baked in the Dockerfile). |
| FABM (USE_FABM) | SEPARATE | needs an external FABM source tree (`FABM_BASE`) not vendored here. |
| BMI (USE_NWM_BMI) | SEPARATE VARIANT | requires `NO_PARMETIS` + `OLDIO` on -- mutually exclusive with the scribed-IO + ParMETIS build. |
| GOTM (USE_GOTM) | SEPARATE | enables a turbulence option needing the GOTM lib built in; skipped (extra dep). |
| ICE / MICE / CICE | ONE AT A TIME | the ice models are mutually exclusive; ATMOS/CICE/WW3-via-ESMF also need ESMF. Not needed for coastal flood. |
| SPK (USE_SPK) | UPSTREAM-BROKEN | the cmake comment itself says "not working". |

**The load-bearing RUNTIME finding (why a mega-binary is not the operational
default):** SCHISM initializes every compiled tracer module UNCONDITIONALLY at
startup -- `schism_init.F90` calls `read_icm_param` (and sets `ntrs(k)=<module>_class`
for GEN/AGE/SED/ICM/CoSiNE/FIB) inside a bare `#ifdef USE_<MODULE>`, with no
runtime guard. So the full-monty binary DEMANDS every module's namelist
(`icm.nml`, `sediment.nml`, ...) and its tracer counts on EVERY run, even a plain
barotropic tide. This is exactly why STOFS and the SCHISM community build
TARGETED executables. **Decision: bake BOTH** -- the full-monty (proves
full-engine-control compiles + links + coexists) AND a clean hydro-core
`pschism_TVD-VL` (the STOFS-class default for surge/tide, and the binary the
verification gate exercises). The landing wave adds targeted variants as needed
(hydro+WWM, hydro+SED, hydro+ICM) rather than forcing every run to feed all
modules.

**Image (container-hygiene hard rule, multi-stage):** the Fortran/C toolchain +
`*-dev` headers + the ~700 MB cloned source tree stay in the build stage and
never reach runtime. Runtime carries the two ~7 MB executables + the
netCDF-Fortran/OpenMPI/HDF5 runtime `.so`s + a slim numpy/xarray/netCDF4
postprocess venv (SCHISM outputs are netCDF). **Image = 650 MB uncompressed /
155 MB compressed** -- the leanest solver worker (cf. hecras 2.2 GB, telemac
3.55 GB, mesh 1.33 GB). Breakdown: postprocess venv (numpy/xarray/netCDF4/pandas)
247 MB; runtime libs (netCDF-Fortran + OpenMPI + HDF5 + libgomp) 86.6 MB;
python:3.11-slim-bookworm base ~148 MB; the two stripped executables (full-monty
6.9 MB + hydro-core 3.6 MB) 10.5 MB; worker code 0.18 MB. The in-image
QuarterAnnulus gate runs green at build time; the entrypoint envelope was smoked
end-to-end (a QA case + manifest -> `status:"ok"`, scribed `out2d_1.nc` +
`zCoordinates_1.nc` written).

## 2. THE VERIFICATION RUN -- Test_QuarterAnnulus (GREEN)

Case: **Test_QuarterAnnulus** from SCHISM's own verification suite
(`columbia.vims.edu/schism/schism_verification_tests/Test_QuarterAnnulus`) -- the
Lynch & Gray annular tidal channel, a barotropic M2 tidal test with a bundled
**analytical solution** (`ForPlot_ana_elev.dat`). A tiny grid (108 elements /
130 nodes, 2 vertical levels), single-M2-constituent open boundary, 5-day run at
dt=300 s (1440 steps). Ran with the hydro-core executable under **MPI, 2 compute
+ 2 scribe ranks** (scribed I/O demands nscribes >= # output vars), ~2 s wall.

Version drift handled honestly: the verification-suite `param.nml` tracks master
and carried namelist vars the v5.11.0 binary does not declare (`nbins_veg_vert`,
`nmarsh_types`); these were stripped (the case does not use vegetation/marsh).
Native station output (`iout_sta=1` + `station.in`) was enabled to extract the
station elevation directly rather than parsing scribed netCDF.

**WHAT WAS COMPARED + tolerances (the M3 Muncie discipline):** modeled station
elevation (`staout_1`) at the analytical-solution point (60686, 16316) vs the
bundled analytical M2 solution, over the spun-up window (t >= 3 d, past the 1-day
tidal ramp `DRAMP=1`):

| metric | value | gate |
| --- | --- | --- |
| amplitude (modeled vs analytical) | 0.4393 m vs 0.4420 m -> **0.6% err (0.0027 m)** | PASS |
| RMSE vs analytical | **0.0155 m** on a 0.44 m signal | PASS |
| correlation | **0.9989** | PASS |
| RMSE vs bundled reference SCHISM output (`ForPlot_elev.dat.0`) | 0.0092 m | (reference is an older ELFE-era run; our result sits CLOSER to the analytical truth) |

The in-image gate (`fixtures/quarterannulus/qa_gate.py`) re-runs this at build
time and exits nonzero outside tolerance (amp_err <= 0.010 m, RMSE <= 0.030 m),
gating on SCHISM's "Run completed successfully" sentinel -- never the exit code
(the HEC-RAS lesson: SCHISM exits 0 even on a solve abort).

## 3. THE MESH SUPPLY PROOF -- coastal_tin -> hgrid.gr3 (GREEN)

`services/workers/schism/schism_gr3.py::tin_to_hgrid` converts an oceanmesh
`coastal_tin` output (lon/lat nodes + triangle connectivity, ADR 0101) into a
SCHISM `hgrid.gr3`. Proven on the **Galveston Bay TIN** (12,159 nodes / 21,667
triangles -- the ADR 0101 validation mesh). The bridge does the four things a
real SCHISM grid needs:

1. **CCW element normalization** -- SCHISM requires positive signed area.
2. **Complete boundary-loop extraction** -- via Eulerian edge-consumption (a
   naive node-walk stranded 4 boundary nodes at pinch points; SCHISM fails any
   unlisted boundary node's "incomplete ball" check). All 2,665 boundary nodes
   covered, 15 loops.
3. **Non-manifold pinch-point cleaning** -- SCHISM rejects a bowtie boundary
   vertex (boundary degree > 2, its element ball has >1 boundary opening ->
   "Illegal bnd node"). oceanmesh left 6; the cleaner opens each by dropping the
   smallest incident sliver (7 triangles removed, 21667 -> 21660), then re-indexes.
4. **Land/open boundary segments** -- exterior loop CCW as mainland, islands CW;
   an optional side (`south`) can be flagged open for downstream forcing.

**Acceptance = SCHISM's `ipre` grid preprocessor FULLY ingests the grid** (serial
mode, hydro-core binary): `Global Grid Size (ne,np,ns,nvrt): 21660 12154 33820 2`
-- it read every node/element, computed all 33,820 sides, passed the spherical-
frame checks (max axis dot product 1.1e-16, pframe dev 4.4e-16), accepted the
boundary (18 land segments / 2660 nodes), ran domain decomposition + the
message-passing table, wrote `global_to_local.prop`, exit 0, no fatal error. The
format bridge works. (Depths are a documented placeholder here -- a real run
samples bathymetry via fetch_dem at the landing.) `test_schism_gr3.py` (6 tests)
covers the bridge on synthetic meshes.

## 4. THE LANDING MAP

### 4a. Forcing data legs (what a real SCHISM run needs at its boundaries/surface)

- **Open-ocean boundary (elev/vel/T/S):** HYCOM is the pyschism default via
  THREDDS OPeNDAP (`tds.hycom.org/thredds/dodsC/`, `GLBy0.08/expt_93.0` =
  GOFS 3.1 to 2024-09-04, then **ESPC-D-V02** -- GOFS 3.1 was decommissioned
  2024-09-04, a fetcher must target ESPC after that date). NOAA's operational
  STOFS-3D-Atlantic instead uses **G-RTOFS** (subtidal WL + 3D T/S) + FES2014
  tides + Copernicus ADT. Live public bucket `s3://noaa-nos-stofs3d-pds/`
  (us-east-1, `--no-sign-request`).
- **Tides (bctides.in):** per open-boundary node, amplitude + phase for the major
  constituents (M2 S2 N2 K2 K1 O1 P1 Q1). **FES2014** (AVISO/CNES, permissive
  license, what STOFS uses) or **TPXO9-atlas** (OSU, registration-gated, non-
  commercial). NOAA CO-OPS harmonic constituents (`api.tidesandcurrents.noaa.gov
  /mdapi/.../harcon.json`) are point-only -- good for validating at US gauges,
  not for generating the boundary. Generated by `pyschism.forcing.tides.Bctides`.
- **Atmospheric (sflux/, nws=2):** CF-netCDF `sflux_air/prc/rad_*.nc` (10m wind,
  MSLP, air temp, humidity, precip rate, down long/shortwave). Sources with
  ready pyschism classes: **GFS** (NOMADS), **HRRR** (3-km CONUS), **ERA5**
  (hindcast). STOFS uses GFS + HRRR. We already have CO-OPS, USGS, NWM, and a
  HYCOM-class fetch surface -- the gaps are FES2014/TPXO tides + the sflux
  atmospheric packer (pyschism does both).

### 4b. Template candidates (paper-first replication -- NATE sign-off table)

| name | question | source_url | source_id | knobs | us_applicable | effort |
| --- | --- | --- | --- | --- | --- | --- |
| Test_QuarterAnnulus | barotropic M2 tidal vs analytical (Lynch-Gray) | columbia.vims.edu/schism/schism_verification_tests/Test_QuarterAnnulus | schism_verification_tests/Test_QuarterAnnulus | bctides.in, drag.gr3, hgrid/vgrid | No (idealized) -- but the cheapest build-works gate (PROVEN green this spike) | S |
| Test_CORIE | real estuary elev/currents/T/S vs ADCP+CTD | columbia.vims.edu/schism/schism_verification_tests/Test_CORIE | schism_verification_tests/Test_CORIE | hgrid (OR State Plane), NARR sflux, multi-station | **Yes** -- Columbia River Estuary, OR/WA (the CORIE ancestor) | M |
| Test_WWM_Duck | nearshore wave transformation + wave-driven currents (DUCK94) | columbia.vims.edu/schism/schism_verification_tests/Test_WWM_Duck | schism_verification_tests/Test_WWM_Duck | wwminput.nml, icou_elfe_wwm, 8m-array spectra | **Yes** -- Duck, NC (USACE FRF), canonical published benchmark | M |
| STOFS-3D-Atlantic replication | match NOAA's operational SCHISM forecast fields | registry.opendata.aws/noaa-nos-stofs3d | s3://noaa-nos-stofs3d-pds/STOFS-3D-Atl/ | FES2014 + G-RTOFS + GFS/HRRR + NWM; clipped sub-domain | **Yes** -- the US operational system; every fetcher maps to a leg | L (clip a sub-domain) |

Theory citation for the barotropic case: Zhang, Ye, Stanev, Grashorn (2016),
"Seamless cross-scale modeling with SCHISM," Ocean Modelling 102, 64-81. Also
noted for a later wave: `Test_ICM_ChesBay` (a real US-domain ICM/water-quality
validation on Chesapeake Bay).

### 4c. Module surface plan (how WWM/SED/ICM surface as tool parameters)

Each module reads its own Fortran namelist alongside the main `param.nml`;
surfacing follows the TELEMAC-family precedent (a typed knob manifest per module,
ADR 0025):
- **WWM-III:** `wwminput.nml` (MSC/MDC spectral bins, FRLOW/FRHIGH, wwmbnd.gr3)
  coupled via `icou_elfe_wwm`/`nstep_wwm` in param.nml.
- **SED3D:** `sediment.nml` (per-class settling velocity, critical shear,
  erodibility, sed_morph, Nbed) + `sed_class` in param.nml; bed IC via
  `bedthick.ic`/`bed_frac_*.ic`.
- **ICM:** `icm.nml` (modular: 17-var core + optional Silica/Zooplankton/pH/SRM +
  SAV/Marsh/SFM/BA sub-models; per-group kinetic rate blocks) + tracer wiring in
  param.nml.

### 4d. Remote-streaming dependency note (LOAD-BEARING for the product)

SCHISM's scribed I/O splits output by variable: `out2d_*.nc` (all 2D fields +
mesh/connectivity, UGRID) + per-variable 3D files (`salinity_*.nc`,
`horizontalVelX_*.nc`, `zCoordinates_*.nc`, ...). **These are the biggest layers
the product will produce.** Live-measured on the NOAA STOFS-3D-Atlantic bucket
(2.9M-node US Atlantic/Gulf domain): ~1.2-1.4 GiB per 12h chunk per variable, on
the order of **70+ GiB/day** of raw gridded netCDF for one operational cycle.
Implication (consistent with the geographic-clipping + render-chokepoint norms):
raw per-variable field netCDF at that scale cannot be streamed to a remote
QGIS/web client -- the product must clip to the case AOI before publishing and
convert 2D surface fields (elevation, max-envelope) to COG-tiled rasters via the
existing raster pipeline, reserving full 3D netCDF for on-demand subsetting. A
small-domain run (our own Test_CORIE / Test_WWM_Duck clip, not the full STOFS
grid) produces proportionally much smaller output and is the realistic default
product size. NATE live-drives SCHISM remotely, so this clip-then-COG discipline
is a landing prerequisite, not an afterthought.

## Consequences

- New worker `services/workers/schism/` (multi-stage Dockerfile compiling
  SCHISM v5.11.0, the `schism_gr3` TIN bridge + tests, the entrypoint envelope,
  the QuarterAnnulus fixture + in-image gate). NO server code, NO registered
  tool, NO contract, NO template registration -- registry UNCHANGED (git-verified:
  only `services/workers/schism/` + docs touched; the offline suite is untouched
  by construction). No flood-seam touch -> flood canary NOT mandated.
- Two documented upstream findings (MARSH missing-`then` x2; EcoSim automatic-
  array vs `-finit-local-zero`) baked as patch/exclusion in the Dockerfile.
- The oceanmesh `coastal_tin` (ADR 0101) now has a proven SCHISM consumer path
  (the `hgrid.gr3` bridge) -- the "NO solver consumes it yet" note in 0101 is
  resolved for the mesh-supply direction.

## VERDICT: GO (for the SCHISM engine-landing wave)

All three make-or-break legs are green: the full-monty build compiles + links,
one official verification case reproduces its published analytical solution to
0.6% amplitude error, and our own coastal TIN feeds SCHISM's grid preprocessor
cleanly. SCHISM is a viable engine landing.

## Open issues (for the landing wave)

1. **Targeted build variants, not one mega-binary** -- the runtime module-init
   coupling means the landing ships hydro-core as the default + hydro+WWM
   (STOFS-class) + hydro+SED/ICM as needed, each with its module namelists. The
   full-monty stays as the "everything compiles + coexists" proof.
2. **Forcing legs to build:** FES2014/TPXO tide -> bctides.in + the sflux
   atmospheric packer (both via pyschism); wire the existing HYCOM-class/CO-OPS/
   NWM fetchers to the boundary/river legs.
3. **EcoSim** -- decide ifort vs a per-module `-finit-local-zero` exception if the
   ecosystem model is ever wanted (redundant with ICM/CoSiNE, low priority).
4. **Remote-streaming clip-then-COG** is a landing prerequisite (section 4d), not
   optional -- 70+ GiB/day raw at continental scale.
5. **Bathymetry on the TIN** -- the bridge's depth is a placeholder; the landing
   samples fetch_dem (NAVD88) onto the nodes.
6. **pyschism dependency** -- the practical pre-processor for boundaries/forcing;
   evaluate vendoring vs a pyschism-in-worker preprocessing stage.
