# ADR 0242 - SCHISM ICM + SED3D/SED2D substrate gate: flags PRESENT (full-monty only), productionization STOP-recipe (targeted-variant image rebuild)

Status: SUBSTRATE-CHARACTERIZED (2026-08-13). Gate answered EMPIRICALLY: the
baked SCHISM binary set DOES compile USE_ICM + USE_SED + USE_COSINE + USE_FIB -
but ONLY inside the single full-monty executable, which unconditionally
initializes every compiled module and demands every module's namelist (proven by
a hard crash at `icm_init.F90:79` on a plain barotropic deck). No targeted
ICM-only or SED-only binary exists. Driving a CLEAN single-module V&V is
therefore a targeted-variant image rebuild - an IMAGE-REBUILD question, NOT a
host-side deck knob - and per the task it is NOT forced. This wave lands the
substrate characterization + the per-row productionization recipe; it does NOT
touch the tool surface, the worker, or the server (coded-tools delta 0). The 9
board rows (ICM x5, SED3D/SED2D x4) stay CAND with the gate finding recorded as
STOP-RECIPE.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD SCHISM block carries an **ICM** section (5 rows, board
L255) and a **SED3D/SED2D** section (4 rows, board L234). ICM (Integrated
Compartment Model) is SCHISM's USACE-lineage water-column biogeochemistry solver
(~17 core state variables: 3 phytoplankton groups, C/N/P organic+inorganic
pools, COD, dissolved oxygen), with optional silica/zooplankton/pH/SAV/marsh/
benthic-flux sub-modules plus the CoSiNE alternate ecosystem model and FIB fecal
bacteria. SED3D is the CSTMS-lineage multi-class non-cohesive sediment transport
solver (suspended load + Van Rijn bedload + Exner bed morphology); SED2D is the
depth-averaged variant.

This is the mirror of the TOMAWAC/ARTEMIS (ADR 0236/0237) and SFINCS SnapWave
(ADR 0238) substrate gates, and of the SCHISM harmonic-analysis STOP (ADR 0156,
`tidal_constituent_extraction_inrun`): SCHISM modules are **compile-time flags**
(`USE_ICM`, `USE_SED`, `USE_COSINE`, `USE_FIB`), so before scoping ANY row we
gate-check whether the physics is present in the baked binary and, if present,
whether it is *drivable* as an isolated case.

## Gate verdict: flags PRESENT, but ONLY in the full-monty binary

Three baked binaries exist in `trid3nt-local/schism:latest`
(`/opt/schism/bin/`, verified via `docker run --entrypoint ls`):

| binary | modules | purpose |
| --- | --- | --- |
| `pschism_TVD-VL` | none | hydro core (surge/tide/baroclinic, transport_validation gate) |
| `pschism_WWM_GOTM_TVD-VL` | WWM + GOTM | targeted coupled waves (ADR 0126/0131) |
| `pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL` | **everything** | full-monty |

The full-monty executable name is SCHISM's own cmake-generated module tag:
`..._ICM_..._SED_...` proves **`USE_ICM=ON` and `USE_SED=ON` (plus `USE_COSINE`,
`USE_FIB`) were compiled in** (Dockerfile `FULLMONTY.cmake`, lines 93-106). This
is NOT the naive "flag never compiled" STOP. It is the **iharind pattern** (ADR
0156): the flag is present in the full-monty binary but the binary that would
actually be used for a clean case does not carry it.

### The full-monty binary is not drivable as an isolated ICM or SED case

EMPIRICAL PROOF (offline, local docker, no MinIO/daemon). The complete
hydro-only QuarterAnnulus deck (the in-image gate deck: hgrid.gr3, vgrid.in,
param.nml, bctides.in, drag.gr3, station.in - **no icm.nml, no sediment.nml**)
was run through the full-monty binary:

```
At line 79 of file /src/schism/src/ICM/icm_init.F90 (unit = 31)
Fortran runtime error: Cannot open file './/icm.nml': No such file or directory
Error termination.  ->  mpirun Exit code 2 (all ranks aborted during init)
```

`icm_init.F90:79` (`open(31,file=...//'icm.nml',...,status='old')`, read of the
`&MARCO` namelist) is reached - so the `USE_ICM` code path **executes** (compiled
in) - and hard-opens icm.nml regardless of what param.nml requests. This is the
ADR-0115 documented behavior ("a full-monty binary unconditionally initializes
every compiled tracer module, so every run must supply that module's namelist"),
now empirically confirmed. The SAME deck runs to completion on `pschism_TVD-VL`
(the deck is valid; only the full-monty binary's demand blocks it). Full evidence
in `docs/proof/templates/schism_icm_sed3d_gate_probe.txt`.

ICM is opened FIRST and aborts; sediment.nml, cosine.nml, and the fib/gen/age/
pahm/marsh/ha inputs would each be demanded next in turn. There is **no runtime
switch** to disable a compiled module's init. Consequently, driving ANY single
ICM or SED row through the full-monty binary requires authoring ALL of icm.nml +
sediment.nml + cosine.nml + fib/gen/age/pahm/marsh/ha inputs SIMULTANEOUSLY, with
all 12 tracer modules interacting - which is neither a clean discriminating-pair
V&V (you cannot attribute a DO change to nutrient loading when sediment,
zooplankton, waves, and fib are all live and cross-coupled) nor a practical deck.

## Decision: STOP-RECIPE (targeted-variant image rebuild), NOT forced

The doctrine-consistent path is a **targeted binary** - exactly how coupled_waves
got `pschism_WWM_GOTM_TVD-VL` (ADR 0126/0131) rather than riding the full-monty,
and exactly the (a)-branch recipe the iharind STOP named (ADR 0156). A production
ICM template wants a `pschism_ICM_TVD-VL` (USE_ICM only); a production SED
template wants `pschism_SED_TVD-VL` (USE_SED only); the wave-enhanced-stress row
wants a joint `USE_SED + USE_WWM` binary; the CoSiNE row wants `USE_COSINE`. Each
is a **worker-image rebuild** (new cmake cache-init + a 4th/5th baked executable
+ ADR 0148 staleness discipline: rebuild + live smoke through the image), out of
a doc-characterization wave's scope, and per the task it is **NOT forced** here.

The full-monty binary already builds these flags green (Dockerfile proven), so a
targeted subset is low-risk - the compile is a demonstrated subset, not new code.

### Targeted-build recipe (cmake cache-init, mirrors WWMGOTM.cmake)

```cmake
# ICM.cmake -> pschism_ICM_TVD-VL  (5 ICM rows + the CoSiNE row adds USE_COSINE)
set(USE_ICM ON CACHE BOOLEAN "" FORCE)
# set(USE_COSINE ON CACHE BOOLEAN "" FORCE)   # only for cosine_sfbay_benchmark

# SED.cmake -> pschism_SED_TVD-VL   (multiclass + morphodynamic + trench rows)
set(USE_SED ON CACHE BOOLEAN "" FORCE)

# SEDWWM.cmake -> pschism_WWM_SED_TVD-VL  (wave_enhanced_bottom_stress_sediment)
set(USE_WWM ON CACHE BOOLEAN "" FORCE)
set(USE_SED ON CACHE BOOLEAN "" FORCE)
```

Add each as a fourth+ `mkdir build_icm && cmake -C COMPILERS.cmake -C ICM.cmake`
leg + `cp` into `/opt/schism/bin` + strip (Dockerfile lines 142-167 pattern) +
an entrypoint `variant` (`icm`/`sed`) + `_resolve_exe` glob (entrypoint.py L78),
then a live in-image smoke through the docker image. EcoSim stays excluded (the
`-finit-local-zero` incompatibility, ADR 0115). ICM needs a **3D vgrid + tracer
transport** enabled (ibc=0, itr_met=3), same core the baroclinic template already
authors (ADR 0189) - the ICM/SED deck author extends `author_baroclinic_estuary_deck`.

## Per-row disposition (all STOP-RECIPE; decks grounded in v5.11.0 sample_inputs)

Namelist facts are from the pinned SCHISM v5.11.0 source (commit
`4d350e49481c625002ee2bf7d7fca32777f53c65`) `sample_inputs/{icm,sediment,cosine}.nml`,
read directly from the build tree; cited as `schism-docs-icm` / `schism-docs-sed3d`
/ `test_suite.md` per the board rows.

### SED3D/SED2D (4 rows) - binary `pschism_SED_TVD-VL` (USE_SED)

1. **`multiclass_suspended_sediment_transport`** [CAND-M] - the substrate row.
   Deck: `sediment.nml` `&SED_CORE` `Sd50` (5-class 0.12-1.2 mm sample) +
   `&SED_OPT` `iSedtype=1` (sand), `Wsed` (settling velocity, 1.06-28.65 mm/s
   sample), `tau_ce` (critical shear, 0.15-0.6 Pa sample), `Nbed=1`, `sed_morph=0`
   (no bed change). Discriminating pair (norm #9): FINE class (Wsed 1.06, tau_ce
   0.15) vs COARSE class (Wsed 28.65, tau_ce 0.6) under identical river+tidal
   forcing -> fine stays suspended / coarse deposits near the source. Analytic,
   geography-free (idealized estuary channel, the baroclinic default geometry).

2. **`morphodynamic_bed_evolution`** [CAND-M] - adds `sed_morph=1`, `morph_fac`
   (time-acceleration), `sed_morph_time`, multi-layer `Nbed`. Discriminating pair:
   morph OFF (sed_morph=0, bed frozen) vs morph ON (sed_morph=1) -> Exner bed
   accretion/erosion pattern emerges only in the ON case. GUARDRAIL to encode:
   the doc's documented river-boundary dry-out failure mode (point-source sediment
   input at the river boundary; the template must clamp/warn).

3. **`trench_migration_benchmark_replication`** [CAND-L] - published V&V:
   `Test_SED_Trench_Migration` / `Test_Sed2d_Trench_Migration` / `Test_SED_meander_2`
   (named in official test_suite.md). Measured-vs-modeled bed-profile evolution;
   SED3D vs SED2D. NATE-gated (published-benchmark replication, US-applicable).

4. **`wave_enhanced_bottom_stress_sediment`** [CAND-L] - REQUIRES the JOINT
   `USE_SED + USE_WWM` binary (`pschism_WWM_SED_TVD-VL`). Discriminating pair:
   current-only bottom stress vs wave-enhanced (WWM radiation-stress-fed bottom
   shear) -> resuspension increase. Cross-module; the heaviest of the four.

### ICM (5 rows) - binary `pschism_ICM_TVD-VL` (USE_ICM; +USE_COSINE for row 4)

1. **`eutrophication_core_wq_run`** [CAND-M] - the substrate row. Deck: `icm.nml`
   `&MARCO` core switches (17 state vars: PB1-3, RPOC/LPOC/DOC, RPON/LPON/DON,
   NH4, NO3, RPOP/LPOP/DOP, PO4, COD, DOX), `iKe` (light attenuation formulation),
   `iLight`, `iPR` (predation: 0 linear / 1 quadratic), `iZB` (zooplankton),
   `iSilica`. Nutrient loading via a river point source (NH4/NO3/PO4 msource).
   Discriminating pair: LOAD vs NO-LOAD (nutrient point source ON vs OFF) ->
   chlorophyll-a bloom + downstream DO sag only under load; SECOND pair LIGHT vs
   DARK (iLight/iRad or Ke0 attenuation) -> phytoplankton growth gated by light.

2. **`chesapeake_bay_icm_benchmark_replication`** [CAND-L] - published V&V:
   `Test_ICM_ChesBay` (also `Test_ICM_UB`), named in test_suite.md; long USACE/
   SCHISM Chesapeake calibration history. DO/chlorophyll seasonal cycle. NATE-gated.

3. **`marsh_nutrient_coupling_icm`** [CAND-M] - `imarsh_icm` (0/1/2:
   off/mechanistic/simple) + `iNmarsh` (N/P dynamics). NOTE: also pulls USE_MARSH
   (the full-monty carries the two `iof_marsh` `then`-fix patches, Dockerfile L70).
   Discriminating pair: marsh OFF vs ON -> estuary-wide nutrient-budget shift from
   marsh uptake.

4. **`cosine_sfbay_benchmark_replication`** [CAND-L] - binary adds `USE_COSINE`.
   Deck: `cosine.nml` `&MARCO` `idelay` (7-day zooplankton-predation delay),
   `ibgraze` (bottom grazing), `idapt` (light adaptation), `iz2graze`. Published
   V&V: `Test_COSINE_SFBay` / `Test_FABM_COSINE_SFBay` (test_suite.md); SF Bay
   nutrient/plankton cycle. NATE-gated.

5. **`fib_bacteria_indicator_transport`** [CAND-M] - USE_FIB (compiled in
   full-monty; `_FIB_` in the name). ROSTER GAP stands: overview.md names "Fecal
   bacteria" as a tracer module but no dedicated `modules/fib.html` was
   enumerated - verify the dedicated doc/deck contract exists before scoping.
   Wastewater/CSO point source -> FIB decay/dilution plume vs a recreational-water
   threshold.

## Consequences

- The 9 board rows are annotated STOP-RECIPE pointing here (ICM x5 L259-278,
  SED x4 L238-253). No row is LANDED; no false-green.
- Zero registry growth, zero coded-tools delta, no worker/server/image change.
  This is doc-only touched-area characterization (the ADR 0238 posture).
- The substrate is REAL (flags compiled, physics executes) - the next SCHISM
  wave that wants ICM or SED starts from a targeted-binary image rebuild, not
  from a research question. The two substrate rows (`multiclass_suspended_sediment_transport`,
  `eutrophication_core_wq_run`) are the cheapest first landings once the targeted
  binaries are baked; each unlocks its section's benchmark-replication rows.
- Open build risk noted, not resolved: ICM/SED require a 3D vgrid + tracer
  transport (ibc=0, itr_met=3) - the baroclinic core (ADR 0189) already authors
  it, so the deck author extends `author_baroclinic_estuary_deck`; the targeted
  binaries are an unproven-but-low-risk subset of the green full-monty compile.
