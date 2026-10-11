"""The PARAMS class body - a declared VALUE per row: a form cell, a default, bounds.

The attribute name IS the param name, so ``PARAMS.spill_fraction`` is attribute
access and a typo is an ``AttributeError`` at import.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Sequence

from trid3nt_contracts.common import Origin

from .errors import DeclarationError

__all__ = [
    "Param",
    "ResolvedParam",
    "ResolvedParams",
    "param_rows",
    "refuse_duplicate_params",
    "wire_value",
]


@dataclass(frozen=True, slots=True)
class Param:
    """One declared value: its default, its bounds, its unit.
    The attribute name it is written under IS ``name``; one with no default that is not optional is REQUIRED."""

    name: str = ""
    desc: str = ""
    default: Any = None
    bounds: tuple[float, float] | None = None
    units: str | None = None
    optional: bool = False
    derived_when_absent: str | None = None
    #: The declared wire type; unset is inferred (bounded -> float, bool default -> bool, else str).
    #: An unbounded NUMERIC default is refused rather than inferred: see __post_init__.
    type: Any = None

    def __set_name__(self, owner: type, name: str) -> None:
        take_name(self, "name", name)

    def __post_init__(self) -> None:
        if self.name and not self.name.isidentifier():
            raise DeclarationError(f"Param name {self.name!r} is not an identifier.")
        if not self.desc:
            raise DeclarationError(f"Param {self.name!r} declares no desc.")
        if self.bounds is not None:
            lo, hi = self.bounds
            if float(lo) > float(hi):
                raise DeclarationError(
                    f"Param {self.name!r} bounds {self.bounds} are inverted."
                )
        if self.derived_when_absent and not self.optional:
            raise DeclarationError(
                f"Param {self.name!r} declares derived_when_absent but is not optional; "
                "a required param has no absence to describe."
            )
        if self.type is None and self.bounds is None \
                and isinstance(self.default, (int, float)) \
                and not isinstance(self.default, bool):
            # A number advertised as str is a schema that lies; declare bounds or an explicit type - guessing is not this class's call.
            raise DeclarationError(
                f"Param {self.name!r} has the numeric default {self.default!r} but "
                "declares neither bounds nor type, so the wire would advertise it "
                "as a STRING. Declare bounds=(lo, hi) - the physical range - or "
                "type=float/int if the value is genuinely unbounded."
            )

    @property
    def required(self) -> bool:
        """No default and not optional: the fill refuses the run while it is unstated."""
        return self.default is None and not self.optional

    @property
    def wire_type(self) -> Any:
        """The declared type, or the one the declaration implies.
        A Point slot is typed by the value it holds; what the WIRE takes for it is
        every form the ingestion reads - a pick's mapping, a pair, a string."""
        if self.type is not None:
            from trid3nt_server.inputs.point import Point

            return dict | list[float] | str if self.type is Point else self.type
        if isinstance(self.default, bool):
            return bool
        if self.bounds is not None:
            return float
        return str


def param_rows(body: Any) -> tuple[Param, ...]:
    """The declared params of a ``PARAMS`` class body, in CLASS-BODY ORDER.
    The sheet's own order - what a reader sees on the card is what the template wrote."""
    return declarations(body, Param)


def declarations(body: Any, kind: type | tuple[type, ...]) -> tuple[Any, ...]:
    """The declarations of a class body, in CLASS-BODY ORDER; a sequence passes
    through, so a body assembled in code is still a body."""
    if isinstance(body, (list, tuple)):
        return tuple(body)
    return tuple(v for v in vars(body).values() if isinstance(v, kind))


def take_name(row: Any, attr: str, name: str) -> None:
    """The attribute a declaration is written under IS its name; one
    declaration under two names refuses."""
    held = getattr(row, attr, "")
    if held and held != name:
        raise DeclarationError(
            f"{held!r} is declared again as {name!r}: a declaration is one row "
            "in one body. Write a fresh declaration for the second row.")
    object.__setattr__(row, attr, name)


def refuse_duplicate_params(declared: "Sequence[Param]") -> None:
    """Two declarations of one name silently last-wins; refuse the declaration."""
    seen: set[str] = set()
    for param in declared:
        if param.name in seen:
            raise DeclarationError(
                f"param {param.name!r} is declared twice; one name, one declaration."
            )
        seen.add(param.name)


@dataclass(frozen=True, slots=True)
class ResolvedParam:
    """One param after resolution: the value, where it came from, the note."""

    name: str
    value: Any
    #: None while the row holds no value.
    origin: Origin | None
    units: str | None = None
    note: str = ""
    required_missing: bool = False


class ResolvedParams:
    """The resolved params - what the RUN reads, never what a declaration reads."""

    __slots__ = ("_rows",)

    def __init__(self, rows: dict[str, ResolvedParam]) -> None:
        object.__setattr__(self, "_rows", dict(rows))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ResolvedParams is frozen; resolve a new sheet instead.")

    def __contains__(self, name: str) -> bool:
        return name in self._rows

    def __iter__(self):
        return iter(self._rows.values())

    def value_of(self, name: str, default: Any = None) -> Any:
        """The concrete value of one row - the run's own read."""
        row = self._rows.get(name)
        return default if row is None else row.value

    def row(self, name: str) -> ResolvedParam | None:
        return self._rows.get(name)

    def values_dict(self) -> dict[str, Any]:
        return {k: v.value for k, v in self._rows.items()}

    def rows(self) -> tuple[ResolvedParam, ...]:
        return tuple(self._rows.values())

    def replacing(self, rows: dict[str, ResolvedParam]) -> "ResolvedParams":
        """A new sheet with these rows overlaid - the sheet itself stays frozen."""
        return ResolvedParams({**self._rows, **rows})


def wire_value(value: Any) -> Any:
    """Render a resolved value for the wire - ONE rule for every surface.
    DISPLAY only: the run reads the sheet, and a row the user did not edit is
    never re-seated from what the card showed."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: wire_value(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, float):
        # Six SIGNIFICANT figures, not decimal places: 9.3e-07 to four decimals reads 0.0, and a physics value
        # reported as zero is worse than no row.
        return float(f"{value:.6g}")
    if isinstance(value, (list, tuple)):
        return [wire_value(v) for v in value]
    return str(value)
