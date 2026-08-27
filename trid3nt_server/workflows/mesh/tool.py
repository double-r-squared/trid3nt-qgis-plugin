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
    MeshToolError,
    get_mesher,
    is_late_bound,
    nearest_names,
)
from trid3nt_server.workflows.mesh.meshers import reg_grid as _reg_grid  # noqa: F401 - registration

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
    "resolve_mesh",
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
    **fields: Any,
) -> Any:
    """BUILD A COMPUTATIONAL MESH for a domain -> a mesh layer + a solver-ready mesh artifact.

    THE tool for "mesh this area", "build the grid / domain a solver runs on",
    "make me a mesh I can inspect before running a model", "author the model
    domain and keep it in the case". Mesh creation is an EXPLICIT act and lives
    HERE: a model template that finds this mesh in the case ASKS before consuming
    it, and never invents one behind your back.

    ``mesher`` names the mesh library that builds it and ``kind`` the shape of
    mesh it makes; every other argument is a field that mesher DECLARES, checked
    at the router - a field the chosen mesher does not declare is refused by name
    rather than ignored.

    ``reg_grid`` builds the uniform lattice a structured deck runs on: give it
    ``bbox`` (or a ``location`` to geocode) and ``resolution_m``, the cell size in
    metres. It carries no bed, so a solver that needs bathymetry declines it
    honestly rather than reading a zero-filled bed as ground.

    Params:
        mesher: which mesh library builds it (registered: reg_grid).
        kind: the mesh shape (reg_grid: structured_grid).
        location: place naming the domain (geocoded). Supply this OR ``bbox``.
        bbox: AOI ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        fields: the chosen mesher's own declared fields (reg_grid: resolution_m).
    """
    import asyncio

    from trid3nt_server.emission.pipeline_emitter import current_turn_case
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.mesh.session import MeshSession

    if not (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip():
        raise MeshToolError(
            "MESH_STAGING_UNAVAILABLE",
            "TRID3NT_CACHE_BUCKET must be set to stage a built mesh into the case.")

    aoi = coerce_bbox_value(bbox) if bbox is not None else None
    if aoi is None and location:
        geo = await asyncio.to_thread(
            TOOL_REGISTRY["geocode_location"].fn, query=location)
        aoi = coerce_bbox_value(getattr(geo, "bbox", None) or geo["bbox"])
    if aoi is not None:
        fields = {"aoi": tuple(float(v) for v in aoi), **fields}

    declaration = tool.build_mesh(mesher=mesher, kind=kind, **fields)
    name = location or f"{mesher} mesh"
    session = MeshSession(declaration, case_id=current_turn_case(), name=name)
    await asyncio.to_thread(session.accept)
    return session.snapshot()
