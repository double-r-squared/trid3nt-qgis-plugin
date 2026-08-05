# ADR 0130 -- HEC-RAS 2025 Beta: THE PREPARE CRUX -- subgrid property tables run on Linux

Status: accepted (2026-08-04)
Follows: ADR 0129 (Linux native-SUBSTITUTION: createterrain WORKS, mesh RUNS,
prepare was INPUT-gated and UNTESTED), ADR 0127 (the headless spike), ADR 0100
(the 6.x M3 STOP -- the 6.x Linux path could not compute 2D subgrid property tables).

This ADR closes the ONE open leg ADR 0129 flagged as decisive: does `ras prepare`'s
subgrid PROPERTY-TABLE computation -- the exact capability the 6.x path lacked --
run under the substituted open-source natives, or does it hit HEC's 81 custom
`CSharp_OSGeof*` GDAL band-algebra / `ComputedBand` entrypoints that exist in no
public GDAL? Lands NO code (registry unchanged by construction); the experiment
lives in `services/workers/hecras2025/subst/crux/` + `scratchpad/hecras2025_crux_proofs/`.

## The question, made precise (decompilation)

`ras prepare` (Ras.Core `PrepareArgs.Execute`) gates on input: it needs a valid
`.ras` project (`Project.IsValidProjectFile`) or a plan (`Plan.IsValid`) or it prints
"Could not parse source" and exits BEFORE any native compute -- which is why ADR 0129
could not reach the crux (it had only a terrain/geometry h5, not a project/plan).

The property-table computation itself is a STATIC method reached via:

    Plan.TryPrepareCompute -> Geometry.PrepareForCompute
      -> FlowAreaLayer.ComputePropertyTables
        -> MeshPropertyTables.ComputeFrom(mesh, elevations, nValues, opts, out tables)

Two findings from decompiling Ras.Core.dll + Geospatial.Core.dll (ilspycmd 8.2):

- **`RecomputeMesh` (the `ras mesh` verb) does NOT compute property tables** -- it
  clears the flow area, completes the topology, and builds the conceptual mesh only.
  Property tables are a PREPARE-stage computation. (This corrects the ADR 0129 open
  note that guessed mesh might author them.)

- **`MeshPropertyTables.ComputeFrom` samples terrain through the managed
  `IResample<float>` abstraction** -- `elevations.SamplePoints(pts)` for faces and
  `elevations.Resample(rasterDef, buf)` + managed `Histogram.ComputeFrom` for cells --
  then builds elevation-volume / elevation-area / hydraulic-profile curves in PURE
  MANAGED C#. There is NO `ComputedBand` / `Band_Max/Min/MeanOfNBands` / `IfThenElse`
  / `BinaryOpBand` anywhere in the subgrid path. HEC's 81 custom band-algebra
  entrypoints are used by OTHER surfaces (raster map algebra in Ras.Mapper), never by
  property-table generation.

## The experiment (live, direct-call harness)

Because a full valid `.ras` project (Project+Plan+Geometry+Terrain+NValue+
BoundaryCondition, association-wired) is a large hand-authoring lift, and the crux is
a static method, we invoked `MeshPropertyTables.ComputeFrom` DIRECTLY from a tiny
.NET 9 harness compiled against the beta DLLs and run INSIDE `hecras2025:subst-exp`
(the substituted GDAL 3.11.5 + HDF5 image from ADR 0129). This exercises the identical
native surface prepare would -- terrain read via the substituted GDAL/HDF5 -- with no
server code and no on-disk project archaeology.

Seed (all tiny, EPSG:26915, 32x32 @ 10 m):
- `elev.h5`   -- HEC terrain from a synthetic DEM (elev 100-124.8 m), via `ras createterrain`.
- `nvalue.h5` -- HEC terrain from a constant Manning's n = 0.06 raster (the n-value surface).
- mesh -- `MeshFactory.FromExtent(terrainExtent, 8, 8)` (64 cells / 144 faces),
  INSET to 80% of the terrain extent so perimeter faces sample interior terrain.

### Iteration log (the proven error-driven method)

1. `Mesh.FromExtent` -> not on `Mesh`; it is `MeshFactory.FromExtent`. Fixed.
2. First run: `ComputeFrom` executed end-to-end, NO EntryPointNotFound, but
   `Result=False` with "Missing terrain data at Face N" x16 and null value tables.
   Root cause was NOT native: the terrain h5 stored its tile path from the original
   `/out/` build dir; loaded from `/work` the tile did not resolve -> SamplePoints
   returned NoData. Rebuilt the terrain in the working dir.
3. Still 16 failing faces -- all PERIMETER faces: the mesh extent equalled the terrain
   extent, so boundary-face sample points landed on the terrain edge (NoData). A
   `SamplePoints` probe confirmed the resampler returns REAL elevations interior
   (112.8, 111.2, 114.4) -- the substituted GDAL/HDF5 read path works. Inset the mesh
   to 80%.
4. **Result=True.** Full property tables computed.

## Verdict

`ras prepare`'s subgrid property-table computation **completes headless on Linux
TODAY** under the substituted open-source natives:

| run | mesh | result | cell vol/elev tables | face elev/area tables |
| --- | --- | --- | --- | --- |
| A | 8x8 = 64 cells / 144 faces | **Result=True** | len 768 each | len 2115 each |
| B | 16x16 = 256 cells / 544 faces | **Result=True** | len 1315 each | len 6086 each |

Real, monotonic curves (cell 0: elevation-volume from (0, 102.4) upward;
face 0: elevation-area from (103.3, 0) upward). **NO `EntryPointNotFound`. NO custom-
GDAL wall.** The terrain-sampling native surface is exactly the createterrain read
path already proven under stock GDAL 3.11.5 + the 2-line `OpenShared` alias (ADR 0129);
the property tables add only managed histogram / hydraulic-profile math on top.

This is verdict **(a) -- the full authoring pipeline (terrain -> mesh -> property
tables) works headless on Linux under substituted open-source natives.** The 81 HEC
custom band-algebra entrypoints are NOT on this path.

## Table schema (vs 6.x)

`MeshReader.WriteTables` persists to the standard `/Geometry/2D Flow Areas/<name>/`
group as the classic HEC ragged Info(start,count) + flat Values layout -- the SAME
pattern as 6.x (Cells Volume Elevation Info/Values, Faces Area Elevation Info/Values,
Face Mannings n). In-memory the harness read:
- Cell: `CellTableStart` / `CellTableCount` + `CellVolumeTable` / `CellElevationTable`.
- Face: `FaceTableStart` / `FaceTableCount` + `FaceElevationTable` / `FaceAreaTable`
  + `FaceWettedPerimeterTable` + `FaceManningsTable`.
So a future 6.x-solver transplant remains schema-plausible (characterize-only; no
transplant attempted or needed).

## Consequences

- The HEC-RAS 2025 headless-on-Linux preprocessing pipeline (terrain, mesh, AND
  subgrid property tables) is now END-TO-END demonstrated under open-source natives.
  The remaining gap to a landable Linux 2D-flood engine is HEC's Linux SOLVER natives
  (`RasNativeParallel`, closed win-x64) -- the watch item -- OR feeding transplanted
  2025 tables to the 6.x solver (schema-plausible, not built). The OWNED-preprocessor
  decision stays NOT forced.
- Reproducible recipe + fixtures + harness captured in
  `services/workers/hecras2025/subst/crux/` (Harness.cs/.csproj, terrain fixtures,
  REPRODUCE.txt, success transcript). Full decompilations + all transcripts in
  `scratchpad/hecras2025_crux_proofs/`.
- No server / worker / tool / contract change; registry unchanged (git: only docs +
  services/workers/hecras2025/subst/crux fixtures). Offline suite untouched.
- Image hygiene: no new images built; decompile/compile/run used throwaway `--rm`
  containers on the pre-existing `mcr.microsoft.com/dotnet/sdk:9.0` (1.2 GB) and
  `hecras2025:subst-exp` (0.54 GB layer). Durable in-repo footprint 68 KB.

## Open issues

- The harness reaches `ComputeFrom` directly; the `.ras`/plan on-disk PARSE path was
  NOT exercised end-to-end (it is a pure managed I/O gate with no native compute, so
  it cannot change the native verdict, but a full `ras prepare` on a hand-authored or
  migrated project would be a nice belt-and-suspenders confirmation).
- n-value surface used a constant 0.06 raster; a real landcover-derived NValueLayer
  would exercise the classification resampler (standard GDAL read; not band-algebra).
- The 2025 `ras` build is `-dev` / schema-unstable; re-characterize every version bump
  (ADR 0127 policy carries).
