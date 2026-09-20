"""The DATA class body - a declared ARTIFACT per row, and what produces it.

The attribute name IS the row name, so ``DATA.dem`` is attribute access and a typo
is an ``AttributeError`` at import. ``tool(...)`` is the one producer word.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Annotated, Any, Mapping

from trid3nt_contracts.coverage import DATA_CLASSES

from .errors import PlanValidationError, SuppliedGeometryError
from .plan import DataRef, Ref, Row, body_rows

__all__ = [
    "BED",
    "DISCHARGE",
    "EXTENT",
    "LEVEL",
    "LINE",
    "OBSERVE",
    "WAVE",
    "WEATHER",
    "CoversAOI",
    "DOMAIN",
    "Data",
    "DataDecl",
    "Producer",
    "SuppliedGeometry",
    "ToolWord",
    "artifact_class",
    "data_rows",
    "shapes_of",
    "tool",
]

#: THE RESERVED ROW NAMES. A row's NAME is its slot: these are the names
#: :data:`trid3nt_server.inputs.slots.SLOTS` keys the role behaviour off, spelled
#: here so the runtime can compare against them without importing the ingestions.
#: The two a solved run stands on are engine-neutral - the closed polygon the
#: equations are solved over and the elevation every node of it carries - and a
#: raster engine fills the same two with a grid, so no word here belongs to an
#: engine. The named stretches of the edge are no row: they ride on the polygon
#: the domain arrived as.
DOMAIN = "domain"
BED = "bed"

#: The LINE a placed read is measured along. Not one of the three - a run solves
#: without it - but a slot for the same reason: a producer's own centerline, a
#: drawn polyline and a line layer all fill it and read the same afterwards.
LINE = "line"

#: The slot a run OPENS ON: one measured value read off whatever reports it near
#: this domain, in the unit the keyword the role fills reads. Engine-neutral for
#: the same reason the three above are - somebody measured something somewhere at
#: some time - and the value it yields is reachable as ``Ref("observe.value")``.
OBSERVE = "observe"

#: The two observations a solve READS BY ROLE rather than by name: the elevation
#: the water surface stands at, and the flow an inflow run carries. Both are
#: observations and ingest as one; they are their own names because the workflow
#: has to know which row is which to build the stages a body of water needs.
LEVEL = "level"
DISCHARGE = "discharge"

#: The RECTANGLE a question is asked inside: the window a domain is cut out of,
#: the grid a raster engine solves on. Not a domain - it has no shoreline - so it
#: is its own name, and the canvas offers a box for it.
EXTENT = "extent"

#: The record of the air over the domain, read by the composite that puts it on
#: the run's own clock. Nothing ingests it on the way in; the name is reserved so
#: a row that carries weather is the row that composite reads.
WEATHER = "weather"

#: The sea state at the open edge: the same kind of thing as the weather over a
#: domain - one record, several columns, measured somewhere near - so it is its
#: own name too, and its ingestion turns those columns into the keywords a
#: spectral deck forces its boundary at.
WAVE = "wave"


# A ROW STATES THE CLASS IT NEEDS; IT NEVER NAMES A FETCHER. Text relevance
# cannot judge the facts that decide whether a source can carry a solve - its
# cell, its coverage over this domain, its window, its datum - and a NAMED
# fetcher is a question frozen against one source that may hold nothing here. So
# a runtime row states a need and the match reads those facts off every
# fetcher's own coverage row. Search is how a MODEL finds a tool to call; a slot
# is filled from declarations.

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


def shapes_of(data_class: str) -> frozenset[str]:
    """The artifact CLASSES one data class arrives in, off the sources that
    measure it.

    Never restated: which shapes a class is published in is the coverage rows'
    own statement, so a source added in a new shape widens what a slot of that
    class accepts without a second list moving."""
    from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY
    from trid3nt_server.tools.search.match import sources_with_coverage

    return frozenset(
        str(_SPEC_REGISTRY[name].output.layer_type)
        for name, row in sources_with_coverage()
        if row.data_class == str(data_class) and name in _SPEC_REGISTRY)


@dataclass(frozen=True, slots=True)
class Producer(Row):
    """How an artifact comes into being: a runner name plus its declared args.

    ``.supplied()`` supersedes the build with an artifact the caller already has."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    supplied_uri: str | None = None
    supplied_validate: Any = None
    #: Marked ``.supplied()``: the caller's own artifact stands in place of the
    #: build, whether or not the mark baked a uri in with it.
    is_supplied: bool = False
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
        return replace(self, supplied_uri=uri, supplied_validate=validate,
                       is_supplied=True)


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
    #: This row is CONTEXT: it names a producer, and its absence continues the
    #: run under the sentence below rather than refusing.
    is_context: bool = False
    #: What the sheet says when a context row came back empty. Stated by the
    #: template in its own words about what is not there.
    absent_note: str = ""
    #: THE NEED this slot states instead of naming a producer: one class of the
    #: coarse vocabulary, which the match filters every fetcher's coverage row
    #: against. The place, the window and the frame are the RUN's and are read
    #: off it, so a question states none of them.
    data_class: str = ""
    #: WHAT OF ITS CLASS this row asks for, by the name the source publishes it
    #: under. On a slot that reads a RECORD it is the variable the run itself
    #: publishes - a measurement and the thing it measures are comparable in one
    #: unit, so the record is read in that variable's, off the run's published
    #: table and never off the row. On every other slot it is the FEATURE asked
    #: for. Empty where any of the class will do. A :class:`Ref` where the
    #: feature is the CALLER'S - one question asked of two kinds of water - and
    #: it is bound at run time the way the point the row is asked at is.
    observes: str | Ref = ""
    #: HOW FAR this question's domain reaches, in kilometres: the one opinion a
    #: question has about its own extent. Generic, because every source calls it
    #: something else - the coverage row's ``ask`` block maps it to the param
    #: the source states it in.
    span_km: float | None = None
    #: What this slot's ingestion is told about the value it is handed - the
    #: point a nearest-site query ranks against, the unit the keyword reads.
    #: Declared on the row because only the row knows them; a late-bound read
    #: here is bound before the ingestion runs, like a producer's own kwargs.
    coercion: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    _row_attr = "name"
    _ref_type = DataRef

    @property
    def role(self) -> str:
        """The engine-neutral SLOT this row is, or ``""`` for a plain row.

        THE ROW'S NAME IS ITS SLOT: a reserved name plays its role however it is
        filled - a drawing, the user's layer, or the match - and nothing
        downstream branches on which."""
        from trid3nt_server.inputs.slots import role_of

        return role_of(self.name)

    def __post_init__(self) -> None:
        if self.name and not self.name.isidentifier():
            raise PlanValidationError(f"Data name {self.name!r} is not an identifier.")
        if self.is_optional and self.producer is not None:
            raise PlanValidationError(
                f"Data {self.name!r} declares a producer AND .optional(): a producer "
                "either produces the artifact or fails typed, so there is no absence "
                "for optional to describe. Drop the producer to make it a context "
                "slot, or declare it .context(), which states what the run says "
                "when the source is empty."
            )
        if self.data_class and self.producer is not None:
            raise PlanValidationError(
                f"Data {self.name!r} names a producer AND states "
                f"need={self.data_class!r}: a row is satisfied one way. Drop the "
                "producer to state a need.")
        if self.data_class and self.data_class not in DATA_CLASSES:
            raise PlanValidationError(
                f"Data {self.name!r} states need={self.data_class!r}, which is "
                f"not one of the classes a coverage row is written in "
                f"({', '.join(DATA_CLASSES)}).")
        if self.observes and not self.data_class:
            raise PlanValidationError(
                f"Data {self.name!r} states of={self.observes!r} and needs no "
                "class: of= names what OF a class this row asks the world for, "
                "so a row that goes out for nothing asks for nothing.")
        if self.span_km is not None and not self.data_class:
            raise PlanValidationError(
                f"Data {self.name!r} states span_km and needs no class: how far "
                "a question reaches is what the match asks a source for, and a "
                "row that names its own producer states the reach on the call.")
        if self.is_context and not (self.producer is not None or self.data_class):
            raise PlanValidationError(
                f"Data {self.name!r} declares .context() and states neither a need "
                "nor a producer: a context row is a row that goes out and looks, "
                "whose absence is legal. A row nothing goes out for already says "
                "absence with .optional()."
            )

    @property
    def is_supplied(self) -> bool:
        return bool(getattr(self.producer, "is_supplied", False))

    @property
    def fills_from_user(self) -> bool:
        """Is this row on the WIRE for a caller to fill?

        Every producer-less row is, and so is a SLOT that names a producer: a
        drawn domain or a surveyed bed supersedes the fetcher the template
        preferred, and the run reads one value either way. A producer marked
        ``.supplied()`` is on the wire too - the mark says the caller's own
        artifact stands in place of the build, and only the caller can hand
        that in."""
        return self.producer is None or bool(self.role) or self.is_supplied

    @property
    def producer_kwargs(self) -> Mapping[str, Any]:
        """Every read this slot's producer declares; empty for a producer-less
        slot."""
        return {} if self.producer is None else dict(self.producer.kwargs)

    @property
    def wire_annotation(self) -> Any:
        """This slot's declared type on the generated tool's signature.
        Always a string: the declared shape rides along as :class:`SuppliedGeometry`
        metadata rather than narrowing the type."""
        if self.role in (OBSERVE, LEVEL, DISCHARGE):
            # A reading is a record to read it off, or the number itself: a user
            # who knows what the water opens at states it and it stands.
            return str | float | None
        if self.role == BED:
            # A bed is a surface, a survey, OR a depth in metres: a schema that
            # advertised only a layer name would refuse the pond the user can
            # describe in one number.
            return str | float | None
        if self.geometry is None:
            return str | None
        return Annotated[str | None, SuppliedGeometry(self.geometry)]

    @property
    def doc_line(self) -> str:
        """What the model reads about this slot: the shape it takes, and whether
        absence is legal. A plain slot names no source, so the shape is all it can
        say; a SLOT that names a producer says what standing a supplied value has."""
        if self.role == DOMAIN:
            return ("the closed polygon this run solves over, as a uri, a layer "
                    "name or a drawn shape"
                    + ("; unfilled, the template's own producer finds one."
                       if self.producer is not None else self._unfilled))
        if self.role == BED:
            return ("what the domain's nodes carry for elevation: a DEM, a "
                    "bathymetry or survey raster, a layer of soundings, or a "
                    "depth in metres below the free surface"
                    + ("; unfilled, the template's own producer supplies it."
                       if self.producer is not None else self._unfilled))
        if self.role in (OBSERVE, LEVEL, DISCHARGE):
            return (f"the {self.data_class or 'value'} this run opens on: a "
                    "layer of sites that report it, or the number itself"
                    + ("; unfilled, the template's own producer looks for one."
                       if self.producer is not None else self._unfilled))
        if self.data_class and self.producer is None:
            # A ROW THAT STATES A NEED NAMES NO SOURCE AND IS NOT UNSOURCED: the
            # match fills it, and what the caller hands in supersedes that.
            return (f"the {self.data_class} this run reads, as a uri or a layer "
                    "name" + self._unfilled)
        shape = f"a {self.geometry} layer" if self.geometry else "a layer"
        if self.producer is not None:
            tail = "unfilled, the template's own producer fetches one"
        elif self.is_optional:
            tail = "absent is legal and the run reports it"
        else:
            tail = "required - the template names no source for it"
        return f"{shape} you supply, as a uri or a layer name; {tail}."

    @property
    def _unfilled(self) -> str:
        """What a slot with no producer says about how it fills itself."""
        return (f"; unfilled, the run matches a source of {self.data_class}."
                if self.data_class else ".")

    def refuse_wrong_shape(self, value: Any) -> None:
        """Refuse a supplied artifact whose CLASS is not the shape this slot declared.

        Suffix-deep and no deeper; an unclassifiable artifact passes."""
        if self.data_class:
            # THE SUPPLIED TWIN OF A CLASS takes every shape that class is
            # measured in - a bathymetry handed in as a raster and one handed in
            # as soundings are both bathymetry - so the shapes are the ones the
            # sources of this class publish and no slot restates them. A value
            # no shape reads out of is the number itself, which every
            # observation and every bed also takes.
            found = artifact_class(value)
            shapes = shapes_of(self.data_class)
            if found is None or not shapes or found in shapes:
                return
            raise SuppliedGeometryError(
                f"Data {self.name!r} needs {self.data_class}, which is measured "
                f"as {' or '.join(sorted(shapes))}; what was supplied reads as "
                f"{found} ({value!r}).")
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

    def __call__(self, producer: Producer) -> "DataDecl":
        """This row, produced by ``producer``: what ``Data(tool(...))`` declares.

        The row shape a modifier is written on - ``Data(tool(...)).context()`` -
        where a bare ``tool(...)`` row has nothing to write one on."""
        if not isinstance(producer, Producer):
            raise PlanValidationError(
                f"Data(...) takes a producer - tool(name, **kwargs) - and was "
                f"given {type(producer).__name__} ({producer!r}).")
        if self.producer is not None:
            raise PlanValidationError(
                f"Data {self.name!r} already names a producer; a row is produced "
                "one way.")
        return replace(self, producer=producer)

    def need(self, data_class: str, *, at: Any = None, of: Any = "",
             span_km: float | None = None, geometry: str | None = None,
             ) -> "DataDecl":
        """THE CLASS this row needs, which the match fills from whatever measures
        it here.

        The slot is the row's NAME. The window and the frame are the RUN's and
        are read off it, so a question states neither; ``at`` is the POINT this
        row is asked at where the domain's own centre is not it - the seed a
        reach is cut from, the place the nearest reporting site is ranked
        against. ``of`` names WHAT OF THE CLASS is asked for - the variable a
        record is read for, the feature a map is read for - and a source
        publishing none of it leaves the match's list; where the feature is the
        caller's rather than the question's it is a ``Ref`` to the param that
        settles it, bound before the match runs the way ``at`` is;
        ``span_km`` is how far the question reaches,
        which the answering source's coverage row maps to its own param.
        ``geometry`` is the SHAPE this row is read as - a class measured in more
        than one shape has sources publishing each, and a step that cuts a box
        with a line cannot be handed a polygon."""
        if geometry is not None and geometry not in _GEOMETRIES:
            raise PlanValidationError(
                f"Data {self.name!r}: .need(geometry={geometry!r}) is not a "
                f"declared shape; the shapes are {sorted(_GEOMETRIES)}.")
        return replace(self, data_class=str(data_class),
                       observes=of if isinstance(of, Ref) else str(of),
                       geometry=geometry,
                       span_km=None if span_km is None else float(span_km),
                       coercion=MappingProxyType({"near": at}))

    def context(self, absent: str = "") -> "DataDecl":
        """This row is CONTEXT: its absence continues the run.

        ``absent`` is the sentence the sheet carries when nothing measured this
        near this domain; unstated, the row's own name says it."""
        return replace(self, is_context=True, absent_note=str(absent or ""))

    @property
    def context_sentence(self) -> str:
        """What the run says when this context row's source held nothing."""
        if self.absent_note:
            return self.absent_note
        return (f"no {self.name.replace('_', ' ')} near this domain; the stated "
                "value stands")


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
            # A row written as ``Data(tool(...))`` carries its producer on the
            # DECLARATION, and its reads of sibling rows - on the producer and on
            # what its slot is TOLD, which names a sibling the same way - are
            # bound here for the same reason a bare producer row's are.
            rows.append(replace(
                value,
                producer=(None if value.producer is None
                          else _bound_producer(value.producer)),
                coercion=MappingProxyType(_row_refs(dict(value.coercion)))))
    return tuple(rows)


def _bound_producer(producer: Producer) -> Producer:
    """``producer`` with its own reads resolved to row refs."""
    return replace(producer, kwargs=_row_refs(producer.kwargs))


def _row_refs(value: Any) -> Any:
    """A declared value with every in-body row identifier turned into its ref."""
    if isinstance(value, Producer):
        if not value.row:
            raise PlanValidationError(
                f"a producer for {value.runner!r} is read by another row but is not "
                "declared as one: give it a name in the DATA body.")
        return DataRef(value.row)
    if isinstance(value, DataDecl):
        if not value.name:
            raise PlanValidationError(
                "a context slot is read by another row but is not declared as one: "
                "give it a name in the DATA body.")
        return DataRef(value.name)
    if isinstance(value, Mapping):
        return {k: _row_refs(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return type(value)(_row_refs(v) for v in value)
    return value
