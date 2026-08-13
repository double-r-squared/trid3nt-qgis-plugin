"""Mesh authoring layer -- reusable mesh components engine composers insert.

NATE doctrine (data + MESH + compute = the big 3 model ingredients): mesh is the
last pillar, extracted here from the private steps scattered inside engine
composers. The landscape stays heterogeneous by construction (regular grids,
raster-cell graphs, TINs); this layer chases REUSABILITY, not a universal mesh.

Components (M1 EXTRACT):
  * :mod:`grid_geometry` -- ``regular_grid`` DOMAIN math (bbox + resolution ->
    origin / spans / cell size / row-col counts).
  * :mod:`raster_cell_mesh` -- the SWMM DEM-cell -> quasi-2D node/link builder
    (relocated; the SWMM engine core + WQ authoring + in-process runner).
  * :mod:`mesh_preview` -- the cross-cutting preview: render ANY paradigm's mesh
    (SWMM cells, SFINCS quadtree, TELEMAC triangle wireframe, regular-grid
    outline) as a publishable ``mesh_grid`` vector layer.

Worker-side folds live in their own trees behind the GPL/build-isolation
boundary (the worker Dockerfiles COPY only ``services/workers/*``): MODFLOW's DIS
constructor (``services/workers/modflow/dis_grid.py``), SWAN's ``_grid_geometry``
(``services/workers/swan/deck_builder.py``), and TELEMAC's gmsh channel mesher.
They keep their paradigm-native derivation; this layer owns the SERVER surface.
"""

from __future__ import annotations

from trid3nt_server.agent.mesh.coastal_tin import (
    CoastalTinError,
    CoastalTinSpec,
    compose_coastal_tin_manifest,
    run_coastal_tin_worker,
)
from trid3nt_server.agent.mesh.grid_geometry import (
    RegularGrid,
    regular_grid_from_bbox,
)
from trid3nt_server.agent.mesh.hecras_geometry import (
    HECRAS_2D_AREAS_GROUP,
    read_2d_flow_area_cells,
)
from trid3nt_server.agent.mesh.mesh_preview import (
    make_grid_outline_layer_uri,
    make_sfincs_mesh_layer_uri,
    make_swmm_mesh_layer_uri,
    mesh_cells_to_feature_collection,
    regular_grid_outline_feature_collection,
    swmm_mesh_to_geojson,
)
from trid3nt_server.agent.mesh.preview_gate import (
    MeshGateStats,
    build_mesh_gate_envelope,
    default_gate_mode,
    mesh_gate_should_fire,
)
from trid3nt_server.agent.mesh.refine_regions import (
    MeshSizingSpec,
    mesh_sizing_from_refine_regions,
    refine_level_for,
)
from trid3nt_server.agent.mesh.spatial_roles import (
    DrawnRoles,
    SpatialRoleError,
    parse_drawn_roles,
)

__all__ = [
    "CoastalTinSpec",
    "CoastalTinError",
    "compose_coastal_tin_manifest",
    "run_coastal_tin_worker",
    "RegularGrid",
    "regular_grid_from_bbox",
    "read_2d_flow_area_cells",
    "HECRAS_2D_AREAS_GROUP",
    "mesh_cells_to_feature_collection",
    "swmm_mesh_to_geojson",
    "make_swmm_mesh_layer_uri",
    "make_sfincs_mesh_layer_uri",
    "regular_grid_outline_feature_collection",
    "make_grid_outline_layer_uri",
    # M2 GENERALIZE (ADR 0099)
    "DrawnRoles",
    "SpatialRoleError",
    "parse_drawn_roles",
    "MeshSizingSpec",
    "mesh_sizing_from_refine_regions",
    "refine_level_for",
    "MeshGateStats",
    "build_mesh_gate_envelope",
    "default_gate_mode",
    "mesh_gate_should_fire",
]
