# ADR 0260 - SCHISM ICM + SED3D targeted binaries BAKED; both modules SOLVE through the image; substrate rows PROVEN, template registration is the remaining cheap step

Status: SUBSTRATE-PROVEN (2026-08-14). Executes the ADR 0242 STOP-recipe: the
targeted-variant binaries (`pschism_ICM_TVD-VL`, `pschism_SED_TVD-VL`) are baked
into `trid3nt-local/schism:latest`, and ONE clean single-module case per family
runs to completion through the image with a physically-sensible discriminating
pair. This wave lands the IMAGE + entrypoint variants + the proven deck-authoring
recipe; it does NOT register new tool-surface templates (that is the named, now
low-risk next step). No server/src change -> no four-slice trigger.
Date: 2026-08-14
Supersedes-nothing (extends ADR 0242).

## Context

ADR 0242 gate-checked the SCHISM ICM (water quality) and SED3D/SED2D (sediment)
board rows and found the flags compiled but ONLY inside the full-monty binary,
which unconditionally initializes every compiled tracer module and hard-demands
every module's namelist (proven by a crash at `icm_init.F90:79` on a plain hydro
deck). It wrote a targeted-variant image-rebuild STOP-recipe (USE_ICM-only /
USE_SED-only cmake cache-inits mirroring `WWMGOTM.cmake`) and did not force it.
This wave executes that recipe.

## Decision: bake the targeted binaries, prove each module solves

### Image (the load-bearing enabler)

`services/workers/schism/Dockerfile` gains two build legs after the wwmgotm leg:
`ICM.cmake` (`set(USE_ICM ON)`) -> `pschism_ICM_TVD-VL`; `SED.cmake`
(`set(USE_SED ON)`) -> `pschism_SED_TVD-VL`. Each is a demonstrated subset of the
green full-monty compile (low risk). The build-stage layers up to the wwmgotm leg
cache-hit, so the two new legs are the only added compile cost. Build-time smoke
(`-v` + `ldd` clean on all five binaries + the QuarterAnnulus analytical gate)
passes through the image. `entrypoint.py` gains `variant` values `icm`/`sed`
(globs `pschism_ICM_*` / `pschism_SED_*`; no collision with the `pschism_WWM_*`
family). Provenance: the exe names are SCHISM's own cmake module-tag; both report
`git hash 4d350e4` (pinned v5.11.0 commit `4d350e49481c625002ee2bf7d7fca32777f53c65`).

### Both modules SOLVE (offline, direct-through-image, no MinIO)

* **SED3D `multiclass_suspended_sediment_transport`**: baroclinic estuary deck
  (idealized Galveston Bay channel, 3D SZ vgrid, ibc=0, itr_met=3, M2 tidal +
  river source) + `sediment.nml` (fine + coarse class) + `bedthick.ic` +
  `bed_frac_[1,2].ic` + `msource.th` river SSC. Fine (Wsed 1.06 mm/s) column-max
  suspended conc 0.0196 kg/m3 vs coarse (28.65 mm/s) 0.0065 -> ratio 3.03 under
  identical forcing (settling is the only difference). `sed_morph=1`
  (`morphodynamic_bed_evolution`) ALSO solves on the same binary.
* **ICM `eutrophication_core_wq_run`**: same base + full v5.11.0 `icm.nml` (17
  core state vars, all namelist groups) with `iRad=1` + a minimal `ICM_rad.th.nc`
  (avoids sflux) + `msource.th` 17-var river inflow. Nutrient LOAD vs NO-LOAD:
  NH4 column-max 1.048 vs 0.025 g/m3 (ratio 42), NO3 0.552 vs 0.054, PO4 0.136 vs
  0.053; DO min slightly lower under load. A downstream nutrient plume appears
  only under load.

Proofs (filled tricontourf, mesh wireframe, georeferenced, IDEALIZED-labelled):
`docs/proof/templates/schism_sed3d_multiclass_settling.png`,
`schism_icm_eutrophication_load.png`,
`schism_icm_sed3d_targeted_binary_smoke.txt`. Offline drivers:
`scripts/smoke_schism_sed.py`, `scripts/smoke_schism_icm.py`,
`scripts/proof_schism_icm_sed3d.py`.

## Recipe corrections (execute, correct, record -- ADR 0249->0250 precedent)

The 0242 recipe was directionally right; execution surfaced five concrete facts:

1. `Erate` must be a Fortran double literal (`1.600000d-03`); the naive `%e`+`d0`
   yields `1.6e-03d0` -> "Bad data for namelist object erate".
2. Scribed I/O requires `nscribe >= #(scribed output variables)` (elev + T + S +
   each `iof_sed`/`iof_icm_core`), else `INIT: Too few scribes`. The entrypoint
   default `nscribe=2` is too low for a module deck -- a landed template must size
   nscribe from its enabled output count.
3. ICM `iRad=0` hard-requires `ihconsv=1` + `nws=2` (sflux atmospheric forcing);
   `iRad=1` + a tiny `ICM_rad.th.nc` (1D `time_series` + scalar `time_step`) is
   the cheap radiation source that keeps the deck sflux-free. `flag_ic(7)=0` is
   the valid cold-start path (IC from `wqc0`), so no ICM IC files are needed.
4. On the idealized channel the tidal+river bottom stress is sub-critical, so bed
   erosion yields ~0 suspension; the SED case is SOURCE-DRIVEN (river SSC via
   `msource.th`) and the settling velocity is the discriminator.
5. `bctides.in` needs ONE extra per-module tracer-type flag (`0` = no boundary
   input) appended to the open-boundary flag line for the compiled SED/ICM module.

## Consequences

- The two substrate board rows move CAND/STOP-RECIPE -> SUBSTRATE-PROVEN: the
  targeted binary is baked, the module solves through the image, and the exact
  deck contract is proven and captured. The remaining step to a knobbed tool
  surface (a registered `schism_sediment_transport` / `schism_water_quality`
  template: contract + composer extending `author_baroclinic_estuary_deck` +
  postprocess COG + registry pins + QGIS-true proof + four-slice) is now a thin,
  de-risked wrapper wave -- deliberately NOT half-landed here to avoid breaking
  the 255 catalog pin / `EXPECTED_TEMPLATES` baseline mid-flight.
- The other seven rows keep their families' STOP-recipes but with the new image
  fact: the binaries EXIST, so each is now "author the deck on the baked
  `pschism_{ICM,SED}_TVD-VL`", not "rebuild the image". `wave_enhanced_bottom_stress_sediment`
  still needs the JOINT `USE_SED+USE_WWM` binary (a third targeted leg), and the
  benchmark-replication rows (Chesapeake/CoSiNE/Trench/meander) stay NATE-gated.
- Zero coded-tools delta, zero registry growth, no server/src change (four-slice
  not triggered). Worker image + entrypoint + docs + offline drivers only. Worker
  `test_entrypoint_manifest.py` green (3).
- EcoSim stays excluded (ADR 0115 `-finit-local-zero` incompatibility). CoSiNE
  (`cosine_sfbay_benchmark_replication`) needs `USE_COSINE` added to the ICM leg.
