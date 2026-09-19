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
    "BED",
    "DISCHARGE",
    "EXTENT",
    "LEVEL",
    "LINE",
    "OBSERVATION",
    "RUNS",
    "CoversAOI",
    "DOMAIN",
    "Data",
    "DataDecl",
    "Producer",
    "SuppliedGeometry",
    "ToolWord",
    "artifact_class",
    "data_rows",
    "tool",
]

#: The three ENGINE-NEUTRAL slots a solved run stands on, named as ROLES rather
#: than as rows: the closed polygon the equations are solved over, the elevation
#: every node of it carries, and the named stretches of its edge. A raster engine
#: fills the same three with a grid, so no word here belongs to an engine.
DOMAIN = "domain"
BED = "bed"
RUNS = "runs"

#: The LINE a placed read is measured along. Not one of the three - a run solves
#: without it - but a slot for the same reason: a producer's own centerline, a
#: drawn polyline and a line layer all fill it and read the same afterwards.
LINE = "line"

#: The slot a run OPENS ON: one measured value read off whatever reports it near
#: this domain, in the unit the keyword reads. Engine-neutral for the same reason
#: the three above are - somebody measured something somewhere at some time - and
#: the value it yields is reachable as ``Ref("<row>.value")``.
OBSERVATION = "observation"

#: The two observations a solve READS BY ROLE rather than by name: the elevation
#: the water surface stands at, and the flow an inflow run carries. Both are
#: observations and ingest as one; they are their own roles because the workflow
#: has to know which row is which to build the stages a body of water needs.
LEVEL = "level"
DISCHARGE = "discharge"

#: The RECTANGLE a question is asked inside: the window a domain is cut out of,
#: the grid a raster engine solves on. Not a domain - it has no shoreline - so it
#: is its own role, and the canvas offers a box for it.
EXTENT = "extent"


# A ROW NAMES ITS PRODUCER; RETRIEVAL NEVER PICKS ONE. What a run stands on is
# DECLARED here, by name, with its ladder and its transforms - it is never
# resolved by ranking a phrase against a catalog. Text relevance cannot judge the
# facts that decide whether a source can carry a solve: its resolution, its CRS,
# its coverage over this domain, its datum. A retrieved source that reads
# plausibly and resolves wrong produces a run that completes and answers a
# different question, which is the failure a declaration exists to make
# impossible. Search is how a MODEL finds a tool to call; a template's inputs are
# the template's own statement.

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
    #: Which engine-neutral SLOT this row is, or ``""`` for a plain row. A slot
    #: is filled the same way whatever fills it - a drawing, the user's layer, or
    #: a producer - and nothing downstream branches on which.
    role: str = ""
    #: This row is CONTEXT: it names a producer, and its absence continues the
    #: run under the sentence below rather than refusing.
    is_context: bool = False
    #: What the sheet says when a context row came back empty. Stated by the
    #: template in its own words about what is not there.
    absent_note: str = ""
    #: What this slot's ingestion is told about the value it is handed - the
    #: point a nearest-site query ranks against, the unit the keyword reads.
    #: Declared on the row because only the row knows them; a late-bound read
    #: here is bound before the ingestion runs, like a producer's own kwargs.
    coercion: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

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
                "slot, or declare it .context(), which states what the run says "
                "when the source is empty."
            )
        if self.is_context and self.producer is None:
            raise PlanValidationError(
                f"Data {self.name!r} declares .context() with no producer: a context "
                "row is a PRODUCER whose absence is legal, and a row with no "
                "producer already says absence with .optional()."
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
        if self.role in (OBSERVATION, LEVEL, DISCHARGE):
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
                       if self.producer is not None else "."))
        if self.role == RUNS:
            return ("the stretches of the domain's edge that carry a boundary "
                    "condition - each two points on the edge and a type "
                    "(inflow, outflow, open); a closed body states none")
        if self.role == BED:
            return ("what the domain's nodes carry for elevation: a DEM, a "
                    "bathymetry or survey raster, a layer of soundings, or a "
                    "depth in metres below the free surface"
                    + ("; unfilled, the template's own producer supplies it."
                       if self.producer is not None else "."))
        if self.role == OBSERVATION:
            return (f"{self.coercion.get('measures') or 'the value'} this run "
                    "opens on: a layer of sites that report it, or the number "
                    "itself"
                    + (f" in {self.coercion['to_units']}"
                       if self.coercion.get("to_units") else "")
                    + ("; unfilled, the template's own producer looks for one."
                       if self.producer is not None else "."))
        shape = f"a {self.geometry} layer" if self.geometry else "a layer"
        if self.producer is not None:
            tail = "unfilled, the template's own producer fetches one"
        elif self.is_optional:
            tail = "absent is legal and the run reports it"
        else:
            tail = "required - the template names no source for it"
        return f"{shape} you supply, as a uri or a layer name; {tail}."

    def refuse_wrong_shape(self, value: Any) -> None:
        """Refuse a supplied artifact whose CLASS is not the shape this slot declared.

        Suffix-deep and no deeper; an unclassifiable artifact passes."""
        if self.role == OBSERVATION:
            # A reading arrives as a record OR as the number itself, and a
            # number has no artifact class to judge.
            return
        if self.role == BED:
            # A bed takes every class a survey arrives in EXCEPT a mesh: a
            # solved domain is not an elevation source, and adopting one would
            # paint the nodes from something nobody measured the ground with.
            if artifact_class(value) == "mesh":
                raise SuppliedGeometryError(
                    f"Data {self.name!r} is the BED slot: it takes a raster "
                    "surface, a layer of soundings or a depth in metres, and "
                    f"what was supplied reads as a mesh ({value!r}).")
            return
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

    def context(self, absent: str = "") -> "DataDecl":
        """This producer row is CONTEXT: its absence continues the run.

        ``absent`` is the sentence the sheet carries when the source held
        nothing near this domain; unstated, the row's own name says it."""
        return replace(self, is_context=True, absent_note=str(absent or ""))

    def domain(self, producer: Producer | None = None) -> "DataDecl":
        """THE DOMAIN: the closed polygon the equations are solved over.

        Geometry only, and one slot however it is filled - drawn on the canvas,
        the user's own layer, or the ``producer`` this question prefers."""
        row = self if producer is None else self(producer)
        return replace(row, role=DOMAIN, geometry="polygon")

    def runs(self, producer: Producer | None = None) -> "DataDecl":
        """THE BOUNDARY RUNS: named stretches of the domain's edge.

        Zero or more, each two points on the edge and a type; a closed body
        states none and its mesh has only walls. Filled by the user's drawing,
        the user's layer, or the domain producer that measured the edge."""
        row = self if producer is None else self(producer)
        return replace(row, role=RUNS, geometry="polyline", is_optional=True)

    def line(self, producer: Producer | None = None) -> "DataDecl":
        """THE LINE a placed read is measured along.

        Unfilled, the domain's own producer answers with the centerline it
        measured beside the polygon; a body that has none is asked for a line on
        the canvas, which is what a profile across a lake is."""
        row = self if producer is None else self(producer)
        return replace(row, role=LINE, geometry="polyline")

    def observation(self, producer: Producer | None = None, *, near: Any = None,
                    units: Any = None, measures: str = "this value",
                    opens: str = "", value_field: str = "value") -> "DataDecl":
        """ONE MEASURED VALUE this run opens on, in the unit the keyword reads.

        ``near`` is the point the nearest reporting site is chosen against,
        ``value_field`` the property the source reports it under, ``measures``
        what the row is looking for and ``opens`` what the run journal says it
        opened on; a number supplied here stands over any record."""
        return self._observed(OBSERVATION, producer, near, units, measures,
                              opens, value_field)

    def level(self, producer: Producer | None = None, *, near: Any = None,
              units: Any = "m", measures: str = "a water-surface elevation",
              opens: str = "the water opens at",
              value_field: str = "value") -> "DataDecl":
        """THE LEVEL the water surface stands at: an observation, read by ROLE.

        An ELEVATION on the datum the bed is painted on - never a height above a
        gauge's own zero, which is a different number about a different surface.
        The run opens flat at it, and a body whose bed is stated as a depth needs
        none: that bed is counted from the free surface itself."""
        return self._observed(LEVEL, producer, near, units, measures, opens,
                              value_field)

    def discharge(self, producer: Producer | None = None, *, near: Any = None,
                  units: Any = None, measures: str = "a streamflow",
                  opens: str = "the inflow run carries",
                  value_field: str = "value") -> "DataDecl":
        """THE FLOW an inflow run carries: an observation, read by ROLE.

        A domain whose edge names runs and whose rows carry a discharge is an
        OPEN CHANNEL, and the workflow adds the step that measures one."""
        return self._observed(DISCHARGE, producer, near, units, measures, opens,
                              value_field)

    def _observed(self, role: str, producer: Producer | None, near: Any,
                  units: Any, measures: str, opens: str,
                  value_field: str) -> "DataDecl":
        """One measured value, under the role its consumer reads it by."""
        row = self if producer is None else self(producer)
        return replace(row, role=role, coercion=MappingProxyType(
            {"near": near, "to_units": units, "measures": measures,
             "opens": opens, "field": value_field}))

    def extent(self, producer: Producer | None = None) -> "DataDecl":
        """THE EXTENT: one lon/lat rectangle, however the caller names it.

        A box picked on the canvas, four numbers, a place or a layer's bounds all
        read the same afterwards, as ``Ref("<row>.bbox")``."""
        row = self if producer is None else self(producer)
        return replace(row, role=EXTENT, geometry="rectangle")

    def bed(self, producer: Producer | None = None) -> "DataDecl":
        """THE BED: what every node of the domain carries for elevation.

        A DEM, a bathymetry or survey raster, a layer of soundings, or a stated
        depth below the free surface - ONE source, and the mesh records which
        source actually painted each node. A measurement that covers part of the
        domain is laid over the wider surface under it by the merge derive, in
        the DATA body, and this slot takes the row that derive produced."""
        row = self if producer is None else self(producer)
        return replace(row, role=BED)

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
