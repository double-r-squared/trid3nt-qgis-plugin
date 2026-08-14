# ADR 0257 - GeoClaw Boussinesq (SGN) dispersive solver image wave

Date: 2026-08-14
Status: accepted

## Context

ADR 0246 (triage sweep 2) left the GeoClaw `### boussinesq` cluster as a
STOP-RECIPE: the Boussinesq Fortran source ships in the geoclaw image
(`$CLAW/geoclaw/src/2d/bouss/`: `bouss_module.f90`, the SGN COO/CRS sparse-matrix
builders, `petsc_driver.f90`, `Makefile.bouss`) but PETSc and MPI were absent, so
the `num_eqn=5` executable that solves the implicit Serre-Green-Naghdi (SGN)
dispersive system could not compile. `Makefile.bouss` hard-errors without
`PETSC_DIR` / `PETSC_OPTIONS` / `CLAW_MPIEXEC` / `CLAW_MPIFC`. The recipe: rebuild
the image with PETSc 3.20+ and an MPI, wire the `bouss_*` setrun knobs, land the
`radial_flat` through-image smoke, then the 1D wave-tank V&V.

## Decision

Execute the recipe. Two of the three rows LAND; the 1D wave-tank stays STOP for a
new (non-image) reason.

### Image (`trid3nt-local/geoclaw:bouss`)

Multi-stage. A `petscbuild` stage installs a conda-forge env
(`petsc>=3.20` -> 3.25.4 + `mpich` + `gfortran`/`make`/`pkg-config`); the runtime
stage copies ONLY the resolved `/opt/petscenv` (micromamba binary + the package
tarball cache stay in the builder). Size 1.08GB -> 2.99GB; the +1.42GB is the
PETSc numerical stack (hypre/mumps/superlu/scalapack/hdf5/openblas) + the conda
compiler the runtime per-deck bouss link needs.

Load-bearing facts discovered in-image:

- **PETSc 3.20+ is mandatory.** Debian's packaged PETSc 3.18 compiles every
  object but FAILS at link: `undefined reference to
  petscviewerasciistdoutsetfileunit_` (added to PETSc in 3.20). conda-forge 3.25
  links clean.
- **UCX FP-trap SIGFPE.** mpich's default UCX transport probes network-interface
  port speed and divides by a zero link speed in a network-less container; a
  benign FP op that gfortran's `-ffpe-trap` (baked into the bouss FFLAGS) turns
  into a fatal `SIGFPE` at `MPI_Init`. Fixed by forcing mpich onto libfabric/tcp:
  `MPIR_CVAR_CH4_NETMOD=ofi` + `FI_PROVIDER=tcp` (image env).
- **Static archives must be kept.** A hygiene `find -name '*.a' -delete` broke the
  runtime bouss link (`cannot find -lgcc`) -- the per-deck compile pulls the
  compiler's `libgcc.a`/`libgfortran.a`. Only docs/man/tests/pyc are pruned.

The `num_eqn=5` bouss xgeoclaw compiles + links + runs in-image (build-time
provenance: `__bouss_module_MOD_*` symbols, `libpetsc.so.3.25` linkage,
`clawdata.num_eqn = 5`).

### Wiring (SWE default byte-identical)

`bouss_equations` (0=SWE / 1=Madsen-Sorensen / 2=SGN) + `bouss_min_depth` +
`bouss_min_level` / `bouss_max_level` thread `GeoClawRunArgs` (contract) ->
`build_geoclaw_build_spec` (composer, forwarded only when engaged) ->
`setrun_builder` (strict parser bumped `geoclaw-spec-7`; emits a `BoussData` block
+ `num_eqn=5` + a `Makefile.bouss` variant with the PETSc pkg-config flags). The
entrypoint prepends `/opt/petscenv/bin` to the make subprocess PATH ONLY for a
bouss deck, so the conda `gfortran` never shadows the system compiler the shallow
lane builds with. `bouss_equations=0` renders num_eqn=3 with no BoussData -- the
non-bouss path is unchanged.

### Physics proof (the discriminating pair)

`radial_flat` (a localized Gaussian hump on flat 100 m water) run twice through
`geoclaw:bouss`: SGN (`bouss_equations=2`) vs SWE (`bouss_equations=0`). At
t=150 s in the wake window r=1500-4000 m, SGN carries a rank-ordered dispersive
wave train (RMS 0.063 m, 11 zero-crossings, ptp 0.227 m) while SWE is flat behind
its single leading N-wave (RMS 0.003 m, 0 zero-crossings) -- a 20.4x RMS ratio; in
2D, SGN concentric dispersive rings vs the SWE single ring. That trailing train IS
the dispersive physics SWE cannot produce. Cost of the implicit PETSc step:
SGN 2m28s vs SWE 8.8s (~17x) on 100x100 + 2 AMR levels, tfinal 200 s.

## Consequence

Board: `sgn_boussinesq_depth_switched_solve` + `radial_flat_bouss_dispersion_smoke_case`
LANDED (the second verification-tier); `1d_bouss_wavetank_replication` stays
STOP-RECIPE for a NEW reason -- it is a separate 1D geoclaw executable path
(`1d_classic`, not the 2D `xgeoclaw` this wave built) replicating a non-US
published physical flume (Matsuyama/USACE), a distinct 1D-solver V&V build, not
the image gap. Registry UNCHANGED at 255 (a knob on the existing geoclaw leg, no
new tool/template). Four-slice at baseline (fetch_resolution x4 [f-o] +
river_dye x2 [p-r]); contracts 710 + geoclaw worker 92 + 244 server geoclaw green.

Real-US case (Chignik/Cascadia SGN-vs-SWE arrival waveform) QUEUED -- an honest
partial: the ~17x implicit-solve cost makes a real AOI a long solve; the capability
is smoke-proven and fully wired, ready to drive when a solve budget is allocated.
Durable orchestrator fact: the geoclaw image now carries PETSc 3.25 + mpich, so any
PETSc-implicit GeoClaw capability (bouss, and future implicit variants) is
unblocked -- but MUST run on the ofi/tcp netmod, never the default UCX.

Proofs: `docs/proof/templates/radial_flat_bouss_dispersion_smoke_case.png` +
`radial_flat_bouss_dispersion_2d_field.png`.
