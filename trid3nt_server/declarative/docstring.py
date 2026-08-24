"""Render a registered tool's docstring from its declarations, in TWO views.

The routing block is emitted FIRST: Bedrock truncates a tool docstring at 1000
characters, so whatever the model needs to ROUTE has to survive the cut. The two
views are that split made explicit -

* ``routing`` - which question this tool answers and which it does not. What a
  surface that only has to help someone CHOOSE the tool needs. The enforced
  budget covers the PRE-``Returns:`` front (summary + routing + negative
  routing), not the rendered view: the returns line rides after it and may run
  the whole string past 1000 characters, which is the point - what the cut can
  take is the part the model does not need to route.
* ``full`` (default) - the routing block plus the param sheet in prose. What the
  model calling the tool needs, because it has to fill those params.

The form card carries the SAME sheet structurally (``declarative.form``), so a
user reviewing a run reads the declaration itself - bounds, units and source
badge - rather than this prose rendering of it.
"""

from __future__ import annotations

from typing import Literal, Sequence

from .params import Param, doors

__all__ = ["render_docstring"]

_FRONT_BUDGET = 1000

#: Which rendering a surface asks for. See the module docstring for who reads which.
DocstringView = Literal["full", "routing"]


def render_docstring(
    *,
    summary: str,
    routing: str,
    params: Sequence[Param],
    returns: str,
    not_for: str = "",
    controls: Sequence[tuple[str, str]] = (),
    view: DocstringView = "full",
) -> str:
    """Build the docstring: summary, routing, negative routing, params, returns.

    ``controls`` documents the run levers that are NOT params (gate mode, restart)
    - the tool accepts them, so the model has to be told they exist. ``view``
    selects the rendering; ``routing`` stops after the returns line.
    """
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
    if controls:
        body += ["", "Run controls:"]
        body += [f"    {name}: {desc.strip()}" for name, desc in controls]
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
