# ADR 0210 - HEC-RAS 2025 rain-on-grid: paper-style channel-refined mesh

Date: 2026-08-09
Status: Accepted (the ADR 0209 rain-on-grid path now authors a GRADED mesh -- a coarse
background grading down to the channel scale along the delineated channel network, via
the RAS Mapper meshing API -- as a user-selectable `channel_refinement` knob. Solved
end-to-end on Coweeta Creek through the 2025 managed engine; the refinement sharpens
channel routing exactly as Godara et al. predict, at ~2x wall cost. Default OFF: the
uniform mesh gives the same peak-Q/extent/mass-balance at half the cost.)
Continues: ADR 0209 (RoG productionized on a UNIFORM 60 m structured grid).

## Context

NATE's catch: our 2025-engine rain-on-grid (ADR 0209) meshes a UNIFORM structured grid
(one cell size everywhere), but the Godara et al. cross-engine study we replicate ran a
graded mesh -- ~100 m background refined by breaklines + nested regions down to 3-5 m
along the river. This ADR achieves the paper's dynamic resolution on the RoG path.

## Decision 1 - the mesher: MeshFactory.TryCreateMesh, seeds ARE the sizing

Decompiled `Geospatial.Vectors.MeshFactory` (the headless RAS Mapper mesher the
AuthorMesh seam already drives):

    bool TryCreateMesh(Polygon perimeter, IList<Point> cellCenters,
                       IList<Polyline> breaklines, out Mesh mesh, out MeshError error,
                       MeshGenerationParams meshParams = null, ProgressReporter = null)

There is NO explicit "refinement region" argument. The realized cell size IS the local
cell-center SPACING (the factory Delaunay-triangulates the seeds into a Voronoi-like cell
mesh; `CreateMeshInternal` -> `GetValidateMeshTIN` on the seeds); `breaklines` only
MAGNETIZE facepoints onto the channel (`ApplyBreaklines` /
`MagnetizeFacepointsEnsureNonColinear`, face alignment, not sizing). `MeshGenerationParams`
carries only `CreateVirtualCells` + `SplitExternalFaces`. So paper-style refinement =
a GRADED seed point cloud (coarse background, fine channel) + channel breaklines. The
uniform path uses `MeshFactory.FromExtent` (a regular lattice); the refined path overrides
`BasicRectangleParams.CreateMesh()` to call `TryCreateMesh` on host-authored seeds.

## Decision 2 - seeds: variable-radius Poisson-disk + degree repair (the HEC 8-side wall)

HEC hard-rejects ANY cell with >8 sides (`ValidateNumFacesPerCell` -> `TryCreateMesh`
fails, no mesh). Nested lattices spawned 6-33 over-degree cells at the size transition (a
coarse cell bordering many fine cells); jitter, Laplacian and centroidal-Voronoi (Lloyd)
relaxation all left MORE. The mesh acceptance is genuinely fragile (a marginal cell at the
8/9 boundary), so robustness is layered, host + driver:

1. A channel DISTANCE field (rasterized, `scipy` EDT); target size `s(d) =
   clamp(channel_m + grade*d, channel_m, background_m)`.
2. VARIABLE-RADIUS Poisson-disk (Bridson) seeding -- blue-noise cells are hexagon-like
   (~6 neighbours) with NO lattice degeneracy; local disk radius == target cell size.
3. CROWDING RELIEF (`_degree_repair`): each pass removes, per over-degree vertex (Voronoi/
   Delaunay degree > 8, down to 0.4*background of a wall), its NEAREST neighbour -- merging
   the two tightest cells. This does not itself bound the raw degree (blue-noise keeps a
   ~14 max), but relieving the crowding lets HEC's own face-collapse (`SplitExternalFaces
   =false`, so it only reduces) carry the mesh to <= 8. Deterministic.
4. SEED STABILITY: the terrain raster step is kept at the prepare default when it is
   already finer than the channel cell -- overriding it shifts the reprojection origin
   sub-pixel, which moved the marginal cell over the 8/9 line (default-terrain 19462-cell
   seeds pass; a 5.5 m override left 1 residual). Only go finer when the channel demands.
5. DRIVER BACKSTOP (`Driver.cs`): if a seed set still yields a residual >8 cell,
   `RealTerrainRoG.CreateMesh` retries `TryCreateMesh` dropping a growing random seed
   fraction (0.4%/attempt) until it meshes -- a topology change the resolution field does
   not feel. (`MeshError.BadCells` count is logged per attempt.)

Coweeta: 22,934 blue-noise seeds -> crowding relief -> 19,462 cells that pass TryCreateMesh
at attempt 0 (`status=Complete`, `badcells=-1`) AND `ras prepare` ("Property Tables
computed / Precipitation layer prepared"), through BOTH the manual stage and the integrated
`run_rog2025` pipeline. Realized cell-size histogram (engine cell centers, NN spacing):
~7,000 fine channel cells (p5 = 25.7 m) + ~10,000 coarse background (80-120 m, p50 = 77 m)
-- the paper's coarse-background / fine-channel split, in-engine verified.

CHANNEL SELECTION: the fetched flowlines are the full OSM/NHD network (Coweeta ~109 km /
28.7 km2 -> ~3.8 km/km2 drainage density; refining every headwater trickle graded-fills
the whole catchment). We refine the MAIN network draining the delineated catchment,
clipped to it (37 km) -- how a modeler refines the conveyance channel, what the paper's
single-channel refinement targets. Honest sizing: 22 m channel (3x finer than the uniform
60 m; the paper's 3-5 m on a 3x-smaller catchment would be ~500k cells here -- 22 m is the
computationally-sane floor). Breaklines: the main stems (>=300 m), magnetizing faces onto
the channel.

## Decision 3 - Coweeta uniform vs refined (25 mm/hr x 6 h, DWE, rain-only)

Both solved through the 2025 CPU engine, metrics restricted to the 28.87 km2 catchment
(subgrid `DEBUG/CellVolume` mass balance; the core metrics are mesh-structure-agnostic --
the wet-/domain-area reporting switches to true per-cell Voronoi areas for the graded mesh):

| metric | uniform 60 m | channel-refined (~22 m) |
|---|---|---|
| peak outlet Q | 195.3 m3/s @ 5.67 h | 200.4 m3/s @ 4.92 h |
| max depth (catchment) | 8.98 m | 9.12 m |
| max velocity | 5.71 m/s | 6.81 m/s |
| runoff volume | 3352e3 m3 | 3448e3 m3 |
| runoff coeff | 0.774 | 0.796 |
| mass closure (rain=runoff+storage) | 99.6% | 99.6% |
| total cells / catchment cells | 40950 / 8018 | 19462 / 10046 |
| wall time (CPU) | 218 s (dt 3.0 s) | 415 s (dt 1.5 s) |

Honest reading: the refinement SHARPENS CHANNEL ROUTING -- the paper's rationale,
confirmed. The peak arrives 0.75 h EARLIER (finer channel cells route water faster, a
steeper rising limb, less numerical diffusion of the conveyance) and max channel velocity
is +19% (the fast channel flow the coarse 60 m cells smear). Peak-Q MAGNITUDE (+2.6%),
wet extent, and mass closure are essentially UNCHANGED -- the equilibrium peak is set by
rainfall rate x area regardless of mesh. The graded mesh uses cells efficiently: 52% FEWER
total cells (coarse hillslopes) yet 25% MORE cells IN the channel. Cost: ~1.9x wall time,
because the fine cells force dt 3.0 -> 1.5 s (2x steps). The HR/TELEMAC 4x peak-Q gap
(ADR 0209) is infiltration-dominated (HR rain-only) and is NOT moved by the mesh -- the
compare chart's cross-engine conclusion stands; the refined hydrograph is added as a
third line showing the sharper rising limb.

## Decision 4 - knob + default: `channel_refinement` OFF by default

`hecras_flood_2d(..., channel_refinement=<target channel cell size m>)`, RAIN-ON-GRID
only. None (default) = the uniform ADR 0209 mesh. A float authors the graded mesh (needs
the delineated catchment + channel network, acquired for the AOI: pour point = lowest DEM
cell -> `_delineate_catchment`, channel = `fetch_river_geometry`; acquisition failure
degrades to uniform with a loud note). Refined metrics use the mesh-agnostic path + a
NEAREST-cell-center depth COG (the structured cell->pixel mapping is invalid for the
graded mesh).

DEFAULT OFF, because refined is NOT strictly better -- it is a fidelity/cost trade: same
peak Q / extent / mass closure at 2x wall cost, buying sharper channel timing + velocity.
Cost-discipline + user-controlled-granularity doctrine -> off by default; the docstring
guides the user to turn it on when channel timing / velocity / hydrograph shape is the
question, not for a screening peak/extent. The showcase stays on the uniform mesh (no
re-seed).

## Consequences

- +0 registered tools (`channel_refinement` is a new knob on `hecras_flood_2d`);
  +0 worker images (mounted-driver, ADR 0209 -- the driver DLL is rebuilt + mounted, the
  stock authoring image is unchanged).
- Files: `scripts/sandbox/hecras/managed_solve/Driver.cs` (RealTerrainRoG.CreateMesh
  override + breakline reader + meshprobe mode), `.../freshtopo/rog_refine.py` (NEW:
  graded-seed + breakline authoring + degree repair), `rog2025_pipeline.py`
  (channel_refinement path + per-cell Voronoi areas + unstructured depth COG),
  `server/.../hecras/flood_2d/flood_2d.py` (knob + channel acquisition),
  `.../freshtopo/test_rog_refine.py` (NEW: 6 offline tests),
  `scripts/sandbox/hecras/proof_rog_refined.py` (mesh + depth + compare proofs).
- Proofs: `docs/proof/templates/hecras_flood_2d_rog_mesh.png` (the graded mesh wireframe --
  fine channel bands vs coarse hillslopes over ESRI), `hecras_flood_2d_rog_depth_refined.png`
  (max depth on the refined mesh; sibling since the default stays uniform),
  `hecras_flood_2d_rog_compare_chart.png` (uniform vs refined vs TELEMAC hydrograph).

## Reproducibility

Driver build + author/prepare/solve recipe in managed_solve/REPRODUCE.md (build synthdrv.dll
in the dotnet SDK image, mount into the stock authoring image). Coweeta refined solve:
`rog_refine.build_refined_inputs` -> `synthdrv realrog` (refine_dir) -> `ras prepare` ->
`ras solve --solver CPU`. Refined result at /home/nate/hecras_probe2025/rog_refine_coweeta;
seed cloud + histogram are re-derivable offline (no docker) via `rog_refine.py`.
