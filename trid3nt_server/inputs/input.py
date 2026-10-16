"""THE INPUT: one declared thing a run is handed - a value, or a typed row.

The attribute it is written under IS its name. A value states its default, bounds
and unit; a row states its type or its producer, and the class it needs and the
shape it takes are each chained on. The module input JSON carries a value's
schema and prose: ``hydrate`` takes it onto the body before the body is declared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from trid3nt_contracts.coverage import DATA_CLASSES

from trid3nt_server.errors import DeclarationError, GateRefusedError

from .validate import scalar

__all__ = ["GEOMETRY_CLASS", "Input", "declared", "hydrate", "take_name"]

# A row states the class it needs, never a fetcher: the match reads cell, coverage, window and datum
# off every fetcher's own row, which a named fetcher would freeze against one source.

#: Vector shapes collapse to one class: a file name cannot tell a point layer from a line layer.
GEOMETRY_CLASS: Mapping[str, str] = MappingProxyType({
    "point": "vector", "polyline": "vector", "polygon": "vector",
    "rectangle": "vector", "raster": "raster"})


@dataclass(frozen=True, slots=True)
class Input:
    """One declared input: a VALUE (default, bounds, unit) or a ROW (a type, a need, a producer).
    A value with no default that is not stated-only is REQUIRED; a row nothing produces is filled
    by the caller. ``stated()`` is the one standing of "the user may state one; unstated the run's
    own machinery answers" - the supplied mesh, the mesher's own edge."""

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
    #: The user MAY state one; unstated, the run's own machinery answers (no verdict, no refusal).
    stated_only: bool = False
    #: The geometry a producer-less row accepts (point | polyline | polygon | rectangle | raster).
    geometry: str | None = None
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
        if self.stated_only and (self.producer is not None or self.data_class):
            raise DeclarationError(
                f"Input {self.name!r} is stated-only AND declares a producer or a "
                "need: stated() is the one standing of the user MAY state one - "
                "unstated, the run's own machinery answers. A producer or a match "
                "IS machinery that answers, so the row is satisfied either way.")
        if self.kind and getattr(self.slot, "name", "") != "domain":
            raise DeclarationError(
                f"Input states kind={self.kind!r} and is not a domain: a kind is "
                "the DOMAIN'S own - the water the equations are solved over - "
                "and no other row has one to state.")

    def __set_name__(self, owner: type, name: str) -> None:
        take_name(self, "name", name)

    @property
    def is_row(self) -> bool:
        """Is this a ROW - an artifact fetched, supplied or drawn - rather than a value?"""
        return (self.slot is not None or self.producer is not None
                or bool(self.data_class) or self.geometry is not None)

    @property
    def required(self) -> bool:
        """A value with no default that is not stated-only: the fill refuses the run while it is unstated."""
        return not self.is_row and self.default is None and not self.stated_only

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
        try:
            number = scalar(self.name, value, self.bounds, units=self.units or "")
        except ValueError as refused:
            # The value is the user's; one moved onto the bound runs an unasked question silently.
            raise GateRefusedError(str(refused)) from None
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
            elif self.stated_only:
                bits.append("the user may state one; unstated, the run's own "
                            "machinery answers")
            else:
                bits.append("required")
            return ", ".join(bits) + "."
        slot = self.slot
        if self.desc:
            # A row's own words are the template's judgment about THIS use of the slot.
            return self.desc
        if slot is not None and slot.desc:
            if self.stated_only:
                return (f"{slot.desc}; the user may state one; unstated, the run "
                        "builds its own.")
            if slot.record and slot.number:
                head = f"the {self.data_class or 'value'} this run opens on: {slot.desc}"
            else:
                head = slot.desc
            if self.producer is not None:
                return f"{head}; unfilled, the template's own producer finds one."
            return head + self._unfilled
        if self.data_class and self.producer is None:
            # A row stating a need names no source and is not unsourced: the match fills it; a caller's value supersedes.
            return (f"the {self.data_class} this run reads, as a uri or a layer "
                    "name" + self._unfilled)
        shape = f"a {self.geometry} layer" if self.geometry else "a layer"
        if self.producer is not None:
            tail = "unfilled, the template's own producer fetches one"
        else:
            tail = "required - the template names no source for it"
        return f"{shape} you supply, as a uri or a layer name; {tail}."

    @property
    def _unfilled(self) -> str:
        """What a row with no producer says about how it fills itself."""
        return (f"; unfilled, the run matches a source of {self.data_class}."
                if self.data_class else ".")

    def stated(self) -> "Input":
        """The user MAY state one; unstated, the run's own machinery answers - a
        supplied mesh the builder replaces, the mesher's own edge. No verdict, no
        refusal, no sentence: the one standing where absence is the design."""
        return replace(self, stated_only=True)

    def need(self, data_class: str = "", *, at: Any = None, of: Any = "",
             kind: str = "", span_km: float | None = None,
             geometry: str | None = None) -> "Input":
        """The class this row needs, filled by the match from whatever measures it here.
        ``at`` names the run input holding the asked point; ``of`` the variable; ``kind`` the domain's own kind
        (wins over the seed's); ``span_km`` the reach. A geometry with no class is the
        user-prompt fetch: the shape the person draws or hands in, absorbing what they supply."""
        if self.producer is not None and geometry is not None:
            raise DeclarationError(
                f"Input {self.name!r} declares a producer AND need(geometry=): a "
                "row is satisfied one way.")
        return replace(self, data_class=str(data_class), observes=str(of),
                       kind=str(kind),
                       geometry=geometry and self._shape(geometry),
                       span_km=None if span_km is None else float(span_km),
                       coercion=MappingProxyType({"near": at}))

    def _shape(self, geometry: str) -> str:
        if geometry not in GEOMETRY_CLASS:
            raise DeclarationError(
                f"Input {self.name!r}: need(geometry={geometry!r}) is not a "
                f"declared shape; the shapes are {sorted(GEOMETRY_CLASS)}.")
        return geometry


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


#: The fields a module input JSON carries for a value: its prose, its schema and
#: its starting value. A need, a producer and a stated() standing are on the row.
_SCHEMA_FIELDS = frozenset({"bounds", "default", "desc", "type", "units"})


def hydrate(body: Any, source: tuple[Path, str] | None, template: str) -> Any:
    """``body`` with every value input merged onto the schema row the module input JSON names it by.

    A class body carries the merge back on its own attributes, so a reader of the
    template sees its schema from registration on; a sequence passes through as
    merged rows. ``source`` is the (directory, module) the JSON lives in, and
    None passes the body through where a template declares alone."""
    if source is None:
        return body
    schema = _schema_fields(*source, template)
    is_class = not isinstance(body, (list, tuple))
    merged = []
    for value in (tuple(vars(body).values()) if is_class else tuple(body)):
        if not isinstance(value, Input) or value.name not in schema:
            merged.append(value)
            continue
        if value.is_row:
            raise DeclarationError(
                f"the module input JSON's inputs section names {value.name!r}, "
                "which is a row: a row states its type and its need on the row, "
                "and only a value carries its schema there.")
        row = _merged(value, schema[value.name], value.name)
        if is_class:
            setattr(body, row.name, row)
        merged.append(row)
    return tuple(merged)


def _schema_fields(directory: Path, module: str, template: str) -> Mapping[str, Mapping[str, Any]]:
    """The schema rows the module input JSON names ``template``'s values by, or none where it names none."""
    path = Path(directory) / f"{module}.json"
    if not path.is_file():
        raise DeclarationError(
            f"module input {path} does not exist: a template's values read "
            "their schema there.")
    section = (json.loads(path.read_text()).get("inputs") or {}).get(template) or {}
    if not isinstance(section, Mapping):
        raise DeclarationError(
            f"module input {path} names template {template!r} as something "
            "that carries no input mapping.")
    for name, fields in section.items():
        if not isinstance(fields, Mapping):
            raise DeclarationError(
                f"module input {template}.{name} is not a field mapping.")
        unknown = set(fields) - _SCHEMA_FIELDS
        if unknown:
            raise DeclarationError(
                f"module input {template}.{name} carries {sorted(unknown)}; "
                f"the fields are {sorted(_SCHEMA_FIELDS)}.")
    return section


def _merged(row: Input, fields: Mapping[str, Any], name: str) -> Input:
    """``row`` with its schema fields taken on - a declared type read back as a type."""
    taken = dict(fields)
    if "type" in taken:
        taken["type"] = _wire_type(taken["type"], name)
    if "bounds" in taken:
        bounds = taken["bounds"]
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise DeclarationError(
                f"module input {name} states bounds {bounds!r}, which are not a pair.")
        taken["bounds"] = tuple(bounds)
    return replace(row, **taken)


def _wire_type(text: Any, name: str) -> Any:
    """The declared wire type of a schema row - the type expression the JSON spells it as."""
    if not isinstance(text, str):
        raise DeclarationError(
            f"module input {name} states type {text!r}, which is not a type expression.")
    from .point import Point

    try:
        return eval(text, {"__builtins__": {}}, {  # noqa: S307
            "Point": Point, "bool": bool, "dict": dict, "float": float,
            "int": int, "list": list, "str": str, "tuple": tuple})
    except Exception as refused:
        raise DeclarationError(
            f"module input {name} states type {text!r}, which does not read as a type.") from refused


def take_name(row: Any, attr: str, name: str) -> None:
    """The attribute a declaration is written under IS its name; one
    declaration under two names refuses."""
    held = getattr(row, attr, "")
    if held and held != name:
        raise DeclarationError(
            f"{held!r} is declared again as {name!r}: a declaration is one row "
            "in one body. Write a fresh declaration for the second row.")
    object.__setattr__(row, attr, name)
