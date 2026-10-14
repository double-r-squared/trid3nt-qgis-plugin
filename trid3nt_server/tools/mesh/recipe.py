"""THE RECIPE - the one object a mesh is defined by.

Not a spec plus a history of edits: the current PROGRAM that produces the mesh,
as three mesher-agnostic params - extent, resolution_m, kind - the backend
constructor's own ``build`` kwargs, and one ordered ops list. Engine vocabulary
is never a param here; a bed and a role are OPS."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from trid3nt_server.inputs.input import Input
from trid3nt_server.tools.mesh.meshers import (
    MeshOp,
    MeshToolError,
    bind_build,
    bind_ops,
    get_mesher,
    mesh_op,
    resolve_op,
)

__all__ = [
    "MeshOp",
    "MeshRecipe",
    "GRADE",
    "INPUTS",
    "MAX_EDGE",
    "RESOLUTION",
    "build_recipe",
    "input_names",
    "jsonable",
    "mesh_op",
    "recipe_from_plan_value",
    "recipe_plan_value",
    "takes_name",
]

#: The edge a mesh ask is built at, as the run input an ask names it by; a
#: template restates it only to say what the edge has to resolve for its question.
RESOLUTION = Input(
    name="mesh_resolution_m", type=float, units="m",
    desc="Target element edge or cell length the mesh is built at. The "
         "granularity is the USER's and nothing refuses one: absent, the "
         "mesh resolves the finest data it stands on, and the card states "
         "the node count it comes to").optional(
    "the finest edge is the finest cell of the input data the mesh "
    "is built over - the bed raster's own cell")

#: How fast the edge may grow and how coarse it may get, as the run inputs a
#: sizing op names them by. Neither has a default: the number is the asking
#: template's opinion, and an ask that names one unstated is asked for it.
GRADE = Input(
    name="mesh_grade", bounds=(0.05, 0.5),
    desc="Mesh gradation: how fast the edge may grow from one element to the "
         "next, from the finest band out to the coarsest")
MAX_EDGE = Input(
    name="mesh_max_edge_m", bounds=(20.0, 5000.0), units="m",
    desc="Coarsest triangle edge, reached far from what the mesh refines toward")

#: Every run input a mesh ask may name; one is seated where an ask names it.
INPUTS = (RESOLUTION, GRADE, MAX_EDGE)


def jsonable(value: Any) -> Any:
    """A recipe value as JSON, or a refusal naming what cannot be recorded."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, MeshOp):
        return {"op": value.fn, **{k: jsonable(v) for k, v in value.kwargs.items()}}
    from trid3nt_server.inputs.bed import Bed

    if isinstance(value, Bed):
        # A BED records as what it IS - the address of the surface it was read
        # from, or the depth it states. The slot's own wrapper says which shape
        # the value arrived in, which a replay re-reads for itself.
        return jsonable(value.source if value.source is not None else value.depth_m)
    from trid3nt_server.inputs.boundary import BoundaryRun

    if isinstance(value, BoundaryRun):
        # A RUN records as the feature it is written down as, which is the shape
        # the ingestion reads back, so a replay rebuilds the same stretch of edge.
        return value.feature
    collection = getattr(value, "as_feature_collection", None)
    if callable(collection):
        # A typed SLOT value records as the geometry it is: the recipe has to
        # replay from what was meshed, and a drawn domain has no address.
        return jsonable(collection())
    if getattr(value, "uri", None):
        # A declared data row arrives as the layer its producer returned, and the
        # meshers read it through the same unwrap: the recipe records the ADDRESS
        # they read, which is what a replay can re-read.
        from trid3nt_server.inputs.geometry import source_uri

        return jsonable(source_uri(value))
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        return jsonable(item())
    raise MeshToolError(
        "MESH_RECIPE_UNSERIALIZABLE",
        f"a {type(value).__name__} cannot be recorded in a mesh recipe "
        f"({value!r}); declare the ask as numbers, strings, sequences or "
        "mappings.")


@dataclass(frozen=True)
class MeshRecipe:
    """The current program that produces one mesh: its params, build and ops.

    Frozen: every editing method returns a NEW recipe."""

    mesher: str
    kind: str
    extent: Any = None
    resolution_m: float | None = None
    #: The backend CONSTRUCTOR's own keyword arguments, stated verbatim - what
    #: Triangle takes as maximum_area/angle, Gridgen as delr/delc/xoff. Empty
    #: for a constructor that takes none; recorded only when non-empty.
    build: Mapping[str, Any] = field(default_factory=dict)
    ops: tuple[MeshOp, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ops", tuple(self.ops))
        object.__setattr__(self, "build", MappingProxyType(dict(self.build or {})))
        bind_ops(get_mesher(self.mesher), self.ops)
        bind_build(get_mesher(self.mesher), self.build)

    def appending(self, op: MeshOp) -> "MeshRecipe":
        """The recipe with one more entry at the end."""
        return replace(self, ops=self.ops + (op,))

    def altering(self, index: int, op: MeshOp) -> "MeshRecipe":
        """The recipe with entry ``index`` replaced."""
        self._in_range(index, "alter")
        ops = list(self.ops)
        ops[index] = op
        return replace(self, ops=tuple(ops))

    def without(self, index: int) -> "MeshRecipe":
        """The recipe with entry ``index`` removed."""
        self._in_range(index, "remove")
        ops = list(self.ops)
        ops.pop(index)
        return replace(self, ops=tuple(ops))

    def with_params(self, **params: Any) -> "MeshRecipe":
        """The recipe with agnostic params replaced. The ops are untouched."""
        unknown = [name for name in params
                   if name not in ("kind", "extent", "resolution_m")]
        if unknown:
            raise MeshToolError(
                "MESH_RECIPE_UNKNOWN_PARAM",
                f"a recipe has three agnostic params - kind, extent, "
                f"resolution_m - and {unknown} is not among them; engine "
                "vocabulary is an op.")
        return build_recipe(
            mesher=self.mesher, ops=self.ops,
            **{"kind": self.kind, "extent": self.extent,
               "resolution_m": self.resolution_m, **params})

    def _in_range(self, index: int, what: str) -> None:
        if not (0 <= int(index) < len(self.ops)):
            raise MeshToolError(
                "MESH_OP_INDEX",
                f"this recipe has {len(self.ops)} op(s), numbered 0 to "
                f"{len(self.ops) - 1}; there is no entry {index} to {what}.")

    def numbered(self) -> list[str]:
        """The ops as the numbered lines a gate card and a journal quote."""
        return [f"{i}: {op!r}" for i, op in enumerate(self.ops)]

    def to_json(self) -> dict[str, Any]:
        doc = {
            "mesher": self.mesher,
            "kind": self.kind,
            "extent": jsonable(self.extent),
            "resolution_m": jsonable(self.resolution_m),
            "ops": [jsonable(op) for op in self.ops],
        }
        # Recorded only when declared: a recipe without a build is byte-identical
        # to one written before the field existed, so its content key stands.
        if self.build:
            doc["build"] = jsonable(self.build)
        return doc

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> "MeshRecipe":
        ops = tuple(
            MeshOp(fn=str(entry["op"]),
                   kwargs={k: v for k, v in dict(entry).items() if k != "op"})
            for entry in (doc.get("ops") or ()))
        return cls(mesher=str(doc["mesher"]), kind=str(doc["kind"]),
                   extent=doc.get("extent"),
                   resolution_m=doc.get("resolution_m"),
                   build=doc.get("build") or {}, ops=ops)


def build_recipe(*, mesher: str, kind: Any = None, extent: Any = None,
                 resolution_m: Any = None, build: Mapping[str, Any] | None = None,
                 ops: Iterable[MeshOp] | None = None) -> MeshRecipe:
    """The declared ask as a validated recipe. Builds NOTHING.

    Omitting ``ops`` takes the mesher's VISIBLE defaults; declaring replaces them.
    ``build`` is the backend constructor's own kwargs, checked against the
    mesher's declared roster; its values must be recordable."""
    registered = get_mesher(mesher)
    declared = registered.default_ops if ops is None else tuple(ops)
    bad = [op for op in declared if not isinstance(op, MeshOp)]
    if bad:
        raise MeshToolError(
            "MESH_OPS_MALFORMED",
            f"a recipe's ops are mesh_op(...) entries; got a "
            f"{type(bad[0]).__name__} ({bad[0]!r}).")
    declared_build = dict(build or {})
    for value in declared_build.values():
        jsonable(value)
    return MeshRecipe(
        mesher=registered.name, kind=registered.kind_or_default(kind),
        extent=extent,
        # A string is the NAME of the run input the resolution is read off.
        resolution_m=(resolution_m if resolution_m is None
                      or isinstance(resolution_m, str) else float(resolution_m)),
        build=declared_build, ops=declared)


def takes_name(recipe: MeshRecipe, op: MeshOp, key: str) -> bool:
    """Does the string ``op`` states under ``key`` NAME a run input? It does
    unless the op's own signature takes a word there: the signature is the
    schema, and an op this process cannot import takes no word."""
    if not isinstance(op.kwargs.get(key), str):
        return False
    _space, fn = resolve_op(get_mesher(recipe.mesher), op.fn)
    if fn is None:
        return True
    param = inspect.signature(fn).parameters.get(key)
    annotation = "" if param is None else param.annotation
    spelled = annotation if isinstance(annotation, str) \
        else getattr(annotation, "__name__", "")
    return {part.strip() for part in spelled.split("|")} - {"None"} != {"str"}


def input_names(recipe: MeshRecipe) -> list[str]:
    """Every run input a recipe names: its extent, a resolution stated as a
    name, and each op argument that takes one."""
    named = [value for value in (recipe.extent, recipe.resolution_m)
             if isinstance(value, str)]
    return named + [op.kwargs[key] for op in recipe.ops for key in op.kwargs
                    if takes_name(recipe, op, key)]


def recipe_plan_value(recipe: MeshRecipe) -> dict[str, Any]:
    """A recipe as the plain mapping a plan step carries in its kwargs.

    The whole ask travels; the step restates none of it."""
    if not isinstance(recipe, MeshRecipe):
        raise MeshToolError(
            "MESH_RECIPE_EXPECTED",
            f"a mesh step carries the template's MESH recipe "
            f"(tool.build_mesh(...)), got {type(recipe).__name__}.")
    # A string where a value belongs is the NAME of the run input it is read
    # off; the run reads each into this mapping before the mesh is keyed.
    plan = {
        "mesher": recipe.mesher,
        "kind": recipe.kind,
        "extent": recipe.extent,
        "resolution_m": recipe.resolution_m,
        "ops": [{"op": op.fn, "kwargs": _thaw(op.kwargs)} for op in recipe.ops],
    }
    # Present only when declared: an undeclared build keys the same content as
    # one written before the field existed.
    if recipe.build:
        plan["build"] = _thaw(recipe.build)
    return plan


def _thaw(value: Any) -> Any:
    """A frozen recipe value as the plain containers a step's kwargs carry.

    A named input passes through untouched: reading it is the run's job."""
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_thaw(v) for v in value)
    return value


def recipe_from_plan_value(value: Mapping[str, Any],
                           **overrides: Any) -> MeshRecipe:
    """Rebuild the recipe a step was handed, with named params replaced.

    ``overrides`` are the params a step RESOLVES; the rest returns as written."""
    ops = tuple(MeshOp(fn=str(entry["op"]), kwargs=dict(entry.get("kwargs") or {}))
                for entry in (value.get("ops") or ()))
    fields = {"mesher": str(value["mesher"]), "kind": value.get("kind"),
              "extent": value.get("extent"),
              "resolution_m": value.get("resolution_m"),
              "build": value.get("build") or None, "ops": ops}
    fields.update(overrides)
    return build_recipe(**fields)
