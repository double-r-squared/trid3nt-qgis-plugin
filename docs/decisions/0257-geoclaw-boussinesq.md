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

## Addendum (2026-08-14) -- the queued real-US case: Chignik SGN-vs-SWE arrival waveform

EXECUTED the queued real-event leg. Source: the REAL 2021 M8.2 Chignik earthquake
(USGS ComCat `ak0219neiszm`, epicenter (-157.8876, 55.3635), Mw 8.2, focal depth
35 km) -- the same event the repo's Chignik drivers use. Both legs IDENTICAL in
every parameter except the dispersive knob: `bouss_equations=0` (SWE) vs `2` (SGN),
recorded at a nearshore shelf gauge (-159.30, 55.30), depth ~180 m.

BUDGET COARSENING (say-so, per the ADR budget guidance): the `earthquake_source`
finite-fault path grows the computational domain to enclose the 294-subfault
rupture footprint -- basin scale (~415x350 km, ~242k base cells), a multi-HOUR SGN
solve. We bounded the domain to the NEAR-FIELD epicenter->gauge propagation window
(-160.2..-157.4 lon x 54.7..55.9 lat, ~30k cells, `compute_class=small`) and drove
a synthetic Okada at the SAME real catalog epicenter/Mw/depth; `amr_levels=2`,
`tfinal=3600 s` (captures the full leading wave: crest + recession + trough),
`bouss_min_depth=10 m`. The arrival waveform + leading-wave shape + trailing train
(the comparison target) survive this; the source is identical across both legs.

Numbers (`geoclaw_sgn_vs_swe_summary.json`; SWE run `01M01A8GYS2CDP318SK03NZ3RX`,
SGN run `01M01AC6YW40KKCPRKDMRKERQ1`): the gauge records a single long-period
leading wave -- crest +0.7259 m at t=1784 s, trough -0.425 m at t=3324 s, range
1.151 m. SGN and SWE are INDISTINGUISHABLE: leading-crest delta +0.03 mm, trailing
(post-crest) RMS ratio SGN/SWE 1.00000, and max |SGN-SWE| over the whole record
just 3.43 mm = 0.30% of the range -- and that residual is < 0.05 mm across the
entire long leading wave, growing to a few mm ONLY in the steeper trailing trough
(t > 2400 s) where the shorter-wavelength content lives. No arrival-time shift, no
distinct trailing dispersive train. Solve wall: SWE 102 s vs SGN 232 s -- SGN 2.3x
SWE HERE, NOT the ~17x of the flat radial_flat smoke: on a real depth-switched AOI
`bouss_min_depth=10 m` + real bathymetry shrink the bouss-active (implicit-PETSc)
cell fraction far below the all-bouss flat-tank regime, so the 17x is an upper
bound for the pathological all-deep-water case, not the cost of a real coastal run.

FIDELITY-LADDER GUIDANCE (the finding): SGN dispersion is NOT worth its extra cost
for a near-field earthquake tsunami whose broad co-seismic source radiates a LONG
leading wave over a short (~150 km) shelf propagation -- SWE is faithful to sub-1%
at the coast; reach for SGN (2=SGN) only when the source is SHORT-wavelength
relative to depth (submarine landslides, impact/collapse waves -- the radial_flat
regime where the 20.4x wake-RMS discriminant appears) or when frequency dispersion
accumulates over FAR-FIELD trans-oceanic propagation (thousands of km), never
reflexively for coastal earthquake run-up.

Proof: `docs/proof/templates/geoclaw_chignik_sgn_vs_swe_arrival.png` (waveform
overlay + magnified mm-scale SGN-SWE residual). Drivers:
`scripts/drive_geoclaw_chignik_sgn_vs_swe.py` (one leg per run, bouss knob injected
via a driver-local `GeoClawRunArgs` monkeypatch -- no tool-surface change, no
registry change) + `scripts/plot_geoclaw_chignik_sgn_vs_swe.py`. Registry unchanged
at 255; no board row flips (the two bouss rows were already LANDED; this fulfills
the QUEUED real-US follow-on).
