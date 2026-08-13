# ADR 0211 - generate_mesh mode=hecras: the refined RAS mesh as a standalone, consumable artifact

Date: 2026-08-09
Status: Accepted (the ADR 0210 channel-refined RAS mesh machinery -- previously reachable
ONLY as the embedded `channel_refinement` knob inside a `hecras_flood_2d` rain-on-grid
run -- is now usable INDEPENDENTLY: `generate_mesh(mesh_mode="hecras")` builds a
channel-refined HEC-RAS cell mesh into the Case, a human inspects the wireframe in QGIS,
and a later `hecras_flood_2d` rain-on-grid run CONSUMES it via the ADR 0200/0208
precondition gate. Live end-to-end on Coweeta Creek: mesh built + validated, consumed +
solved, and the absent path unchanged.)
Extends: ADR 0210 (rog_refine graded seeds + breaklines + the meshprobe/realrog driver),
0200/0208 (generate_mesh, MeshArtifact, the precondition gate + SCHISM consume precedent),
0209 (rog2025_pipeline, the mounted-driver integration).

## Context

NATE's directive: the refined-mesh machinery must be a first-class user act, not only an
embedded knob. A modeler should be able to AUTHOR the RAS mesh, LOOK at it, and only then
SOLVE on it -- the same "mesh is an explicit precondition" inversion ADR 0200 landed for
the TELEMAC/SCHISM TIN, now for the HEC-RAS rain-on-grid cell mesh.

## Decision 1 - what IS a portable HEC-RAS mesh: the authoring inputs, not a mesh file

The 2025 managed engine has NO single mesh file. It realizes a Voronoi-like CELL mesh
INSIDE the project from `MeshFactory.TryCreateMesh(perimeter, cell-center seeds, channel
breaklines)` over a local-SI terrain (ADR 0210). So the portable artifact = the AUTHORING
INPUTS:

  * `seeds.f64` -- the graded Poisson-disk cell-center seed cloud (the sizing field);
  * `breaklines.json` -- the main-stem channel breaklines (face magnetization);
  * `local_dem.tif` -- the reprojected local-SI terrain (the frame the seeds live in);
  * `prep.json` -- the local frame (origin/size/UTM epsg/cell), so consumption
    reconstructs `Rog2025Prep` WITHOUT re-reprojecting;
  * `catchment.geojson` + `flowlines.fgb` -- the modeled domain (metrics mask) + the
    channel network it graded toward.

REPRODUCIBILITY (settled with EVIDENCE, the design fork the directive named). Consumption
re-realizes the mesh from the stored seeds; is `TryCreateMesh` deterministic on identical
seeds? Two independent in-container `meshprobe` realizations of ONE Coweeta seed cloud
(19,462 seeds) produced BYTE-IDENTICAL cell centers (sha256 `a95bab60...`), identical
counts (19,462 cells / 56,131 faces), both meshing at attempt 0 (`ok=True status=Complete
badcells=-1`, no seed-drop retry). So it IS deterministic: the consume path re-realizes
EXACTLY the cell mesh NATE inspected -- no realized geometry need be stored to solve. The
realized cell polygons ARE persisted, but only as the DISPLAY face (below), so the
wireframe approved is provably the wireframe that solves. (Had it been nondeterministic,
the fallback was to store the realized cell geometry and rebuild the project from it; the
evidence made that unnecessary.)

## Decision 2 - the display face: the realized cells as a vector wireframe

The HEC cells are the clipped Voronoi of the cell centers (up to 8 sides), which no MDAL
`.2dm` element type (E3T/E4Q/E6T/E8Q/E9Q) represents as a general N-gon. So the display
layer is a VECTOR polygon layer (`cells_lonlat.fgb`, `layer_type="vector"`): the Voronoi
cells reprojected local->UTM->EPSG:4326 with a per-cell `size_m` attribute. QGIS renders
it as the mesh wireframe (fine channel bands vs coarse hillslopes) exactly as the depth
proof does, from the meshprobe cell centers -- a fast build (no prepare/solve).

## Decision 3 - build: generate_mesh gains mode=hecras (question-class name kept)

`generate_mesh(mesh_mode="hecras")` (or an `engine="hecras"` hint) -> `mode="hecras_rog"`.
It reuses, never duplicates: `flood_2d.acquire_channel_inputs` (delineate the catchment at
the pour point + fetch the channel network), `rog2025_pipeline.prepare_local_terrain` (the
frame), `rog_refine.build_refined_inputs` (seeds + breaklines), and a NEW in-container
`meshprobe` runner (`hecras_mesh.run_meshprobe`) that realizes + VALIDATES the cell mesh
(<= 8 sides/cell) with NO prepare/solve. The `min_edge_length_m` / `max_edge_length_m`
granularity levers ARE the channel + hillslope target cell sizes (e.g. 22 / 90 m) -- no new
knobs. A pour-point-derived AOI is tightened to a catchment-scale window (the generic
0.14 deg buffer would mesh a needlessly huge domain).

## Decision 4 - consume: hecras_flood_2d RoG checks the case, gates, re-realizes

`model_hecras_flood_2d_rog` runs the ADR 0200 precondition gate with `engine="hecras"` at
start:

  * a compatible hecras mesh exists + accepted (labeled default USE; the mesh > AOI basis
    ranking) -> materialize the stored bundle + `run_rog2025_prebuilt`: reconstruct the
    frame, stage the stored seeds/breaklines, and drive the SAME `realrog` author ->
    prepare -> solve -- NO fetch_dem, NO delineation, NO re-seeding (logged "CONSUMED case
    mesh ... NO fresh delineation");
  * declined / absent -> unchanged ADR 0209 (uniform) / 0210 (`channel_refinement`)
    behavior;
  * incompatible (e.g. a TELEMAC-only watershed TIN offered to HEC-RAS) -> the gate's loud
    skip, run proceeds fresh.

Compat facts (`artifact.py`): HEC-RAS is a BUNDLE engine (`ENGINE_MESH_REQUIREMENTS
["hecras"] = {"bundle": True, "needs_validated": True}`) -- compatible iff the
`hecras_inputs` bundle carries seeds + breaklines + local_dem + prep_json AND
`cells_validated` (the meshprobe confirmed a valid cell mesh). A hecras mesh honestly
declines TELEMAC/SCHISM (no `.slf`/`.gr3`); a TELEMAC/SCHISM mesh declines HEC-RAS (no
bundle).

## Live proof (Coweeta Creek NC, through the daemon)

Case (a) = `01KZMDT3XNGKGVEVNJTWP9YZ8J`; consume run `01KZMDX1YMPHRVE55BX7XWSG5V`.

- (a) `generate_mesh(mesh_mode="hecras", bbox=[-83.47,35.02,-83.36,35.10],
  pour_point=(-83.404,35.057), min=22/max=90)`: mesh `01KZMDTJVCRT58K9ASTD1VWRH3`, 19,554
  cells / 56,470 faces, channel p5 25.8 m / hillslope p50 76.2 m, meshprobe attempt-0
  clean, a `layer_type="vector"` wireframe layer in case (a). RECONNECT-DURABLE: a fresh
  connection re-selecting case (a) lists 2 persisted layers -- the mesh wireframe
  (`cells_lonlat.fgb`) + the consume depth COG.
- (b) `hecras_flood_2d(design_storm_mm_per_hr=25, input_mode="user_gated")` in case (a):
  the mesh gate FIRED ("This case has a mesh: HEC-RAS RoG mesh (19554 cells) ... Use it as
  the hecras model domain?"), accepted (proceed) -> log "CONSUMED case mesh ... re-realized
  from the stored seeds, NO fresh delineation/seeding" (the stored `seeds.f64` was staged
  into the solve dir, no fetch_dem/delineation ran) -> solved `consumed=True peak_q=202
  m3/s max_depth=8.83 m coeff=0.789 wall=972 s` (matches the ADR 0210 refined reference
  200.4 m3/s / 0.796 -- the consume path reproduces the refined-mesh physics), depth COG
  published into case (a).
- (c) `hecras_flood_2d(design_storm_mm_per_hr=25, input_mode="auto")` in a fresh case
  (`01KZMEV37SW9DG6W0WVYA6GQ14`, no mesh): absent path, uniform mesh, `consumed=False
  peak_q=345 m3/s wall=71.8 s` -- green, 0209 behavior unchanged.

!run lines:
- `!run generate_mesh(mesh_mode='hecras', bbox=[-83.47, 35.02, -83.36, 35.1], pour_point=[-83.40402, 35.05746], min_edge_length_m=22.0, max_edge_length_m=90.0)`
- `!run hecras_flood_2d(bbox=[-83.47, 35.02, -83.36, 35.1], design_storm_mm_per_hr=25.0, storm_duration_hr=6.0, resolution_m=90, input_mode='user_gated')`

## Consequences

- +0 registered tools (`generate_mesh` gains the hecras mode; `hecras_flood_2d` gains the
  consume gate). +0 worker images (mounted-driver + the existing `meshprobe` mode).
- Files: `services/workers/hecras2025/subst/crux/freshtopo/hecras_mesh.py` (NEW: build +
  meshprobe + Voronoi display), `rog2025_pipeline.py` (`run_rog2025_prebuilt` consume path),
  `server/.../mesh/generate_mesh/hecras_build.py` (NEW: acquire + stage + artifact),
  `.../generate_mesh/generate_mesh.py` (mode=hecras branch + docstring),
  `.../mesh/artifact.py` (hecras bundle facts + `materialize_hecras_mesh_inputs`),
  `.../hecras/flood_2d/flood_2d.py` (the consume gate + `acquire_channel_inputs` pour-point
  param), tests (`server/tests/test_generate_mesh.py` +10, `freshtopo/test_hecras_mesh.py`
  +3), corpus (+4 hecras-mesh phrasings), the showcase (a hecras-mode entry).
- The MeshArtifact `hecras_inputs` bundle + `cells_validated` are additive; every prior
  MeshArtifact / gate path is unchanged (TELEMAC/SCHISM/SWAN behavior identical).

## Reproducibility

The determinism evidence (byte-identical meshprobe realizations) re-runs via
`synthdrv meshprobe <spec>` twice on one `seeds.f64` and `cmp` of the two `cellcenters.f64`
(sha256 `a95bab60dd0c5865cefdece17377392ac4c1843ce76c35b019c53b6cb93948b5`). Seed cloud +
breaklines are re-derivable offline via `rog_refine.build_refined_inputs`.
