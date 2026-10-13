"""THE INPUT: one declared thing a run is handed - a value, or a typed row.

The attribute it is written under IS its name. A value states its default, bounds
and unit; a row states its type or its producer, and the class it needs, the shape
it takes and whether absence is legal are each chained on.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from trid3nt_contracts.coverage import DATA_CLASSES

from trid3nt_server.errors import DeclarationError, GateRefusedError

__all__ = ["GEOMETRY_CLASS", "Input", "declared", "take_name"]

# A row states the class it needs, never a fetcher: the match reads cell, coverage, window and datum
# off every fetcher's own row, which a named fetcher would freeze against one source.

#: Vector shapes collapse to one class: a file name cannot tell a point layer from a line layer.
GEOMETRY_CLASS: Mapping[str, str] = MappingProxyType({
    "point": "vector", "polyline": "vector", "polygon": "vector",
    "rectangle": "vector", "raster": "raster"})


@dataclass(frozen=True, slots=True)
class Input:
    """One declared input: a VALUE (default, bounds, unit) or a ROW (a type, a need, a producer).
    A value with no default that is not optional is REQUIRED; a row nothing produces is filled by the
    caller, and its absence is legal only where it says so."""

    #: The TYPE a row states - BED, DOMAIN ... - whose ingestion reads its value, or the producer that fills it; ``None`` on a value and on a plain row.
    slot: Any = None
    name: str = ""
    desc: str = ""
    default: Any = None
    bounds: tuple[float, float] | None = None
    units: str | None = None
    #: The declared wire type; unset is inferred (bounded -> float, bool default -> bool, else str).
    #: An unbounded NUMERIC default is refused rather than inferred: see __post_init__.
    type: Any = None
    #: What fills a row nobody supplied: a runner name plus its declared args, built in the runtime and held here.
    producer: Any = None
    #: Absence is legal; on a row only where nothing produces it - a producer either produces or fails.
    is_optional: bool = False
    #: The geometry a producer-less row accepts (point | polyline | polygon | rectangle | raster).
    geometry: str | None = None
    #: CONTEXT row: it goes out and looks, and its absence continues the run under the sentence below.
    is_context: bool = False
    #: What the run says of an input left absent: a context row's empty source, an optional value's derivation.
    absent_note: str = ""
    #: The class of the coarse vocabulary this row needs instead of naming a producer; place, window and frame are the run's.
    data_class: str = ""
    #: What of the class is asked for, by the name the source publishes it under; empty where any will do.
    observes: str = ""
    #: The domain's own kind, stated only where the seed cannot imply it; a stated kind wins.
    kind: str = ""
    #: How far the domain reaches, in km; the row's ``ask`` block maps it to the source's own param.
    span_km: float | None = None
    #: What the run tells this row's ingestion: ``near`` names the run input holding the point a nearest-site query ranks against.
    coercion: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.slot is not None and not hasattr(self.slot, "ingestion"):
            if not hasattr(self.slot, "runner"):
                raise DeclarationError(
                    f"Input(...) takes a type - BED, DOMAIN ... - or a producer - "
                    f"tool(name, **kwargs) - and was given {type(self.slot).__name__} "
                    f"({self.slot!r}).")
            object.__setattr__(self, "producer", self.slot)
            object.__setattr__(self, "slot", None)
        if self.name and not self.name.isidentifier():
            raise DeclarationError(f"Input name {self.name!r} is not an identifier.")
        if self.bounds is not None:
            lo, hi = self.bounds
            if float(lo) > float(hi):
                raise DeclarationError(
                    f"Input {self.name!r} bounds {self.bounds} are inverted.")
        if self.type is None and self.bounds is None \
                and isinstance(self.default, (int, float)) \
                and not isinstance(self.default, bool):
            # A number advertised as str is a schema that lies; declare bounds or an explicit type - guessing is not this class's call.
            raise DeclarationError(
                f"Input {self.name!r} has the numeric default {self.default!r} but "
                "declares neither bounds nor type, so the wire would advertise it "
                "as a STRING. Declare bounds=(lo, hi) - the physical range - or "
                "type=float/int if the value is genuinely unbounded.")
        if self.is_optional and self.producer is not None:
            raise DeclarationError(
                f"Input {self.name!r} declares a producer AND .optional(): a producer "
                "either produces the artifact or fails typed, so there is no absence "
                "for optional to describe. Drop the producer to make it a context "
                "slot, or declare it .context(), which states what the run says "
                "when the source is empty.")
        if self.data_class and self.producer is not None:
            raise DeclarationError(
                f"Input {self.name!r} names a producer AND states "
                f"need={self.data_class!r}: a row is satisfied one way. Drop the "
                "producer to state a need.")
        if self.data_class and self.data_class not in DATA_CLASSES:
            raise DeclarationError(
                f"Input {self.name!r} states need={self.data_class!r}, which is "
                f"not one of the classes a row is written in "
                f"({', '.join(DATA_CLASSES)}).")
        if self.observes and not self.data_class:
            raise DeclarationError(
                f"Input {self.name!r} states of={self.observes!r} and needs no "
                "class: of= names what OF a class this row asks the world for, "
                "so a row that goes out for nothing asks for nothing.")
        if self.span_km is not None and not self.data_class:
            raise DeclarationError(
                f"Input {self.name!r} states span_km and needs no class: how far "
                "a question reaches is what the match asks a source for, and a "
                "row that names its own producer states the reach on the call.")
        if self.data_class and self.slot is not None \
                and self.data_class not in self.slot.classes:
            raise DeclarationError(
                f"Input states need={self.data_class!r} as a {self.slot.name}, "
                f"which reads {sorted(self.slot.classes)}.")
        if self.kind and getattr(self.slot, "name", "") != "domain":
            raise DeclarationError(
                f"Input states kind={self.kind!r} and is not a domain: a kind is "
                "the DOMAIN'S own - the water the equations are solved over - "
                "and no other row has one to state.")
        if self.is_context and not (self.producer is not None or self.data_class):
            raise DeclarationError(
                f"Input {self.name!r} declares .context() and states neither a need "
                "nor a producer: a context row is a row that goes out and looks, "
                "whose absence is legal. A row nothing goes out for already says "
                "absence with .optional().")

    def __set_name__(self, owner: type, name: str) -> None:
        take_name(self, "name", name)

    @property
    def is_row(self) -> bool:
        """Is this a ROW - an artifact fetched, supplied or drawn - rather than a value?"""
        return (self.slot is not None or self.producer is not None
                or bool(self.data_class) or self.geometry is not None)

    @property
    def required(self) -> bool:
        """A value with no default that is not optional: the fill refuses the run while it is unstated."""
        return not self.is_row and self.default is None and not self.is_optional

    @property
    def fills_from_user(self) -> bool:
        """Is this input on the WIRE for a caller to fill?

        True for every value, every producer-less row and every typed one."""
        return self.producer is None or self.slot is not None

    @property
    def wire_type(self) -> Any:
        """The type the generated tool's signature takes for this input.
        A row is always a string - the declared shape does not narrow it; a Point is
        typed by the value it holds and takes every form its ingestion reads."""
        if self.is_row:
            # A schema advertising only a layer name would refuse a known level or a one-number pond.
            return str | float | None if getattr(self.slot, "number", False) else str | None
        if self.type is not None:
            from .point import Point

            return dict | list[float] | str if self.type is Point else self.type
        if isinstance(self.default, bool):
            return bool
        if self.bounds is not None:
            return float
        return str

    def accept(self, value: Any) -> Any:
        """``value`` as this input takes it: a bounded one held to its range, and
        refused by name outside it rather than moved onto the bound."""
        if self.bounds is None:
            return value
        # bool is an int: True would coerce to 1.0 and slip past a bounded input's refusal.
        try:
            number = None if isinstance(value, bool) else float(value)
        except (TypeError, ValueError):
            number = None
        if number is None:
            raise GateRefusedError(
                f"{self.name}={value!r} is not a number, but declares bounds "
                f"{self.bounds} ({self.units or 'no units'}). It is not silently defaulted.")
        lo, hi = float(self.bounds[0]), float(self.bounds[1])
        if not lo <= number <= hi:
            # The value is the user's; one moved onto the bound runs an unasked question silently.
            units = f" {self.units}" if self.units else ""
            raise GateRefusedError(
                f"{self.name}={number:g}{units} is outside the declared range "
                f"{lo:g} to {hi:g}{units}; state a value inside it.")
        # The bound compares numbers without retyping: an int resolved to float would reach an INTEGER keyword and refuse far from here.
        return int(number) if self.type is int else number

    @property
    def doc_line(self) -> str:
        """What the model reads about this input: a value's meaning, unit, range and
        default; a row's shape, and what standing a supplied value has."""
        if not self.is_row:
            bits = [self.desc.rstrip(".")]
            if self.units:
                bits.append(f"{self.units}")
            if self.bounds is not None:
                bits.append(f"range {self.bounds[0]:g}-{self.bounds[1]:g}")
            if self.default is not None:
                bits.append(f"default {self.default!r} (the template's starting value, "
                            "not a site measurement)")
            else:
                bits.append("required" if self.required else "optional")
            return ", ".join(bits) + "."
        role = getattr(self.slot, "name", "")
        if role == "domain":
            return ("the closed polygon this run solves over, as a uri, a layer "
                    "name or a drawn shape"
                    + ("; unfilled, the template's own producer finds one."
                       if self.producer is not None else self._unfilled))
        if role == "bed":
            return ("what the domain's nodes carry for elevation: a DEM, a "
                    "bathymetry or survey raster, a layer of soundings, or a "
                    "depth in metres below the free surface"
                    + ("; unfilled, the template's own producer supplies it."
                       if self.producer is not None else self._unfilled))
        if role == "mesh":
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
        """What a row with no producer says about how it fills itself."""
        return (f"; unfilled, the run matches a source of {self.data_class}."
                if self.data_class else ".")

    def supplied(self, *, geometry: str) -> "Input":
        """This row is filled by something the caller SUPPLIES, not by a producer:
        the shape it accepts, and nothing about where the thing comes from."""
        if self.producer is not None:
            raise DeclarationError(
                f"Input {self.name!r} declares a producer AND .supplied(): a row "
                "is satisfied one way.")
        return replace(self, geometry=self._shape("supplied", geometry))

    def optional(self, absent: str = "") -> "Input":
        """Absence is legal, and LABELLED: ``absent`` is what the run derives or says in its place."""
        return replace(self, is_optional=True,
                       absent_note=str(absent or self.absent_note))

    def need(self, data_class: str, *, at: Any = None, of: Any = "",
             kind: str = "", span_km: float | None = None,
             geometry: str | None = None) -> "Input":
        """The class this row needs, filled by the match from whatever measures it here.
        ``at`` names the run input holding the asked point; ``of`` the variable; ``kind`` the domain's own kind
        (wins over the seed's); ``span_km`` the reach; ``geometry`` the shape the row is read as."""
        return replace(self, data_class=str(data_class), observes=str(of),
                       kind=str(kind),
                       geometry=geometry and self._shape("need", geometry),
                       span_km=None if span_km is None else float(span_km),
                       coercion=MappingProxyType({"near": at}))

    def context(self, absent: str = "") -> "Input":
        """This row is CONTEXT: its absence continues the run.

        ``absent`` is the sentence the sheet carries when nothing measured this
        near this domain; unstated, the row's own name says it."""
        return replace(self, is_context=True, absent_note=str(absent or ""))

    def _shape(self, word: str, geometry: str) -> str:
        if geometry not in GEOMETRY_CLASS:
            raise DeclarationError(
                f"Input {self.name!r}: .{word}(geometry={geometry!r}) is not a "
                f"declared shape; the shapes are {sorted(GEOMETRY_CLASS)}.")
        return geometry

    @property
    def context_sentence(self) -> str:
        """What the run says when this context row's source held nothing."""
        if self.absent_note:
            return self.absent_note
        return (f"no {self.name.replace('_', ' ')} near this domain; the stated "
                "value stands")


def declared(body: Any) -> tuple[Input, ...]:
    """The Inputs of a class body in CLASS-BODY ORDER, a bare producer a row named for its attribute;
    a sequence passes through, so a body assembled in code is still a body.
    A value with no desc refuses the declaration."""
    found = body if isinstance(body, (list, tuple)) else vars(body).values()
    rows = tuple(Input(value, name=value.row) if hasattr(value, "runner") else value
                 for value in found
                 if isinstance(value, Input) or hasattr(value, "runner"))
    for row in rows:
        if not row.is_row and not row.desc:
            raise DeclarationError(f"Input {row.name!r} declares no desc.")
    return rows


def take_name(row: Any, attr: str, name: str) -> None:
    """The attribute a declaration is written under IS its name; one
    declaration under two names refuses."""
    held = getattr(row, attr, "")
    if held and held != name:
        raise DeclarationError(
            f"{held!r} is declared again as {name!r}: a declaration is one row "
            "in one body. Write a fresh declaration for the second row.")
    object.__setattr__(row, attr, name)
