"""The param resolver: the six doors, in order, held to the declared bounds.

Every resolution leaves a provenance row; a value outside its declared bounds
refuses by name rather than being moved quietly onto the bound.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from .errors import GateRefusedError
from .params import (
    Derived,
    Param,
    ParamNotResolved,
    ParamRef,
    ParamValues,
    ResolvedParam,
    ResolvedParams,
    doors,
    param_rows,
    refuse_duplicate_params,
    wire_value,
)

__all__ = ["merge_provenance", "provenance_entries", "resolve_params"]


async def resolve_params(
    declared: Any,
    supplied: Mapping[str, Any],
    *,
    question: Mapping[str, Any] | None = None,
) -> ResolvedParams:
    """Walk the doors for every declared param and return the resolved sheet.
    ``supplied`` is door 1 and NEVER ambient - no case-store lookup; ``question``
    is door 2, the agent-filled values from the ask."""
    # The door ORDER is precedence, not evaluation order: a derivation may read any
    # other param, so labeled defaults are seated before derivations run and a
    # derived param competes only with its own fallbacks, never another param's.
    declared = param_rows(declared)
    refuse_duplicate_params(declared)
    rows: dict[str, ResolvedParam] = {}

    for param in declared:
        value, door, note = _door_1_2(param, supplied, question or {})
        if value is not None:
            rows[param.name] = _finish(param, value, door, note)

    for param in declared:
        if param.name in rows or param.door == doors.DERIVED or param.default is None:
            continue
        rows[param.name] = _finish(param, param.default, param.door,
                                   f"declared {param.door} default",
                                   basis=_BASIS_DEFAULT)

    # Derivations may read each other; resolve to a fixpoint rather than pinning
    # PARAMS to a dependency-sorted order.
    pending = [p for p in declared if p.name not in rows and p.door == doors.DERIVED]
    while pending:
        progressed = []
        for param in pending:
            try:
                value = await _derive(param, rows)
            except ParamNotResolved:
                # ONLY a missing param means "wait for the next pass"; any other
                # AttributeError is a bug inside the derivation and propagates.
                continue
            progressed.append(param)
            if value is not None:
                rows[param.name] = _seat_derived(
                    param, value, f"derived by {param.resolve}")
        if not progressed:
            raise GateRefusedError(
                "derivations "
                + ", ".join(sorted(p.name for p in pending))
                + " cannot resolve: each one reads a param that never arrives."
            )
        pending = [p for p in pending if p not in progressed and p.name not in rows]

    for param in declared:
        if param.name in rows:
            continue
        if param.default is not None:
            rows[param.name] = _finish(
                param, param.default, param.door,
                f"declared {param.door} default",
                basis=_BASIS_DEFAULT,
            )
            continue
        # The sixth door: a value with no door left is ASKED FOR (a gate) or REFUSED typed -
        # never invented. The refusal is the interpreter's, once the plan's gates
        # have had their turn.
        rows[param.name] = ResolvedParam(
            name=param.name, value=None, door=param.door, basis=param.basis,
            units=param.units, consequence=param.consequence,
            note=("not supplied (declared optional)" if param.optional
                  else "REQUIRED and not supplied"),
            real_source=param.real_source,
            required_missing=not param.optional,
        )

    return ResolvedParams(rows)


def _door_1_2(param: Param, supplied: Mapping[str, Any],
              question: Mapping[str, Any]) -> tuple[Any, str, str]:
    if param.name in supplied and supplied[param.name] is not None:
        return supplied[param.name], doors.USER, "supplied on this invocation"
    if param.name in question and question[param.name] is not None:
        return question[param.name], doors.QUESTION, "read from the ask"
    return None, param.door, ""


async def _derive(param: Param, rows: Mapping[str, ResolvedParam]) -> Any:
    fn = _load(param.resolve or "")
    values = ParamValues(dict(rows))
    # A DECLARED binding is the same shape a step's kwargs are: a ParamRef reads
    # the sheet, anything else is the value itself. Reading a row that is not
    # seated yet raises ParamNotResolved, which is this pass saying "wait".
    out = fn(values) if param.resolve_kwargs is None else fn(**{
        name: getattr(values, read.name) if isinstance(read, ParamRef) else read
        for name, read in param.resolve_kwargs.items()})
    if inspect.isawaitable(out):
        out = await out
    return out


def _seat_derived(param: Param, produced: Any, default_note: str) -> ResolvedParam:
    """Seat a derivation's output, keeping whatever EVIDENCE it returned with it.
    A derivation that read the world returns :class:`Derived`; a pure one returns
    the bare value and the declaration's own note stands."""
    if isinstance(produced, Derived):
        return _finish(param, produced.value, doors.DERIVED,
                       produced.note or default_note,
                       real_source=produced.real_source)
    return _finish(param, produced, doors.DERIVED, default_note)


def _load(dotted: str) -> Any:
    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        raise GateRefusedError(f"resolve path {dotted!r} is not a dotted import path.")
    return getattr(importlib.import_module(module_path), attr)


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
    if door == doors.DERIVED:
        return "derived"
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
