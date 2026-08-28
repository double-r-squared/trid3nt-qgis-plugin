"""``build_mesh`` - the one mesh router.

A mesh ask is a SPEC (which mesher, which kind, and that mesher's own fields)
plus an ordered chain of named edits. Declared in a template the ask is a frozen
value that builds NOTHING at import; called standalone it builds now and stashes
the artifact in the case. Both go through the same validation, which is the point
of a single router: every field is checked against the registering mesher's
declaration and anything else is refused by name.

Resolution order when a run needs a mesh: an explicit mesh argument wins, then a
compatible mesh already authored in the case, then the declared spec's default
build.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from types import MappingProxyType

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.lib.slots import deep_freeze
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    mesh_compatible_with_engine,
    read_mesh_artifact_sidecar,
)
from trid3nt_server.workflows.mesh.meshers import (
    EDGE_RESOLUTION_SPECS,
    MeshToolError,
    get_mesher,
    is_late_bound,
    nearest_names,
)
# Importing a mesher REGISTERS it; the roster is this block and nothing else.
from trid3nt_server.workflows.mesh.meshers import coastal_edge as _coastal_edge  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import corridor_tin as _corridor_tin  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import hecras as _hecras  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import om2d as _om2d  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import reg_grid as _reg_grid  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import telapy_mesh as _telapy_mesh  # noqa: F401,E402
from trid3nt_server.workflows.mesh.meshers import watershed as _watershed  # noqa: F401,E402

__all__ = [
    "DeclaredEdit",
    "bind_edit_inputs",
    "jsonable",
    "MeshDeclaration",
    "MeshResolution",
    "MeshSpec",
    "MeshTool",
    "MeshToolError",
    "build_mesh",
    "declaration_from_plan_value",
    "declaration_plan_value",
    "resolve_mesh",
    "supplied_mesh_artifact",
    "tool",
    "validate_edit",
    "validate_spec",
]


def jsonable(value: Any) -> Any:
    """A spec/edit value as JSON, or a refusal naming what cannot be recorded."""
    if is_late_bound(value):
        raise MeshToolError(
            "MESH_SPEC_UNBOUND",
            f"{value!r} is a late-bound read, not a value: the recipe records what "
            "a run actually built with, so bind the declaration first.")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        return jsonable(item())
    raise MeshToolError(
        "MESH_SPEC_UNSERIALIZABLE",
        f"a {type(value).__name__} cannot be recorded in a mesh recipe ({value!r}); "
        "declare the ask as numbers, strings, sequences or mappings.")


@dataclass(frozen=True)
class MeshSpec:
    """WHICH mesher, and the fields that mesher declared - validated, frozen."""

    mesher: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    @property
    def kind(self) -> Any:
        return self.fields.get("kind")

    def to_json(self) -> dict[str, Any]:
        return {"mesher": self.mesher,
                **{k: jsonable(v) for k, v in self.fields.items()}}

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> "MeshSpec":
        fields = {k: v for k, v in dict(doc).items() if k != "mesher"}
        return validate_spec(str(doc["mesher"]), fields)


@dataclass(frozen=True)
class DeclaredEdit:
    """One named edit and its inputs, in the position the chain puts it."""

    action: str
    inputs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))


@dataclass(frozen=True)
class MeshDeclaration:
    """A frozen mesh ask: the spec plus the DECLARED edits that prefix its recipe.

    A template holds one of these at module scope, so it is deep-frozen and builds
    nothing until a session opens over it.
    """

    spec: MeshSpec
    edits: tuple[DeclaredEdit, ...] = ()

    def edit(self, action: str, *values: Any, **inputs: Any) -> "MeshDeclaration":
        """Append a DECLARED edit -> a new declaration. Builds nothing.

        Positional values bind to the action's declared inputs in declaration
        order, so a one-input action reads as ``.edit("add_obstacle", D.walls)``.
        """
        bound = validate_edit(self.spec.mesher, action, bind_edit_inputs(
            self.spec.mesher, action, values, inputs))
        return MeshDeclaration(
            self.spec, self.edits + (DeclaredEdit(str(action), bound),))


def bind_edit_inputs(mesher: str, action: str, values: tuple[Any, ...],
                     inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Bind positional edit values onto the action's declared input names."""
    declared = list(get_mesher(mesher).action(action).inputs)
    if len(values) > len(declared):
        raise MeshToolError(
            "MESH_EDIT_TOO_MANY_INPUTS",
            f"edit {action!r} on mesher {mesher!r} declares {len(declared)} inputs "
            f"({declared}); {len(values)} positional values were passed.")
    bound = dict(zip(declared, values))
    for name in bound:
        if name in inputs:
            raise MeshToolError(
                "MESH_EDIT_DUPLICATE_INPUT",
                f"edit {action!r}: {name!r} was given both positionally and by name.")
    bound.update(inputs)
    return bound


def validate_spec(mesher: str, fields: Mapping[str, Any]) -> MeshSpec:
    """Check a spec against the registering mesher's declared fields -> a MeshSpec.

    Refuses loudly and by name: an unknown mesher, a field the mesher never
    declared, a missing required field, a wrong type, a value outside a declared
    vocabulary. Late-bound reads pass the type and vocabulary checks - their value
    is not decided until the interpreter binds them.
    """
    registered = get_mesher(mesher)
    where = f"mesher {registered.name!r}"
    unknown = [k for k in fields if k not in registered.fields]
    if unknown:
        raise MeshToolError(
            "MESH_SPEC_UNKNOWN_FIELD",
            f"{where} declares no field {unknown[0]!r} "
            f"({nearest_names(unknown[0], registered.fields)}). Unknown fields: "
            f"{sorted(unknown)}.")
    resolved: dict[str, Any] = {}
    for name, declared in registered.fields.items():
        value = declared.check(fields.get(name), where=where)
        if value is not None:
            resolved[name] = deep_freeze(value)
    return MeshSpec(mesher=registered.name, fields=resolved)


def validate_edit(mesher: str, action: str,
                  inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Check one edit against the mesher's action registry -> its resolved inputs."""
    registered = get_mesher(mesher)
    act = registered.action(action)
    where = f"edit {act.name!r} on mesher {registered.name!r}"
    unknown = [k for k in inputs if k not in act.inputs]
    if unknown:
        raise MeshToolError(
            "MESH_EDIT_UNKNOWN_INPUT",
            f"{where} declares no input {unknown[0]!r} "
            f"({nearest_names(unknown[0], act.inputs)}). Unknown inputs: "
            f"{sorted(unknown)}.")
    resolved: dict[str, Any] = {}
    for name, declared in act.inputs.items():
        value = declared.check(inputs.get(name), where=where)
        if value is not None:
            resolved[name] = deep_freeze(value)
    return MappingProxyType(resolved)


class MeshTool:
    """The declaration face: ``tool.build_mesh(...)`` in a template's MESH block."""

    @staticmethod
    def build_mesh(*, mesher: str, kind: Any = None,
                   **fields: Any) -> MeshDeclaration:
        """Declare a mesh ask -> a frozen :class:`MeshDeclaration`. Builds nothing."""
        if kind is not None:
            fields = {"kind": kind, **fields}
        return MeshDeclaration(validate_spec(mesher, fields))


tool = MeshTool()


def declaration_plan_value(declaration: MeshDeclaration) -> dict[str, Any]:
    """A declaration as the plain mapping a plan step carries in its kwargs.

    Mappings and sequences are what the interpreter walks to substitute late-bound
    reads, so the declaration travels as one: the mesher, every field the router
    checked, and the DECLARED edit chain in its order. What the step's runner
    receives is therefore the whole ask with its values bound, and it rebuilds that
    ask rather than restating parts of it - a knob or an edit the template declared
    cannot go missing between the declaration and the mesh.
    """
    if not isinstance(declaration, MeshDeclaration):
        raise MeshToolError(
            "MESH_DECLARATION_EXPECTED",
            f"a mesh step carries the template's MESH declaration "
            f"(tool.build_mesh(...)), got {type(declaration).__name__}.")
    return {"mesher": declaration.spec.mesher,
            "fields": _thaw(declaration.spec.fields),
            "edits": [{"action": edit.action, "inputs": _thaw(edit.inputs)}
                      for edit in declaration.edits]}


def _thaw(value: Any) -> Any:
    """A frozen declaration value as the plain containers a step's kwargs carry.

    A declaration is deep-frozen, and a read-only proxy is not the ``dict`` a
    mesher's own field check accepts, so the mapping a step hands back to the
    router is a plain one. Late-bound reads pass through untouched - binding them
    is the interpreter's job, not this one's.
    """
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_thaw(v) for v in value)
    return value


def declaration_from_plan_value(value: Mapping[str, Any],
                                **overrides: Any) -> MeshDeclaration:
    """Rebuild the declaration a step was handed, with named fields replaced.

    ``overrides`` are for the fields a step RESOLVES rather than the template -
    a domain the plan navigated, an input a producer fetched. Everything else,
    including the edit chain that prefixes the recipe, comes back exactly as it
    was declared.
    """
    fields = {**dict(value["fields"]), **overrides}
    declaration = tool.build_mesh(mesher=str(value["mesher"]), **fields)
    for edit in value.get("edits") or ():
        declaration = declaration.edit(str(edit["action"]), **dict(edit["inputs"]))
    return declaration


@dataclass(frozen=True)
class MeshResolution:
    """Which mesh a run uses, and why: ``explicit`` | ``discovered`` | ``declared``."""

    source: str
    reason: str
    artifact: MeshArtifact | None = None
    declaration: MeshDeclaration | None = None


def resolve_mesh(
    declaration: MeshDeclaration | None = None, *,
    explicit: Any = None,
    engine: str | None = None,
    case_id: str | None = None,
    loaded_mesh_uris: list[str] | None = None,
    s3_client: Any = None,
) -> MeshResolution:
    """Pick the mesh a run should use: explicit first, then the case, then the spec.

    An explicit mesh NEVER falls through: if it cannot be read, or the engine
    cannot solve on it, that is a refusal rather than a quiet substitution.
    """
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
        _refuse_incompatible(art, engine)
        return MeshResolution("explicit", "supplied on the run", artifact=art)

    for art in reversed(find_case_mesh_artifacts(
            case_id=case_id, loaded_mesh_uris=loaded_mesh_uris,
            s3_client=s3_client)):
        if engine is not None and not mesh_compatible_with_engine(art, engine)[0]:
            continue
        return MeshResolution(
            "discovered", f"mesh {art.name!r} already authored in this case",
            artifact=art)

    if declaration is None:
        raise MeshToolError(
            "MESH_UNRESOLVED",
            "no mesh was supplied, none compatible was found in this case, and no "
            "mesh was declared, so there is nothing to build or adopt.")
    return MeshResolution(
        "declared", f"the declared {declaration.spec.mesher!r} build",
        declaration=declaration)


def supplied_mesh_artifact(explicit: Any, *, engine: str) -> MeshArtifact | None:
    """The mesh a run was HANDED, resolved and checked against its engine.

    The reader lives here rather than in each consuming template: a mesh artifact
    is the mesh front's record, and a step that opened the object store for itself
    would be doing world-reads a step must never do. ``None`` for an unfilled slot;
    a refusal, never a fall-through, for one the engine cannot solve on.
    """
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    if explicit is None or not str(explicit).strip():
        return None
    return resolve_mesh(explicit=explicit, engine=engine,
                        s3_client=_get_s3_client()).artifact


def _refuse_incompatible(art: MeshArtifact, engine: str | None) -> None:
    if engine is None:
        return
    ok, reason = mesh_compatible_with_engine(art, engine)
    if not ok:
        raise MeshToolError("MESH_ENGINE_INCOMPATIBLE", reason)


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
    min_edge_length_m: float | None = None,
    max_edge_length_m: float | None = None,
    input_mode: str | None = None,
    **fields: Any,
) -> Any:
    """BUILD A COMPUTATIONAL MESH for a domain -> a mesh layer + a solver-ready mesh artifact.

    THE tool for "mesh this watershed / coastline / river reach", "build the grid
    or domain a solver runs on", "make me a mesh I can inspect before running a
    model", "author the model domain and keep it in the case". Mesh creation is an
    EXPLICIT act and lives HERE: a model template that finds this mesh in the case
    ASKS before consuming it, and never invents one behind your back.

    ``mesher`` names the mesh library that builds it and ``kind`` the shape of
    mesh it makes; every other argument is a field that mesher DECLARES, checked
    at the router - a field the chosen mesher does not declare is refused by name
    rather than ignored. The roster:

    * ``om2d`` - OceanMesh2D: the GSHHG shoreline cuts the water domain, sized by
      distance to shore and by wavelength over the fetched bed. Obstacles punch
      out of it with their outlines constrained in, regions refine, and a named
      side becomes the open boundary.
    * ``telapy_mesh`` - adopt an EXISTING TELEMAC geometry (a ``.slf`` someone
      else authored) through TELEMAC's own reader, then edit it.
    * ``watershed`` - the basin upstream of a ``pour_point`` IS the domain,
      triangulated and refined toward its channel network, with a sampled bed.
    * ``coastal_edge`` - the OSM coastline + NHD water polygon is the domain,
      refined by distance to shore and by wavelength over depth. Naming an
      ``open_boundary_side`` also emits the SCHISM geometry; without one the mesh
      is closed and SCHISM honestly declines it.
    * ``corridor_tin`` - a river reach: the water it actually occupies, cut at the
      two end transects that become inflow and outflow. Bed-less by construction.
    * ``hecras_rog`` - a coarse hillslope cell mesh grading down to the channel,
      realized and validated by the HEC-RAS engine's own mesh factory.
    * ``reg_grid`` - the uniform lattice a structured deck runs on.

    ``input_mode="user_gated"`` stops at the MESH GATE instead of finishing: the
    mesh lands on the map as an editable mesh layer, its probes come back, and
    one tool per edit action the chosen mesher registers is mounted for as long
    as the session stays open - refine it, hand-edit the layer in QGIS and feed
    it back, then ``mesh_accept`` (or ``mesh_restart`` to drop the edits). AUTO
    (the default) builds and accepts inline.

    Edge levers: ``min_edge_length_m`` / ``max_edge_length_m`` bound the cell or
    triangle size (for ``hecras_rog`` they ARE the channel and hillslope target
    cell sizes), ``grade`` limits how fast the two may transition. Both edges are
    declared >= 5 m; a finer ask is quoted the floor and the AOI-dependent
    <= 8-sides-per-cell acceptance rather than silently snapped. ``om2d`` takes
    the same band inside its ``refine`` block instead. US-only.

    Params:
        mesher: which mesh library builds it (om2d | telapy_mesh | watershed |
            coastal_edge | corridor_tin | hecras_rog | reg_grid).
        kind: the mesh shape that mesher makes (unstructured_tri | graded_cells |
            structured_grid).
        location: place naming the domain (geocoded). Supply this OR ``bbox``.
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        min_edge_length_m: finest cell/triangle edge (m); the CHANNEL cell for
            ``hecras_rog``. Declined by name by a mesher that sizes another way.
        max_edge_length_m: coarsest edge (m); the HILLSLOPE cell for ``hecras_rog``.
        input_mode: ``user_gated`` to review + edit the mesh at the gate before
            it is accepted; ``auto`` (default) builds and accepts inline.
        fields: the chosen mesher's own remaining declared fields (pour_point,
            grade, open_boundary_side, resolution_m, extent_km, width_m, banks,
            refine, bed, geometry, crs_authid).
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

    # The edge band is on the SIGNATURE because it is the granularity lever three
    # of the meshers share and the one whose floor the tool declares; a mesher that
    # does not size by an edge band refuses it by name at the router.
    for name, value in (("min_edge_length_m", min_edge_length_m),
                        ("max_edge_length_m", max_edge_length_m)):
        if value is not None:
            fields = {name: float(value), **fields}

    declared = get_mesher(mesher).fields
    if "extent" not in declared and "domain" not in declared:
        # A mesher that takes neither an extent nor a domain is handed an existing
        # geometry, so an extent would reach nothing. Refused BY NAME, the same as
        # any other field this mesher never declared - a dropped extent reads as a
        # lever that shaped a mesh it never touched.
        spatial = [name for name, value in
                   (("location", location), ("bbox", bbox)) if value is not None]
        if spatial:
            raise MeshToolError(
                "MESH_SPEC_UNKNOWN_FIELD",
                f"mesher {mesher!r} declares no field {spatial[0]!r}: it adopts the "
                f"geometry it is given rather than cutting one from an extent "
                f"({nearest_names(spatial[0], declared)}). Named extents: "
                f"{sorted(spatial)}.")
    if "extent" in declared and "extent" not in fields:
        extent = coerce_bbox_value(bbox) if bbox is not None else None
        if extent is None and location:
            geo = await asyncio.to_thread(
                TOOL_REGISTRY["geocode_location"].fn, query=location)
            extent = coerce_bbox_value(getattr(geo, "bbox", None) or geo["bbox"])
        if extent is not None:
            fields = {"extent": tuple(float(v) for v in extent), **fields}
    elif "domain" in declared and "domain" not in fields:
        # A mesher whose domain is a DOMAIN rather than a box gets one acquired for
        # it: an extent alone does not say which river a corridor follows.
        fields = {"domain": await _acquire_domain(location, bbox), **fields}

    declaration = tool.build_mesh(mesher=mesher, kind=kind, **fields)
    name = location or f"{mesher} mesh"
    session = MeshSession(declaration, case_id=current_turn_case(), name=name)
    if (resolve_input_gate_mode(input_mode) == "user_gated"
            and current_emitter() is not None):
        # The mesh stops at the gate: presented, editable, and NOT yet the
        # case's mesh - accepting it is the user's next act, not this call's.
        return await present_mesh(open_mesh_gate(session))
    await asyncio.to_thread(session.accept)
    return session.snapshot()


async def _acquire_domain(location: str | None, bbox: Any) -> dict[str, Any]:
    """The reach a corridor follows, plus the mid-reach seed it is navigated from.

    The SAME acquisition a template's plan runs, reached from a standalone call so
    the mesh a user builds by hand is the mesh a template would have built.
    """
    from trid3nt_server.workflows.lib import Domain
    from trid3nt_server.workflows.lib.domain import bind_domain, reset_domain
    from trid3nt_server.workflows.telemac.steps.reach import (
        fetch_reach_flowline,
        geocode_reach,
        reach_seed,
    )

    coerced = coerce_bbox_value(bbox) if bbox is not None else None
    if not location and coerced is None:
        raise MeshToolError(
            "MESH_DOMAIN_UNRESOLVED",
            "a corridor follows a named river reach, so name the place (or draw "
            "the extent it runs through); there is nothing here to navigate from.")
    reach = await geocode_reach(location=location,
                                bbox=None if location else tuple(coerced))
    token = bind_domain(Domain(bbox=reach["bbox"], label=reach["name"]))
    try:
        rivers = await fetch_reach_flowline(prefetched=None)
        seed = await reach_seed(reach=reach, rivers=rivers)
    finally:
        reset_domain(token)
    return {"reach": reach, "seed": seed}
