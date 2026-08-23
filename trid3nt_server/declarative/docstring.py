"""Render a registered tool's LLM-facing docstring from its declarations.

The routing block is emitted FIRST: Bedrock truncates a tool docstring at 1000
characters, so whatever the model needs to ROUTE has to survive the cut.
"""

from __future__ import annotations

from typing import Sequence

from .params import Param, doors

__all__ = ["render_docstring"]

_FRONT_BUDGET = 1000


def render_docstring(
    *,
    summary: str,
    routing: str,
    params: Sequence[Param],
    returns: str,
    not_for: str = "",
) -> str:
    """Build the docstring: summary, routing, negative routing, params, returns."""
    head = [summary.strip(), "", routing.strip()]
    if not_for:
        head += ["", f"Do NOT use this for: {not_for.strip()}"]
    front = "\n".join(head)
    if len(front) > _FRONT_BUDGET:
        raise ValueError(
            f"routing block is {len(front)} chars; it must fit the {_FRONT_BUDGET}-char "
            "truncation budget or the model never sees the routing."
        )

    body = ["", "Params:"]
    for p in _ordered(params):
        body.append(f"    {p.name}: {_param_line(p)}")
    body += ["", f"Returns: {returns.strip()}", ""]
    return front + "\n" + "\n".join(body)


def _ordered(params: Sequence[Param]) -> list[Param]:
    """Question-bearing params first; constants last (the 'advanced' fold, in prose)."""
    rank = {doors.QUESTION: 0, doors.USER: 1, doors.GATE: 1,
            doors.SCENARIO: 2, doors.DERIVED: 3, doors.CONSTANT: 4}
    return sorted(params, key=lambda p: (rank.get(p.door, 5), p.name))


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
