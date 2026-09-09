"""The DATA class body - a declared ARTIFACT per row, and what produces it.

The attribute name IS the row name, so ``DATA.dem`` is attribute access and a typo
is an ``AttributeError`` at import. ``tool(...)`` is the one producer word.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Annotated, Any, Mapping

from .errors import PlanValidationError, SuppliedGeometryError
from .plan import DataRef, Row, body_rows
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
    """Validator sentinel: a domain must be BOUND and have an extent before a
    supplied artifact is adopted. The artifact's own extent is never read, so one
    covering LESS than the modelled domain is adopted without saying so."""

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
    Annotation metadata, not the type: ``typing.get_type_hints`` drops it before
    any model-facing schema is built."""

    shape: str

    def __repr__(self) -> str:
        return f"geometry={self.shape}"


def artifact_class(value: Any) -> str | None:
    """``raster`` | ``mesh`` | ``vector`` for a supplied artifact, or ``None``.
    Read off the URI SUFFIX only. ``None`` means unclassifiable and is never
    grounds for a refusal."""
    uri = getattr(value, "uri", None) or (value if isinstance(value, str) else None)
    if not isinstance(uri, str):
        return None
    stem = uri.split("?", 1)[0].rstrip("/")
    dot = stem.rfind(".")
    return _CLASS_BY_SUFFIX.get(stem[dot:].lower()) if dot >= 0 else None


@dataclass(frozen=True, slots=True)
class Producer(Row):
    """How an artifact comes into being: a runner name plus its declared args.

    ``.supplied()`` supersedes the build with an artifact the caller already has."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    ladder_rungs: tuple["Producer", ...] = ()
    temporal: TemporalSpec | None = None
    supplied_uri: str | None = None
    supplied_validate: Any = None
    #: The DATA-body attribute name this producer was declared under.
    row: str = ""

    _ref_type = DataRef

    def __post_init__(self) -> None:
        if not self.runner:
            raise PlanValidationError("Producer declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))

    def supplied(self, uri: str | None = None, *,
                 validate: Any = CoversAOI) -> "Producer":
        """Take the artifact the caller supplied instead of building one.

        ``CoversAOI`` checks only that a domain is bound, never the extent."""
        return replace(self, supplied_uri=uri, supplied_validate=validate)

    def ladder(self, *rungs: "Producer") -> "Producer":
        """Declare the fallback rungs this producer degrades through, in order.
        A RUNG IS A PRODUCER, not a label: the machinery walks them, records which
        one answered, and says so when the answering rung changed dataset."""
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
        ``method`` unset takes the quantity-class default; a hole wider than
        ``max_gap`` refuses rather than being bridged."""
        return replace(self, temporal=spec_from(to, method, max_gap, None,
                                                self.temporal))

    def normalize(self, *, units: str) -> "Producer":
        """Declare the units this artifact is delivered in (explicit table, no guessing)."""
        return replace(self, temporal=spec_from(None, None, "native*3", units,
                                                self.temporal))


class ToolWord:
    """The ONE author word a template declares with: ``tool(name, **kwargs)`` for a
    DATA row's producer, ``tool.build_mesh(...)`` for the mesh ask. No role prefix:
    what a runner does to the world is the tool REGISTRY's knowledge."""

    def __call__(self, name: str, **kwargs: Any) -> Producer:
        return Producer(runner=name, kwargs=kwargs)

    @staticmethod
    def build_mesh(**ask: Any) -> Any:
        """Declare a mesh ask -> a frozen declaration, checked at the mesh router."""
        from trid3nt_server.workflows.mesh.tool import MeshTool

        return MeshTool.build_mesh(**ask)


#: The author word itself. One object, so ``from ...runtime import tool`` and
#: ``from ...mesh.tool import tool`` are the same name for the same thing.
tool = ToolWord()


@dataclass(frozen=True, slots=True)
class DataDecl(Row):
    """A declared artifact: a name the plan Refs, and what satisfies it.
    A PRODUCER-LESS declaration is a CONTEXT SLOT - the artifact is named, its source
    is not; what fills it comes from outside, or ``.optional()`` allows an absence."""

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
    _ref_type = DataRef

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

        Empty for a producer-less slot."""
        if self.producer is None:
            return {}
        reads = dict(self.producer.kwargs)
        for rung in self.producer.ladder_rungs:
            reads.update(rung.kwargs)
        return reads

    @property
    def wire_annotation(self) -> Any:
        """This slot's declared type on the generated tool's signature.
        Always a string: the declared shape rides along as :class:`SuppliedGeometry`
        metadata rather than narrowing the type."""
        if self.geometry is None:
            return str | None
        return Annotated[str | None, SuppliedGeometry(self.geometry)]

    @property
    def doc_line(self) -> str:
        """What the model reads about this slot: the shape it takes, and whether
        absence is legal. The slot names no source, so the shape is all it can say."""
        shape = f"a {self.geometry} layer" if self.geometry else "a layer"
        tail = ("absent is legal and the run reports it" if self.is_optional
                else "required - the template names no source for it")
        return f"{shape} you supply, as a uri or a layer name; {tail}."

    def refuse_wrong_shape(self, value: Any) -> None:
        """Refuse a supplied artifact whose CLASS is not the shape this slot declared.

        Suffix-deep and no deeper; an unclassifiable artifact passes."""
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
        On a producer-less slot this is the whole declaration: the shape it
        accepts, and nothing about where the thing comes from."""
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


#: The unfilled CONTEXT SLOT a ``DATA`` body writes its modifiers onto. Every
#: modifier returns a fresh row, so the prototype itself is never a template's
#: row and two bodies can never share one object.
Data = DataDecl()


def data_rows(body: Any) -> tuple[DataDecl, ...]:
    """The declared rows of a ``DATA`` class body, in CLASS-BODY ORDER.
    An in-body row-to-row identifier is rewritten into the same late-bound
    :class:`DataRef` an out-of-body ``DATA.<row>`` yields."""
    rows: list[DataDecl] = []
    for value in body_rows(body, (Producer, DataDecl)):
        if isinstance(value, Producer):
            rows.append(DataDecl(name=value.row, producer=_bound_producer(value)))
        else:
            rows.append(value)
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
