# Managed 2025 HEC-RAS engine: end-to-end 2D solve on Linux (ADR 0207)

Proves the 2025 managed engine migrates + preprocesses + SOLVES a 2D unsteady
shallow-water plan on Linux with the CPU solver -- no Windows, no Wine, no native
preprocessing binaries. This is the shim that unblocks the frozen HR2D rain-on-grid
chain (the 6.6 Fortran path's Windows-preprocessing dependency is bypassed).

All artifacts (proprietary DLLs, probe project dirs, downloaded example zip) live
OUTSIDE the repo and are gitignored. Nothing proprietary is committed. This dir
holds only OUR C# driver source.

## Images
- `trid3nt-local/hecras2025-authoring:latest` -- carries the 2025 beta managed
  assemblies + the ADR 0129 substituted Linux GDAL/HDF5 natives + patched
  `ras.runtimeconfig.json` (framework-dependent, .NET 9). The `ras` CLI verbs
  createterrain/mesh/prepare/solve/map run here on Linux.
- `mcr.microsoft.com/dotnet/sdk:9.0` -- builds the driver.
- `local/ilspy:9` -- decompiler (ADR 0205 recipe) for reading the managed assemblies.

## Key facts decoded from the assemblies
- The ONLY native P/Invoke in `Ras.Engine.dll` is `Ras.Engine.CUDA.dll` (the GPU
  path). The CPU solver (`SolverImpSWE`/`SolverExpSWE`/`SolverDWE`/`ComputeDriver`)
  is pure managed C# -> runs on Linux.
- `ras prepare` = the 2025 preprocessing step. Ingests data, computes the subgrid
  property tables + boundary conditions + initial water surface, writes a single
  ready-to-run `<plan>.r2r.h5` (the Windows `.tmp.hdf`/`.b##` equivalent). Runs on Linux.
- `ras solve <r2r> <out> --solver CPU` = the compute. Runs on Linux.
- `migrate project -s <v6.prj> -d <dir>` (migrate.dll) converts a v6 project to a
  2025 project on Linux (needs a `migrate.runtimeconfig.json` copied from
  `ras.runtimeconfig.json`; the beta ships only a self-contained one).
- 2025 project layout: `<name>.ras` (XML: PlanAssociations + GeometryAssociations)
  + `Geometries/*.h5` + `Boundary Conditions/*.h5` + `Plans/*.h5` + `Terrains/*`
  + `Surface Layers/<NValue Layer>.h5` (the NValue dir IS "Surface Layers").
- BETA BUG worked around: `Project.SaveAs(dir,name)` passes the just-created
  `Terrains` directory to `Terrain.ExportFullCopy` (which expects a file path), so
  it throws "exists as a directory" after writing .ras/Geometries/BC/Plans/Terrains
  but BEFORE the NValue write. Driver catches it and writes the NValue layer file
  itself into `Surface Layers/`.

## The driver (Driver.cs)
Uses the built-in `Ras.Synthetics` framework to author a fully-associated 2025
project in managed code (terrain + nvalue + BC + associations), then the shipped
`ras prepare`/`ras solve` run it. Three cases have been proven:
- `InflowOnly`  : inflow-BC channel filling a basin (mass balance: added vol == inflow).
- `RainBox`     : NO inflow/outflow lines; a uniform CONSTANT precipitation BC
  (PrecipitationLayer, SpatialDataType=Constant, ConstantValue=rate). Rain is
  applied to every cell in-memory by `PrecipitationLayer.InitializeComputeDriver`
  -> depth rises uniformly and linearly; rise scales exactly linearly with rate.

## Build
    P=/home/nate/hecras_probe2025     # any host dir with 28G free (NOT tmpfs)
    mkdir -p $P/appdll
    CID=$(docker create trid3nt-local/hecras2025-authoring:latest)
    docker cp "$CID:/opt/hecras2025/app/." $P/appdll/ ; docker rm $CID
    cp Driver.cs Driver.csproj $P/driver/
    docker run --rm -v "$P/appdll:/dll" -v "$P/driver:/src" -w /src \
      mcr.microsoft.com/dotnet/sdk:9.0 \
      bash -lc 'dotnet build -c Release -o /src/out Driver.csproj'

## Author + prepare + solve (rain-on-grid demo)
    docker run --rm -v "$P:/probe" --entrypoint /bin/sh \
      trid3nt-local/hecras2025-authoring:latest -c '
      cd /opt/hecras2025/app
      cp /probe/driver/out/synthdrv.dll .
      cp ras.runtimeconfig.json synthdrv.runtimeconfig.json
      dotnet synthdrv.dll /probe/rain 100          # author the rain-only project
      RAS=$(ls /probe/rain/*.ras|head -1)
      mkdir -p /probe/rain_r2r
      dotnet ras.dll prepare -s "$RAS" -o /probe/rain_r2r -f   # -> Base Plan.r2r.h5
      dotnet ras.dll solve "/probe/rain_r2r/Base Plan.r2r.h5" \
        /probe/rain_result.h5 --solver CPU -f'

## Verify (result HDF)
`Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh/Cell Depth` (Nt, Ncell).
Rain-only run: depth uniform min==max, rises linearly; rate=100 -> +0.10 ft/hr,
rate=300 -> +0.30 ft/hr (ratio 3.0, mass-conservative). Inflow run: added volume
matches inflow discharge.

## Real-catchment rain-on-grid (ADR 0209)

`Driver.cs` gained a `realrog` mode: `dotnet synthdrv.dll realrog <spec.json>` authors a
`RealTerrainRoG` project (structured 2D area over the AOI extent + constant design-storm
precipitation + a NormalDepth outlet on the pour-point wall). The host then OVERWRITES the
exported synthetic `Terrains/Terrain.tif` with a reprojected real DEM (local SI metres,
TILED + NoData + OVERVIEWS -- else `ras prepare` reports "Missing terrain data at Face"),
and runs `ras prepare` + `ras solve --solver CPU`. The full host pipeline (reproject ->
author -> prepare -> solve -> metrics from `DEBUG/CellVolume` + mass-balance outlet Q,
catchment-restricted) is `services/workers/hecras2025/subst/crux/freshtopo/rog2025_pipeline.py`.
Units: in an SI project `ConstantValue` IS the rate in mm/hr (mass-checked). Infiltration:
ABSENT in the 2025 beta -> rain-only. The BC line must PROTRUDE past the mesh corners or it
is classed INTERNAL. Live-proven on Muncie (de-risk) + Coweeta Creek NC (25 mm/hr x 6 h,
peak 195 m3/s).

## Channel-refined (graded) mesh -- paper-style dynamic resolution (ADR 0210)
`spec.json` may carry `"refine_dir": "/probe/rog_<name>/refine"`. When set, `RealTerrainRoG`
overrides `CreateMesh()` to call `MeshFactory.TryCreateMesh(perimeter, seeds, breaklines,
...)` on a graded cell-center cloud + channel breaklines (authored host-side by
`freshtopo/rog_refine.py`: variable-radius Poisson-disk seeds sized from a channel distance
field + crowding relief; main-stem breaklines) instead of the uniform `FromExtent`. HEC
rejects any >8-sided cell, so `CreateMesh` retries with a growing random seed drop until it
meshes. Fast check: `dotnet synthdrv.dll meshprobe <spec.json>` builds the mesh + dumps
`cellcenters.f64` + `mesh_probe.json` (no solve) -> the realized cell-size histogram. Then
the same `realrog` -> prepare -> solve path runs on the graded mesh. Coweeta (22 m channel /
90 m background, 19462 cells): passes prepare, solves in ~7 min, sharpens channel routing
(peak 5.7->4.9 h, max vel 5.7->6.8 m/s) vs the uniform 60 m mesh. Knob:
`hecras_flood_2d(..., channel_refinement=<target channel cell size m>)`, default OFF.

## v6 migration path (alternative authoring, for a real catchment)
    dotnet migrate.dll project -s "<Muncie.prj>" -d /probe/muncie2025 --force
Migration carries geometry+terrain+2D but leaves the plan's Terrain / NValue / BC
associations UNSET in this beta (and drops the Muncie inflow BC). Completing those
associations (or authoring a fresh 2025 RoG project via the synthetic path with a
real terrain) is the remaining plumbing to a real-catchment RoG solve.
