"""The DATA class body - a declared ARTIFACT per row, and what produces it.

A template writes its data as a class body, so the attribute name IS the row
name and a reference to the row is attribute access on the body
(``DATA.dem``) - a typo is an ``AttributeError`` at import rather than a string
nobody checked. Row-to-row dataflow inside the body is the plain identifier.

``tool(...)`` is the one author word for a producer. There is no fetch/build
prefix: what a runner does to the world is the REGISTRY's knowledge, not a
second place for an author to state it, and the review gate labels world-reads
from the tool's own registration.

``.resample()`` / ``.normalize()`` ride the declaration too: the cadence and the
units an artifact ARRIVES in are part of what it is, and declaring them is what
keeps consumer-side realignment from happening silently (see ``temporal.py``)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Annotated, Any, Mapping

from .errors import PlanValidationError, SuppliedGeometryError
from .plan import DataRef
from .temporal import TemporalSpec, spec_from

__all__ = [
    "CoversAOI",
    "Data",
    "DataDecl",
    "Producer",
    "SuppliedGeometry",
    "ToolWord",
    "artifact_class",
    "data_rows",
    "tool",
]


class _CoversAOI:
    """Validator sentinel: check a supplied artifact against the BOUND DOMAIN.

    What it actually asks for is that a domain is bound and has an extent when an
    artifact is supplied, so the run cannot adopt one against no modelled world at
    all. It does NOT compare the artifact's own extent to that domain: answering
    that means opening the file, and the species a slot accepts include meshes no
    reader in this process can open. An artifact that covers LESS than the
    modelled domain is therefore adopted, and the run models the smaller world it
    describes without saying so.
    """

    def __repr__(self) -> str:
        return "CoversAOI"


CoversAOI = _CoversAOI()

#: The shapes a producer-less slot can declare it accepts.
_GEOMETRIES: frozenset[str] = frozenset(
    {"point", "polyline", "polygon", "rectangle", "raster", "mesh"})

#: What a declared shape means for the KIND of artifact that can satisfy it. The
#: exact vector shape (a point layer vs a line layer) is not knowable from a file
#: name, so the shapes collapse to one vector class here.
_GEOMETRY_CLASS: Mapping[str, str] = MappingProxyType({
    "point": "vector", "polyline": "vector", "polygon": "vector",
    "rectangle": "vector", "raster": "raster", "mesh": "mesh"})

#: Which class an artifact SUFFIX belongs to. A suffix nobody lists here leaves
#: the artifact unclassifiable, and an unclassifiable artifact is adopted rather
#: than refused: this check answers what a file name can honestly answer, and a
#: refusal must never rest on a guess.
_CLASS_BY_SUFFIX: Mapping[str, str] = MappingProxyType({
    ".tif": "raster", ".tiff": "raster", ".vrt": "raster", ".img": "raster",
    ".asc": "raster", ".jp2": "raster",
    ".slf": "mesh", ".sel": "mesh", ".med": "mesh", ".2dm": "mesh",
    ".gr3": "mesh", ".msh": "mesh",
    ".fgb": "vector", ".geojson": "vector", ".json": "vector", ".shp": "vector",
    ".gpkg": "vector", ".kml": "vector", ".gml": "vector"})


@dataclass(frozen=True, slots=True)
class SuppliedGeometry:
    """The shape a context slot accepts, carried ON the generated argument's type.

    Annotation metadata rather than the type itself: a supplied artifact arrives
    as a string whichever shape it is, so the shape is what the wire has to SAY,
    not what it has to enforce. ``typing.get_type_hints`` drops it before any
    model-facing schema is built.
    """

    shape: str

    def __repr__(self) -> str:
        return f"geometry={self.shape}"


def artifact_class(value: Any) -> str | None:
    """``raster`` | ``mesh`` | ``vector`` for a supplied artifact, or ``None``.

    Read off the URI SUFFIX, because that is the only thing about a supplied
    artifact that is knowable without opening it. ``None`` means unclassifiable -
    an in-memory sketch, a bare layer handle, a suffix nobody declares - and is
    never grounds for a refusal.
    """
    uri = getattr(value, "uri", None) or (value if isinstance(value, str) else None)
    if not isinstance(uri, str):
        return None
    stem = uri.split("?", 1)[0].rstrip("/")
    dot = stem.rfind(".")
    return _CLASS_BY_SUFFIX.get(stem[dot:].lower()) if dot >= 0 else None


class _Row:
    """A row in a ``DATA`` class body: the ATTRIBUTE NAME is the row's name.

    ``__set_name__`` is how the name arrives, so a template never writes it twice,
    and ``__get__`` makes ``DATA.<row>`` the late-bound :class:`DataRef` every
    binding block and plan step already speaks. Reading the body's own attribute
    is therefore checked by Python at import: a misspelled row is an
    ``AttributeError`` at the line that wrote it.
    """

    __slots__ = ()

    #: Which field on the concrete row type holds the declared name.
    _row_attr = "row"

    def __set_name__(self, owner: type, name: str) -> None:
        declared = getattr(self, self._row_attr, "")
        if declared and declared != name:
            raise PlanValidationError(
                f"row {declared!r} is bound to a second name {name!r}: a row is one "
                "declaration in one body. Write a fresh tool(...) / Data modifier "
                "for the second row.")
        object.__setattr__(self, self._row_attr, name)

    def __get__(self, obj: Any, owner: type | None = None) -> DataRef:
        name = getattr(self, self._row_attr, "")
        if not name:
            raise PlanValidationError(
                f"{self!r} was read as a row reference but carries no row name; a "
                "reference is attribute access on the DATA body that declares it.")
        return DataRef(name)


@dataclass(frozen=True, slots=True)
class Producer(_Row):
    """How an artifact comes into being: a runner name plus its declared args.

    ``.supplied()`` supersedes the build with an artifact the caller already has;
    whether that makes sense for a given runner is the registry's knowledge, so
    the declaration surface carries no fetch/build role of its own.
    """

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    ladder_rungs: tuple["Producer", ...] = ()
    temporal: TemporalSpec | None = None
    supplied_uri: str | None = None
    supplied_validate: Any = None
    #: The DATA-body attribute name this producer was declared under.
    row: str = ""

    def __post_init__(self) -> None:
        if not self.runner:
            raise PlanValidationError("Producer declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))

    def supplied(self, uri: str | None = None, *,
                 validate: Any = CoversAOI) -> "Producer":
        """Take the artifact the caller supplied instead of building one.

        ``CoversAOI`` checks that a domain is bound before the artifact is
        adopted; it does not compare the artifact's extent to it (see
        :class:`_CoversAOI`).
        """
        return replace(self, supplied_uri=uri, supplied_validate=validate)

    def ladder(self, *rungs: "Producer") -> "Producer":
        """Declare the fallback rungs this producer degrades through, in order.

        A RUNG IS A PRODUCER, not a label: the machinery walks them, records which
        one answered, and says so out loud when the answering rung changed dataset.
        A declaration that named rungs it could not call would be a claim about a
        mechanism it does not have, which is worse than declaring nothing.
        """
        if not rungs:
            raise PlanValidationError(f"{self.runner}: .ladder() declares no rungs.")
        wrong = [r for r in rungs if not isinstance(r, Producer)]
        if wrong:
            raise PlanValidationError(
                f"{self.runner}: .ladder() takes PRODUCERS - tool(...) the "
                f"machinery can call - and was given "
                f"{type(wrong[0]).__name__} ({wrong[0]!r}). A rung the interpreter "
                "cannot call is a fallback that never fires.")
        return replace(self, ladder_rungs=self.ladder_rungs + tuple(rungs))

    def resample(self, *, to: str, method: str | None = None,
                 max_gap: str = "native*3") -> "Producer":
        """Declare the cadence this artifact is delivered at, and how it gets there.

        ``method`` unset takes the producer's quantity-class default (rates
        conservative, states linear, categorical nearest). A hole wider than
        ``max_gap`` refuses rather than being bridged.
        """
        return replace(self, temporal=spec_from(to, method, max_gap, None,
                                                self.temporal))

    def normalize(self, *, units: str) -> "Producer":
        """Declare the units this artifact is delivered in (explicit table, no guessing)."""
        return replace(self, temporal=spec_from(None, None, "native*3", units,
                                                self.temporal))


class ToolWord:
    """The ONE author word a template declares with: ``tool(...)``.

    ``tool(name, **kwargs)`` declares a DATA row's producer. No role prefix: what
    a runner does to the world - reads it, derives from what is already staged -
    is the tool REGISTRY's knowledge, and a second statement of it on the
    declaration is a place for the two to disagree.

    ``tool.build_mesh(...)`` declares the MESH ask, so a template imports one name
    and every ask it makes reads the same way. The mesh router owns the validation
    and is reached lazily, because the router is built on this library.
    """

    def __call__(self, name: str, **kwargs: Any) -> Producer:
        return Producer(runner=name, kwargs=kwargs)

    @staticmethod
    def build_mesh(**ask: Any) -> Any:
        """Declare a mesh ask -> a frozen declaration, checked at the mesh router."""
        from trid3nt_server.workflows.mesh.tool import MeshTool

        return MeshTool.build_mesh(**ask)


#: The author word itself. One object, so ``from ...lib import tool`` and
#: ``from ...mesh.tool import tool`` are the same name for the same thing.
tool = ToolWord()


@dataclass(frozen=True, slots=True)
class DataDecl(_Row):
    """A declared artifact: a name the plan Refs, and what satisfies it.

    A PRODUCER-LESS declaration (``producer=None``) is a CONTEXT SLOT: the
    template names the artifact it can use and says nothing about where it comes
    from, because naming a default fetcher for a breakwater or a clip zone is an
    opinion the question does not carry. What satisfies it arrives from outside -
    a layer the user already has, a file URI, a gate's answer - or nothing does,
    and ``.optional()`` says that absence is legal.
    """

    #: The DATA-body attribute name this row was declared under.
    name: str = ""
    producer: Producer | None = None
    #: Absence is legal. Only meaningful on a producer-less slot; a declared
    #: producer either produces or fails.
    is_optional: bool = False
    #: The GEOMETRY a producer-less slot accepts (point | polyline | polygon |
    #: rectangle | raster | mesh). Declared so the slot says what shape of thing
    #: it takes, which is the only thing a template CAN say about a context layer
    #: whose source it deliberately does not name.
    geometry: str | None = None
    #: How a supplied artifact is checked against the domain - BOUND-DOMAIN-ONLY
    #: under ``CoversAOI`` (see :class:`_CoversAOI`), which is not a coverage test.
    supplied_validate: Any = CoversAOI

    _row_attr = "name"

    def __post_init__(self) -> None:
        if self.name and not self.name.isidentifier():
            raise PlanValidationError(f"Data name {self.name!r} is not an identifier.")
        if self.is_optional and self.producer is not None:
            raise PlanValidationError(
                f"Data {self.name!r} declares a producer AND .optional(): a producer "
                "either produces the artifact or fails typed, so there is no absence "
                "for optional to describe. Drop the producer to make it a context "
                "slot, or drop .optional()."
            )

    @property
    def is_supplied(self) -> bool:
        return getattr(self.producer, "supplied_uri", None) is not None

    @property
    def producer_kwargs(self) -> Mapping[str, Any]:
        """Every read this slot's producer declares, RUNGS INCLUDED.

        A rung is a producer with its own reads, and whether a revised param
        reaches this artifact is a question about all of them - a ladder whose
        fallback consumed the value the review changed is exactly as stale as one
        whose primary did. Empty for a producer-less slot.
        """
        if self.producer is None:
            return {}
        reads = dict(self.producer.kwargs)
        for rung in self.producer.ladder_rungs:
            reads.update(rung.kwargs)
        return reads

    @property
    def wire_annotation(self) -> Any:
        """This slot's declared type on the generated tool's signature.

        A supplied artifact arrives as a uri or a layer name, so the type is a
        string whatever shape the slot takes; the declared shape rides along as
        :class:`SuppliedGeometry` metadata so the wire says what it accepts.
        """
        if self.geometry is None:
            return str | None
        return Annotated[str | None, SuppliedGeometry(self.geometry)]

    @property
    def doc_line(self) -> str:
        """What the model reads about this slot: the shape it takes, and absence.

        The slot names no source, so the shape is the only thing the prose CAN
        say - and saying it is what keeps a caller from filling a mesh slot with a
        raster because nothing told them otherwise.
        """
        shape = f"a {self.geometry} layer" if self.geometry else "a layer"
        tail = ("absent is legal and the run reports it" if self.is_optional
                else "required - the template names no source for it")
        return f"{shape} you supply, as a uri or a layer name; {tail}."

    def refuse_wrong_shape(self, value: Any) -> None:
        """Refuse a supplied artifact whose CLASS is not the shape this slot declared.

        Suffix-deep and no deeper: it separates a raster from a mesh from a vector,
        which is what a name can answer without a read. An unclassifiable artifact
        passes - the consumer's own species reader is what finally refuses a vector
        that carries the wrong geometry inside it.
        """
        if self.geometry is None:
            return
        found = artifact_class(value)
        wanted = _GEOMETRY_CLASS[self.geometry]
        if found is None or found == wanted:
            return
        raise SuppliedGeometryError(
            f"Data {self.name!r} declares geometry={self.geometry!r}, so it takes a "
            f"{wanted} artifact; what was supplied reads as {found} ({value!r}). "
            "Supply the shape the slot declares, or leave it unfilled."
        )

    def supplied(self, *, geometry: str | None = None,
                 validate: Any = CoversAOI) -> "DataDecl":
        """This slot is filled by something the caller SUPPLIES, not by a producer.

        On a producer-less slot this is the whole declaration: the template names
        the shape it accepts and nothing about where the thing comes from.
        """
        if self.producer is not None:
            raise PlanValidationError(
                f"Data {self.name!r} declares a producer AND .supplied(): a producer "
                "that can be superseded says so on the producer "
                "(tool(...).supplied(...)), not on the slot."
            )
        if geometry is not None and geometry not in _GEOMETRIES:
            raise PlanValidationError(
                f"Data {self.name!r}: .supplied(geometry={geometry!r}) is not a "
                f"declared shape (known: {sorted(_GEOMETRIES)})."
            )
        return replace(self, geometry=geometry, supplied_validate=validate)

    def optional(self) -> "DataDecl":
        """Absence is legal, and LABELLED: the run says the slot went unfilled."""
        return replace(self, is_optional=True)


#: The unfilled CONTEXT SLOT a ``DATA`` body writes its modifiers onto:
#: ``walls = Data.supplied(geometry="polyline").optional()``. Every modifier
#: returns a fresh row, so the prototype itself is never a template's row - it
#: refuses being named, which is what keeps two bodies from sharing one object.
Data = DataDecl()


def data_rows(body: Any) -> tuple[DataDecl, ...]:
    """The declared rows of a ``DATA`` class body, in CLASS-BODY ORDER.

    Order is the declaration's own, because a ladder and a chain both read down
    the body. A row-to-row reference written as a plain identifier inside the body
    is the producer OBJECT, which by now knows its own name, so it is rewritten
    here into the same late-bound :class:`DataRef` an out-of-body ``DATA.<row>``
    yields - one shape reaches the validator and the binder.
    """
    if isinstance(body, (list, tuple)):
        return tuple(body)
    rows: list[DataDecl] = []
    for name, value in vars(body).items():
        if isinstance(value, Producer):
            rows.append(DataDecl(name=name, producer=_bound_producer(value)))
        elif isinstance(value, DataDecl):
            rows.append(replace(value, name=name))
    return tuple(rows)


def _bound_producer(producer: Producer) -> Producer:
    """``producer`` with its own reads - and every rung's - resolved to row refs."""
    return replace(producer, kwargs=_row_refs(producer.kwargs),
                   ladder_rungs=tuple(_bound_producer(r)
                                      for r in producer.ladder_rungs))


def _row_refs(value: Any) -> Any:
    """A declared value with every in-body row identifier turned into its ref."""
    if isinstance(value, Producer):
        if not value.row:
            raise PlanValidationError(
                f"a producer for {value.runner!r} is read by another row but is not "
                "declared as one: give it a name in the DATA body.")
        return DataRef(value.row)
    if isinstance(value, Mapping):
        return {k: _row_refs(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return type(value)(_row_refs(v) for v in value)
    return value
