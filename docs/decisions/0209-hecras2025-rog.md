# ADR 0209 - HEC-RAS 2025 rain-on-grid, productionized on the managed engine

Date: 2026-08-09
Status: Accepted (rain-on-grid on the HEC-RAS 2025 managed engine is LIVE end-to-end
on Linux: a fetched-AOI DEM is authored into a 2025 project with a structured 2D
area + constant design-storm precipitation + a NormalDepth outlet, then prepared and
SOLVED on the CPU, and outlet discharge / max depth / velocity / runoff volume are
extracted from the result HDF. The Coweeta Creek cross-engine comparison against
TELEMAC-2D is two-sided for the first time. Registration unfreeze + showcase land on
this path.)
Continues: ADR 0207 (the 2025 managed engine solves 2D unsteady on Linux; rain
delivery proven on a synthetic basin) and ADR 0199/0205 (the frozen 6.6 RoG tail).

## Context

ADR 0207 unblocked the HR2D rain-on-grid SOLVE on Linux via the 2025 managed engine
(pure-managed CPU solver; the only native P/Invoke is the CUDA path) and proved
uniform rain on a synthetic flat basin. The frozen tail (ADR 0199 D2 / 0205 D3) was
the real-catchment path: author a 2025 project over a REAL terrain, deliver a design
storm, solve, extract RoG metrics, register, compare, seed. This ADR lands it.

## Decision 1 - units: in an SI project ConstantValue IS the rate in mm/hr (factor 1.0)

Decoded from `PrecipitationLayer.InitializeComputeDriver` (Ras.Core): the constant
precip is scaled by `num = Time.Seconds.ConvertTo(Hours,1) * (SI ? 0.001 : 1/12)`,
i.e. SI -> `(1/3600)*0.001` converts ConstantValue (mm/hr) to m/s; USCustomary ->
`(1/3600)*(1/12)` converts in/hr to ft/s. So a constant `ConstantValue = R` in an SI
project applies R mm/hr exactly. MASS-CHECKED live on a closed flat basin (SI): rate
25 -> 25.0002 mm/hr uniform rise over 1 h, rate 50 -> 50.0000 mm/hr, spatial spread
0.0000 mm (perfectly uniform, mass-conservative). The ADR 0207 REPRODUCE note's
"+0.10 ft/hr at rate 100" was mislabeled -- it is 0.10 m/hr (SI). Calibration factor
= 1.0 mm/hr per unit; no fudge factor.

## Decision 2 - infiltration is ABSENT in the 2025 managed engine (rain-only, stated)

Decompile evidence (Ras.Core + Ras.Engine): NO Infiltration / CurveNumber / GreenAmpt
/ SCS symbol anywhere in the managed engine; the BoundaryConditions layer set is
Precipitation / Evapotranspiration / Air* / Humidity -- no InfiltrationLayer. The 26
"Infiltrat*" hits are all in `Ras.Mapper.dll`, the decoupled 6.6 geometry/UI layer
that the managed CPU solver does not consume (and which ADR 0205 showed is gutted).
Verdict: the 2025 beta cannot apply an infiltration loss; RoG here is RAIN-ONLY (gross
rainfall, an upper-bound runoff). Stated honestly in the tool docstring + the compare
chart. The SCS-CN authoring for the 6.6 path stays decoded-but-frozen (ADR 0205 D3).

## Decision 3 - authoring: a real catchment through the synthetic framework

`RealTerrainRoG : BasicRectangleParams` (scripts/sandbox/hecras/managed_solve/Driver.cs)
authors a structured 2D area over the AOI extent with a NormalDepth outlet; the host
(rog2025_pipeline.py) reprojects the fetched DEM to a LOCAL SI grid (origin 0,0;
metres; elevation m) and OVERWRITES the exported synthetic Terrain.tif with it, so
`ras prepare` samples the real terrain. Four beta constraints were cracked, each
evidence-first:

1. TERRAIN FORMAT -- the terrain tif MUST be TILED (256) + carry NoData + OVERVIEW
   pyramids (the `TiffExportEngine.ExportWithOverviews` shape); a plain striped tif
   makes `ras prepare` report "Missing terrain data at Face". Fixed: rasterio writes
   tiled + `build_overviews([2,4,8,16])`.
2. TERRAIN RESOLUTION -- the terrain must be FINER than the mesh cell (face-profile
   sub-sampling); resolution = cell/6 (>= 5 m).
3. NODATA -- reprojection-rotation corners + holes are nearest-filled (a constant
   sentinel/cliff also trips face sampling).
4. OUTLET BC -- an external BC line must PROTRUDE past the mesh corners (endpoints
   outside the perimeter) or `TryIdentifyInternalExternal` classifies it INTERNAL
   ("only Flow is supported for internal boundary conditions"): the perimeter polygon
   `ContainsFuzzy` an inset line. Fixed: outlet polyline spans `Scale(-0.05, 1.05)`.
   NormalDepth (the physical RoG outlet) then works external; ConstantStage at the
   channel bed INJECTS water (holds a high tailwater) and is not used.

Equation set is `SolverControl.EquationSet.DWE` (Diffusion Wave, default) or `SWE`
(full momentum), set on the plan.

## Decision 4 - metrics: TRUE subgrid volume, not depth x area

The result HDF `DEBUG/CellVolume` (the subgrid volume-elevation integral) is the TRUE
per-cell storage; `depth * cell_area` overcounts ~6x on relief (a cell with a deep
sub-cell channel reports a large depth but a small volume). Storage V(t) =
sum(CellVolume) over the catchment cells; outlet Q = R_in - dV/dt (a single NormalDepth
outlet, so all outflow is the pour-point discharge); `DEBUG/FaceFlow` is an independent
cross-check. Metrics are restricted to cells INSIDE the delineated catchment polygon
(the same domain TELEMAC meshed) for a fair like-for-like. Muncie de-risk (8591 cells,
100 m, 2 h): mass balance closes (rain 4124e3 = runoff 1271e3 + storage 2852e3 m3).

## Decision 5 - Coweeta cross-engine comparison (finally two-sided)

Coweeta Creek NC (28.87 km2 delineated catchment, pour point -83.404 35.058), design
storm 25 mm/hr x 6 h, the ADR 0196 TELEMAC benchmark event:

| metric | HEC-RAS 2025 (DWE, rain-only) | TELEMAC-2D (AMC II, CN loss) |
|---|---|---|
| peak outlet Q | 195.3 m3/s @ 5.7 h | 45.5 m3/s |
| runoff coeff | 0.77 | ~0.28 (CN ~80) |
| runoff volume | 3352e3 m3 | 162e3 m3 (paper-event partial) |
| max depth | 8.98 m | 6.95 m |
| max velocity | 5.71 m/s | (steep-slope sensitive) |
| wall time | 218 s (CPU, 40950-cell domain / 8018 catchment) | 64 s (9521 tri) |
| mesh | structured 60 m subgrid | triangular TIN |

Honest reading: the ~4x peak-Q gap is DOMINATED by the infiltration difference (HR
rain-only coeff 0.77 vs TELEMAC AMC-II CN loss coeff ~0.28) -- HR delivers essentially
all rain to the outlet and reaches equilibrium (peak Q ~= rainfall rate x area = 200
m3/s), TELEMAC removes ~65-70% to infiltration. Max depths agree to order (8.98 vs
6.95 m). STABILITY: on this steep catchment (relief ~600 m over ~6 km) the structured
Diffusion Wave solve is stable and mass-conservative through the storm; the full-SWE
run is recorded in the compare script. Unlike the paper's finding (their HR2D was ~5x
slower with square-cell artifacts vs TELEMAC), our 2025 CPU engine is a different
generation. COST finding (direction-consistent with the paper): the Diffusion Wave
solve is fast (218 s wall) and mass-conservative; the full-SWE (momentum + turbulent
mixing) solve on the SAME steep catchment is DRAMATICALLY costlier (killed at >1.5 h
CPU, still not converged) -- the full-momentum scheme on the structured grid is
impractical here, so DWE is the scheme of record for steep rain-on-grid. Reported
as-is, not force-fit to the paper.

## Decision 6 - integration: mounted-driver, NO worker-image rebuild

The 2025 authoring image (`trid3nt-local/hecras2025-authoring:latest`, id afb76f3ccd00)
runs the `ras` CLI unchanged; the compiled `synthdrv.dll` is MOUNTED at runtime
(cp into /opt/hecras2025/app), exactly as ADR 0207's managed_solve runs. No worker code
is baked, so the WORKER-IMAGE LAW's rebuild requirement does not apply -- the mounted
driver is the correct pattern here. rog2025_pipeline.py drives author -> prepare -> solve
-> extract entirely through the stock image.

## Consequences

- +0 registered tools (existing `hecras_flood_2d`); the RoG knobs (design_storm_mm_per_hr,
  storm_duration_hr) are UNFROZEN on a `rain_on_grid` branch dispatching the 2025 path;
  curve_number/amc stay inert with an honest "no infiltration in the 2025 beta" note.
- +0 worker images (mounted driver). +1 host pipeline (rog2025_pipeline.py) + the
  extended C# driver + the proof renderer.
- Files: scripts/sandbox/hecras/managed_solve/Driver.cs (RealTerrainRoG + realrog mode),
  services/workers/hecras2025/subst/crux/freshtopo/rog2025_pipeline.py (the pipeline +
  metrics), scripts/sandbox/hecras/proof_rog2025.py, scripts/sandbox/hecras/rog_compare_engines.py
  (2025 path wired), server/.../hecras/flood_2d/flood_2d.py (RoG knobs), tests.
- Proofs: docs/proof/templates/hecras_flood_2d_rog_depth.png (max depth over ESRI +
  catchment boundary; water correctly concentrates in the dendritic stream network),
  hecras_flood_2d_rog_compare_chart.png (dock-exact hydrograph vs TELEMAC).
- Proprietary DLLs + probe project dirs live outside the repo (gitignored); reproduce
  via managed_solve/REPRODUCE.md.
- Tasks 280 closable (metrics + comparison + proofs + integration + registration).
  Task 281 (crack READ_UN_HYDROLOGY2D for the 6.6 path) is OBVIATED -- RoG goes through
  the 2025 engine, not the 6.6 Fortran hydrology scaffold; closable as won't-do.

## Reproducibility

Managed CLI `ras 0.1.0.2965-dev`, .NET 9 shared runtime. Driver build + author/prepare/
solve recipe in managed_solve/REPRODUCE.md. Coweeta DEM + delineated catchment at
/tmp/rog_coweeta (ADR 0193/0196 site). Units mass-check + Muncie de-risk + Coweeta
solve are re-runnable via rog2025_pipeline.py.
