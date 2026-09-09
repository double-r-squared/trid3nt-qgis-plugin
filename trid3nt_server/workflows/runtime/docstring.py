"""Render a registered tool's docstring from its declarations, in TWO views.

The routing block is emitted FIRST and must fit 1000 characters; ``routing`` stops
after the returns line, ``full`` adds the param sheet in prose.
"""

from __future__ import annotations

from typing import Any, Literal

from .params import Param, doors, param_rows

__all__ = ["render_docstring"]

_FRONT_BUDGET = 1000

#: Which rendering a surface asks for.
DocstringView = Literal["full", "routing"]


def render_docstring(
    *,
    summary: str,
    routing: str,
    params: Any,
    returns: str,
    not_for: str = "",
    sheet: str = "",
    controls: tuple[tuple[str, str], ...] = (),
    context: tuple[tuple[str, str], ...] = (),
    view: DocstringView = "full",
) -> str:
    """Build the docstring: summary, routing, negative routing, params, returns.
    ``sheet``, ``controls`` and ``context`` document the engine surface, the run
    levers that are not params, and the producer-less Data slots on the same wire."""
    head = [summary.strip(), "", routing.strip()]
    if not_for:
        head += ["", f"Do NOT use this for: {not_for.strip()}"]
    front = "\n".join(head)
    if len(front) > _FRONT_BUDGET:
        raise ValueError(
            f"routing block is {len(front)} chars; it must fit the {_FRONT_BUDGET}-char "
            "truncation budget or the model never sees the routing."
        )
    if view == "routing":
        return front + "\n" + "\n".join(["", f"Returns: {returns.strip()}", ""])

    body = ["", "Params:"]
    for p in _ordered(params):
        body.append(f"    {p.name}: {_param_line(p)}")
    if sheet:
        body += ["", sheet.strip()]
    if context:
        body += ["", "Context layers:"]
        body += [f"    {name}: {line.strip()}" for name, line in context]
    if controls:
        body += ["", "Run controls:"]
        body += [f"    {name}: {desc.strip()}" for name, desc in controls]
    body += ["", f"Returns: {returns.strip()}", ""]
    return front + "\n" + "\n".join(body)


def _ordered(params: Any) -> list[Param]:
    """Question-bearing params first; constants last.
    Takes whatever the caller passes - a params sequence or a whole ``PARAMS``
    body - and documents only that, never the full declaration."""
    rank = {doors.QUESTION: 0, doors.USER: 1, doors.GATE: 1,
            doors.SCENARIO: 2, doors.DERIVED: 3, doors.CONSTANT: 4}
    return sorted(param_rows(params), key=lambda p: (rank.get(p.door, 5), p.name))


def _param_line(p: Param) -> str:
    bits = [p.desc.rstrip(".")]
    if p.units:
        bits.append(f"{p.units}")
    if p.bounds is not None:
        bits.append(f"range {p.bounds[0]:g}-{p.bounds[1]:g}")
    if p.default is not None:
        bits.append(f"default {p.default!r}"
                    + (" (labeled scenario default, not a site measurement)"
                       if p.door == doors.SCENARIO else ""))
    elif p.door == doors.DERIVED:
        bits.append("derived when unset")
    elif p.optional:
        bits.append("optional")
    return ", ".join(bits) + "."
