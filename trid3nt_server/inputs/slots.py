"""The engine-neutral SLOTS, ingested once: what fills each, and what it becomes.

A ROW'S NAME is its slot. This is the one list of the reserved names, and it says
for each what ingestion reads it, which data classes its role serves and what the
canvas offers for it - so a workflow that needs a domain asks for one the same way
whichever engine solves over it, and nothing downstream branches on whether it was
drawn, supplied or matched.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from trid3nt_server.workflows.runtime.data import (
    BED,
    DISCHARGE,
    DOMAIN,
    EXTENT,
    LEVEL,
    LINE,
    OBSERVE,
    WAVE,
    WEATHER,
)

from .bed import bed
from .domain import domain, needs_beside
from .extent import extent
from .line import line
from .observation import observation
from .wave import wave

__all__ = ["SLOTS", "Slot", "ask_on_canvas", "ingest_slot", "needs_of",
           "role_of", "takes_op"]

logger = logging.getLogger("trid3nt_server.inputs.slots")


@dataclass(frozen=True, slots=True)
class Slot:
    """One reserved row name: what reads its value, what it may ask the world
    for, and what the canvas offers when the user fills it by hand."""

    #: The one function that takes every form this slot's value arrives in.
    #: ``None`` where the value is read by whatever consumes it, not on the way in.
    ingest: Callable[..., Any] | None = None
    #: The classes a row under this name may state a need of. Empty means the
    #: name states no need at all - a geometry is drawn, supplied or measured.
    classes: frozenset[str] = frozenset()
    #: The draw kind, the PURPOSE that shows only the tool this slot needs, and
    #: the prompt. ``None`` is never asked for - a bed is a survey or a number,
    #: not a shape to draw.
    draw: tuple[str, str, str] | None = None


#: THE RESERVED NAMES. A row named one of these plays that role; every other row
#: is a plain one, read by whatever declared it.
SLOTS: Mapping[str, Slot] = MappingProxyType({
    DOMAIN: Slot(ingest=domain, classes=frozenset({"hydrography"}),
                 draw=("polygon", "domain",
                       "Draw the outline of the water body this run solves over")),
    BED: Slot(ingest=bed,
              classes=frozenset({"bathymetry", "terrain", "channel survey"})),
    # The level and the discharge ARE observations: they are their own names so
    # a workflow can tell which row is which, and they read the same afterwards.
    LEVEL: Slot(ingest=observation, classes=frozenset({"water level series"})),
    DISCHARGE: Slot(ingest=observation, classes=frozenset({"discharge series"})),
    OBSERVE: Slot(ingest=observation,
                  classes=frozenset({"water quality sample", "bed material",
                                     "groundwater level"})),
    LINE: Slot(ingest=line,
               draw=("polyline", "line",
                     "Draw the line this reading is measured along")),
    EXTENT: Slot(ingest=extent,
                 draw=("rectangle", "",
                       "Draw the box this question is asked inside")),
    # A table of weather is read by the composite that expands it onto the run's
    # own clock, so nothing reads it on the way in.
    WEATHER: Slot(classes=frozenset({"weather forcing"})),
    # A sea state is a record of several columns too, but every one of them
    # becomes a keyword the deck states, so the turn from the record's words to
    # the engine's is this slot's ingestion.
    WAVE: Slot(ingest=wave, classes=frozenset({"wave series"})),
})


def role_of(name: str) -> str:
    """The role a row called ``name`` plays, or "" for a plain row."""
    return str(name) if str(name) in SLOTS else ""


def takes_op(role: str) -> bool:
    """Whether this slot's ingestion reads an OP - the move a run states for
    filling the slot where nothing measured it.

    A slot that declares none is never handed one, so the runtime refuses an op
    stated for it rather than dropping it the way an unread coercion key is."""
    slot = SLOTS.get(str(role))
    return bool(slot and slot.ingest
                and "op" in inspect.signature(slot.ingest).parameters)


def needs_of(role: str, kind: str) -> tuple[tuple[str, str, str], ...]:
    """The rows a slot of this ROLE declares beside the one its own class
    matched, for a value of this KIND.

    Only the domain declares any, and only for the kinds that do not arrive
    closed. The runtime produces them through the match and hands each back
    under its own name; the slot states the need and never fetches."""
    return needs_beside(kind) if str(role) == DOMAIN else ()


def ingest_slot(role: str, value: Any, *, label: str = "",
                **coercion: Any) -> Any:
    """One slot's value, through the ingestion its ROLE reads.

    ``coercion`` is what the RUN told this slot about the value - the point a
    nearest site is ranked against, the unit the keyword it fills reads. A role
    nothing here reads returns the value as it came: the slot is then whatever
    its own consumer makes of it. An ingestion is handed the keys IT declares:
    the point a row is asked at is the ASK's, and only the ingestion that ranks
    a nearest site reads it."""
    slot = SLOTS.get(str(role))
    if slot is None or slot.ingest is None:
        return value
    takes = inspect.signature(slot.ingest).parameters
    found = slot.ingest(value, label=label or str(role),
                        **{k: v for k, v in coercion.items()
                           if v is not None and k in takes})
    if inspect.isawaitable(found):
        # This runs OFF the loop, so an ingestion that reads a stored layer
        # gets a loop of its own in this thread.
        return asyncio.run(found)
    return found


async def ask_on_canvas(role: str, *, tool: str, param: str,
                        input_mode: Any = None) -> Any:
    """Ask the canvas for a slot the caller did not fill -> the typed value.

    ``None`` whenever there is nothing to ask - no live session, a role the
    canvas offers nothing for, or a declined drawing - so the caller's own
    refusal is what a reader sees, never a geometry nobody drew."""
    slot = SLOTS.get(str(role))
    if slot is None or slot.draw is None:
        return None
    from trid3nt_server.gates.input_review import resolve_input_gate_mode
    from trid3nt_server.render.pipeline_emitter import current_emitter

    if resolve_input_gate_mode(input_mode) != "user_gated" \
            or current_emitter() is None:
        return None
    from trid3nt_server.gates.draw_input import gate_draw_input

    geometry, purpose, prompt = slot.draw
    outcome = await gate_draw_input(tool_name=tool, param=param,
                                    geometry=geometry, purpose=purpose,
                                    prompt=prompt)
    if not outcome.drawn:
        logger.info("%s: the %s slot was not drawn (%s)", tool, role,
                    outcome.reason)
        return None
    return ingest_slot(role, outcome.value, label=param)
