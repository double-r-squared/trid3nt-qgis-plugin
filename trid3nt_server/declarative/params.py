"""``Param`` - a declared VALUE (fits a form cell, resolves through a door,
clamps to declared bounds). Frozen; construction validates the declaration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Sequence

from .errors import PlanValidationError
from .plan import ParamRef

__all__ = [
    "Door",
    "Param",
    "ParamNotResolved",
    "ParamValues",
    "ResolvedParam",
    "ResolvedParams",
    "doors",
    "refuse_duplicate_params",
]


#: Resolution doors, in the order the resolver walks them.
Door = Literal["user", "question", "derived", "scenario", "constant", "gate"]


class doors:  # noqa: N801 - a namespace of door constants, not a type
    """The six doors, in resolution order."""

    USER = "user"
    QUESTION = "question"
    DERIVED = "derived"
    SCENARIO = "scenario"
    CONSTANT = "constant"
    GATE = "gate"


_ORDER: tuple[str, ...] = (
    doors.USER, doors.QUESTION, doors.DERIVED,
    doors.SCENARIO, doors.CONSTANT, doors.GATE,
)

#: A door's ``basis`` on the run's ``SyntheticInput`` provenance record.
_BASIS_FOR_DOOR: dict[str, str] = {
    doors.USER: "user",
    doors.QUESTION: "prompt_interpreted",
    doors.DERIVED: "derived",
    doors.SCENARIO: "default_demo",
    doors.CONSTANT: "default_demo",
    doors.GATE: "user",
}


@dataclass(frozen=True, slots=True)
class Param:
    """One declared value: its door, its bounds, its law-9 consequence tag.

    ``resolve`` is a DOTTED IMPORT PATH to a pure ``(params) -> value`` derivation
    (the GateSpec provider idiom - the declaration stays serializable and engine
    knowledge stays in the engine). ``user_lever`` marks a derived/constant value
    the form lets the user override; ``optional`` marks a value whose absence is
    legal (no gate, no refusal). ``derived_when_absent`` names what stands in when
    an optional param resolves to nothing, so the absence still leaves a
    derived-basis provenance row instead of a silent hole.
    """

    name: str
    desc: str
    door: Door = doors.SCENARIO
    default: Any = None
    bounds: tuple[float, float] | None = None
    units: str | None = None
    resolve: str | None = None
    user_lever: bool = False
    optional: bool = False
    consequence: Literal["physics", "scenario", "numerical", "aoi"] = "scenario"
    real_source: str | None = None
    derived_when_absent: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.isidentifier():
            raise PlanValidationError(f"Param name {self.name!r} is not an identifier.")
        if not self.desc:
            raise PlanValidationError(f"Param {self.name!r} declares no desc.")
        if self.door not in _ORDER:
            raise PlanValidationError(f"Param {self.name!r} door {self.door!r} unknown.")
        if self.door == doors.DERIVED and not self.resolve:
            raise PlanValidationError(
                f"Param {self.name!r} is door=derived but names no resolve path."
            )
        if self.door in (doors.SCENARIO, doors.CONSTANT) and self.default is None \
                and not self.optional:
            raise PlanValidationError(
                f"Param {self.name!r} is door={self.door} but declares no default "
                "(a labeled default IS the door; declare optional=True if absence is legal)."
            )
        if self.bounds is not None:
            lo, hi = self.bounds
            if float(lo) > float(hi):
                raise PlanValidationError(
                    f"Param {self.name!r} bounds {self.bounds} are inverted."
                )
        if self.derived_when_absent and not self.optional:
            raise PlanValidationError(
                f"Param {self.name!r} declares derived_when_absent but is not optional; "
                "a required param has no absence to describe."
            )

    @property
    def basis(self) -> str:
        return _BASIS_FOR_DOOR[self.door]


def refuse_duplicate_params(declared: "Sequence[Param]") -> None:
    """Two declarations of one name silently last-wins; refuse the declaration."""
    seen: set[str] = set()
    for param in declared:
        if param.name in seen:
            raise PlanValidationError(
                f"param {param.name!r} is declared twice; one name, one declaration."
            )
        seen.add(param.name)


class ParamNotResolved(AttributeError):
    """A derivation read a param the sheet has not seated yet.

    An ``AttributeError`` so attribute semantics hold, but its own type so the
    resolver's fixpoint can tell "wait for a dependency" from a real bug inside a
    derivation - which is also an AttributeError and must never be swallowed.
    """


@dataclass(frozen=True, slots=True)
class ResolvedParam:
    """One param after resolution: the value, the door it came through, the note."""

    name: str
    value: Any
    door: Door
    basis: str
    units: str | None = None
    consequence: str = "scenario"
    note: str = ""
    clamped_from: Any = None
    real_source: str | None = None
    required_missing: bool = False

    def with_value(self, value: Any, *, basis: str | None = None,
                   note: str | None = None) -> "ResolvedParam":
        return replace(self, value=value,
                       basis=basis if basis is not None else self.basis,
                       note=note if note is not None else self.note)


class ResolvedParams:
    """The resolved param sheet. ``p.name`` yields a LATE-BOUND :class:`ParamRef`.

    Attribute access is what ``plan(p, d)`` uses, and a plan is built once, before
    the gates it declares have run - so it must describe the read rather than
    perform it. The concrete value is reached through ``get``/``values_dict``, or
    through the :class:`ParamValues` view handed to derivations and chart builders.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: dict[str, ResolvedParam]) -> None:
        object.__setattr__(self, "_rows", dict(rows))

    def __getattr__(self, name: str) -> ParamRef:
        rows = object.__getattribute__(self, "_rows")
        if name not in rows:
            raise ParamNotResolved(
                f"param {name!r} is not declared (declared: {sorted(rows)})"
            )
        return ParamRef(name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ResolvedParams is frozen; resolve a new sheet instead.")

    def __contains__(self, name: str) -> bool:
        return name in self._rows

    def __iter__(self):
        return iter(self._rows.values())

    def get(self, name: str, default: Any = None) -> Any:
        row = self._rows.get(name)
        return default if row is None else row.value

    def row(self, name: str) -> ResolvedParam | None:
        return self._rows.get(name)

    def values_dict(self) -> dict[str, Any]:
        return {k: v.value for k, v in self._rows.items()}

    def rows(self) -> tuple[ResolvedParam, ...]:
        return tuple(self._rows.values())

    def values_view(self) -> "ParamValues":
        """The concrete-value view, for code that runs WITH the sheet, not on it."""
        return ParamValues(self._rows)

    def replacing(self, rows: dict[str, ResolvedParam]) -> "ResolvedParams":
        """A new sheet with these rows overlaid - the sheet itself stays frozen."""
        return ResolvedParams({**self._rows, **rows})


class ParamValues:
    """Concrete-value view of a resolved sheet: ``v.name`` IS the value.

    Handed to derivations and chart builders, which run at a moment when the value
    exists and is what they need. Distinct from :class:`ResolvedParams` so a
    plan-construction read cannot silently collapse into an early-bound value.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: dict[str, ResolvedParam]) -> None:
        self._rows = dict(rows)

    def __getattr__(self, name: str) -> Any:
        rows = object.__getattribute__(self, "_rows")
        if name not in rows:
            raise ParamNotResolved(
                f"param {name!r} is not declared (declared: {sorted(rows)})"
            )
        return rows[name].value

    def __contains__(self, name: str) -> bool:
        return name in self._rows

    def get(self, name: str, default: Any = None) -> Any:
        row = self._rows.get(name)
        return default if row is None else row.value
