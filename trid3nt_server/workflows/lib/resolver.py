"""The param resolver: the six doors, in order, with bounds clamping.

Replaces the per-workflow ``try/except/clamp`` blocks. Every resolution leaves a
provenance row; a clamp leaves a note naming the declared bound it hit.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import replace
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput

from .errors import GateRefusedError
from .params import (
    Derived,
    Param,
    ParamNotResolved,
    ParamValues,
    ResolvedParam,
    ResolvedParams,
    doors,
    refuse_duplicate_params,
    wire_value,
)

__all__ = ["merge_provenance", "provenance_entries", "rederive_revised",
           "reseat_revised", "resolve_params"]


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
    out = fn(ParamValues(dict(rows)))
    if inspect.isawaitable(out):
        out = await out
    return out


def _seat_derived(param: Param, produced: Any, default_note: str) -> ResolvedParam:
    """Seat a derivation's output, keeping whatever EVIDENCE it returned with it.

    A derivation that read the world returns :class:`Derived`; a pure one returns
    the bare value and the declaration's own note stands.
    """
    if isinstance(produced, Derived):
        return _finish(param, produced.value, doors.DERIVED,
                       produced.note or default_note,
                       real_source=produced.real_source)
    return _finish(param, produced, doors.DERIVED, default_note)


def reseat_revised(declared: Sequence[Param], resolved: ResolvedParams,
                   revised: Mapping[str, Any],
                   *, note: str = "revised at input review",
                   ) -> tuple[ResolvedParams, list[str]]:
    """Re-seat values a user gave at a gate, through the GATE door.

    The declared bounds and the non-numeric refusal still apply - a gate is an
    answer surface, not a bypass - and every genuinely changed row is re-stamped
    ``basis=user`` so the run's provenance says the user set it. ``note`` is what
    the row records about HOW it was answered (edited on the form, drawn on the
    canvas); it reaches the provenance entry the result carries. Returns the new
    sheet plus the names that actually changed. Names that are not declared params
    cannot be seated and are reported by the caller, never silently absorbed.
    """
    by_name = {p.name: p for p in declared}
    rows: dict[str, ResolvedParam] = {}
    changed: list[str] = []
    for name, value in revised.items():
        param = by_name.get(name)
        if param is None or resolved.row(name) is None:
            continue
        if resolved.value_of(name) == value:
            continue
        rows[name] = _finish(param, value, doors.GATE, note)
        changed.append(name)
    return (resolved.replacing(rows) if rows else resolved), changed


async def rederive_revised(
    declared: Sequence[Param], resolved: ResolvedParams, changed: Sequence[str],
) -> tuple[ResolvedParams, list[str], list[str]]:
    """Re-run the derivations over an APPROVED sheet, to the same fixpoint.

    A revision that leaves derived rows on their pre-revision values ships a sheet
    that contradicts itself - saturation computed from 20 C beside an approved
    30 C. Derived rows therefore re-derive against the approved values, with a
    note naming the revision.

    The user always wins: a row the user supplied or edited (``basis=user``) is
    PINNED and never recomputed. When the revised sheet would now derive something
    else for such a row, the pin stands and the row's note says so - a silent
    overwrite of an explicit edit is the same swallow this library exists to
    outlaw.

    Returns the new sheet, the names that actually RE-DERIVED (they are revisions
    too, so dependent data is evicted on them), and the conflict notes.
    """
    derived = [p for p in declared if p.door == doors.DERIVED]
    if not changed or not derived:
        return resolved, [], []

    rows: dict[str, ResolvedParam] = {r.name: r for r in resolved.rows()}
    pinned = {name for name, row in rows.items() if row.basis == "user"}
    revision = ", ".join(sorted(changed))
    updates: dict[str, ResolvedParam] = {}
    rederived: list[str] = []

    # Derivations may read each other, so re-run to a fixpoint exactly as the
    # first resolution did - one pass would leave a chain half re-derived.
    for _ in range(len(derived) + 1):
        progressed = False
        for param in derived:
            current = rows.get(param.name)
            if current is None or param.name in pinned:
                continue
            fresh = await _rederive_row(param, rows, current,
                                        f"re-derived by {param.resolve} after "
                                        f"input review revised {revision}")
            if fresh is None:
                continue
            rows[param.name] = updates[param.name] = fresh
            if param.name not in rederived:
                rederived.append(param.name)
            progressed = True
        if not progressed:
            break

    notes: list[str] = []
    for param in derived:
        current = rows.get(param.name)
        if current is None or param.name not in pinned:
            continue
        fresh = await _rederive_row(param, rows, current, "")
        if fresh is None:
            continue
        note = (f"the sheet approved at input review would derive "
                f"{wire_value(fresh.value)}, but this value was set explicitly "
                "and stands")
        rows[param.name] = updates[param.name] = replace(
            current, note=f"{current.note}; {note}" if current.note else note)
        notes.append(f"{param.name}: {note}")

    return (resolved.replacing(updates) if updates else resolved), rederived, notes


async def _rederive_row(param: Param, rows: Mapping[str, ResolvedParam],
                        current: ResolvedParam, note: str) -> ResolvedParam | None:
    """Re-run one derivation; ``None`` when it cannot run yet or lands unchanged.

    Compared AFTER ``_finish``, so a derivation whose raw value moves but clamps
    back onto the same declared bound is correctly read as unchanged.
    """
    try:
        value = await _derive(param, rows)
    except ParamNotResolved:
        return None
    if value is None:
        return None
    fresh = _seat_derived(param, value, note)
    return None if fresh.value == current.value else fresh


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
    source = real_source if real_source is not None else param.real_source
    return ResolvedParam(
        name=param.name, value=value, door=door, basis=basis,
        units=param.units, consequence=param.consequence, note=note,
        clamped_from=clamped_from,
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

    The composite's row WINS on a name collision: it stamped what actually
    resolved (``basis=fetched`` once the data landed), while the declaration only
    knows what was asked for. Two rows for one param is a contradiction, not a
    record.
    """
    kept = list(existing)
    taken = {row.param for row in kept}
    kept.extend(row for row in declared if row.param not in taken)
    return kept


def _provenance_value(value: Any) -> Any:
    # Rendered by the one shared rule, then flattened: a provenance row's value is
    # a scalar or a string, so a coordinate pair travels as its text.
    rendered = wire_value(value)
    return str(rendered) if isinstance(rendered, (list, bool)) else rendered
