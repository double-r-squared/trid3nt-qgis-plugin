"""The param seating: a stated value or the declared default, held to its bounds.

Every resolution leaves a provenance row; a value outside its declared bounds
refuses by name rather than being moved quietly onto the bound.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from .errors import GateRefusedError
from .params import (
    Param,
    ResolvedParam,
    ResolvedParams,
    doors,
    param_rows,
    refuse_duplicate_params,
    wire_value,
)

__all__ = ["merge_provenance", "provenance_entries", "resolve_params", "seat_param"]


async def resolve_params(declared: Any,
                         supplied: Mapping[str, Any]) -> ResolvedParams:
    """Every declared param seated off ``supplied`` -> the resolved sheet.
    ``supplied`` is NEVER ambient - no case-store lookup."""
    declared = param_rows(declared)
    refuse_duplicate_params(declared)
    return ResolvedParams({param.name: seat_param(param, supplied.get(param.name))
                           for param in declared})


def seat_param(param: Param, value: Any) -> ResolvedParam:
    """One param: the stated value, else its declared default, else REQUIRED
    and missing - never invented. A value outside its bounds refuses by name."""
    if value is not None:
        return _finish(param, value, doors.USER, "supplied on this invocation")
    if param.default is not None:
        return _finish(param, param.default, param.door,
                       f"declared {param.door} default", basis=_BASIS_DEFAULT)
    return ResolvedParam(
        name=param.name, value=None, door=param.door, basis=param.basis,
        units=param.units, consequence=param.consequence,
        note=("not supplied (declared optional)" if param.optional
              else "REQUIRED and not supplied"),
        real_source=param.real_source, required_missing=not param.optional)


#: A value seated from its own DECLARED DEFAULT is a labeled default whatever door
#: it hangs under - the door says who may override it, not where this value came from.
_BASIS_DEFAULT = "default_demo"


#: The only two bases that HAVE a real source. A value the caller typed, or one
#: seated from a declared default, did not come from the data the declaration
#: names - claiming otherwise on the row would be the provenance lying.
_SOURCED_BASES = frozenset({"derived", "fetched"})


def _finish(param: Param, value: Any, door: str, note: str, *,
            basis: str | None = None,
            real_source: str | None = None) -> ResolvedParam:
    if param.bounds is not None:
        coerced = _as_float(value)
        if coerced is None:
            raise GateRefusedError(
                f"{param.name}={value!r} is not a number, but declares bounds "
                f"{param.bounds} ({param.units or 'no units'}). It is not silently defaulted."
            )
        lo, hi = float(param.bounds[0]), float(param.bounds[1])
        if not lo <= coerced <= hi:
            # The lever is the user's. A value moved onto the bound runs a
            # question nobody asked and says nothing while it does it.
            units = f" {param.units}" if param.units else ""
            raise GateRefusedError(
                f"{param.name}={coerced:g}{units} is outside the declared range "
                f"{lo:g} to {hi:g}{units}; state a value inside it."
            )
        # The bound compares numbers; it does not RETYPE the param. A row that
        # declares int and resolves to a float states a value its own
        # declaration says it cannot hold, and an engine keyword typed INTEGER
        # refuses it several steps later, naming the keyword rather than this.
        value = int(coerced) if param.type is int else coerced
    if basis is None:
        basis = "user" if door in (doors.USER, doors.GATE) else _basis(param, door)
    source = real_source if real_source is not None else param.real_source
    return ResolvedParam(
        name=param.name, value=value, door=door, basis=basis,
        units=param.units, consequence=param.consequence, note=note,
        real_source=source if basis in _SOURCED_BASES else None,
    )


def _basis(param: Param, door: str) -> str:
    if door == doors.QUESTION:
        return "prompt_interpreted"
    return param.basis


def _as_float(value: Any) -> float | None:
    # bool IS an int in Python, so True would coerce to 1.0 and slip past the
    # refusal a bounded param exists to make. A flag is not a measurement.
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def provenance_entries(resolved: ResolvedParams,
                       declared: Any) -> list[SyntheticInput]:
    """The run's provenance rows - what the input-review gate and the layer carry.
    A ``default_demo`` + ``physics`` row is what makes the gate refuse in auto mode;
    an absent param declaring ``derived_when_absent`` still leaves a derived row."""
    by_name = {p.name: p for p in param_rows(declared)}
    out: list[SyntheticInput] = []
    for row in resolved.rows():
        param = by_name.get(row.name)
        if param is None:
            continue
        if row.value is None:
            if param.derived_when_absent:
                out.append(SyntheticInput(
                    param=row.name, value="derived", units=row.units,
                    basis="derived", consequence=row.consequence,
                    note=f"not supplied; {param.derived_when_absent}",
                ))
            continue
        out.append(SyntheticInput(
            param=row.name,
            value=_provenance_value(row.value),
            units=row.units,
            basis=row.basis,
            consequence=row.consequence,
            real_source_if_any=row.real_source,
            note=(f"{param.desc} [{row.note}]" if row.note else param.desc),
        ))
    return out


def merge_provenance(existing: Sequence[SyntheticInput],
                     declared: Sequence[SyntheticInput]) -> list[SyntheticInput]:
    """Merge a composite step's own provenance rows with the plan's declared rows.
    The composite's row WINS on a name collision - it stamped what actually
    resolved, while the declaration only knows what was asked for."""
    kept = list(existing)
    taken = {row.param for row in kept}
    kept.extend(row for row in declared if row.param not in taken)
    return kept


def _provenance_value(value: Any) -> Any:
    # Rendered by the one shared rule, then flattened: a provenance row's value is
    # a scalar or a string, so a coordinate pair travels as its text.
    rendered = wire_value(value)
    if isinstance(rendered, dict):
        return ", ".join(f"{k}={v}" for k, v in rendered.items() if v is not None)
    return str(rendered) if isinstance(rendered, (list, bool)) else rendered
