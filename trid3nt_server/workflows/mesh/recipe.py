"""THE RECIPE - the one object a mesh is defined by.

Not a spec plus a history of edits: the current PROGRAM that produces the mesh,
as three mesher-agnostic params - extent, resolution_m, kind - and one ordered
ops list. Engine vocabulary is never a param here; a bed and a role are OPS."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from trid3nt_server.workflows.mesh.meshers import (
    MeshOp,
    MeshToolError,
    bind_ops,
    get_mesher,
    is_late_bound,
    mesh_op,
)

__all__ = [
    "MeshOp",
    "MeshRecipe",
    "build_recipe",
    "jsonable",
    "mesh_op",
    "recipe_from_plan_value",
    "recipe_plan_value",
]


def jsonable(value: Any) -> Any:
    """A recipe value as JSON, or a refusal naming what cannot be recorded."""
    if is_late_bound(value):
        raise MeshToolError(
            "MESH_RECIPE_UNBOUND",
            f"{value!r} is a late-bound read, not a value: the recipe records "
            "what a run actually built with, so bind the declaration first.")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, MeshOp):
        return {"op": value.fn, **{k: jsonable(v) for k, v in value.kwargs.items()}}
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
    """The current program that produces one mesh: three params and its ops.

    Frozen: every editing method returns a NEW recipe."""

    mesher: str
    kind: str
    extent: Any = None
    resolution_m: float | None = None
    ops: tuple[MeshOp, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ops", tuple(self.ops))
        bind_ops(get_mesher(self.mesher), self.ops)

    # -- editing ----------------------------------------------------------- #
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

    # -- the record -------------------------------------------------------- #
    def numbered(self) -> list[str]:
        """The ops as the numbered lines a gate card and a journal quote."""
        return [f"{i}: {op!r}" for i, op in enumerate(self.ops)]

    def to_json(self) -> dict[str, Any]:
        return {
            "mesher": self.mesher,
            "kind": self.kind,
            "extent": jsonable(self.extent),
            "resolution_m": jsonable(self.resolution_m),
            "ops": [jsonable(op) for op in self.ops],
        }

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> "MeshRecipe":
        ops = tuple(
            MeshOp(fn=str(entry["op"]),
                   kwargs={k: v for k, v in dict(entry).items() if k != "op"})
            for entry in (doc.get("ops") or ()))
        return cls(mesher=str(doc["mesher"]), kind=str(doc["kind"]),
                   extent=doc.get("extent"),
                   resolution_m=doc.get("resolution_m"), ops=ops)

    @property
    def unbound(self) -> list[str]:
        """The names in this recipe the interpreter has not bound to values yet."""
        out = [name for name in ("extent", "resolution_m")
               if is_late_bound(getattr(self, name))]
        out += [f"ops[{i}].{name}" for i, op in enumerate(self.ops)
                for name, value in op.kwargs.items() if is_late_bound(value)]
        return out


def build_recipe(*, mesher: str, kind: Any = None, extent: Any = None,
                 resolution_m: Any = None,
                 ops: Iterable[MeshOp] | None = None) -> MeshRecipe:
    """The declared ask as a validated recipe. Builds NOTHING.

    Omitting ``ops`` takes the mesher's VISIBLE defaults; declaring replaces them."""
    registered = get_mesher(mesher)
    declared = registered.default_ops if ops is None else tuple(ops)
    bad = [op for op in declared if not isinstance(op, MeshOp)]
    if bad:
        raise MeshToolError(
            "MESH_OPS_MALFORMED",
            f"a recipe's ops are mesh_op(...) entries; got a "
            f"{type(bad[0]).__name__} ({bad[0]!r}).")
    return MeshRecipe(
        mesher=registered.name, kind=registered.kind_or_default(kind),
        extent=extent,
        resolution_m=(resolution_m if resolution_m is None or is_late_bound(
            resolution_m) else float(resolution_m)),
        ops=declared)


def recipe_plan_value(recipe: MeshRecipe) -> dict[str, Any]:
    """A recipe as the plain mapping a plan step carries in its kwargs.

    The whole ask travels; the step restates none of it."""
    if not isinstance(recipe, MeshRecipe):
        raise MeshToolError(
            "MESH_RECIPE_EXPECTED",
            f"a mesh step carries the template's MESH recipe "
            f"(tool.build_mesh(...)), got {type(recipe).__name__}.")
    # Mappings and sequences are what the interpreter walks to substitute
    # late-bound reads, so the recipe travels as one and comes back with its
    # values bound.
    return {
        "mesher": recipe.mesher,
        "kind": recipe.kind,
        "extent": recipe.extent,
        "resolution_m": recipe.resolution_m,
        "ops": [{"op": op.fn, "kwargs": _thaw(op.kwargs)} for op in recipe.ops],
    }


def _thaw(value: Any) -> Any:
    """A frozen recipe value as the plain containers a step's kwargs carry.

    Late-bound reads pass through untouched: binding them is not this job."""
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
              "resolution_m": value.get("resolution_m"), "ops": ops}
    fields.update(overrides)
    return build_recipe(**fields)
