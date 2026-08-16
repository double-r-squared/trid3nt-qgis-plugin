# SCHISM solver worker (spike -- ADR 0115)

Builds SCHISM v5.11.0 (pinned commit `4d350e4`) from source with gfortran +
OpenMPI + netCDF-Fortran, baking two executables:

- `pschism_WWM_COSINE_ICM_FIB_SED_ANALYSIS_PREC_EVAP_PAHM_HA_MARSH_GEN_AGE_TVD-VL`
  -- the full-monty single binary (hydro core + WWM waves, SED3D sediment, ICM +
  CoSiNE + FIB water quality, GEN/AGE tracers, PaHM hurricane, marsh, harmonic
  analysis, precip/evap). Every compiled tracer module is initialized on every
  run, so a run must supply that module's namelist -- targeted variants are the
  practical pattern.
- `pschism_TVD-VL` -- the hydro-core binary (plain surge/tide/baroclinic), the
  STOFS-class default and the one the verification gate exercises.

Findings baked in (ADR 0115): EcoSim (USE_ECO) is excluded (its automatic-array
initializers are illegal under `-finit-local-zero`; needs ifort or a per-module
flag exception); MARSH carries two one-line upstream `then` fixes.

## Verification gate

`fixtures/quarterannulus/qa_gate.py` runs SCHISM's own `Test_QuarterAnnulus`
(Lynch-Gray analytical M2 tidal channel) under MPI (2 compute + 2 scribe) and
asserts the station elevation reproduces the bundled analytical solution
(amp err <= 0.010 m, RMSE <= 0.030 m over the spun-up window). Runs at image
build time; measured green at RMSE 0.0155 m / amp err 0.0027 m / corr 0.999.

## Mesh supply

`schism_gr3.py::tin_to_hgrid` converts an oceanmesh `coastal_tin` output
(lon/lat nodes + triangles) into a SCHISM `hgrid.gr3` -- CCW element
normalization, complete boundary-loop extraction, non-manifold pinch-point
cleaning, exterior/island land segments. Proven: SCHISM's `ipre` grid check
fully ingests the Galveston TIN grid (ne=21660, np=12154, ns=33820), passing
all geometry/boundary checks. `test_schism_gr3.py` covers the bridge.

## Run envelope

`entrypoint.py`: bind-mount a ready SCHISM case + `manifest.json` at `/data`;
it runs the selected variant under mpirun, gates on the "Run completed
successfully" sentinel (never the exit code), and writes `schism_metrics.json`.
