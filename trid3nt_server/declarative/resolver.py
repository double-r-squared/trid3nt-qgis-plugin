"""The param resolver: the six doors, in order, with bounds clamping.

Replaces the per-workflow ``try/except/clamp`` blocks. Every resolution leaves a
provenance row; a clamp leaves a note naming the declared bound it hit.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from .errors import GateRefusedError
from .params import (
    Param,
    ParamNotResolved,
    ResolvedParam,
    ResolvedParams,
    doors,
    refuse_duplicate_params,
)

__all__ = ["merge_provenance", "provenance_entries", "resolve_params"]


async def resolve_params(
    declared: Sequence[Param],
    supplied: Mapping[str, Any],
    *,
    question: Mapping[str, Any] | None = None,
) -> ResolvedParams:
    """Walk the doors for every declared param and return the resolved sheet.

    ``supplied`` is what this invocation explicitly passed (door 1 - NEVER ambient:
    no case-store lookup). ``question`` carries agent-filled values from the ask
    (door 2).

    Door ORDER is precedence, not evaluation order: a derivation may read any
    other param, so labeled defaults are seated before derivations run and a
    derived param competes only with its own fallbacks, never another param's.
    """
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
                rows[param.name] = _finish(param, value, doors.DERIVED,
                                           f"derived by {param.resolve}")
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
        # Door 6: a value with no door left is ASKED FOR (a gate) or REFUSED typed -
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
    view = ResolvedParams(dict(rows))
    out = fn(view)
    if inspect.isawaitable(out):
        out = await out
    return out


def _load(dotted: str) -> Any:
    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        raise GateRefusedError(f"resolve path {dotted!r} is not a dotted import path.")
    return getattr(importlib.import_module(module_path), attr)


#: A value seated from its own DECLARED DEFAULT is a labeled default whatever door
#: it hangs under - the door says who may override it, not where this value came from.
_BASIS_DEFAULT = "default_demo"


def _finish(param: Param, value: Any, door: str, note: str, *,
            basis: str | None = None) -> ResolvedParam:
    clamped_from = None
    if param.bounds is not None:
        coerced = _as_float(value)
        if coerced is None:
            raise GateRefusedError(
                f"{param.name}={value!r} is not a number, but declares bounds "
                f"{param.bounds} ({param.units or 'no units'}). It is not silently defaulted."
            )
        lo, hi = float(param.bounds[0]), float(param.bounds[1])
        pinned = min(max(coerced, lo), hi)
        if pinned != coerced:
            clamped_from = coerced
            note = (f"{note}; CLAMPED from {coerced:g} to the declared "
                    f"{'minimum' if pinned == lo else 'maximum'} {pinned:g}"
                    f"{' ' + param.units if param.units else ''}").lstrip("; ")
        value = pinned
    if basis is None:
        basis = "user" if door in (doors.USER, doors.GATE) else _basis(param, door)
    return ResolvedParam(
        name=param.name, value=value, door=door, basis=basis,
        units=param.units, consequence=param.consequence, note=note,
        clamped_from=clamped_from, real_source=param.real_source,
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
                       declared: Sequence[Param]) -> list[SyntheticInput]:
    """The run's provenance rows - what the input-review gate and the layer carry.

    A ``default_demo`` + ``physics`` row is what makes the gate refuse in auto mode
    (law 9). An absent param that declares ``derived_when_absent`` still leaves a
    derived-basis row: the user has to see what the run measured against.
    """
    by_name = {p.name: p for p in declared}
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
            value=_wire_value(row.value),
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

    The composite's row WINS on a name collision: it stamped what actually
    resolved (``basis=fetched`` once the data landed), while the declaration only
    knows what was asked for. Two rows for one param is a contradiction, not a
    record.
    """
    kept = list(existing)
    taken = {row.param for row in kept}
    kept.extend(row for row in declared if row.param not in taken)
    return kept


def _wire_value(value: Any) -> Any:
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 4)
    return str(value)
