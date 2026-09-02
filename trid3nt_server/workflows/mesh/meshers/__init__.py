"""What a MESHER is: namespaces, a role adapter, a default recipe.

A mesher registers three things and nothing else. Its NAMESPACES are the sets of
callables a recipe's ops may name, each tagged with the phase it runs in - the
wrapped library's own functions under the library's own names, plus whatever
primitives that mesher owns. The SHARED primitives namespace rides along for
every mesher. Its ROLE ADAPTER is its ``build``: how the recipe's extent becomes
this library's domain object and how ``resolution_m`` threads as its defaults.
Its DEFAULT RECIPE is the hard-baked, visible ops list an undeclared ask gets.

Everything else - the recipe, validation, ``mesh_op``, the gate's cards, regen,
the artifact - is shared, so a new mesher is three registrations and no router
grows.

Every mesher returns the SAME neutral :class:`Mesh` - nodes, cells, an optional
bed - which is what lets one build feed several solver writers and one display
face.
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from types import MappingProxyType

__all__ = [
    "POST",
    "PRE",
    "BoundOp",
    "EDGE_RESOLUTION_SPECS",
    "Mesh",
    "MeshOp",
    "MeshToolError",
    "Mesher",
    "OpNamespace",
    "bind_ops",
    "fetch_activation_rows",
    "fetch_fallback_note",
    "get_mesher",
    "input_digest",
    "is_late_bound",
    "mesh_op",
    "nearest_names",
    "op_names",
    "register_mesher",
    "registered_meshers",
    "resolve_op",
]

#: The two phases an op can run in. Which one an op is in is DERIVED from the
#: namespace it was registered in - the caller never says it, and a namespace
#: that spans both would make the derivation a guess.
PRE = "pre"
POST = "post"


class MeshToolError(RuntimeError):
    """A typed mesh refusal: an error code plus the reason, never a silent skip.

    ``escalation`` carries the call that DOES what the refused ask could not, as
    ``{"tool": name, "overrides": {...}}``, for the refusals whose answer is
    another primitive rather than a different argument.
    """

    def __init__(self, error_code: str, message: str,
                 escalation: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.escalation = dict(escalation) if escalation else None


def _edge_resolution_specs() -> tuple[Any, ...]:
    """The DECLARED range ``resolution_m`` is bounded by.

    A MESH-GENERATOR constraint with a practical 5 m floor: a finer edge reliably
    trips HEC-RAS's <= 8-sides-per-cell acceptance on any non-trivial AOI and
    over-refines a TIN. There is NO fixed coarse ceiling - realizability is
    AOI-dependent and enforced at build time by a typed refusal (> 8-sided cells,
    a triangulator failure), never a silent snap. The declaration carries the
    floor and the 8-side reality so a gate card can quote them.
    """
    from trid3nt_contracts.tool_registry import ResolutionSpec

    return (
        ResolutionSpec(
            param="resolution_m", unit="m", min_value=5.0,
            native_hint="3DEP 10 m (fetch_dem) terrain native",
            constraint_source="solver",
            rationale=(
                "the one agnostic size word: the finest cell or triangle edge. "
                "Below ~5 m the seed cloud reliably trips HEC's <= 8-sides-per-"
                "cell acceptance (AOI-dependent, enforced by a typed build "
                "error) and over-refines a TIN; no fixed coarse ceiling "
                "(gradation + AOI govern, and the coarsest background edge "
                "defaults to 10x this)")),
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

    A declared recipe field carries ``P.<name>`` / ``D.<name>`` / ``Ref(...)``
    until the interpreter binds it, so its type and its membership in a choice
    set are not answerable at declaration time - and asking either of a
    placeholder raises rather than answering.
    """
    try:
        from trid3nt_server.workflows.lib.plan import ParamRef, Ref
    except Exception:  # noqa: BLE001 -- the library is absent in a stripped env
        return False
    return isinstance(value, (Ref, ParamRef))


def input_digest(value: Any) -> str:
    """``sha256:<hex>`` for an input whose content is not its declaration.

    A LOCAL file is digested by its bytes; anything else by its own text, which
    is the strongest honest statement available about a remote object this
    process never read.
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

    A fetcher returns a typed layer OR the same layer as a mapping, so reading
    only the attribute reports UNMEASURED provenance for a fetch that measured it.
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


# --------------------------------------------------------------------------- #
# The op vocabulary: two origins, both verbatim.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MeshOp:
    """One entry of a recipe's ops list: a function NAME and its kwargs.

    The name is VERBATIM - the wrapped library's own spelling, or one of our
    primitives under its real ``def`` name. Never an alias, because an alias is a
    word that implies.
    """

    fn: str
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fn", str(self.fn))
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.kwargs.items())
        return f"mesh_op({self.fn!r}{', ' + args if args else ''})"


def mesh_op(fn: str, **kwargs: Any) -> MeshOp:
    """One recipe entry, frozen. Builds NOTHING.

    The declaration face of the word. Its other face is the registered
    ``mesh_op`` tool (``workflows/mesh/op_tool.py``), which appends the same
    entry to a live session's recipe and regenerates.
    """
    return MeshOp(fn=fn, kwargs=kwargs)


@dataclass(frozen=True)
class OpNamespace:
    """A set of callables a recipe's ops may name, and WHEN they run.

    ``module`` is the real module when THIS process can import it, which is what
    lets the signature be the schema. ``names`` is the declared roster for a
    library that lives somewhere this process cannot import from - a
    GPL-isolated container - whose signatures are the driver's to bind; an op
    from such a namespace passes validation with a journaled note.
    """

    origin: str
    phase: str
    module: Any = None
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in (PRE, POST):
            raise MeshToolError(
                "MESH_NAMESPACE_PHASE",
                f"namespace {self.origin!r} declares phase {self.phase!r}; a "
                f"namespace runs in {PRE!r} or {POST!r}.")
        if self.module is None and not self.names:
            raise MeshToolError(
                "MESH_NAMESPACE_EMPTY",
                f"namespace {self.origin!r} declares neither a module to read "
                "its callables from nor a roster of names.")
        if self.module is not None and not getattr(self.module, "__all__", ()):
            raise MeshToolError(
                "MESH_NAMESPACE_UNDECLARED",
                f"namespace {self.origin!r} reads its roster off "
                f"{self.module.__name__}.__all__, and that module declares none.")

    def roster(self) -> tuple[str, ...]:
        """Every name this namespace answers to.

        A module's own ``__all__`` is the roster: it is what the module DECLARES
        it offers, so a helper it happens to import is not an op and a primitive
        stays an op however it is reached.
        """
        if self.names:
            return self.names
        return tuple(sorted(getattr(self.module, "__all__", ())))

    def callable_for(self, fn: str) -> Callable[..., Any] | None:
        """The real callable behind ``fn``, or ``None`` when it lives elsewhere."""
        if self.module is None:
            return None
        return getattr(self.module, str(fn), None)


def _shared_primitives() -> tuple[OpNamespace, ...]:
    """The primitives namespace that rides along for EVERY mesher.

    Imported where it is asked rather than at module scope: the primitives are
    written against :class:`Mesh`, which is declared here.
    """
    from trid3nt_server.workflows.mesh.shared import primitives

    return (OpNamespace(origin="primitives", phase=POST, module=primitives),)


@dataclass(frozen=True)
class BoundOp:
    """One recipe entry resolved against the namespace that owns it.

    ``fn`` is ``None`` for a name whose callable this process cannot import; the
    driver that can is where it binds, and ``note`` is what the journal carries
    about that.
    """

    op: MeshOp
    origin: str
    phase: str
    fn: Callable[..., Any] | None = None
    note: str | None = None

    @property
    def name(self) -> str:
        return self.op.fn

    @property
    def kwargs(self) -> Mapping[str, Any]:
        return self.op.kwargs


def _namespaces(mesher: "Mesher") -> tuple[OpNamespace, ...]:
    return tuple(mesher.namespaces) + _shared_primitives()


def op_names(mesher: "Mesher") -> tuple[str, ...]:
    """Every name this mesher's ops may use, across both origins, sorted."""
    seen: set[str] = set()
    for space in _namespaces(mesher):
        seen.update(space.roster())
    return tuple(sorted(seen))


def resolve_op(mesher: "Mesher", fn: str) -> tuple[OpNamespace, Callable[..., Any] | None]:
    """Which namespace owns ``fn`` -> ``(namespace, callable | None)``.

    A name present in BOTH origins refuses loudly: the phase an op runs in is
    derived from its namespace, so a name two namespaces answer to has no
    derivable phase and the recipe would mean two different programs.
    """
    found = [space for space in _namespaces(mesher) if str(fn) in space.roster()]
    if len(found) > 1:
        raise MeshToolError(
            "MESH_OP_AMBIGUOUS",
            f"{fn!r} is registered in {[s.origin for s in found]} for mesher "
            f"{mesher.name!r}; an op's phase is derived from its namespace, so a "
            "name two of them answer to has no derivable phase.")
    if not found:
        raise MeshToolError(
            "MESH_OP_UNKNOWN",
            f"mesher {mesher.name!r} registers no op {fn!r} "
            f"({nearest_names(fn, op_names(mesher))}).")
    space = found[0]
    return space, space.callable_for(fn)


def bind_ops(mesher: "Mesher",
             ops: Iterable[MeshOp]) -> tuple[BoundOp, ...]:
    """Validate a recipe's ops against the mesher's namespaces -> bound entries.

    THE SIGNATURE IS THE SCHEMA. The name must exist in the combined namespace,
    and the kwargs must bind to the real callable's signature. A function this
    process cannot import has no signature to bind against, so it passes through
    with a note the journal carries and the driver that owns it binds instead.
    Declared order is preserved; duplicates are legal.
    """
    bound: list[BoundOp] = []
    for op in ops:
        space, fn = resolve_op(mesher, op.fn)
        note = None
        if fn is None:
            note = (f"{op.fn!r} is a {space.origin} function this process cannot "
                    "import, so its kwargs were not bound here; the driver that "
                    "runs it binds them against the real signature")
        else:
            _bind_signature(mesher, op, fn)
        bound.append(BoundOp(op=op, origin=space.origin, phase=space.phase,
                             fn=fn, note=note))
    return tuple(bound)


def _bind_signature(mesher: "Mesher", op: MeshOp,
                    fn: Callable[..., Any]) -> None:
    """Bind ``op``'s kwargs to the real callable, or refuse in its own words."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return
    try:
        signature.bind_partial(**{k: None for k in op.kwargs})
    except TypeError as exc:
        declared = [name for name, prm in signature.parameters.items()
                    if prm.kind not in (prm.VAR_POSITIONAL, prm.VAR_KEYWORD)]
        raise MeshToolError(
            "MESH_OP_BAD_KWARGS",
            f"mesh_op({op.fn!r}, ...) on mesher {mesher.name!r}: {exc}. The "
            f"function's own parameters are {declared}.") from None


# --------------------------------------------------------------------------- #
# The neutral mesh, and the registry.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Mesh:
    """A built mesh in the ONE shape every mesher returns and every writer reads.

    ``points`` is ``(N, 2)`` in ``crs_authid`` units, ``cells`` is ``(M, 3)``
    triangles or ``(M, 4)`` quads, 0-based, both numpy arrays. ``bed`` is the node
    elevation positive up, or ``None`` when no bed was painted - a solver that
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
    """A registered mesh library: namespaces, a role adapter, a default recipe.

    ``build`` IS the role adapter: it turns the recipe's extent into this
    library's domain object, threads ``resolution_m`` as its defaults, and runs
    the ops in their derived phases. ``kinds`` are the mesh shapes it makes, the
    first being what an undeclared ask gets.

    ``deterministic`` is a MEASURED claim about the library, not a hope: a mesher
    declares False when identical inputs have been observed to produce different
    meshes, and the recipe carries that so a replay is read as an equivalent
    rebuild rather than as the same mesh.
    """

    name: str
    build: Callable[[Any], Mesh]
    kinds: tuple[str, ...]
    namespaces: tuple[OpNamespace, ...] = ()
    default_ops: tuple[MeshOp, ...] = ()
    deterministic: bool = True

    def kind_or_default(self, kind: Any) -> str:
        """The kind this ask builds, checked against what this mesher makes."""
        if kind is None:
            return self.kinds[0]
        if is_late_bound(kind):
            return kind
        if str(kind) not in self.kinds:
            raise MeshToolError(
                "MESH_KIND_UNSUPPORTED",
                f"mesher {self.name!r} makes {list(self.kinds)} meshes, not "
                f"{kind!r}.")
        return str(kind)


_MESHERS: dict[str, Mesher] = {}


def register_mesher(
    name: str,
    build: Callable[[Any], Mesh],
    *,
    kinds: Iterable[str],
    namespaces: Iterable[OpNamespace] = (),
    default_ops: Iterable[MeshOp] = (),
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
    mesher = Mesher(
        name=key, build=build, kinds=tuple(str(k) for k in kinds),
        namespaces=tuple(namespaces), default_ops=tuple(default_ops),
        deterministic=bool(deterministic))
    if not mesher.kinds:
        raise MeshToolError(
            "MESH_KIND_UNDECLARED",
            f"mesher {key!r} declares no kind of mesh it makes.")
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
