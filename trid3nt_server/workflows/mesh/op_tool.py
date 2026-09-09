"""``mesh_op`` - the runtime face of the one word a recipe is written in.

The registered tool appends an entry to the recipe of the mesh open at the gate,
regenerates the mesh wholesale and re-presents it; alter and remove target an
entry by the INDEX the gate numbers them with."""

from __future__ import annotations

import asyncio
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.mesh.meshers import (
    MeshOp,
    MeshToolError,
    get_mesher,
    op_names,
)

__all__ = ["mesh_op"]

_METADATA = AtomicToolMetadata(
    name="mesh_op",
    ttl_class="live-no-cache",
    cacheable=False,
    tier="general",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def mesh_op(fn: str | None = None, at: int | None = None,
                  remove: bool = False, mesh: str | None = None,
                  **kwargs: Any) -> dict[str, Any]:
    """REFINE THE MESH open at the gate by editing its RECIPE -> the rebuilt mesh.

    THE tool for "make it finer along the channel", "size it by depth", "mark the
    seaward edge as the open boundary", "paint the bed from this raster", "undo
    that sizing step". The mesh is rebuilt WHOLESALE from the new recipe.

    ``fn`` is the function's OWN name, verbatim: for om2d, oceanmesh's
    feature_sizing_function, distance_sizing_from_line_function,
    wavelength_sizing_function, enforce_mesh_gradation, delete_boundary_faces,
    laplacian2, fix_mesh, identify_ocean_boundary_sections; plus set_bed,
    set_boundary_roles. Order matters; duplicates are legal.

    Params:
        fn: the function to call. Omit only with remove=True.
        at: the entry to ALTER (with fn) or REMOVE (with remove=True); the gate
            numbers them. Omit to APPEND.
        remove: drop the entry at ``at``.
        mesh: the mesh id, if two are open.
        kwargs: the function's own arguments.
    """
    from trid3nt_server.workflows.mesh.gate import (
        active_mesh_session, open_mesh_gates, present_mesh,
    )

    session = active_mesh_session(mesh)
    if remove:
        if at is None:
            raise MeshToolError(
                "MESH_OP_INDEX",
                "removing an entry needs the index to remove; the gate numbers "
                f"this recipe's ops: {session.recipe.numbered()}.")
        await asyncio.to_thread(session.remove_op, int(at))
    else:
        entry = _entry(session, fn, kwargs)
        if at is None:
            await asyncio.to_thread(session.append_op, entry)
        else:
            await asyncio.to_thread(session.alter_op, int(at), entry)
    gate = next(g for g in open_mesh_gates() if g.mesh_id == session.mesh_id)
    return await present_mesh(gate)


def _entry(session: Any, fn: str | None, kwargs: dict[str, Any]) -> MeshOp:
    """One entry, checked against what this mesher's namespaces actually hold."""
    if not fn:
        raise MeshToolError(
            "MESH_OP_UNKNOWN",
            "mesh_op needs the name of the function to call; the names this "
            f"{session.mesher.name!r} mesh answers to are "
            f"{list(op_names(get_mesher(session.mesher.name)))}.")
    return MeshOp(fn=str(fn), kwargs=dict(kwargs))
