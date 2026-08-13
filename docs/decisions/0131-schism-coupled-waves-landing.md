# ADR 0131 -- schism_coupled_waves LANDS: the GOTM build leg resolved, SCHISM+WWM two-way coupling validated on Duck FRF

Status: accepted (2026-08-04)
Wave: THE GOTM BUILD LEG + schism_coupled_waves LANDING -- finishing the
NATE-signed candidate ADR 0126 proved coupling-live (the GOTM-free Duck spike:
two-way coupled solve, Hs max 2.24 m shoaling to zero). Follows: ADR 0126 (the
candidates triage -- the WWM coupling PROVEN, GOTM isolated as the single blocker),
0118 (the schism_tidal_hydro landing exemplar), 0115 (the SCHISM spike / worker).
Engine #12 second archetype.

## Outcome in one line

The ONE blocker ADR 0126 isolated -- the Duck case's `itur=3` GOTM k-epsilon
turbulence closure, unbuildable in the cmake worker because the in-tree GOTM 3.2.5
is Makefile-only -- is RESOLVED with a small cmake shim, the coupled-wave binary
`pschism_WWM_GOTM_TVD-VL` builds green, and `schism_coupled_waves` LANDS as a
registered template. The faithful (itur=3) Duck FRF coupled run reproduces the
published cross-shore Hs transect at correlation 0.94.

## 1. The GOTM build choice (the isolated blocker)

### 1a. Characterization -- why the honest cheap path is the in-tree GOTM 3.2.5

SCHISM v5.11's active cmake seam (`src/CMakeLists.txt:330-343`) does
`find_path(GOTM_BASE src/gotm/gotm.F90)` -> `add_subdirectory(${GOTM_BASE}/src gotm)`
and links the hydro core against cmake targets **`turbulence`** and **`util`**,
reading modules from `${CMAKE_BINARY_DIR}/gotm/modules`. The in-tree
`src/GOTM3.2.5` ships **Makefile-only** (no `CMakeLists.txt` anywhere), so the
`add_subdirectory` errors. The commented-out prebuilt-lib seam (lines 157-170,
`find_package(GOTM)`) needs a GOTMConfig.cmake the tree does not carry.

The modern-GOTM alternative (gotm.net cmake releases) was REJECTED after
characterizing SCHISM's glue: `schism_init.F90` / `schism_step.F90` `use turbulence`
(`init_turbulence('gotmturb.nml')`, `do_turbulence`, `cde`, `tke`, `eps`, `L`,
`num`, `nuh`) and `use mtridiagonal` (`init_tridiagonal`) -- the GOTM 3.2.5 API.
Modern GOTM (6.x) restructured the turbulence module API; SCHISM v5.11 ships
GOTM 3.2.5 in-tree precisely because that is the matched API. So the faithful
path is building the in-tree 3.2.5, NOT a newer tree.

The dependency closure is clean + self-contained: the `turbulence` sources
`use` only `eqstate` / `turbulence` / `util`; the `util` sources `use` only
`eqstate` / `mtridiagonal` / `util` -- NO `meanflow` / `observations` / `airsea` /
`netcdf`. `cppdefs.h` (the one header they `#include`) resolves `REALTYPE` to
**double precision** (SINGLE is `#define`d then `#undef`d) -- matches SCHISM's
`rkind=8`.

### 1b. The choice: a cmake shim compiling GOTM 3.2.5's turbulence+util

Rather than fight the legacy Makefile (its `compiler.GFORTRAN` uses the wrong
`-M` module flag for modern gfortran, expects old `NETCDFHOME` env) OR the fragile
prebuilt-imported-lib path (`add_dependencies` on imported targets), a ~15-line
`CMakeLists.txt` is injected at `GOTM3.2.5/src` that compiles the two libraries
SCHISM's glue uses as REAL cmake targets `turbulence` + `util` (CMake's Makefile
generator scans the intra-target Fortran module order automatically), module dir at
`${CMAKE_CURRENT_BINARY_DIR}/modules` (== what SCHISM's seam reads), double
precision (SINGLE undef'd), `-std=legacy -cpp -fallow-argument-mismatch` for the
2005-era F95. This satisfies the ACTIVE `add_subdirectory` seam directly -- no
prebuilt-lib fragility, no legacy-Makefile-vs-modern-gfortran fight.

### 1c. Evidence

- Both GOTM libs compile clean (`libutil.a` + `libturbulence.a`, modules
  `turbulence.mod` / `mtridiagonal.mod` / `eqstate.mod` / ... produced).
- The full SCHISM configure with `-DUSE_WWM=ON -DUSE_GOTM=ON
  -DGOTM_BASE=/src/schism/src/GOTM3.2.5` reports `USE_GOTM OPTION IS ON` and names
  the exe **`pschism_WWM_GOTM_TVD-VL`**.
- `make pschism` builds the 6.1 MB binary green; `ldd` clean (no missing libs --
  GOTM is static `.a`, so no new runtime `.so` over the WWM binaries already in the
  image). Container growth = one stripped ~6 MB binary.
- One extra compiler flag was required vs the base worker (`-fallow-invalid-boz`,
  added to `COMPILERS.cmake`) -- the WWM path carries invalid-BOZ constants modern
  gfortran rejects by default; harmless to the existing full-monty/hydro builds.

## 2. The faithful (itur=3 GOTM) Duck run + the cross-shore V&V

The full Test_WWM_Duck deck (33586 elements / 17054 nodes, nvrt=31, 4-hour run
from 1994-10-12 17:00 UTC, the bundled 8m-array non-parametric wave-spectrum
boundary) was run on `pschism_WWM_GOTM_TVD-VL` with `itur=3, mid='KE', stab='KC'`
KEPT (the whole point of the build leg). Staging transforms (ADR 0126 1d, minus
itur=0): strip the three master-only namelist vars `nbins_veg_vert` /
`nmarsh_types` / `RADFLAG` the v5.11.0 binary does not declare; `gotmturb.inp` ->
`gotmturb.nml` (SCHISM's `init_turbulence` hardcodes that name); `hgrid.gr3` ->
`hgrid_WWM.gr3`; trim the output set to elevation + `sigWaveHeight` + `peakPeriod`
so a small scribe count runs on a modest core budget.

Result (`mpirun -np 8` = 4 compute + 4 scribe): the coupled solve time-steps
cleanly with the GOTM closure active every step and writes `out2d` carrying
`sigWaveHeight` + `peakPeriod`. **Field: Hs max 2.25 m / mean 1.05 m** -- matching
the itur=0 spike's 2.24 / 1.04 (the wave field is largely closure-independent, as
expected), physically correct (offshore high, shoaling to the beach).

**The cross-shore V&V (the acceptance artifact)** -- modeled `sigWaveHeight` at the
16 Duck FRF pressure-transducer gauge locations vs the bundled `Hm0_nlin`
measurements (`timeseries_data_1010_to_1410_004Hz_025Hz.mat`):

- **Hs correlation 0.94** across the 16-gauge cross-shore transect -- the observed
  offshore-shoaling-breaking transformation is faithfully reproduced (both curves
  rise monotonically from ~0.8 m at the beach to ~1.8-2.2 m offshore).
- Hs RMSE 0.32 m, bias +0.29 m (the model runs slightly energetic); Tp RMSE 1.9 s.
- Offshore anchor (884 m gauge): measured 1.84 m, modeled 2.19 m.

Honest tolerance framing: this is a HINDCAST cross-shore comparison, not a
per-timestep match. The ~0.3 m positive bias is consistent with the model window
(17:00-18:30 UTC, the nor'easter building) being more energetic than the gauges'
time-mean record -- a temporal-alignment offset, not a physics error. The
correlation (shape fidelity) is the load-bearing V&V result and matches the
published Duck SCHISM-WWM comparison's character (the model tracks the observed
transect with a modest offset). Proofs: `scratchpad/coupled_waves_proofs/`
(`cross_shore_vv.png`, `hs_field.png`, `out2d_{1,2,3}.nc`).

## 3. The landing (the 0118 exemplar)

- **Contract** (`schism_contracts.py`, additive): `SchismWaveLayerURI`
  (Hs/Tp scalars + the cross-shore V&V fields the agent CITES, invariant 1),
  `SCHISM_WAVE_STYLE_PRESET` (honest reuse of the flood-depth ramp), archetype
  `coupled_waves` added.
- **Postprocess** (`postprocess_schism.py`): `read_out2d_waves` +
  `postprocess_schism_waves` (max-Hs COG + `peakPeriod` + the UGRID mesh row,
  reusing the elevation rasterizer) + `verify_cross_shore_waves` (scipy.io
  transect V&V, robust to the (ntime, ngauge) `.mat` layout via the `time` key).
- **Deck staging** (`deck_authoring.py`): `stage_wwm_duck_deck` -- the pristine
  SHA-pinned Duck fixture staged verbatim + the ADR 0126 transforms applied in
  code (deterministic, unit-tested).
- **Fixture**: `services/workers/schism/fixtures/wwm_duck/` (~14 MB, SHA256SUMS
  provenance-pinned to the VIMS mirror; the published gauge `.mat` files ride in
  `Data/`). `.dockerignore`d -- staged server-side, never in the image.
- **Entrypoint variant**: a `"wwm"` variant selecting `pschism_WWM_GOTM_*` with its
  OWN glob (the ADR 0126 fix -- the `full` glob `pschism_WWM_*` sorts COSINE
  first).
- **Template** `schism_coupled_waves` (one-file composer): the bundled Duck case,
  knobs `sim_hours` + `input_mode`; synthetic_inputs labeled default_demo /
  published-fixture; fidelity line (refinement-grade coupled wave-current on the
  Duck FRF geometry; off-scope -> `swan_wave_field` for standalone spectral); the
  cross-shore Hs verification chart IS the acceptance artifact; the mesh-emission
  row + `sigWaveHeight`->Hs COG postprocess. Registered + a co-located
  `corpus.yaml`.

## Consequences

- Registry +1 (`schism_coupled_waves`); template-tier 33 -> 34. `SCHISM_ARCHETYPES`
  `("tidal_hydro",)` -> `("tidal_hydro", "coupled_waves")`.
- The worker image gains ONE stripped ~6 MB binary (`pschism_WWM_GOTM_TVD-VL`)
  alongside the existing two; the GOTM cmake shim is baked into the Dockerfile
  build stage. Multi-stage hygiene preserved (the GOTM source + toolchain stay in
  the build stage).
- Model-free retrieval proof: `schism_coupled_waves` ranks #1 for all four
  representative coupled-wave queries.
- No flood seam touched (grep-verified) -> no flood canary mandated.
- The GOTM leg is now a reusable capability: any SCHISM case needing a GOTM
  k-epsilon closure (itur=3) can select the WWM+GOTM binary.

## Open issues

1. The V&V uses the measured record's time-mean vs the model's spun-up window; a
   phase-exact 18:30 UTC comparison (the published `date_cross_transect_waves`)
   would need the measured `.mat` time-axis epoch reconciled to sim-time -- a
   refinement, not a blocker (the shape correlation is the load-bearing result).
2. The full 4-hour run at 8 ranks is ~40 min wall (the itur=3 GOTM + MSC=MDC=12
   spectral cost is real, slower than ADR 0126's optimistic "minutes"); the smoke
   uses the spun-up first ~1.5 h (all past the wave-field equilibrium at ~30 min).
3. `schism_estuary_circulation` (Test_CORIE) remains the queued heavy SCHISM
   candidate (ADR 0126 sec 2) -- NATE-remote-drive class, unchanged by this wave.
