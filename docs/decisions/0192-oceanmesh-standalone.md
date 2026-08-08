# ADR 0192 - OceanMesh2D coastal meshing: standalone-first

Status: Accepted (standalone sandbox landed; pipeline placement deferred to NATE)
Date: 2026-08-08

## Context

We need OceanMesh2D-class coastal meshing (shoreline-following, bathymetry-aware,
gradation-limited unstructured triangulation) to feed the unstructured-grid
solvers (TELEMAC-2D, SCHISM, unstructured SWAN). Practitioners commonly build
OceanMesh2D meshes and hand them to TELEMAC, so TELEMAC-compatible geometry
output matters.

Three candidate Python dependencies were evaluated in the agent venv:
- `oceanmesh` (CHLNDDEV) - the authentic OceanMesh2D Python port. NOT on PyPI in
  this environment (pip index 404); a source build needs CGAL + pybind11
  (`libcgal-dev` absent). It IS already built and working in the isolated,
  GPL-segregated image `trid3nt-local/mesh:latest` (v1.0.0+0.g66bfdfe).
- `ocsmesh` (NOAA) - pip-installs cleanly into the venv (ocsmesh 2.2.0) but its
  triangulation backend `jigsaw`/`jigsawpy` is unavailable on the index here, so
  it cannot triangulate end-to-end today.
- `pyposeidon` - same jigsaw/gmsh backend gap.

## Decision

Build OceanMesh2D-class meshing as a STANDALONE sandbox capability first, using
the authentic CHLNDDEV `oceanmesh` inside `trid3nt-local/mesh:latest` as the
mesh engine, driven from `scripts/sandbox/oceanmesh/` in the agent venv. Emit the
meshes (2dm + SELAFIN .slf + SCHISM gr3 + ADCIRC fort.14) plus ESRI-imagery proof
renders for NATE to inspect in QGIS. Do NOT wire it into any workflow, template,
or engine pipeline. The placement decision (TELEMAC geometry supply vs SCHISM
hgrid vs unstructured SWAN vs a standalone registered mesh tool) is explicitly
NATE's, made after inspecting the meshes.

`oceanmesh` is the pick because it is the only candidate that produces meshes in
this environment and it IS the OceanMesh2D reference methodology (Roberts,
Pringle, Westerink 2019). `ocsmesh` remains the documented pip-installable
alternative should a jigsaw binary be added to the venv.

## Consequence

- New files only, all under `scripts/sandbox/oceanmesh/` + `docs/`. No product
  code, workflow, template, or engine tree was touched.
- Meshes are emitted to `docs/proof/templates/oceanmesh_meshes/` (loadable in
  QGIS) with renders at `docs/proof/templates/oceanmesh_standalone_<aoi>.png`.
- TELEMAC compatibility is proved by reading each `.slf` back with the telemac
  worker's own `data_manip` SERAFIN reader; MDAL readability is proved by loading
  `.2dm` and `.slf` as `QgsMeshLayer`.
- The engine lives behind a docker boundary (GPL isolation preserved); promotion
  to a registered standalone tool would keep that boundary or vendor an in-venv
  mesher (ocsmesh + jigsaw). See docs/research/oceanmesh-front-proposal.md.
