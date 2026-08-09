# ADR 0205 - HEC-RAS 2D rain-on-grid: the headless precip-interpolation decode + the hydrology residual

Date: 2026-08-08
Status: Accepted (the MetInterp precip-interpolation folder is DECODED byte-exact from
the un-stripped 6.6 solver and authored in Python; the engine now reads PAST the segfault
that blocked all 12 prior attempts. Rain APPLICATION is frozen one layer deeper at the
2D-hydrology module -- a bounded continuation.)
Continues: ADR 0199 (the RoG cross-engine wave + the reference-hunt negative). NATE picked
the "headless RAS Mapper .NET" attempt over waiting on a Windows GUI export.

## Context

ADR 0199 froze the HR2D rain-on-grid SOLVE at link 3: `READ_UN_M2D_PRECIP_INTERP`
(`MetInterp.f90:92`) opens a per-2D-area `Event Conditions/Meteorology/Precipitation/
2D Flow Areas/<area>` interpolation folder; 12 blind-authored guesses segfaulted
(`severe (174) SIGSEGV`), and the reference hunt proved the folder is absent from every
shipped USACE example (a Windows RAS-preprocessing artifact). The recorded secondary
avenue: the `trid3nt-local/hecras2025-authoring` image runs RAS Mapper .NET headless, so a
C# driver invoking RasMapperLib's precip interpolation COULD generate the folder. This ADR
executed that attempt.

## Decision 1 - the headless RAS-Mapper WRITER does not exist in our assemblies (documented negative)

Decompiled the authoring image's managed assemblies with `ilspycmd` 9.0.0.7889 (built into
a derived `local/ilspy:9` image over `mcr.microsoft.com/dotnet/sdk:9.0`; run with
`DOTNET_ROLL_FORWARD=Major`). Findings:

- `Ras.Mapper.dll` (2025 beta) has the meteorology rasterizer REMOVED. `RASEventConditions.
  CompleteForComputations` -- the method that processes precipitation during "compute" -- is
  a no-op stub for precip (starts/stops a stopwatch, writes nothing); `WindLayerNew.
  RasterizeSaveData` returns `prog.ReportError("Wind code removed")`; `AccumulatedPrecipitation
  Layer.RasterizeAccumulatedRatesInternal` is empty. The only `2D Flow Areas` HDF writes in
  the beta are the geometry writer and Initial Conditions -- NOT the meteorology interp folder.
- The 2025 managed engine (`Ras.Engine.dll`) computes the precip->cell interpolation ENTIRELY
  IN MEMORY and solves directly: `Ras.Layers.BoundaryConditions.PrecipitationLayer.
  InitializeComputeDriver` computes `RasterDefinition.GetNearestNeighborWeights(cellCenters)`
  and feeds `BoundaryConditionCollection.SetPrecipitation(times, t0, bandGetter, scale,
  JaggedWeights)` -> an in-memory `PrecipitationBC`; `SpatiotemporalBoundaryCondition.
  ApplyWeights` sums `weight * source[index]` per cell each timestep. It never serializes the
  `Precipitation/2D Flow Areas/<area>` HDF folder the 6.6 Fortran solver reads.
- The production 6.6 image carries no `Ras.Mapper` assembly at all (Fortran engines +
  ras_commander python only).

So no assembly we possess WRITES the 6.6-format folder: the beta gutted it, the managed
engine bypasses it, the folder is emitted only by the Windows 6.x Fortran preprocessor. The
"headless RAS-Mapper writer" avenue is a dead end -- BUT the decompile yielded the exact
mapping MATH (nearest-neighbour raster-pixel -> cell-centre; one source entry per cell,
weight 1.0), which settled that the 12 prior failures were wrong LAYOUT, not wrong physics.

## Decision 2 - the schema DECODED from the un-stripped 6.6 solver binary (the pivot win)

The 6.6 `RasUnsteady` binary is NOT fully stripped. `READ_HDF_INTERP_COEFF` and
`READ_UN_M2D_PRECIP_INTERP` (`Read_UN_Hydrology2D`/`MetInterp` family) carry the exact
dataset names, and the type-encoded HDF read wrappers pin the dtypes/ranks. The per-2D-area
folder `Event Conditions/Meteorology/Precipitation/2D Flow Areas/<area name>` holds SIX
datasets in the classic HEC ragged/CSR layout (identical in shape to the 2D subgrid tables
our geometry writer already authors and the same engine already reads):

    Cell Info     (Nc, 2) int32    [start, count] into the flat cell arrays
    Cell Indexes  (Nnz,)  int32    source met-grid pixel index per entry
    Cell Weights  (Nnz,)  float32  interpolation weight per entry
    Face Info     (Nf, 2) int32    [start, count] into the flat face arrays
    Face Indexes  (Nnz_f,) int32
    Face Weights  (Nnz_f,) float32

dtypes/ranks proven from the disassembly's read wrappers (2 calls each = cell + face):
`mod_hdf_..._h5dread_f_integer2` -> Info (INTEGER rank-2); `..._h5dread_f_integer1` ->
Indexes (INTEGER rank-1); `..._h5dread_f_real1` -> Weights (REAL rank-1). Intel default
INTEGER = int32, REAL = float32; the module descriptor sizes (0x60 vs 0x48) confirm rank-2
vs rank-1. The reader self-sizes each array from its own dataspace.

`write_uniform_precipitation` authors a 1x1 gridded source (`Raster Cols=Rows=1`), so
nearest-neighbour maps every cell/face to the single source pixel: one CSR entry per
element, `Index = 0`, `Weight = 1.0`, `Info[i] = [i, 1]`. Implemented in Python
(`hecras_meteorology.write_precipitation_interpolation`), NOT a C# driver -- Decision 1
proved no callable writer exists in our DLLs, and the binary decode is a more authoritative
source than the removed .NET code. Offline-tested; composed into the plan by
`hecras_deck2d.compose_pure2d_deck`.

VERIFIED LIVE (2068-cell carved-Muncie de-risk deck, `trid3nt-local/hecras:latest`,
id e2216711e2b0): RasGeomPreprocess exit 0; RasUnsteady reads PAST "initializing 2D
Area(s)" -- the `MetInterp` per-area interpolation SIGSEGV that blocked all 12 ADR-0199
attempts is GONE. The schema decode is correct.

## Decision 3 - the hydrology module is the residual (rain application, honest freeze)

With the interp folder accepted, the engine advances into `READ_UN_HYDROLOGY2D`
(`Read_UN_Hydrology2D.f90`) -- further than any prior run reached -- and faults there:
`HDF_ERROR trying to use HDF output file` / HDF5 `H5Gcreate2(): not a location` (invalid
`hdf_output_id` when the hydrology output is set up during 2D-area init). Bisection on the
2068-cell deck:

- interp folder present + Infiltration ABSENT -> RasUnsteady FINISHES (crash-free), but the
  run applies ZERO precipitation: `Cell Cumulative Precipitation Depth` max = 0, max face
  velocity = 0, Volume Accounting carries no precip term. Rain never links to cells.
- interp folder present + SCS-CN Infiltration present -> faults at `READ_UN_HYDROLOGY2D`.

Root cause (binary symbols): precip APPLICATION routes through the hydrology module --
`INIT_PRECIP2CELL` -> `mod_ibc.precip2fvcell` -> `precipmodule.setprecipratecell`, with the
`UPDATEPrecipDischarge: Cell are not linked` guard. Without a successful
`READ_UN_HYDROLOGY2D` the cells are never linked, so precip discharge = 0; and
`READ_UN_HYDROLOGY2D` itself faults in its output-id/region setup
(`getregidbyname`/`gethydmulti2dregion` + `hdf_output_id`). Both the SCS-CN loss AND the
rain source therefore depend on the same hydrology-output/region setup, which is a
Windows-preprocessing artifact class distinct from (and below) the interp folder.

The SCS-CN authoring is complete and byte-exact regardless: `write_infiltration_layer`
(Infiltration group) + the newly-decoded sibling `write_percent_impervious`
(`Percent Impervious` group -- `READ_UN_HYDROLOGY2D` reads it via
`surfacemodule.setsurfacepercentimpervious`; both groups' schemas taken from the shipped
`BaldEagleDamBrk.g09.hdf`). They are gated behind `apply_infiltration` (default False) so the
default RoG deck is crash-free; `apply_infiltration=True` authors them and reaches the
documented hydrology fault. Neither path yet delivers a real rain-on-grid solve.

Per the honesty floor (ADR 0195 precedent): the registered `hecras_flood_2d` RoG surface +
the worker parser stay FROZEN. Nothing errobearing is registered; no worker image is rebuilt.

## Unblock - the NATE Windows reference plan HDF (unchanged from ADR 0199, now narrower)

The residual is no longer the precip interpolation folder (DECODED + authored + engine-read).
It is the 2D-hydrology output/region setup that `READ_UN_HYDROLOGY2D` performs and that links
precip to cells. One reference plan HDF computed by a Windows HEC-RAS 6.x GUI on a tiny 2D
rain-on-grid area (Constant or gridded precip + any infiltration method, pressed Compute)
would expose the `Results`/hydrology output structure + the region linkage the Linux engine
expects pre-set. Drop at `scripts/sandbox/hecras/_ref/<name>.p##.tmp.hdf`; the staged
continuation diffs it against a crash-free `apply_infiltration=True` deck to author the
missing hydrology-output scaffold, then completes the solve / metric-extractor / registration
/ showcase chain (ADR 0199 Decision 2 frozen tail).

## Consequences

- +0 registered tools / +0 templates / +0 worker images this wave (the composer additions are
  host-side authoring; the registered surface stays frozen pending a real solve).
- Files: `services/workers/hecras2025/subst/crux/freshtopo/hecras_meteorology.py`
  (`write_precipitation_interpolation` + `PRECIP_INTERP_ROOT`), `hecras_infiltration.py`
  (`write_percent_impervious`), `hecras_deck2d.py` (`apply_infiltration` gate + wiring),
  `test_hecras_rog.py` (12 tests, +2), `scripts/sandbox/hecras/rog_muncie_live.py` (live driver).
- Offline: freshtopo tree 39 passed (RoG 12, deck2d 10) with `env -u TRID3NT_CACHE_BUCKET`;
  server-suite 9-failure baseline untouched (no server code changed).
- Board rows `hecras_2d_rain_on_grid` + `rog_cross_engine_telemac_vs_hecras`: link-3 interp
  folder DECODED + engine-read (past the segfault); the solve residual moves down to the
  hydrology module. HR2D comparison row still pending a real solve (honest, not fabricated).
- Decompiler note: the `local/ilspy:9` image + the extracted decompiled trees live only in the
  session scratchpad (proprietary DLLs are gitignored; nothing vendored). Reproduce via the
  ADR 0199 DLL-extraction recipe + `ilspycmd -p`.
