# ADR 0207 - HEC-RAS 2D rain-on-grid: the preprocessing-shim hunt (the 2025 managed engine solves on Linux)

Date: 2026-08-09
Status: Accepted (the HR2D rain-on-grid SOLVE is UNBLOCKED on Linux -- not through the
6.6 Fortran path but through the 2025 managed engine, which preprocesses AND solves 2D
unsteady shallow-water on Linux with the CPU solver, no Windows, no Wine. A uniform
rain-on-grid demo is live: rain applied to every cell, mass-conservative. The remaining
work to a real-catchment Muncie/Coweeta RoG solve is authoring plumbing, not a
Windows dependency -- a bounded continuation.)
Continues: ADR 0205 (the headless precip-interpolation decode + the READ_UN_HYDROLOGY2D
residual) and ADR 0199 (the RoG cross-engine wave). NATE asked: does ANY publicly
available shim generate the Windows-preprocessing artifacts without Windows?

## Context

ADR 0199/0205 drove the 6.6 Fortran RoG solve to its last link and froze it: the precip
interpolation folder was DECODED byte-exact and authored (the engine reads past the
MetInterp segfault), but the residual moved down to `READ_UN_HYDROLOGY2D`, which faults
in its output-id/region setup (`H5Gcreate2: invalid location` / `getregidbyname`) when
SCS-CN is present, and delivers ZERO rain without it (`precip2fvcell` "Cell are not
linked"). Root cause (confirmed, ras-commander `RasPreprocess.py` verbatim): "Windows
preprocessing is required for ALL HEC-RAS Linux versions (6.3.1+). The Linux binaries
cannot produce the .tmp.hdf or .b## files needed to begin execution." The 6.6 hydrology
output/region scaffold is a Windows-preprocessing artifact.

This ADR hunted three shim classes to produce that class of artifact (or bypass it)
without Windows.

## The survey table

| Candidate | reads / writes / orchestrates | Verdict |
|---|---|---|
| **2025 managed engine** (`ras prepare` + `ras solve`, CUDA-free CPU path) | **WRITES its own R2R + SOLVES** | **WORKS on Linux -- the shim (Class A)** |
| Wine + Windows 6.6 `RasUnsteady.exe` | would run the Windows preprocessor/solver | NOT NEEDED (Class A works natively); not probed |
| `ras-commander` 0.99.x | orchestrates Windows RAS; `_update_precipitation_hdf` stops at `Imported Raster Data/Values`, offloads plan-HDF gen to Windows | orchestrates only (ruled out, ADR 0199) |
| `fema-ffrd/rashdf` | READS HEC-RAS geometry/plan/results HDF; exports to GIS (shp/parquet/gpkg) | read-only, no writer |
| `pyHMT2D` (psu-efd) | ORCHESTRATES a Windows HEC-RAS install via COM API (`init_model`/`run_model`); "only Windows is supported" | orchestrates, Windows-only |
| HEC-Commander notebooks | orchestrate the Windows RAS controller/CLI batch runs | orchestrate, Windows |
| RAS Mapper .NET (`Ras.Mapper.dll`) meteorology rasterizer | WOULD write the 6.6 folder | gutted in the beta (ADR 0205: "Wind code removed", precip stub) -- dead |

Net: no OSS tool writes the 6.6 Windows-preprocessing artifacts on Linux (all read or
orchestrate). But Class A does not need to -- it replaces the 6.6 preprocessing step
entirely with its own Linux-native preprocessing + solve.

## Decision 1 - Class A: the 2025 managed engine SOLVES 2D unsteady on Linux (the win)

Decompiling the authoring image's managed assemblies (`ilspycmd` 9, ADR 0205 recipe)
established the pivotal fact: the ONLY native P/Invoke in `Ras.Engine.dll` is
`Ras.Engine.CUDA.dll` (the GPU path). The CPU solver stack -- `ComputeDriver`, `Solver`,
`SolverImpSWE`/`SolverExpSWE`/`SolverDWE`, `IExplicitSolver` -- is pure managed C#, so it
runs on the Linux .NET 9 shared runtime. The ADR 0127 spike had noted a `RasNativeParallel`
gap; the shipped beta's `healthcheck` in fact only probes GDAL (substituted in the
authoring image, ADR 0129) -- it passes on Linux.

The 2025 CLI workflow, all proven LIVE on Linux in `trid3nt-local/hecras2025-authoring`:

1. `migrate.dll project -s <v6.prj> -d <dir>` -- converts a v6 project to a 2025
   project (needs a framework-dependent `migrate.runtimeconfig.json` copied from the
   patched `ras.runtimeconfig.json`; the beta ships only a self-contained one). Muncie
   v6 -> 2025 migrated with "Success!" on Linux.
2. `ras prepare -s <project.ras> -o <dir>` -- THE preprocessing step (the Windows
   `.tmp.hdf`/`.b##` equivalent): ingests data, computes the subgrid property tables +
   boundary conditions + initial water surface, writes one ready-to-run `<plan>.r2r.h5`.
   Runs the full pipeline on Linux with NO missing-native wall.
3. `ras solve <plan>.r2r.h5 <out.h5> --solver CPU` -- the compute. On Linux:
   `Equation Set: Shallow Water Equations / Initializing Solver (CPU) / Computing (CPU) /
   Computations completed`.

A 2025 project is authored in managed code via the built-in `Ras.Synthetics` framework
(`SyntheticTestCases.CreateSyntheticTestCase` -> a fully-associated project: terrain +
NValue layer + BC + plan associations). A beta bug in `Project.SaveAs` (it passes the
just-created `Terrains` directory to `Terrain.ExportFullCopy`, which expects a file path,
throwing "exists as a directory" before the NValue write) is worked around by catching
it and writing the NValue layer file into the `Surface Layers/` dir (the actual
`GetDefaultNValueLayersDirectory` location). Driver + full reproduction:
`scripts/sandbox/hecras/managed_solve/` (Driver.cs/csproj + REPRODUCE.md).

Live-proven CPU solves on Linux (`hecras2025-authoring:latest`, id afb76f3ccd00):
- **Inflow channel** (`InOutPlanarParams`): 45-cell basin, 30 cfs ramped inflow;
  depth rises 1.0 -> 2.66 ft over 300 s; volume added ~7470 cf vs inflow ~7500 cf
  (mass balance correct); max face velocity 0.69 ft/s; 31 output steps.

## Decision 2 - Class A delivers RAIN-ON-GRID on Linux (the actual unblock)

The `PrecipitationLayer.InitializeComputeDriver` path (the same in-memory nearest-neighbour
precip decode ADR 0205 found) is driven directly: on the 2025 BoundaryCondition set
`Precipitation.IsEnabled=true`, `SpatialDataType=Constant`, `ConstantValue=<rate>`. The
engine applies rain to every cell in memory -- NO 6.6 hydrology-output/region scaffold,
NO interpolation folder, NO Windows preprocessing.

Live-proven RAIN-ON-GRID solve on Linux (rain-only basin, no inflow/outflow BC lines):
- `ras prepare` reports "Precipitation layer prepared successfully"; `ras solve --solver CPU`
  completes.
- Cell Depth rises SPATIALLY UNIFORM (min == max) and LINEARLY, driven purely by
  precipitation: rate=100 -> +0.10 ft/hr, rate=300 -> +0.30 ft/hr, ratio exactly 3.0
  (mass-conservative, rate-linear). This is the exact behaviour the 6.6 Fortran path
  could NOT reach ("Cell are not linked" -> zero precip). The ADR 0205 residual is
  resolved on this path: the 2025 engine links rain to cells and delivers it.

## Decision 3 - Class B (Wine) NOT needed; Class C is read/orchestrate only

Class A works natively, so running the Windows 6.6 binaries under Wine is unnecessary and
was not probed (per the directive's time-box + "if a class unblocks, stop at a rain-
delivering solve"). For the record, the Wine route would target the Windows RAS
preprocessor/compute; it remains a documented, untried fallback with no advantage over the
native Class A path.

Class C (OSS ecosystem): every candidate READS or ORCHESTRATES, none WRITES the Windows
preprocessing artifacts on Linux -- `rashdf` (fema-ffrd) is read-only (extract -> GIS);
`pyHMT2D` orchestrates a Windows HEC-RAS via COM (Windows-only); HEC-Commander notebooks
orchestrate the Windows controller; `ras-commander` already ruled out (ADR 0199). The
RAS Mapper .NET writer is gutted in the beta (ADR 0205). Citations in the survey table.

## Consequences

- The HR2D rain-on-grid SOLVE is UNBLOCKED on Linux via the 2025 managed engine. NATE's
  Windows-GUI export (the ADR 0199/0205 unblock) is NO LONGER REQUIRED for a RoG solve.
- +0 registered tools / +0 templates / +0 worker images this wave (a probe + survey; the
  registered `hecras_flood_2d` RoG surface + worker parser stay FROZEN until the
  real-catchment path lands and is verified through an image -- honesty floor).
- Files (in-repo): `scripts/sandbox/hecras/managed_solve/{Driver.cs,Driver.csproj,REPRODUCE.md}`
  (OUR C# driver + reproduction; no proprietary binaries committed). Verdict appended to
  ADR 0205; board rows updated.
- Proprietary DLLs, the extracted decompiled trees, the probe project dirs, and the
  downloaded USACE example zip live only in the session scratchpad / an external host dir
  (all gitignored or outside the repo). Reproduce via REPRODUCE.md.

## Remaining continuation (bounded, NOT a Windows dependency)

A real-catchment (Muncie/Coweeta) RoG solve through Class A still needs authoring
plumbing, either:
(a) complete the v6->2025 migration's unset plan associations (Terrain / NValue / BC),
    which migration leaves blank in this beta and which also drops Muncie's inflow BC; or
(b) author a fresh 2025 RoG project via the synthetic path but with a REAL terrain raster
    (the fresh-AOI DEM) + the design-storm constant precipitation, then prepare + solve,
    then extract RoG metrics (outlet discharge, max depth/velocity, wet extent) from the
    2025 result HDF, register the `hecras_flood_2d` RoG knobs on a 2025-solve worker, and
    seed the showcase.
This is the frozen tail (ADR 0199 Decision 2 / ADR 0205 Decision 3), now reachable
entirely on Linux. Coweeta + metrics + cross-engine comparison + showcase remain a
follow-up wave (this wave stayed bounded at the proven rain-delivering solve).

## Reproducibility / provenance

- Managed CLI: `ras 0.1.0.2965-dev`; runtime .NET 9 shared, `DOTNET_ROLL_FORWARD=Major`
  for the decompiler.
- rashdf read-only: github.com/fema-ffrd/rashdf. pyHMT2D Windows-COM orchestration:
  github.com/psu-efd/pyHMT2D. ras-commander Windows-preprocessing requirement: quoted
  verbatim in ADR 0199.
- The only native dependency of the 2025 solve is CUDA (GPU path); the CPU path is pure
  managed and Linux-portable.
