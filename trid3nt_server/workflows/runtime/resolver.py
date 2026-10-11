"""The param seating: a stated value or the declared default, held to its bounds.

Every resolution leaves a provenance row; a value outside its declared bounds
refuses by name rather than being moved quietly onto the bound.
"""

from __future__ import annotations

from typing import Any, Sequence

from trid3nt_contracts.common import Origin, SyntheticInput

from .errors import GateRefusedError
from .params import (
    Param,
    ResolvedParam,
    ResolvedParams,
    param_rows,
    wire_value,
)

__all__ = ["merge_provenance", "provenance_entries", "seat_param"]


def seat_param(param: Param, value: Any) -> ResolvedParam:
    """One param: the stated value, else its declared default, else REQUIRED
    and missing - never invented. A value outside its bounds refuses by name."""
    if value is not None:
        return _finish(param, value, Origin.USER, "supplied on this invocation")
    if param.default is not None:
        return _finish(param, param.default, Origin.TEMPLATE, "declared default")
    return ResolvedParam(
        name=param.name, value=None, origin=None, units=param.units,
        note=("not supplied (declared optional)" if param.optional
              else "REQUIRED and not supplied"),
        required_missing=not param.optional)


def _finish(param: Param, value: Any, origin: Origin, note: str) -> ResolvedParam:
    if param.bounds is not None:
        coerced = _as_float(value)
        if coerced is None:
            raise GateRefusedError(
                f"{param.name}={value!r} is not a number, but declares bounds "
                f"{param.bounds} ({param.units or 'no units'}). It is not silently defaulted."
            )
        lo, hi = float(param.bounds[0]), float(param.bounds[1])
        if not lo <= coerced <= hi:
            # The lever is the user's; a value moved onto the bound runs an unasked question silently.
            units = f" {param.units}" if param.units else ""
            raise GateRefusedError(
                f"{param.name}={coerced:g}{units} is outside the declared range "
                f"{lo:g} to {hi:g}{units}; state a value inside it."
            )
        # The bound compares numbers without retyping: an int param resolved to float would reach an INTEGER keyword and refuse far from here.
        value = int(coerced) if param.type is int else coerced
    return ResolvedParam(name=param.name, value=value, origin=origin,
                         units=param.units, note=note)


def _as_float(value: Any) -> float | None:
    # bool is an int: True would coerce to 1.0 and slip past a bounded param's refusal.
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def provenance_entries(resolved: ResolvedParams,
                       declared: Any) -> list[SyntheticInput]:
    """The run's provenance rows - what the layer and the journal carry.
    An absent param declaring ``derived_when_absent`` still leaves a derived row."""
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
                    basis=Origin.DERIVED,
                    note=f"not supplied; {param.derived_when_absent}",
                ))
            continue
        out.append(SyntheticInput(
            param=row.name,
            value=_provenance_value(row.value),
            units=row.units,
            basis=row.origin,
            note=(f"{param.desc} [{row.note}]" if row.note else param.desc),
        ))
    return out


def merge_provenance(existing: Sequence[SyntheticInput],
                     declared: Sequence[SyntheticInput]) -> list[SyntheticInput]:
    """Merge a composite step's own provenance rows with the declared rows.
    The composite's row WINS on a name collision - it stamped what actually
    resolved, while the declaration only knows what was asked for."""
    kept = list(existing)
    taken = {row.param for row in kept}
    kept.extend(row for row in declared if row.param not in taken)
    return kept


def _provenance_value(value: Any) -> Any:
    # A provenance row's value is a scalar or a string, so a coordinate pair travels as its text.
    rendered = wire_value(value)
    if isinstance(rendered, dict):
        return ", ".join(f"{k}={v}" for k, v in rendered.items() if v is not None)
    return str(rendered) if isinstance(rendered, (list, bool)) else rendered
