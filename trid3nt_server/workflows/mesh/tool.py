"""``build_mesh`` - the one mesh router.

A mesh ask is a RECIPE: mesher, kind, domain, the one size word, the ordered ops
list. Declared in a template it builds NOTHING at import, called standalone it
builds now, and both take the same validation: an unknown op refuses by name."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.runtime.accepts import Accepts
from trid3nt_server.workflows.runtime.data import tool
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    read_mesh_artifact_sidecar,
)
from trid3nt_server.workflows.mesh.meshers import (
    EDGE_RESOLUTION_SPECS,
    MeshOp,
    MeshToolError,
    get_mesher,
    mesh_op,
    op_names,
    registered_meshers,
)
from trid3nt_server.workflows.mesh.recipe import (
    MeshRecipe,
    build_recipe,
    jsonable,
    recipe_from_plan_value,
    recipe_plan_value,
)
# Importing a mesher REGISTERS it; the roster is this block and nothing else.
from trid3nt_server.workflows.mesh.meshers import om2d as _om2d  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import reg_grid as _reg_grid  # noqa: F401,E402

__all__ = [
    "MeshOp",
    "MeshRecipe",
    "MeshResolution",
    "MeshTool",
    "MeshToolError",
    "accepts_for",
    "build_mesh",
    "build_recipe",
    "jsonable",
    "kind_accepted",
    "mesh_kind",
    "mesh_op",
    "recipe_from_plan_value",
    "recipe_plan_value",
    "resolve_mesh",
    "supplied_mesh_artifact",
    "tool",
]


class MeshTool:
    """The mesh ask's validation face, reached as ``tool.build_mesh(...)``."""

    @staticmethod
    def build_mesh(*, mesher: str, kind: Any = None, extent: Any = None,
                   resolution_m: Any = None,
                   ops: Any = None) -> MeshRecipe:
        """Declare a mesh ask -> a frozen :class:`MeshRecipe`. Builds nothing."""
        return build_recipe(mesher=mesher, kind=kind, extent=extent,
                            resolution_m=resolution_m, ops=ops)


@dataclass(frozen=True)
class MeshResolution:
    """Which mesh a run uses, and why: ``explicit`` | ``discovered`` | ``declared``."""

    source: str
    reason: str
    artifact: MeshArtifact | None = None
    recipe: MeshRecipe | None = None


def resolve_mesh(
    recipe: MeshRecipe | None = None, *,
    explicit: Any = None,
    accepts: Accepts | None = None,
    case_id: str | None = None,
    loaded_mesh_uris: list[str] | None = None,
    s3_client: Any = None,
) -> MeshResolution:
    """Pick the mesh a run uses: explicit first, then the case, then the recipe.

    An explicit mesh never falls through: unreadable or unaccepted refuses."""
    if explicit is not None:
        if isinstance(explicit, MeshArtifact):
            art: MeshArtifact | None = explicit
        elif s3_client is None:
            # Naming the caller's missing reader rather than blaming the mesh: the
            # two are different failures and only one is the user's to fix.
            raise MeshToolError(
                "MESH_EXPLICIT_UNREADABLE",
                f"the mesh supplied for this run ({explicit!r}) is a uri and no "
                "object-store reader was supplied to resolve it, so what it is "
                "cannot be checked against the engine.")
        else:
            art = read_mesh_artifact_sidecar(str(explicit), s3_client)
        if art is None:
            raise MeshToolError(
                "MESH_EXPLICIT_UNREADABLE",
                f"the mesh supplied for this run ({explicit!r}) carries no readable "
                "mesh artifact record, so what it is cannot be checked against the "
                "engine; supply a mesh this case built.")
        # Two questions, two owners: the ``mesh`` row is the TEMPLATE's statement
        # of which supplied meshes its pipeline was built and tested against, and
        # readiness is the ARTIFACT's own. Both are asked here, at the door,
        # rather than several steps later by a deck that assumed a shape the mesh
        # does not have.
        _refuse_unaccepted_kind(art, accepts)
        _refuse_unsolvable(art)
        return MeshResolution("explicit", "supplied on the run", artifact=art)

    for art in reversed(find_case_mesh_artifacts(
            case_id=case_id, loaded_mesh_uris=loaded_mesh_uris,
            s3_client=s3_client)):
        if art.unsolvable_reason() is not None:
            continue
        if not kind_accepted(art, accepts):
            continue
        return MeshResolution(
            "discovered", f"mesh {art.name!r} already authored in this case",
            artifact=art)

    if recipe is None:
        raise MeshToolError(
            "MESH_UNRESOLVED",
            "no mesh was supplied, none compatible was found in this case, and no "
            "mesh was declared, so there is nothing to build or adopt.")
    # The build path is untouched by ``accepts``: a run with nothing supplied and
    # nothing to adopt builds the kind it declared.
    return MeshResolution(
        "declared", f"the declared {recipe.mesher!r} build", recipe=recipe)


def accepts_for(tool_name: str) -> Accepts | None:
    """The supply contract the REGISTERED workflow ``tool_name`` declares.

    ``None`` for a name with no registered workflow - the absence that refuses."""
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get(str(tool_name))
    workflow = getattr(getattr(entry, "fn", None), "workflow", None)
    return getattr(workflow, "accepts", None)


def supplied_mesh_artifact(explicit: Any, *,
                           tool_name: str) -> MeshArtifact | None:
    """The mesh a run was HANDED, resolved and checked against the calling row.

    ``None`` for an unfilled slot; a refusal, never a fall-through, otherwise."""
    from trid3nt_server import storage

    if explicit is None or not str(explicit).strip():
        return None
    return resolve_mesh(explicit=explicit,
                        accepts=accepts_for(tool_name),
                        s3_client=storage.client()).artifact


def mesh_kind(art: MeshArtifact) -> Any:
    """The KIND of mesh this is, as the build that made it recorded its own ask.

    ``None`` for a record that states none, which is not a failed match."""
    provenance = getattr(art, "provenance", None) or {}
    return (provenance.get("recipe") or {}).get("kind")


def kind_accepted(art: MeshArtifact, accepts: Accepts | None) -> bool:
    """Is ``art``'s kind a member of the declared ``mesh`` row? -> nothing more.

    No row and an unstated kind both answer False: neither is a permission."""
    if accepts is None:
        return False
    return accepts.accepts("mesh", mesh_kind(art))


def _refuse_unaccepted_kind(art: MeshArtifact, accepts: Accepts | None) -> None:
    """Refuse a supplied mesh the template's ``mesh`` row does not name, by name."""
    kinds = accepts.kinds("mesh") if accepts is not None else None
    if kinds is None:
        raise MeshToolError(
            "MESH_SUPPLY_UNDECLARED",
            "this template declares no supplied-mesh compatibility, so the mesh "
            f"handed to it ({art.name!r}, built by {art.mode!r}) has nothing to be: "
            "the mesh row of an Accepts(...) declaration is what states which kinds "
            "of mesh a pipeline was built and tested against, and there is no "
            "tested supplied-mesh path without one.")
    if kind_accepted(art, accepts):
        return
    got = mesh_kind(art)
    is_a = repr(got) if got is not None else "of no recorded kind"
    raise MeshToolError(
        "MESH_KIND_MISMATCH",
        f"this template accepts {list(kinds)!r} meshes; the mesh "
        f"supplied for it ({art.name!r}, built by {art.mode!r}) is {is_a}. The "
        "mesh row a template declares is what its pipeline was built and tested "
        "against, so a mesh outside it is not a domain it can solve on - build or "
        f"supply one of {list(kinds)!r} instead.")


def _refuse_unsolvable(art: MeshArtifact) -> None:
    """Refuse a supplied mesh no solve could be staged on, in its own words."""
    reason = art.unsolvable_reason()
    if reason is not None:
        raise MeshToolError("MESH_NOT_SOLVABLE", reason)


_METADATA = AtomicToolMetadata(
    name="build_mesh",
    ttl_class="live-no-cache",
    cacheable=False,
    tier="general",
    resolution_specs=EDGE_RESOLUTION_SPECS,
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def build_mesh(
    mesher: str = "reg_grid",
    kind: str | None = None,
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    extent: Any = None,
    resolution_m: float | None = None,
    ops: list[dict] | None = None,
    input_mode: str | None = None,
) -> Any:
    """BUILD A COMPUTATIONAL MESH for a domain -> a mesh layer + a solver-ready artifact.

    THE tool for "mesh this watershed / coastline / river reach", "build the grid
    or domain a solver runs on", "author the model domain".

    Params:
        mesher: om2d (OceanMesh2D triangles, cut from the shoreline or from a
            polygon) or reg_grid (a uniform lattice).
        kind: unstructured_tri | structured_grid.
        location: place naming the domain. Supply this OR bbox.
        bbox: AOI (min_lon, min_lat, max_lon, max_lat) in EPSG:4326.
        extent: a POLYGON to mesh the interior of, not bbox.
        resolution_m: the finest cell or triangle edge, in metres.
        ops: the ordered program, [{"fn": name, ...kwargs}], calling the mesh
            library's own functions plus set_bed and set_boundary_roles. Omit for
            the mesher's defaults; refine a built one with mesh_op.
        input_mode: user_gated stops at the mesh gate to edit; auto accepts inline.
    """
    import asyncio

    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter, current_turn_case,
    )
    from trid3nt_server.gates.input_review import resolve_input_gate_mode
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.mesh.gate import open_mesh_gate, present_mesh
    from trid3nt_server.workflows.mesh.session import MeshSession

    if not (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip():
        raise MeshToolError(
            "MESH_STAGING_UNAVAILABLE",
            "TRID3NT_CACHE_BUCKET must be set to stage a built mesh into the case.")

    if extent is None:
        extent = coerce_bbox_value(bbox) if bbox is not None else None
        if extent is None and location:
            geo = await asyncio.to_thread(
                TOOL_REGISTRY["geocode_location"].fn, query=location)
            extent = coerce_bbox_value(getattr(geo, "bbox", None) or geo["bbox"])
        if isinstance(extent, (tuple, list)):
            extent = tuple(float(v) for v in extent)

    recipe = MeshTool.build_mesh(
        mesher=mesher, kind=kind, extent=extent, resolution_m=resolution_m,
        ops=None if ops is None else [_wire_op(entry) for entry in ops])
    name = location or f"{mesher} mesh"
    session = MeshSession(recipe, case_id=current_turn_case(), name=name)
    if (resolve_input_gate_mode(input_mode) == "user_gated"
            and current_emitter() is not None):
        # The mesh stops at the gate: presented, editable, and NOT yet the
        # case's mesh - accepting it is the user's next act, not this call's.
        return await present_mesh(open_mesh_gate(session))
    await asyncio.to_thread(session.accept)
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
