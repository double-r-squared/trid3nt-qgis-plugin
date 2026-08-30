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
    "EDGE_RESOLUTION_SPECS",
    "EditAction",
    "Mesh",
    "MeshField",
    "MeshToolError",
    "Mesher",
    "RESTAGE_TOOL",
    "apply_layer_edits_action",
    "checked_refine",
    "contained_extent",
    "fetch_activation_rows",
    "fetch_fallback_note",
    "get_mesher",
    "input_digest",
    "is_late_bound",
    "nearest_names",
    "register_mesher",
    "registered_meshers",
    "staged_coverage",
]


class MeshToolError(RuntimeError):
    """A typed mesh refusal: an error code plus the reason, never a silent skip.

    ``escalation`` carries the call that DOES what the refused edit could not, as
    ``{"tool": name, "overrides": {...}}``, for the refusals whose answer is
    another primitive rather than a different argument.
    """

    def __init__(self, error_code: str, message: str,
                 escalation: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.escalation = dict(escalation) if escalation else None


def _edge_resolution_specs() -> tuple[Any, ...]:
    """The DECLARED edge-length ranges every mesher's edge band is bounded by.

    Both are MESH-GENERATOR constraints with a practical 5 m floor: a finer edge
    reliably trips HEC-RAS's <= 8-sides-per-cell acceptance on any non-trivial AOI
    and over-refines a TIN. There is NO fixed coarse ceiling - realizability is
    AOI-dependent and enforced at build time by a typed refusal (> 8-sided cells,
    a triangulator failure), never a silent snap. The declaration carries the
    floor and the 8-side reality so a gate card can quote them.
    """
    from trid3nt_contracts.tool_registry import ResolutionSpec

    native = "3DEP 10 m (fetch_dem) terrain native"
    return (
        ResolutionSpec(
            param="min_edge_length_m", unit="m", min_value=5.0,
            native_hint=native, constraint_source="solver",
            rationale=(
                "finest cell/triangle edge; below ~5 m the seed cloud reliably "
                "trips HEC's <= 8-sides-per-cell acceptance (AOI-dependent, "
                "enforced by a typed build error), and over-refines a TIN; no "
                "fixed coarse ceiling (grade + AOI govern)")),
        ResolutionSpec(
            param="max_edge_length_m", unit="m", min_value=5.0,
            native_hint=native, constraint_source="solver",
            rationale=(
                "coarsest background edge; bounded below by the 5 m floor, no "
                "fixed ceiling (a coarser hillslope background is valid; "
                "realizability is the AOI-dependent 8-side/grade build check, a "
                "typed error not a silent snap)")),
    )


EDGE_RESOLUTION_SPECS = _edge_resolution_specs()


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


def _field_of(layer: Any, name: str) -> Any:
    """One field of a fetched layer, whichever shape the registry handed back.

    A fetcher returns a typed layer OR the same layer as a mapping, so reading only
    the attribute reports UNMEASURED provenance for a fetch that measured it.
    """
    if isinstance(layer, Mapping):
        return layer.get(name)
    return getattr(layer, name, None)


def fetch_activation_rows(layer: Any) -> list[tuple[str, float]]:
    """The ladder rungs that ACTUALLY served a fetch -> ``[(rung, coverage), ...]``.

    Rows with zero coverage are rungs the ladder considered and did not use, so
    they are dropped rather than narrated as sources.
    """
    rows: list[tuple[str, float]] = []
    for row in (_field_of(layer, "fallbacks") or []):
        rung = _field_of(row, "rung")
        coverage = _field_of(row, "coverage")
        if rung is None or coverage is None or float(coverage) <= 0.0:
            continue
        rows.append((str(rung), float(coverage)))
    return rows


def fetch_fallback_note(layer: Any) -> str | None:
    """The one-line narration a fetch attached to a substitution, in either shape."""
    note = _field_of(layer, "fallback_note")
    return str(note) if note else None


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


def checked_refine(where: str, refine: Any,
                   declared: Mapping[str, float]) -> dict[str, float]:
    """The refine knobs a mesher declares, checked BY NAME -> numbers with defaults.

    A ``refine`` block is a mapping, so the field check can only say it is one; the
    knobs inside it are checked here, against the same rule the outer fields obey -
    an undeclared knob is refused by name rather than dropped on the floor where it
    would read as a lever that did nothing.
    """
    given = dict(refine or {})
    unknown = [k for k in given if k not in declared]
    if unknown:
        raise MeshToolError(
            "MESH_SPEC_UNKNOWN_KNOB",
            f"{where}: refine declares no knob {unknown[0]!r} "
            f"({nearest_names(unknown[0], declared)}). Unknown knobs: "
            f"{sorted(unknown)}.")
    resolved: dict[str, float] = {}
    for name, default in declared.items():
        value = given.get(name, default)
        if is_late_bound(value):
            raise MeshToolError(
                "MESH_SPEC_UNBOUND",
                f"{where}: refine[{name!r}] is a late-bound read ({value!r}) rather "
                "than a value, so this mesh cannot be built; bind the declaration "
                "against a resolved sheet before demanding the mesh.")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MeshToolError(
                "MESH_SPEC_BAD_TYPE",
                f"{where}: refine[{name!r}] takes a number, got "
                f"{type(value).__name__} ({value!r}).")
        resolved[name] = float(value)
    return resolved


#: Containment slack, in degrees. A caller retyping the staged box to six decimals
#: is restating it to ~0.1 m, and a box that reads as the staged one must not
#: refuse on the last bit of a float.
_CONTAINMENT_EPS_DEG = 1e-9

#: The primitive an out-of-coverage extent change escalates to. There is ONE
#: re-run path, and a moved extent takes it rather than growing a second one.
RESTAGE_TOOL = "rerun_workflow"


def staged_coverage(mesh: "Mesh") -> tuple[float, float, float, float] | None:
    """The lon/lat box this mesh's inputs were STAGED over, when it states one.

    A mesher whose staged inputs cover more ground than the mesh occupies declares
    ``staged_coverage``; otherwise the built extent is the coverage, because that
    is the box its inputs were fetched for.
    """
    declared = mesh.meta.get("staged_coverage") or mesh.meta.get("extent")
    if declared is None:
        return None
    return tuple(float(v) for v in declared)  # type: ignore[return-value]


def contained_extent(mesh: "Mesh", extent: Any, *,
                     edit: str) -> tuple[float, float, float, float]:
    """The new extent as a CROP, or a refusal naming the restage.

    Containment is BINARY. Inside the staged coverage an extent change is a crop -
    the mesh is re-derived over the smaller box and the inputs already staged for
    the larger one stand. Moved, or larger by any amount, and the inputs the mesh
    would need were never staged: partial coverage produces silently wrong edges,
    so the change is not an edit at all and escalates to the rerun primitive with
    the new box.
    """
    new = tuple(float(v) for v in extent)
    if len(new) != 4:
        raise MeshToolError(
            "MESH_EXTENT_MALFORMED",
            f"edit {edit!r} takes (min_lon, min_lat, max_lon, max_lat); got "
            f"{list(new)}.")
    if new[0] >= new[2] or new[1] >= new[3]:
        raise MeshToolError(
            "MESH_EXTENT_MALFORMED",
            f"edit {edit!r}: the extent {list(new)} has no area (min must be "
            "below max on both axes).")
    coverage = staged_coverage(mesh)
    if coverage is None:
        raise MeshToolError(
            "MESH_COVERAGE_UNKNOWN",
            f"edit {edit!r} is judged against the coverage this mesh's inputs were "
            "staged over, and this mesh states none; a mesher offering an extent "
            "edit carries an 'extent' or 'staged_coverage' in its mesh meta.")
    eps = _CONTAINMENT_EPS_DEG
    if (new[0] >= coverage[0] - eps and new[1] >= coverage[1] - eps
            and new[2] <= coverage[2] + eps and new[3] <= coverage[3] + eps):
        return new
    escalation = {"tool": RESTAGE_TOOL, "overrides": {"bbox": list(new)}}
    raise MeshToolError(
        "MESH_EXTENT_OUTSIDE_COVERAGE",
        f"the extent {list(new)} is not contained in the coverage this mesh's "
        f"inputs were staged over ({list(coverage)}), so cropping to it would mesh "
        "ground nothing was fetched for. A moved or grown extent is a RESTAGE, not "
        f"an edit: ask the question again over the new box with "
        f"{escalation['tool']}(run_id=<the run that built this mesh>, "
        f"overrides={escalation['overrides']!r}).",
        escalation=escalation)


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

    Both are ``None`` for a mesh whose realized topology lives in an ENGINE BUNDLE
    rather than in arrays: the engine re-realizes the nodes and cells from the
    authoring inputs the mesher staged, so there is no geometry here to claim. Such
    a mesh states its counts in ``meta["artifact"]`` and carries its own display
    face, because the formats that write connectivity have nothing to write.
    """

    points: Any
    cells: Any
    crs_authid: str
    bed: Any | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))

    def _declared_count(self, name: str) -> int:
        return int((self.meta.get("artifact") or {}).get(name, 0))

    @property
    def node_count(self) -> int:
        if self.points is None:
            return self._declared_count("node_count")
        return int(self.points.shape[0])

    @property
    def element_count(self) -> int:
        if self.cells is None:
            return self._declared_count("element_count")
        return int(self.cells.shape[0])

    @property
    def nodes_per_cell(self) -> int:
        return 0 if self.cells is None else int(self.cells.shape[1])

    @property
    def has_cells(self) -> bool:
        return self.cells is not None

    @property
    def has_bed(self) -> bool:
        return self.bed is not None


@dataclass(frozen=True)
class Mesher:
    """A registered mesh library: its build, its spec fields, its edit actions.

    ``deterministic`` is a MEASURED claim about the library, not a hope: a mesher
    declares False when identical inputs have been observed to produce different
    meshes, and the recipe carries that so a replay is read as an equivalent
    rebuild rather than as the same mesh.
    """

    name: str
    build: Callable[[Mapping[str, Any]], Mesh]
    actions: Mapping[str, EditAction] = field(default_factory=dict)
    fields: Mapping[str, MeshField] = field(default_factory=dict)
    deterministic: bool = True

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


def _refuse_optional_before_required(action: EditAction) -> None:
    """Refuse an action whose generated tool signature could not compile.

    A registered action becomes an agent tool whose REAL parameters are its
    declared inputs in declaration order, and Python has no parameter that is
    required after one that defaults. Caught here, at the declaration, rather than
    as a SyntaxError when a gate opens over the mesher months later.
    """
    seen_optional: str | None = None
    for name, declared in action.inputs.items():
        if not declared.required:
            seen_optional = seen_optional or name
        elif seen_optional is not None:
            raise MeshToolError(
                "MESH_ACTION_INPUT_ORDER",
                f"edit action {action.name!r} declares the required input {name!r} "
                f"after the optional {seen_optional!r}; an action's inputs are its "
                "generated tool's parameters in declaration order, so every "
                "required input must be declared before the first optional one.")


def register_mesher(
    name: str,
    build: Callable[[Mapping[str, Any]], Mesh],
    actions: Iterable[EditAction] = (),
    fields: Iterable[MeshField] = (),
    deterministic: bool = True,
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
    actions = tuple(actions)
    for action in actions:
        _refuse_optional_before_required(action)
    mesher = Mesher(
        name=key, build=build,
        actions={a.name: a for a in actions},
        fields={f.name: f for f in fields},
        deterministic=bool(deterministic))
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


def apply_layer_edits_action(
        regenerate: Callable[[Mesh, Mesh], Mesh] | None = None) -> EditAction:
    """The hand-edit action every mesher can offer: an edited layer BECOMES the mesh.

    Recorded with the layer's digest and ``replayable: false`` - the edits live in
    the layer rather than in the recipe, so a replay refuses rather than
    reproducing a different mesh under the same recipe.

    ``regenerate(previous, adopted) -> Mesh`` is the mesher's rewrite of everything
    bound to the topology the edit replaced: the per-solver files it wrote and the
    quantities it measured on the old cells. A mesher whose solve consumes a record
    only IT can write MUST supply one, because a session that accepted a mesh
    missing that record would hand the run a mesh it cannot stage.
    """

    def apply(mesh: Mesh, *, layer: str) -> Mesh:
        return _apply_layer_edits(mesh, layer=layer, regenerate=regenerate)

    return EditAction(
        name="apply_layer_edits",
        apply=apply,
        inputs={"layer": MeshField(
            "layer", types=(str,), required=True, hashed=True,
            doc="path to the edited .2dm mesh layer")},
        replayable=False,
        doc="Adopt a hand-edited mesh layer as the current mesh.")


#: Meta a mesher writes ABOUT one topology: the per-solver geometry files it wrote
#: and the quantities it measured on those cells. An adopted layer is a different
#: topology, so carrying either forward would hand a solver the pre-edit geometry
#: under the edited mesh's name.
_TOPOLOGY_BOUND_META = ("files", "probes")

#: Claims about the topology that a mesher DECLARED for the artifact. They describe
#: the cells the edit replaced, so an adopted mesh states them afresh from what it
#: can actually back rather than inheriting the pre-edit mesh's word.
_TOPOLOGY_BOUND_CLAIMS = ("open_boundary_info",)


def _apply_layer_edits(mesh: Mesh, *, layer: str,
                       regenerate: Callable[[Mesh, Mesh], Mesh] | None) -> Mesh:
    from trid3nt_server.workflows.mesh.shared.nodes import read_2dm_mesh

    _refuse_unadoptable(mesh, regenerate)
    points, cells, z = read_2dm_mesh(str(layer))
    carried = {k: v for k, v in mesh.meta.items()
               if k not in _TOPOLOGY_BOUND_META}
    artifact = {k: v for k, v in dict(carried.get("artifact") or {}).items()
                if k not in _TOPOLOGY_BOUND_CLAIMS}
    if artifact or "artifact" in carried:
        carried["artifact"] = artifact
    # A .2dm always carries a node z column, so the edited layer cannot say whether
    # a bed was ever sampled; the mesh it replaces is what knows.
    adopted = Mesh(points=points, cells=cells, crs_authid=mesh.crs_authid,
                   bed=(z if mesh.has_bed else None), meta=carried)
    return adopted if regenerate is None else regenerate(mesh, adopted)


def _refuse_unadoptable(mesh: Mesh, regenerate: Any) -> None:
    """Refuse a hand-edit whose result no solve could be staged from.

    An accepted mesh is a promise that a run can be staged on it, so the refusal
    belongs HERE - at the edit - rather than at the deck, where it would arrive
    after the user already accepted.
    """
    if regenerate is not None:
        return
    if not mesh.has_cells:
        raise MeshToolError(
            "MESH_EDIT_NOT_STAGEABLE",
            "this mesh states no cells of its own - the engine realizes them - so "
            "an adopted layer cannot be reconciled with what a solve would be "
            "staged from.")
