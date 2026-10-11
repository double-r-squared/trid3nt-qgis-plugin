"""The DATA class body - a declared ARTIFACT per row, and what produces it.

The attribute name IS the row name, so ``DATA.dem`` is attribute access and a typo
is an ``AttributeError`` at import. ``tool(...)`` is the one producer word.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from trid3nt_contracts.coverage import DATA_CLASSES

from trid3nt_server.inputs.geometry import artifact_class
from trid3nt_server.inputs.slots import BED, DOMAIN, MESH, Slot

from .errors import DeclarationError, SuppliedGeometryError
from .params import declarations, take_name

__all__ = [
    "Data",
    "DataDecl",
    "Producer",
    "ToolWord",
    "data_rows",
    "shapes_of",
    "tool",
]

# A row states the class it needs, never a fetcher: the match reads cell, coverage, window and datum
# off every fetcher's own row, which a named fetcher would freeze against one source.

_GEOMETRIES: frozenset[str] = frozenset(
    {"point", "polyline", "polygon", "rectangle", "raster"})

#: Vector shapes collapse to one class: a file name cannot tell a point layer from a line layer.
_GEOMETRY_CLASS: Mapping[str, str] = MappingProxyType({
    "point": "vector", "polyline": "vector", "polygon": "vector",
    "rectangle": "vector", "raster": "raster"})

def shapes_of(data_class: str) -> frozenset[str]:
    """The artifact CLASSES one data class arrives in, off the sources that measure it.
    Read from the rows, so a source published in a new shape widens what a slot accepts."""
    from trid3nt_server.tools.fetchers._router.registration import _SPEC_REGISTRY
    from trid3nt_server.tools.search.match import sources_with_coverage

    return frozenset(
        str(_SPEC_REGISTRY[name].output.layer_type)
        for name, row in sources_with_coverage()
        if row.data_class == str(data_class) and name in _SPEC_REGISTRY)


@dataclass(frozen=True, slots=True)
class Producer:
    """How an artifact comes into being: a runner name plus its declared args."""

    runner: str
    kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    row: str = ""

    def __set_name__(self, owner: type, name: str) -> None:
        take_name(self, "row", name)

    def __post_init__(self) -> None:
        if not self.runner:
            raise DeclarationError("Producer declares no runner path.")
        object.__setattr__(self, "kwargs", MappingProxyType(dict(self.kwargs)))


class ToolWord:
    """The ONE author word a template declares with: ``tool(name, **kwargs)`` for a
    DATA row's producer, ``tool.build_mesh(...)`` for the mesh ask. No role prefix:
    what a runner does to the world is the tool REGISTRY's knowledge."""

    def __call__(self, name: str, **kwargs: Any) -> Producer:
        return Producer(runner=name, kwargs=kwargs)

    @staticmethod
    def build_mesh(**ask: Any) -> Any:
        """Declare a mesh ask -> a frozen declaration, checked at the mesh router."""
        from trid3nt_server.tools.mesh.tool import MeshTool

        return MeshTool.build_mesh(**ask)


#: One object, so every import path of ``tool`` is the same name.
tool = ToolWord()


@dataclass(frozen=True, slots=True)
class DataDecl:
    """A declared artifact: a name a template reads, and what satisfies it.
    A PRODUCER-LESS declaration is a CONTEXT SLOT - the artifact is named, its source
    is not; what fills it comes from outside, or ``.optional()`` allows an absence."""

    name: str = ""
    producer: Producer | None = None
    #: Absence is legal only on a producer-less slot; a producer either produces or fails.
    is_optional: bool = False
    #: The TYPE this row states - the slot whose ingestion reads its value; ``None`` is a plain row, read by whatever declared it.
    slot: Slot | None = None
    #: The geometry a producer-less slot accepts (point | polyline | polygon | rectangle | raster).
    geometry: str | None = None
    #: CONTEXT row: it names a producer, and its absence continues the run under the sentence below.
    is_context: bool = False
    #: What the sheet says when a context row came back empty.
    absent_note: str = ""
    #: The class of the coarse vocabulary this slot needs instead of naming a producer; place, window and frame are the run's.
    data_class: str = ""
    #: What of the class is asked for, by the name the source publishes it under; empty where any will do.
    observes: str = ""
    #: The domain's own kind, stated only where the seed cannot imply it; a stated kind wins.
    kind: str = ""
    #: How far the domain reaches, in km; the row's ``ask`` block maps it to the source's own param.
    span_km: float | None = None
    #: The point a nearest-site query ranks against: ``near`` names the run input holding it.
    coercion: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.name and not self.name.isidentifier():
            raise DeclarationError(f"Data name {self.name!r} is not an identifier.")
        if self.is_optional and self.producer is not None:
            raise DeclarationError(
                f"Data {self.name!r} declares a producer AND .optional(): a producer "
                "either produces the artifact or fails typed, so there is no absence "
                "for optional to describe. Drop the producer to make it a context "
                "slot, or declare it .context(), which states what the run says "
                "when the source is empty."
            )
        if self.data_class and self.producer is not None:
            raise DeclarationError(
                f"Data {self.name!r} names a producer AND states "
                f"need={self.data_class!r}: a row is satisfied one way. Drop the "
                "producer to state a need.")
        if self.data_class and self.data_class not in DATA_CLASSES:
            raise DeclarationError(
                f"Data {self.name!r} states need={self.data_class!r}, which is "
                f"not one of the classes a row is written in "
                f"({', '.join(DATA_CLASSES)}).")
        if self.observes and not self.data_class:
            raise DeclarationError(
                f"Data {self.name!r} states of={self.observes!r} and needs no "
                "class: of= names what OF a class this row asks the world for, "
                "so a row that goes out for nothing asks for nothing.")
        if self.span_km is not None and not self.data_class:
            raise DeclarationError(
                f"Data {self.name!r} states span_km and needs no class: how far "
                "a question reaches is what the match asks a source for, and a "
                "row that names its own producer states the reach on the call.")
        if self.data_class and self.slot is not None \
                and self.data_class not in self.slot.classes:
            raise DeclarationError(
                f"Data states need={self.data_class!r} as a {self.slot.name}, "
                f"which reads {sorted(self.slot.classes)}.")
        if self.kind and self.slot is not DOMAIN:
            raise DeclarationError(
                f"Data states kind={self.kind!r} and is not a domain: a kind is "
                "the DOMAIN'S own - the water the equations are solved over - "
                "and no other row has one to state.")
        if self.is_context and not (self.producer is not None or self.data_class):
            raise DeclarationError(
                f"Data {self.name!r} declares .context() and states neither a need "
                "nor a producer: a context row is a row that goes out and looks, "
                "whose absence is legal. A row nothing goes out for already says "
                "absence with .optional()."
            )

    def __set_name__(self, owner: type, name: str) -> None:
        take_name(self, "name", name)

    @property
    def fills_from_user(self) -> bool:
        """Is this row on the WIRE for a caller to fill?

        True for every producer-less row and every typed one."""
        return self.producer is None or self.slot is not None

    @property
    def wire_annotation(self) -> Any:
        """This slot's declared type on the generated tool's signature.
        Always a string: the declared shape does not narrow the type."""
        if self.slot is not None and self.slot.number:
            # A schema advertising only a layer name would refuse a known level or a one-number pond.
            return str | float | None
        return str | None

    @property
    def doc_line(self) -> str:
        """What the model reads about this slot: the shape it takes, and whether
        absence is legal. A plain slot names no source, so the shape is all it can
        say; a SLOT that names a producer says what standing a supplied value has."""
        if self.slot is DOMAIN:
            return ("the closed polygon this run solves over, as a uri, a layer "
                    "name or a drawn shape"
                    + ("; unfilled, the template's own producer finds one."
                       if self.producer is not None else self._unfilled))
        if self.slot is BED:
            return ("what the domain's nodes carry for elevation: a DEM, a "
                    "bathymetry or survey raster, a layer of soundings, or a "
                    "depth in metres below the free surface"
                    + ("; unfilled, the template's own producer supplies it."
                       if self.producer is not None else self._unfilled))
        if self.slot is MESH:
            return ("a mesh this case already built, as its uri or its layer "
                    "name, solved on in place of the one the run would build"
                    "; unfilled, the run builds its own.")
        if self.slot is not None and self.slot.record and self.slot.number:
            return (f"the {self.data_class or 'value'} this run opens on: a "
                    "layer of sites that report it, or the number itself"
                    + ("; unfilled, the template's own producer looks for one."
                       if self.producer is not None else self._unfilled))
        if self.data_class and self.producer is None:
            # A row stating a need names no source and is not unsourced: the match fills it; a caller's value supersedes.
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
            # The supplied twin takes every shape the class's sources publish; a value no shape reads out of is the number itself.
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

    def supplied(self, *, geometry: str | None = None) -> "DataDecl":
        """This slot is filled by something the caller SUPPLIES, not by a producer.
        On a producer-less slot this is the whole declaration: the shape it
        accepts, and nothing about where the thing comes from."""
        if self.producer is not None:
            raise DeclarationError(
                f"Data {self.name!r} declares a producer AND .supplied(): a row "
                "is satisfied one way.")
        if geometry is not None and geometry not in _GEOMETRIES:
            raise DeclarationError(
                f"Data {self.name!r}: .supplied(geometry={geometry!r}) is not a "
                f"declared shape (known: {sorted(_GEOMETRIES)})."
            )
        return replace(self, geometry=geometry)

    def optional(self) -> "DataDecl":
        """Absence is legal, and LABELLED: the run says the slot went unfilled."""
        return replace(self, is_optional=True)

    def __call__(self, head: "Slot | Producer") -> "DataDecl":
        """This row, typed or produced: ``Data(BED)`` states the slot that reads it,
        ``Data(tool(...))`` what produces it. The row shape modifiers are written on."""
        if isinstance(head, Slot):
            return replace(self, slot=head)
        producer = head
        if not isinstance(producer, Producer):
            raise DeclarationError(
                f"Data(...) takes a type - BED, DOMAIN ... - or a producer - "
                f"tool(name, **kwargs) - and was given {type(producer).__name__} "
                f"({producer!r}).")
        if self.producer is not None:
            raise DeclarationError(
                f"Data {self.name!r} already names a producer; a row is produced "
                "one way.")
        return replace(self, producer=producer)

    def need(self, data_class: str, *, at: Any = None, of: Any = "",
             kind: str = "", span_km: float | None = None,
             geometry: str | None = None) -> "DataDecl":
        """The class this row needs, filled by the match from whatever measures it here.
        ``at`` names the run input holding the asked point; ``of`` the variable; ``kind`` the domain's own kind
        (wins over the seed's); ``span_km`` the reach; ``geometry`` the shape the row is read as."""
        if geometry is not None and geometry not in _GEOMETRIES:
            raise DeclarationError(
                f"Data {self.name!r}: .need(geometry={geometry!r}) is not a "
                f"declared shape; the shapes are {sorted(_GEOMETRIES)}.")
        return replace(self, data_class=str(data_class),
                       observes=str(of),
                       kind=str(kind), geometry=geometry,
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


#: The unfilled CONTEXT SLOT a ``DATA`` body writes modifiers onto; each returns a fresh row, so bodies never share one.
Data = DataDecl()


def data_rows(body: Any) -> tuple[DataDecl, ...]:
    """The declared rows of a ``DATA`` class body, in CLASS-BODY ORDER; a bare
    producer row is a row named for the attribute it is written under."""
    return tuple(DataDecl(name=value.row, producer=value)
                 if isinstance(value, Producer) else value
                 for value in declarations(body, (Producer, DataDecl)))
