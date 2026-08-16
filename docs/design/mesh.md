# mesh/ -- the shared mesh layer

`trid3nt_server/mesh/` (was `agent/mesh/`, ADR 0277) is the first-class mesh
layer: the geometry builders and the mesh-preview gate machinery shared across
solvers. The deep composer-mesh extraction (pulling each engine's private mesh
step into a shared front pipeline) is a SEPARATE future program -- this folder
holds only what already existed as shared mesh code.

## What lives here

- `grid_geometry.py`, `raster_cell_mesh.py`, `coastal_tin.py`,
  `hecras_geometry.py` -- geometry construction per mesh family.
- `refine_regions.py`, `spatial_roles.py` -- refinement regions + role tagging.
- `mesh_preview.py`, `preview_gate.py` -- the preview artifact + the
  approve-mesh gate seam.
- `modflow_package_validation.py` -- MODFLOW package checks (resolves the
  `mf6` binary via `parents[2] / "bin" / "mf6"`).
- `swmm_network.py`, `swmm_deck_runner.py`, `swmm_mechanism_compare.py`,
  `_swmm_solve_subprocess.py` -- SWMM node-link network + deck execution.

## Composition

`workflows/` composers call mesh builders to produce the modeled domain;
`gates/cards/solver_confirm.py` imports `mesh.raster_cell_mesh` for the
pre-solve cell-count estimate. The preview gate emits a mesh wireframe the
plugin renders for approval.

## Invariants / extension points

- Mesh = the modeled domain; engine proof renders overlay the mesh wireframe.
- The preview gate is a DECLARED user-decision seam, not hand-wired.
