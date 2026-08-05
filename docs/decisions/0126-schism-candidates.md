# ADR 0126 -- SCHISM candidates wave (schism_estuary_circulation + schism_coupled_waves): TRIAGE

Status: accepted (2026-08-04)
Wave: THE SCHISM CANDIDATES WAVE -- NATE-signed, triage-first (the mined/spike
labels are hypotheses; land honestly; STOPs with precise blockers + ready recipes
are first-class outcomes). Two capability-named candidates:
schism_estuary_circulation (Test_CORIE, Columbia River estuary) and
schism_coupled_waves (Test_WWM_Duck, Duck NC WWM coupling).
Follows: ADR 0115 (the SCHISM spike -- the built worker, the full-monty WWM-carrying
binary, the landing map), 0118 (the landed schism_tidal_hydro pattern -- the
exemplar), 0116 (the clip-to-AOI + COG output contract + the mesh-emission row).

## Outcome in one line

NEITHER candidate lands a registered template this wave. BOTH are characterized to
the deck/binary/runtime level with ready recipes. The load-bearing new evidence:
the SCHISM+WWM **two-way wave-current coupling RUNS end-to-end** on a targeted
WWM binary and produces a physically-correct Duck wave field -- the ONE thing
blocking a faithful schism_coupled_waves landing is the case's GOTM turbulence
closure, isolated precisely below. Registry UNCHANGED (188); offline suite
UNCHANGED (exactly 9 by SET -- zero server code touched); no flood seam; the repo
schism image UNCHANGED (the WWM binary was proven in a throwaway build).

## Triage table

| candidate (capability) | published case | deck self-contained? | forcing ships? | binary in image? | run weight | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| schism_estuary_circulation | Test_CORIE (Columbia R. estuary, OR/WA) | YES (~600 MB) | YES (NARR sflux bundled, T/S nudging, hotstart, river flux.th) | YES -- hydro-core `pschism_TVD-VL`, module=None | 28-day 3D BAROCLINIC (nvrt=54, 20641 nodes) -- HEAVY | LANDABLE (bake-and-parameterize); live acceptance DEFERRED to NATE remote drive (coastal_tin acceptance-b class) |
| schism_coupled_waves | Test_WWM_Duck (Duck NC FRF, 12 Oct 1994) | YES (~17 MB) | YES (spectral boundary `DUCK94_wave_spectra_8m_array.nc` bundled) | NO -- needs a targeted WWM+GOTM binary | 4-hour barotropic wave-coupling (MSC=MDC=12) -- CHEAP | STOP with build recipe -- coupling PROVEN (itur=0 spike); faithful V&V blocked on GOTM (itur=3) |

## 1. schism_coupled_waves (Test_WWM_Duck) -- STOP with a build recipe; coupling PROVEN

### 1a. What the shipped case actually is (obtained + characterized)

The full Test_WWM_Duck deck was fetched from the SVN-over-HTTP mirror
(`https://columbia.vims.edu/schism/schism_verification_tests/Test_WWM_Duck/`;
`svn` is not installed, but mod_dav_svn serves plain HTTP file GETs -- the deck was
pulled with curl). It is SMALL and fully self-contained: 33586 elements / 17054
nodes (`hgrid_corbathy_MSL.gr3`, local FRF projection), nvrt=31 SZ, a **4-hour**
run (`rnday=0.16667`, dt=10 s) over a high tide during an energetic wave event,
MSC=MDC=12 spectral bins. The wave boundary is a bundled non-parametric spectrum
(`wwminput.nml`: IBOUNDFORMAT=6, LBCSP=T, FILEWAVE=DUCK94_wave_spectra_8m_array.nc)
-- so the forcing SHIPS WITH THE CASE (no WW3/parametric build needed). Published
verification data rides along in `Data/` (16-gauge cross-shore Hm0/Tp transect
`timeseries_data_1010_to_1410_004Hz_025Hz.mat`; sled 3D currents
`12101994_sled_data.mat`) plus `Duck_SCHISM-COMP.PNG` and the plotting recipe
`plot_duck94_12Oct.py`. SHA256 pins recorded in the wave proofs.

### 1b. The binary: full-monty is walled; a targeted WWM binary is cheap

The full-monty binary
(`pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL`)
carries WWM but -- the ADR 0115 runtime finding -- UNCONDITIONALLY inits every
compiled tracer module, so it demands icm.nml/sediment.nml/cosine.nml/... on every
run; the Duck deck ships none. It walls the case. A TARGETED WWM-only binary
`pschism_WWM_TVD-VL` (USE_WWM only) builds cheaply and CLEANLY (verification build
green; WWM was already proven to compile -- it is the "WWM" in the full-monty
name). This half of the recipe is cheap.

### 1c. THE isolated blocker: GOTM turbulence (itur=3)

Duck sets `itur=3, mid='KE', stab='KC'` -- the GOTM k-epsilon closure. In SCHISM
v5.11.0 there is NO module-free k-epsilon:

- `itur=3` (GOTM) -> `#ifndef USE_SED`... no: `schism_init.F90` guards it
  `#ifndef USE_GOTM -> parallel_abort('Compile with GOTM')`. Needs USE_GOTM.
- `itur=5` (the built-in "Tsinghua" k-epsilon, reuses mid='KE') -> guarded
  `#ifndef USE_SED -> parallel_abort('Two_phase_mix needs USE_SED')`. Needs USE_SED.
- `itur=2` = Pacanowski-Philander (Richardson-number), `itur=0` = constant. Both
  module-free but NOT k-epsilon -- a real physics deviation for the faithful V&V.

GOTM is NOT buildable in the cmake worker as-is: SCHISM's cmake does
`find_path(GOTM_BASE src/gotm/gotm.F90)` then `add_subdirectory(${GOTM_BASE}/src)`,
which needs a CMake-enabled GOTM. The in-tree `src/GOTM3.2.5` is Makefile-only
(no `src/CMakeLists.txt` -- cmake errors "does not contain a CMakeLists.txt"), and
there is no `schism-dev/gotm` cmake fork (404). So a faithful (itur=3) Duck build
needs GOTM SOURCED as a cmake-buildable tree.

### 1d. Capability PROVEN (the spike -- honest, physics-deviated)

To prove the coupling itself works, a targeted `pschism_WWM_TVD-VL` was built and
the Duck deck run with a GOTM/SED-free closure (`itur=0`; a documented screening
deviation -- the wave field is largely turbulence-closure-independent). Param
transforms required (all version-drift/module-off, the QA-fixture discipline):
strip master-only namelist vars `nbins_veg_vert` / `nmarsh_types` / `radflag`
(the v5.11.0 binary does not declare them), set `itur=0`, add `hgrid_WWM.gr3`
(= hgrid.gr3; WWM's own grid file), trim outputs for a small nscribe. Result
(MinIO-free, `mpirun -np 8` = 4 compute + 4 scribe, itur=0):

- The two-way coupled solve RUNS: cold-start init -> tracers -> time-stepping,
  writing `out2d_1.nc` (UGRID) + `zCoordinates_1.nc`, WWM active every step.
- `out2d` carries **`sigWaveHeight` (Hs) + `peakPeriod` (Tp)** -- the postprocess
  targets, confirmed by name.
- The wave field is PHYSICALLY CORRECT: Hs **max 2.24 m / mean 1.04 m**, Tp max
  10.8 s, high offshore, shoaling to ~0 at the beach (the cross-shore transform),
  plausible for the Duck 12 Oct 1994 nor'easter (offshore Hs ~2 m). Proof render:
  `scratchpad/schism_candidates_proofs/wwm_duck_hs_tp_field.png`.

So the capability -- waves + currents two-way coupled, Hs/Tp on a real US
nearshore mesh -- is 90% built. Only the GOTM turbulence faithfulness blocks a
publishable V&V claim.

### 1e. Ready recipe to LAND schism_coupled_waves (faithful)

1. **GOTM leg (the blocker):** either (a) add a legacy-Makefile GOTM static-lib
   build stage compiling the in-tree `src/GOTM3.2.5` via its own Makefile, then
   feed the compiled lib + module dir to SCHISM's cmake as a pre-built GOTM
   (the commented-out `GOTM_DIR` / `find_package(GOTM)` path in
   `src/CMakeLists.txt:156-170` is the intended seam), or (b) vendor a
   cmake-enabled old-API GOTM tree at `GOTM_BASE`. Build
   `pschism_WWM_GOTM_TVD-VL` (USE_WWM + USE_GOTM). Container-hygiene: the WWM+GOTM
   binary is ~7 MB; the WWM runtime .so's are already in the image (full-monty
   carries them) -- image growth is one stripped binary.
2. **Entrypoint variant:** add a `"wwm"` variant to `entrypoint.py::_resolve_exe`
   selecting `pschism_WWM_*TVD-VL` -- NOTE the current `variant=="full"` glob
   `pschism_WWM_*` sorts COSINE before a targeted binary, so a distinct "wwm" glob
   (or an explicit name) is needed.
3. **Deck:** bundle the SHA-pinned Duck fixture (17 MB) under
   `services/workers/schism/fixtures/wwm_duck/` (the QuarterAnnulus precedent);
   stage it verbatim + the param transforms of 1d (minus itur=0 -- keep itur=3).
4. **Contract:** `SchismWaveLayerURI` (Hs/Tp fields + a verification triple vs the
   bundled 8m-array Hm0) -- the SchismElevationLayerURI sibling.
5. **Postprocess:** `sigWaveHeight` -> max-Hs COG (reuse the elevation rasterizer,
   a flood-depth-family ramp) + the UGRID mesh row + the cross-shore Hs verification
   vs `Data/timeseries_data_*.mat` (16 gauges, scipy.io.loadmat).
6. Capability-named template `schism_coupled_waves`; registry 188 -> 189.

## 2. schism_estuary_circulation (Test_CORIE) -- LANDABLE bake-and-parameterize; live acceptance DEFERRED

### 2a. What the shipped case is

Test_CORIE is a real Columbia River estuary hindcast: 38960 elements / 20641 nodes
(`hgrid.gr3` in OR State Plane NAD27; `hgrid.ll` lon/lat rides along), nvrt=54
(SZ), **ibc=0 (3D BAROCLINIC)**, `rnday=28` days at dt=90 s, `ihot=1` (hotstart),
`nws=2` atmosphere. Module required: **None** -- it runs on the hydro-core binary
we ALREADY ship (`pschism_TVD-VL`). The forcing SHIPS WITH THE CASE:

- Atmosphere: 32 days of NARR `sflux/` (air/prc/rad; the repo stores them as SVN
  symlinks into `sflux.0/` -- staging must resolve the symlinks to the real
  `sflux.0/*.nc`). No NARR fetch needed -- the honest "must-build-forcing" fear is
  FALSE here.
- Ocean/river/IC: `hotstart.nc` (192 MB), T/S nudging `TEM_nu.nc`/`SAL_nu.nc`
  (129 MB each), `flux.th` river discharge, `bctides.in`. Multi-station
  verification bundled (`ForPlot_*.ADP` ADCP, `*.CTD`, `ForPlot_elev/T/S/u/v.dat`).

So the archetype is a BAKE-AND-PARAMETERIZE published-deck-runner (Muncie/HEC-RAS
class), NOT a forcing-build blocker. The code path is the schism_tidal_hydro QA
staging (stage the deck verbatim) plus the bundled forcing files as manifest
inputs, dispatched to the existing hydro-core binary.

### 2b. Why the live acceptance is DEFERRED, not blocked

The honest weight: a 28-day 3D baroclinic solve (nvrt=54, 20641 nodes, T/S
transport + nudging + atmosphere) is ENGINE-CLASS heavy (hours of wall-clock), and
the deck is ~600 MB to stage over MinIO. The published verification compares
multi-station elev/T/S/currents against ADP+CTD at fixed observation dates across
the full 28-day window -- truncating to a cheap smoke breaks the V&V (it exercises
the code path but not the published match). This is exactly the coastal_tin
acceptance-b posture (ADR 0118): the code path is landable + unit-testable now,
the live drive is NATE-remote-drive class. No new binary, no new forcing leg --
the only gates are compute weight + deck staging.

### 2c. Ready recipe

Bundle/stage the CORIE deck (resolve the `sflux/` symlinks to `sflux.0/`), stage a
manifest with the shipped forcing as inputs[] + the hydro variant, run
`pschism_TVD-VL` (already in the image), postprocess the multi-station
elev/T/S/vel vs the bundled `ForPlot_*.ADP`/`.CTD` observations (a multi-station
timeseries verification chart, not a single COG). Register `schism_estuary_circulation`
(baroclinic archetype) when NATE greenlights the heavy live drive; registry
188 -> 189.

## Consequences

- Registry UNCHANGED (188 -- git-verified: only `docs/` touched; zero server/
  contract/worker code). Offline suite UNCHANGED (exactly 9 by SET, by construction).
  No flood seam -> no flood canary mandated.
- New durable evidence: SCHISM+WWM two-way coupling is PROVEN to build + run + emit
  a correct Hs/Tp field (the schism_coupled_waves capability is de-risked to a
  single build blocker). The postprocess variable names are pinned
  (`sigWaveHeight`, `peakPeriod`).
- Both candidate build recipes are precise + ready (sections 1e / 2c). A future
  wave lands schism_coupled_waves once the GOTM cmake leg is resolved (the cheapest
  next SCHISM template -- cheap case, self-contained forcing, proven coupling), and
  schism_estuary_circulation when NATE greenlights the heavy CORIE live drive.

## Open issues

1. **GOTM in the cmake worker** -- the single blocker for a faithful WWM_Duck V&V.
   Legacy-Makefile GOTM static-lib pre-build (recipe 1e.1a) is the most likely
   cheap path; needs a build spike.
2. **CORIE 600 MB deck + 28-day baroclinic run** -- a MinIO staging + compute-budget
   question, not an engineering blocker; sequence behind NATE's remote-drive go.
3. The Duck deck's `sflux`-style SVN symlinks (CORIE) -- staging must dereference
   `sflux.0/` real files, not copy the 35-byte link stubs.
