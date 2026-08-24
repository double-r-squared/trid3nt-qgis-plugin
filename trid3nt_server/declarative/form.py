"""The resolved param sheet as the FORM CARD payload.

The provenance table narrates what a value IS; this builds the EDIT SURFACE for
it - the declaration (label, bounds, units, door) travelling beside the resolved
value so the card can render a property grid the user edits in place.
"""

from __future__ import annotations

from typing import Sequence

from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from .params import Param, ResolvedParam, ResolvedParams, doors, wire_value

__all__ = ["build_param_sheet", "source_badge"]


#: Render rank per door: the question the user asked first, the solver's own
#: constants last. The card folds the tail rank under "advanced".
_RANK = {doors.QUESTION: 0, doors.USER: 0, doors.GATE: 1,
         doors.DERIVED: 2, doors.SCENARIO: 3, doors.CONSTANT: 4}


def build_param_sheet(workflow: str, title: str, declared: Sequence[Param],
                      resolved: ResolvedParams) -> ParamSheet | None:
    """The sheet a ``FormGate`` presents, in render order. ``None`` when empty.

    Every row is editable (a derived row warns through its badge rather than
    locking), and CONSTANT-door rows carry ``advanced=True`` so the card folds
    the non-question physics away without hiding it.
    """
    by_name = {p.name: p for p in declared}
    rows = [
        _row(by_name[row.name], row)
        for row in sorted(resolved.rows(),
                          key=lambda r: (_RANK.get(_door_of(by_name, r), 5), r.name))
        if row.name in by_name
    ]
    if not rows:
        return None
    return ParamSheet(workflow=workflow, title=title or f"Review the inputs for {workflow}",
                      rows=rows)


def _door_of(by_name: dict[str, Param], row: ResolvedParam) -> str:
    """The DECLARED door, not the one this value happened to come through.

    A scenario param the caller passed explicitly resolves through the USER door,
    but it is still the same question-shaped row on the form, and a sheet that
    reshuffled itself because the user filled one cell would be unreadable.
    """
    return by_name[row.name].door


def _row(param: Param, resolved: ResolvedParam) -> ParamSheetRow:
    return ParamSheetRow(
        name=param.name,
        value=wire_value(resolved.value),
        units=param.units,
        desc=param.desc,
        door=param.door,
        basis=resolved.basis,
        source_badge=source_badge(param, resolved),
        bounds=param.bounds,
        user_lever=param.user_lever,
        editable=True,
        advanced=param.door == doors.CONSTANT,
        note=resolved.note or None,
    )


def source_badge(param: Param, resolved: ResolvedParam) -> str:
    """The short phrase the card shows beside the value: where it CAME FROM.

    Rendered server-side because the server owns the doors - a client
    re-deriving "derived from what" out of ``basis`` would be guessing at a
    declaration it cannot see.
    """
    if resolved.value is None:
        return "not supplied" if param.optional else "REQUIRED - not yet supplied"
    if resolved.basis == "user":
        return "you supplied this"
    if resolved.basis == "prompt_interpreted":
        return "read from your prompt"
    if resolved.basis == "fetched":
        return (f"fetched from {resolved.real_source}" if resolved.real_source
                else "fetched from real data")
    if resolved.basis == "derived":
        # The EVIDENCE first: a derivation that read the world reports what it
        # read, and naming the function instead ("derived by aquifer_k_ms") tells
        # the user only the name of the row they are already looking at.
        if resolved.real_source:
            return f"derived from {resolved.real_source}"
        return f"derived by {_tail(param.resolve)}" if param.resolve else "derived"
    if resolved.consequence == "physics":
        return "labeled default - NOT a site measurement"
    return "labeled default"


def _tail(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]
