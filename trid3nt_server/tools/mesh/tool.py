"""``build_mesh`` - the one mesh router.

A mesh ask is a RECIPE: mesher, kind, domain, the one size word, the ordered ops
list. Declared in a template it builds NOTHING at import, called standalone it
builds now, and both take the same validation: an unknown op refuses by name."""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal, Mapping

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.inputs.extent import as_bbox
from trid3nt_server.inputs.slots import EXTENT
from trid3nt_server.workflows.runtime.data import tool
from trid3nt_server.tools.mesh.meshers import (
    MeshOp,
    MeshToolError,
    get_mesher,
    mesh_op,
    op_names,
    registered_meshers,
)
from trid3nt_server.tools.mesh.recipe import (
    MeshRecipe,
    build_recipe,
    jsonable,
    recipe_from_plan_value,
    recipe_plan_value,
)
# Importing a mesher REGISTERS it; the roster is this block and nothing else.
from trid3nt_server.tools.mesh.meshers import flopy_grid as _flopy_grid  # noqa: F401,E402
from trid3nt_server.tools.mesh.meshers import om2d as _om2d  # noqa: F401,E402
from trid3nt_server.tools.mesh.meshers import reg_grid as _reg_grid  # noqa: F401,E402

#: The registered mesher roster as the type ``build_mesh`` reads its mesher by,
#: so the input's accept rule and its dropdown are the registry itself.
MesherName = Literal[registered_meshers()]

__all__ = [
    "MeshOp",
    "MeshRecipe",
    "MeshTool",
    "MeshToolError",
    "build_mesh",
    "build_recipe",
    "jsonable",
    "mesh_op",
    "recipe_from_plan_value",
    "recipe_plan_value",
    "tool",
]


class MeshTool:
    """The mesh ask's validation face, reached as ``tool.build_mesh(...)``."""

    @staticmethod
    def build_mesh(*, mesher: str, kind: Any = None, extent: Any = None,
                   resolution_m: Any = None,
                   build: Mapping[str, Any] | None = None,
                   ops: Any = None) -> MeshRecipe:
        """Declare a mesh ask -> a frozen :class:`MeshRecipe`. Builds nothing."""
        return build_recipe(mesher=mesher, kind=kind, extent=extent,
                            resolution_m=resolution_m, build=build, ops=ops)


_METADATA = AtomicToolMetadata(
    name="build_mesh",
    ttl_class="live-no-cache",
    tier="general",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def build_mesh(
    mesher: MesherName = "reg_grid",
    kind: str | None = None,
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    extent: Annotated[Any, EXTENT] = None,
    resolution_m: float | None = None,
    build: dict | None = None,
    ops: list[dict] | None = None,
    input_mode: str | None = None,
) -> Any:
    """BUILD A COMPUTATIONAL MESH for a domain -> a mesh layer + a solver-ready artifact.

    THE tool for "mesh this watershed / coastline / river reach", "build the grid
    or domain a solver runs on", "author the model domain".

    Params:
        mesher: om2d (OceanMesh2D triangles over a polygon's interior),
            reg_grid (a uniform lattice over a box), triangle (Triangle's own
            constrained triangulation, planar), voronoi (the Voronoi dual of
            that same build), or gridgen (a quadtree over a rectilinear base,
            DISV-shaped).
        kind: unstructured_tri | structured_grid | voronoi | quadtree.
        location: place naming the domain. Supply this OR bbox.
        bbox: AOI (min_lon, min_lat, max_lon, max_lat) in EPSG:4326.
        extent: the POLYGON to mesh the interior of - what om2d takes, and what
            its edge is read as the shoreline of. For om2d a box is not a
            domain: fetch the coastline over it and the water it leaves is what
            is meshed. The FloPy meshers (triangle, voronoi, gridgen) also take
            a rectangular box as the extent itself: a rectangular aquifer is a
            rectangle.
        resolution_m: the finest cell or triangle edge, in metres. Omit for
            the finest cell of the input rasters the recipe names (the bed's).
        build: the mesher backend's own constructor kwargs, stated verbatim
            under its parameter names - maximum_area / angle / nodes /
            additional_args for triangle and voronoi, delr / delc / xoff /
            yoff / angrot for gridgen (om2d and reg_grid take none).
        ops: the ordered program, [{"fn": name, ...kwargs}], calling the mesh
            library's own functions plus set_bed and set_boundary_roles. Omit for
            the mesher's defaults; refine a built one with mesh_op.
        input_mode: user_gated presents the mesh at the gate to edit and accept;
            auto accepts inline.
    """
    import asyncio

    from trid3nt_server.render.pipeline_emitter import current_turn_case
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.mesh.gate import gate_mesh_build
    from trid3nt_server.tools.mesh.session import MeshSession

    if not (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip():
        raise MeshToolError(
            "MESH_STAGING_UNAVAILABLE",
            "TRID3NT_CACHE_BUCKET must be set to stage a built mesh into the case.")

    if extent is None:
        extent = as_bbox(bbox) if bbox is not None else None
        if extent is None and location:
            geo = await asyncio.to_thread(
                TOOL_REGISTRY["geocode_location"].fn, query=location)
            extent = as_bbox(getattr(geo, "bbox", None) or geo["bbox"])
        if isinstance(extent, (tuple, list)):
            extent = tuple(float(v) for v in extent)

    recipe = MeshTool.build_mesh(
        mesher=mesher, kind=kind, extent=extent, resolution_m=resolution_m,
        build=build,
        ops=None if ops is None else [_wire_op(entry) for entry in ops])
    name = location or f"{mesher} mesh"
    session = MeshSession(recipe, case_id=current_turn_case(), name=name)
    # user_gated stops at the gate - presented, editable, accepted by the reply;
    # auto and a headless call accept inline. The gate owns both.
    await gate_mesh_build(session, tool_name="build_mesh", input_mode=input_mode)
    return session.snapshot()


def _wire_op(entry: Any) -> MeshOp:
    """One ops entry off the wire, in the one shape a recipe entry has."""
    if isinstance(entry, MeshOp):
        return entry
    if not isinstance(entry, dict) or not entry.get("fn"):
        raise MeshToolError(
            "MESH_OPS_MALFORMED",
            f"an ops entry is {{'fn': <name>, ...kwargs}}; got {entry!r}. The "
            f"names each mesher answers to: "
            f"{ {m: list(op_names(get_mesher(m))) for m in registered_meshers()} }.")
    return MeshOp(fn=str(entry["fn"]),
                  kwargs={k: v for k, v in entry.items() if k != "fn"})
