# ADR 0246 - Triage sweep 2: GeoClaw boussinesq + MODFLOW PRT + HAZUS lifelines

Date: 2026-08-13
Status: accepted

## Context

Second board triage sweep over three small clusters in
docs/validation/module-coverage-board.md, handled sequentially by family. The
knob-or-STOP rule: LAND only a cheap knob/parameter/template on machinery that
already exists; STOP-RECIPE anything needing a binary/image variant, a missing
data source, or heavy new machinery. Every claim verified IN-IMAGE (docker run
the worker image / probe the shipped library), never guessed from docs.

Three families, nine open rows:

- GeoClaw boussinesq (### boussinesq): `sgn_boussinesq_depth_switched_solve`,
  `radial_flat_bouss_dispersion_smoke_case`, `1d_bouss_wavetank_replication`.
- MODFLOW PRT (### PRT): `prt_backward_capture_zone_quadrefined`,
  `prt_forward_transient_flow_pathlines`,
  `prt_backward_lateral_boundary_injection_wells`.
- HAZUS lifelines (### HAZUS Earthquake - Lifeline Network Damage Models):
  `transportation_network_seismic_damage`, `potable_water_network_seismic_damage`,
  `electric_power_network_seismic_damage`.

## Decision

Zero landings this sweep. Five STOP-RECIPE, one SUBSUMED, three READY-TO-LAND.
No new tools, no image rebuild.

### GeoClaw boussinesq - all three STOP-RECIPE (image-variant)

In-image probe of `trid3nt-local/geoclaw:latest`: the bouss Fortran SOURCE is
present ($CLAW_SRC/geoclaw/src/2d/bouss/: bouss_module.f90, SGN COO/CRS
sparse-matrix builders, petsc_driver.f90, implicit_update_bouss_2Calls.f90,
Makefile.bouss) but PETSc + MPI are ABSENT - `PETSC_DIR` empty, no mpirun/mpif90
on PATH, no petsc headers. The bouss example Makefile (Makefile.bouss) hard-errors
at compile: `ifndef PETSC_DIR $(error PETSC_DIR not set)` plus mandatory
PETSC_OPTIONS / CLAW_MPIEXEC / CLAW_MPIFC. A num_eqn=5 xgeoclaw therefore cannot
compile in-image - this is an image variant, not a knob. Unblock: rebuild the
geoclaw image with PETSc 3.20+ and an MPI (mpich/openmpi), set the four env vars,
`make new` once, then setrun_builder emits bouss_equations + bouss_min_depth +
bouss_min_level/max_level; land radial_flat as the through-image smoke gate first,
then the 1D wavetank V&V. All three sequence into one PETSc image wave.

### MODFLOW PRT - two STOP-RECIPE, one SUBSUMED

PRT itself is CONFIRMED native in `trid3nt-local/modflow:latest` (mf6 6.7.0
02/05/2026, flopy 3.10.0, `from flopy.mf6 import ModflowPrt` OK; backward
capture-zone + transient PRT already live via the wellhead track, ADR 0215), so
the cluster's core adjudication question is answered - PRT is present.

- `prt_backward_capture_zone_quadrefined` + `prt_backward_lateral_boundary_injection_wells`
  STOP on the DISV quad-refinement, not PRT: `which gridgen` = none,
  `which triangle` = none in-image (flopy.utils.gridgen.Gridgen imports but needs
  the gridgen executable at runtime). Only structured-grid PRT is wired. Unblock:
  install + SHA-pin the USGS gridgen binary into the modflow image, author the
  refined DISV, ModflowPrt on it, worker-image rebuild + through-image smoke.
- `prt_forward_transient_flow_pathlines` is SUBSUMED: the transient-PRT physics
  it asks for is already landed and live (wellhead_transient_multiwell_capture,
  ADR 0215 - steady spin-up + N GwfSto periods, isochrones evolve with drawdown).
  The only delta is tracking direction (forward vs the wired backward); a thin
  forward-pathline product on the proven structured PRT path, no binary/image gap.

### HAZUS lifelines - all three READY-TO-LAND (board correction)

The board's "unknown/likely unsurfaced/missing" was WRONG. pelicun 3.9.0 runs in
the SERVER VENV (venvs/agent, no worker image -> no image tax) and SHIPS the full
DamageAndLossModelLibrary lifeline datasets under
resources/DamageAndLossModelLibrary/seismic/{transportation_network Hazus v5.1,
water_network Hazus v6.1, power_network Hazus v5.1} - each with fragility.csv +
consequence + a pelicun_config.py auto-population script; dlml_resource_paths.json
maps all three DL_Methods. The agent-side `run_dl_calculation` seam is
asset-class-agnostic (the network path is an `is_for_water_network_assessment`
flag through the same run_pelicun).

Proof: a single HwyBridge AIM (assetSubtype='HwyBridge', BridgeClass/StateCode/
YearBuilt/NumOfSpans/MaxSpanLength/Skew/DeckWidth/StructureLength/ConstructType) +
a demand.csv with SA_1.0 + PGD, DL_Method='Hazus Earthquake - Transportation', run
in-venv through run_dl_calculation: the bundled auto-pop classified HWB28, assigned
components [HWB.GS.28, HWB.GF] with the K_skew/K_3D/PGD scaling factors, and
produced a full DL_summary (repair_cost / repair_time / collapse / irreparable)
over 100 realizations. The water (gi type=Pipe, PGV+PGD repair-rate) and power (gi
type=Substation, PGA) auto-pops also ENGAGE in-venv; they fault only on the exact
R2D AIM key spellings + a Losses config block - landing details, not
machinery/data/binary gaps.

Not landed in this sweep because a full, registered, three-class template family
(new workflow module + tool + categories/pins/corpus/retrieve-check registration
+ water/power AIM-key + Losses completion) is a scoped build job, above triage.
Recorded READY-TO-LAND with the collapse recipe on the board (one
`hazus_lifeline_seismic_dl_run` template with a `lifeline_class` knob; no image).

## Consequence

Board: 5 STOP-RECIPE, 1 SUBSUMED, 3 READY-TO-LAND; TOTALS sweep note appended,
per-row markers updated. Two durable in-image facts for the orchestrator: (1) the
geoclaw image has no PETSc/MPI - any bouss (or other PETSc-implicit) GeoClaw
capability is an image wave; (2) the modflow image has no gridgen/triangle - any
DISV/DISU quad-refined grid path is an image wave. The biggest correction is
lifelines: the HAZUS network fragility library is already on disk and drivable, so
the lifeline family is a low-risk in-venv build (no image tax), not a data gap.

Docs-only sweep (read-only probes + scratch smokes); no production code changed.
