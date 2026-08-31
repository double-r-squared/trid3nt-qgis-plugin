"""The PARAMS class body - a declared VALUE per row (fits a form cell, resolves
through a door, clamps to declared bounds). Frozen; construction validates the
declaration.

A template writes its params as a class body, so the attribute name IS the param
name and a reference to it is attribute access on the body
(``PARAMS.spill_fraction``) - a typo is an ``AttributeError`` at import rather
than a string nobody checked, and another template's param name is unwritable."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Sequence

from .errors import PlanValidationError
from .plan import ParamRef, Row, body_rows

__all__ = [
    "Derived",
    "Door",
    "Param",
    "ParamNotResolved",
    "ParamValues",
    "ResolvedParam",
    "ResolvedParams",
    "doors",
    "param_rows",
    "refuse_duplicate_params",
    "wire_value",
]


@dataclass(frozen=True, slots=True)
class Derived:
    """What a derivation returns when it has EVIDENCE to record beside the value.

    A pure arithmetic derivation just returns the number. One that READ THE WORLD
    knows something the declaration cannot: which texture it sampled, which rung
    answered, whether the fit was clamped. Returning that with the value is what
    keeps it on the row - the form card's badge and the run's provenance both read
    it - instead of leaving it in a log line.
    """

    value: Any
    note: str = ""
    real_source: str | None = None


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
class Param(Row):
    """One declared value: its door, its bounds, its law-9 consequence tag.

    A row in a ``PARAMS`` class body: the attribute name it is written under IS
    ``name``, so the declaration says it once and ``PARAMS.<name>`` is its own
    late-bound reference.

    ``resolve`` is a DOTTED IMPORT PATH to a pure ``(params) -> value`` derivation
    (the GateSpec provider idiom - the declaration stays serializable and engine
    knowledge stays in the engine). ``user_lever`` marks a derived/constant value
    the form lets the user override; ``optional`` marks a value whose absence is
    legal (no gate, no refusal). ``derived_when_absent`` names what stands in when
    an optional param resolves to nothing, so the absence still leaves a
    derived-basis provenance row instead of a silent hole.
    """

    name: str = ""
    desc: str = ""
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
    #: The declared WIRE type - what the registration factory annotates the
    #: generated tool argument with, hence what the model sees in the schema.
    #: Unset is inferred from the declaration (bounded -> float, bool default ->
    #: bool, otherwise str); declare it where the inference would be wrong. An
    #: unbounded NUMERIC default is refused rather than inferred: see __post_init__.
    type: Any = None
    #: Whether the wire exposes this param at all. ``False`` marks a value the
    #: model never sends because a coercion resolves it from other wire args.
    wire: bool = True

    _row_attr = "name"
    _ref_type = ParamRef

    def __post_init__(self) -> None:
        if self.name and not self.name.isidentifier():
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
        if self.type is None and self.bounds is None \
                and isinstance(self.default, (int, float)) \
                and not isinstance(self.default, bool):
            # The wire-type inference ends in `str`, and a NUMBER advertised to the
            # model as a string is a schema that lies: the model sends "12", the
            # deck writer multiplies a string, and nothing refused on the way. The
            # two honest declarations are bounds (which also clamp) or an explicit
            # type; guessing between them is not this class's call.
            raise PlanValidationError(
                f"Param {self.name!r} has the numeric default {self.default!r} but "
                "declares neither bounds nor type, so the wire would advertise it "
                "as a STRING. Declare bounds=(lo, hi) - the physical range - or "
                "type=float/int if the value is genuinely unbounded."
            )

    @property
    def basis(self) -> str:
        return _BASIS_FOR_DOOR[self.door]

    @property
    def wire_type(self) -> Any:
        """The declared type, or the one the declaration implies."""
        if self.type is not None:
            return self.type
        if isinstance(self.default, bool):
            return bool
        if self.bounds is not None:
            return float
        return str


def param_rows(body: Any) -> tuple[Param, ...]:
    """The declared params of a ``PARAMS`` class body, in CLASS-BODY ORDER.

    The sheet's own order: the resolver, the form card and the synthesized
    signature all walk it, and the order a reader sees on the card is the order
    the template wrote.
    """
    return body_rows(body, Param)


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
    """The resolved param sheet - what the RUN reads, never what the plan reads.

    A plan is a static value built before any sheet exists (``plan(ops)``, with
    ``PARAMS.<name>`` describing each read), so nothing here is reachable at
    plan-construction time. What this class serves is the interpreter binding a
    ref, the gate machinery re-seating an approval, and the
    :class:`ParamValues` view handed to derivations and chart builders.

    ``p.name`` still yields a :class:`ParamRef` for the templates that predate the
    skeleton and build their own ``Plan`` from a sheet.
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

    def value_of(self, name: str, default: Any = None) -> Any:
        """The concrete value of one row - the interpreter's own read."""
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


def wire_value(value: Any) -> Any:
    """Render a resolved value for the wire - ONE rule for every surface.

    The form card and the provenance row describe the same param, so they round
    the same way: six SIGNIFICANT figures, not decimal places. A hydraulic
    conductivity of 9.3e-07 rounded to four decimals is 0.0, and a row that
    reports a physics value as zero is worse than no row at all.

    The trade, stated: six significant figures also shorten a LARGE value - a
    latitude of 42.0176777 renders as 42.0177, about 10 m. This is DISPLAY: the
    run reads the sheet, never this rendering, and a row the user did not edit is
    never re-seated from what the card showed.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return float(f"{value:.6g}")
    if isinstance(value, (list, tuple)):
        return [wire_value(v) for v in value]
    return str(value)
