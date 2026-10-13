"""Render a registered tool's docstring from its declarations, in TWO views.

The routing block is emitted FIRST and must fit 1000 characters; ``routing`` stops
after the returns line, ``full`` adds every input in prose.
"""

from __future__ import annotations

from typing import Literal, Sequence

from trid3nt_server.inputs.input import Input

__all__ = ["render_docstring"]

_FRONT_BUDGET = 1000

DocstringView = Literal["full", "routing"]


def render_docstring(
    *,
    summary: str,
    routing: str,
    inputs: Sequence[Input],
    returns: str,
    not_for: str = "",
    sheet: str = "",
    controls: tuple[tuple[str, str], ...] = (),
    view: DocstringView = "full",
) -> str:
    """Build the docstring: summary, routing, negative routing, the inputs, returns.
    ``sheet`` documents the engine surface and ``controls`` the run controls; a control
    written under an input's name is said on that input's line, so each is documented once."""
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

    said = dict(controls)
    body = ["", "Params:"]
    for p in _ordered(inputs):
        body.append("    " + f"{p.name}: {p.doc_line} {said.pop(p.name, '')}".strip())
    if sheet:
        body += ["", sheet.strip()]
    if said:
        body += ["", "Run controls:"]
        body += [f"    {name}: {desc.strip()}" for name, desc in said.items()]
    body += ["", f"Returns: {returns.strip()}", ""]
    return front + "\n" + "\n".join(body)


def _ordered(inputs: Sequence[Input]) -> list[Input]:
    """Every input on the wire: required first, then defaulted, then the optional values and the rows, each in body order."""
    return sorted((p for p in inputs if p.fills_from_user),
                  key=lambda p: 0 if p.required else 1 if p.default is not None else 2)
