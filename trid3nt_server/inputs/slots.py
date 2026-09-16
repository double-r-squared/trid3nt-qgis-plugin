"""The engine-neutral SLOTS, ingested once: what fills each, and what it becomes.

A slot names the kind it takes and the role it plays. This is the one place that
says which ingestion a role reads through and what the canvas offers for it, so
a workflow that needs a domain asks for one the same way whichever engine solves
over it, and nothing downstream branches on whether it was drawn or fetched.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Mapping

from trid3nt_server.workflows.runtime.data import (
    BED,
    DOMAIN,
    EXTENT,
    OBSERVATION,
    RUNS,
)

from .bed import bed
from .boundary import boundary_runs
from .domain import domain
from .extent import extent
from .observation import observation

__all__ = ["DRAW_PURPOSES", "ask_on_canvas", "ingest_slot"]

logger = logging.getLogger("trid3nt_server.inputs.slots")

#: Each slot's ingestion - the one function that takes every form its value
#: arrives in. A role with no entry is a slot nothing here knows how to read.
_INGESTIONS: Mapping[str, Callable[..., Any]] = {
    DOMAIN: domain,
    BED: bed,
    RUNS: boundary_runs,
    OBSERVATION: observation,
    EXTENT: extent,
}

#: What the canvas offers for a slot the user fills by hand: the draw kind, and
#: the PURPOSE that shows only the tool this slot needs. A role absent here is
#: never asked for - a bed is a survey or a number, not a shape to draw.
DRAW_PURPOSES: Mapping[str, tuple[str, str, str]] = {
    DOMAIN: ("polygon", "domain",
             "Draw the outline of the water body this run solves over"),
    RUNS: ("polyline", "boundary run",
           "Draw each stretch of the edge that carries a boundary condition"),
    EXTENT: ("rectangle", "",
             "Draw the box this question is asked inside"),
}


def ingest_slot(role: str, value: Any, *, label: str = "",
                **coercion: Any) -> Any:
    """One slot's value, through the ingestion its ROLE reads.

    ``coercion`` is what the ROW told its slot about the value - the point a
    nearest site is ranked against, the unit a keyword reads. A role nothing here
    knows returns the value as it came: the slot is then whatever its own
    consumer makes of it."""
    read = _INGESTIONS.get(str(role))
    if read is None:
        return value
    found = read(value, label=label or str(role),
                 **{k: v for k, v in coercion.items() if v is not None})
    if inspect.isawaitable(found):
        # This runs OFF the loop, so an ingestion that reaches a service - a
        # place name to geocode - gets a loop of its own in this thread.
        return asyncio.run(found)
    return found


async def ask_on_canvas(role: str, *, tool: str, param: str,
                        input_mode: Any = None) -> Any:
    """Ask the canvas for a slot the caller did not fill -> the typed value.

    ``None`` whenever there is nothing to ask - no live session, a role the
    canvas offers nothing for, or a declined drawing - so the caller's own
    refusal is what a reader sees, never a geometry nobody drew."""
    offered = DRAW_PURPOSES.get(str(role))
    if offered is None:
        return None
    from trid3nt_server.gates.input_review import resolve_input_gate_mode
    from trid3nt_server.render.pipeline_emitter import current_emitter

    if resolve_input_gate_mode(input_mode) != "user_gated" \
            or current_emitter() is None:
        return None
    from trid3nt_server.gates.draw_input import gate_draw_input

    geometry, purpose, prompt = offered
    outcome = await gate_draw_input(tool_name=tool, param=param,
                                    geometry=geometry, purpose=purpose,
                                    prompt=prompt)
    if not outcome.drawn:
        logger.info("%s: the %s slot was not drawn (%s)", tool, role,
                    outcome.reason)
        return None
    return ingest_slot(role, outcome.value, label=param)
