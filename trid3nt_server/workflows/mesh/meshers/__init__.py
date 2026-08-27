"""The mesher registry: one file per mesh library, each registering its build
beside the edit actions it supports.

A mesher DECLARES its spec fields and its edit actions. The router validates a
spec and an edit against those declarations and refuses anything else BY NAME, so
a field a mesher never declared can never reach its library as a silent no-op.
Adding a mesher is one file; nothing here knows about any particular library.

Every mesher returns the SAME neutral :class:`Mesh` - nodes, cells, an optional
bed - which is what lets one build feed several solver writers and one display
face.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from types import MappingProxyType

__all__ = [
    "EditAction",
    "Mesh",
    "MeshField",
    "MeshToolError",
    "Mesher",
    "apply_layer_edits_action",
    "get_mesher",
    "input_digest",
    "is_late_bound",
    "nearest_names",
    "register_mesher",
    "registered_meshers",
]


class MeshToolError(RuntimeError):
    """A typed mesh refusal: an error code plus the reason, never a silent skip."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def nearest_names(name: str, known: Iterable[str]) -> str:
    """The closest declared spellings to ``name``, as a ready-to-quote phrase."""
    pool = sorted(known)
    close = difflib.get_close_matches(str(name), pool, n=3, cutoff=0.6)
    if close:
        return f"did you mean {', '.join(repr(c) for c in close)}? declared: {pool}"
    return f"declared: {pool}"


def is_late_bound(value: Any) -> bool:
    """Is ``value`` a plan-time DESCRIPTION of a read rather than the value?

    A declared spec field carries ``P.<name>`` / ``D.<name>`` / ``Ref(...)`` until
    the interpreter binds it, so its type and its membership in a choice set are
    not answerable at declaration time - and asking either of a placeholder raises
    rather than answering.
    """
    try:
        from trid3nt_server.workflows.lib.plan import ParamRef, Ref
    except Exception:  # noqa: BLE001 -- the library is absent in a stripped env
        return False
    return isinstance(value, (Ref, ParamRef))


def input_digest(value: Any) -> str:
    """``sha256:<hex>`` for a recipe input whose content is not its declaration.

    A LOCAL file is digested by its bytes; anything else by its own text, which is
    the strongest honest statement available about a remote object this process
    never read.
    """
    text = str(value)
    try:
        path = Path(text)
        if path.is_file():
            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MeshField:
    """One declared input: what it accepts, whether it is required, what it means.

    ``choices`` bounds a vocabulary field; ``hashed`` marks an input whose CONTENT
    the recipe records as a digest (a geometry, an edited layer) rather than
    inline.
    """

    name: str
    types: tuple[type, ...] = ()
    required: bool = False
    choices: tuple[Any, ...] = ()
    default: Any = None
    hashed: bool = False
    doc: str = ""

    def check(self, value: Any, *, where: str) -> Any:
        """Validate one value against this declaration -> the value, or a refusal."""
        if value is None:
            if self.required:
                raise MeshToolError(
                    "MESH_SPEC_MISSING_FIELD",
                    f"{where} requires {self.name!r} ({self.doc or 'no description'}) "
                    "and it was not supplied.")
            return self.default
        if is_late_bound(value):
            return value
        if self.types and not isinstance(value, self.types):
            names = "/".join(t.__name__ for t in self.types)
            raise MeshToolError(
                "MESH_SPEC_BAD_TYPE",
                f"{where}: {self.name!r} takes {names}, got "
                f"{type(value).__name__} ({value!r}).")
        if self.choices and value not in self.choices:
            raise MeshToolError(
                "MESH_SPEC_BAD_VALUE",
                f"{where}: {self.name!r} takes one of "
                f"{[c for c in self.choices]}, got {value!r}.")
        return value


@dataclass(frozen=True)
class EditAction:
    """A named edit: one library call, its typed inputs, and whether it replays.

    ``apply`` takes the current mesh plus the declared inputs as keywords and
    returns the new mesh. ``replayable=False`` marks an action whose change lives
    in an input the recipe can only digest, so replaying the recipe would produce
    a different mesh - the replay refuses instead.
    """

    name: str
    apply: Callable[..., "Mesh"]
    inputs: Mapping[str, MeshField] = field(default_factory=dict)
    replayable: bool = True
    doc: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        hashed = [f.name for f in self.inputs.values() if f.hashed]
        if len(hashed) > 1:
            # The recipe records ONE `source` key beside the digest, so a second
            # hashed input would overwrite the first's provenance.
            raise MeshToolError(
                "MESH_ACTION_AMBIGUOUS_SOURCE",
                f"edit action {self.name!r} declares hashed inputs {hashed}; the "
                "recipe records one source per edit, so an action may hash at "
                "most one input.")


@dataclass(frozen=True)
class Mesh:
    """A built mesh in the ONE shape every mesher returns and every writer reads.

    ``points`` is ``(N, 2)`` in ``crs_authid`` units, ``cells`` is ``(M, 3)``
    triangles or ``(M, 4)`` quads, 0-based, both numpy arrays. ``bed`` is the node
    elevation positive up, or ``None`` when no bed was sampled - a solver that
    needs bathymetry declines a bed-less mesh rather than reading zeros as ground.
    """

    points: Any
    cells: Any
    crs_authid: str
    bed: Any | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))

    @property
    def node_count(self) -> int:
        return int(self.points.shape[0])

    @property
    def element_count(self) -> int:
        return int(self.cells.shape[0])

    @property
    def nodes_per_cell(self) -> int:
        return int(self.cells.shape[1])

    @property
    def has_bed(self) -> bool:
        return self.bed is not None


@dataclass(frozen=True)
class Mesher:
    """A registered mesh library: its build, its spec fields, its edit actions."""

    name: str
    build: Callable[[Mapping[str, Any]], Mesh]
    actions: Mapping[str, EditAction] = field(default_factory=dict)
    fields: Mapping[str, MeshField] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", MappingProxyType(dict(self.actions)))
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def action(self, name: str) -> EditAction:
        found = self.actions.get(str(name))
        if found is None:
            raise MeshToolError(
                "MESH_UNKNOWN_ACTION",
                f"mesher {self.name!r} registers no edit action {name!r} "
                f"({nearest_names(name, self.actions)}).")
        return found


_MESHERS: dict[str, Mesher] = {}


def register_mesher(
    name: str,
    build: Callable[[Mapping[str, Any]], Mesh],
    actions: Iterable[EditAction] = (),
    fields: Iterable[MeshField] = (),
) -> Mesher:
    """Record a mesher under ``name`` -> the registered :class:`Mesher`.

    A duplicate name raises at import: two libraries answering to one name is a
    routing ambiguity the router cannot resolve honestly.
    """
    key = str(name)
    if key in _MESHERS:
        raise MeshToolError(
            "MESH_DUPLICATE_MESHER",
            f"a mesher named {key!r} is already registered.")
    mesher = Mesher(
        name=key, build=build,
        actions={a.name: a for a in actions},
        fields={f.name: f for f in fields})
    _MESHERS[key] = mesher
    return mesher


def get_mesher(name: str) -> Mesher:
    """The mesher registered under ``name``, or a typed refusal naming the roster."""
    found = _MESHERS.get(str(name))
    if found is None:
        raise MeshToolError(
            "MESH_UNKNOWN_MESHER",
            f"no mesher named {name!r} is registered "
            f"({nearest_names(name, _MESHERS)}).")
    return found


def registered_meshers() -> tuple[str, ...]:
    """The registered mesher names, sorted."""
    return tuple(sorted(_MESHERS))


def apply_layer_edits_action() -> EditAction:
    """The hand-edit action every mesher can offer: an edited layer BECOMES the mesh.

    Recorded with the layer's digest and ``replayable: false`` - the edits live in
    the layer rather than in the recipe, so a replay refuses rather than
    reproducing a different mesh under the same recipe.
    """
    return EditAction(
        name="apply_layer_edits",
        apply=_apply_layer_edits,
        inputs={"layer": MeshField(
            "layer", types=(str,), required=True, hashed=True,
            doc="path to the edited .2dm mesh layer")},
        replayable=False,
        doc="Adopt a hand-edited mesh layer as the current mesh.")


def _apply_layer_edits(mesh: Mesh, *, layer: str) -> Mesh:
    from trid3nt_server.workflows.mesh.watershed import read_2dm_mesh

    points, cells, z = read_2dm_mesh(str(layer))
    # A .2dm always carries a node z column, so the edited layer cannot say whether
    # a bed was ever sampled; the mesh it replaces is what knows.
    return Mesh(points=points, cells=cells, crs_authid=mesh.crs_authid,
                bed=(z if mesh.has_bed else None), meta=dict(mesh.meta))
