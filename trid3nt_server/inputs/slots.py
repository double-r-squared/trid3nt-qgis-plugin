"""The engine-neutral SLOTS, ingested once: what fills each, and what it becomes.

A ROW'S NAME is its slot; this is the one list of the reserved names, with the
ingestion, data classes and canvas offer of each. Nothing downstream branches on
whether a value was drawn, supplied or matched.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

__all__ = ["BED", "DISCHARGE", "DOMAIN", "EXTENT", "LEVEL", "LINE", "OBSERVE",
           "SLOTS", "Slot", "WAVE", "WEATHER", "ask_on_canvas", "draw_on_canvas",
           "ingest_slot", "needs_of", "role_of"]

logger = logging.getLogger("trid3nt_server.inputs.slots")

#: THE RESERVED ROW NAMES. A row's NAME is its slot: these are the names :data:`SLOTS`
#: keys the role behaviour off. The named stretches of the edge are no row: they ride
#: on the polygon the domain arrived as.
DOMAIN = "domain"
BED = "bed"

#: The LINE a placed read is measured along.
LINE = "line"

#: The slot a run OPENS ON: one measured value read off whatever reports it near this
#: domain, in the unit the keyword the role fills reads.
OBSERVE = "observe"

#: The two observations a solve READS BY ROLE rather than by name: the elevation the
#: water surface stands at, and the flow an inflow run carries.
LEVEL = "level"
DISCHARGE = "discharge"

#: The RECTANGLE a question is asked inside: the window a domain is cut out of, the grid
#: a raster engine solves on. Not a domain: it has no shoreline.
EXTENT = "extent"

#: The record of the air over the domain, read by the composite that puts it on the
#: run's own clock. Nothing ingests it on the way in.
WEATHER = "weather"

#: The sea state at the open edge: one record, several columns, measured somewhere near;
#: its ingestion turns those columns into the keywords a spectral deck forces.
WAVE = "wave"


@dataclass(frozen=True, slots=True)
class Slot:
    """One reserved row name: what reads its value, what it may ask the world
    for, and what the canvas offers when the user fills it by hand."""

    #: The inputs module whose same-named function takes every form this slot's value
    #: arrives in; "" where the value is read by whatever consumes it. Named rather than
    #: imported: the runtime reads these facts while the ingestions are still importing.
    ingestion: str = ""
    #: The classes a row under this name may state a need of. Empty means the
    #: name states no need at all - a geometry is drawn, supplied or measured.
    classes: frozenset[str] = frozenset()
    #: The draw kind, the PURPOSE that shows only the tool this slot needs, and
    #: the prompt. ``None`` is never asked for - a bed is a survey or a number,
    #: not a shape to draw.
    draw: tuple[str, str, str] | None = None
    #: The value is READ OFF A RECORD somebody measured, so what its row
    #: observes is a published variable and is read in that variable's unit;
    #: on any other slot ``of`` names the feature a source publishes.
    record: bool = False
    #: The caller may state the value as the NUMBER itself - the level the
    #: water opens at, the depth of a pond - rather than a layer to read it off.
    number: bool = False

    @property
    def ingest(self) -> Callable[..., Any] | None:
        """The ingestion function itself, or ``None``."""
        if not self.ingestion:
            return None
        module = importlib.import_module(f"{__package__}.{self.ingestion}")
        return getattr(module, self.ingestion)


#: THE RESERVED NAMES. A row named one of these plays that role; every other row
#: is a plain one, read by whatever declared it.
SLOTS: Mapping[str, Slot] = MappingProxyType({
    DOMAIN: Slot(ingestion="domain", classes=frozenset({"hydrography"}),
                 draw=("polygon", "domain",
                       "Draw the outline of the water body this run solves over")),
    BED: Slot(ingestion="bed",
              classes=frozenset({"bathymetry", "terrain", "channel survey"}),
              number=True),
    # The level and the discharge ARE observations: they are their own names so
    # a workflow can tell which row is which, and they read the same afterwards.
    LEVEL: Slot(ingestion="observation",
                classes=frozenset({"water level series"}),
                record=True, number=True),
    DISCHARGE: Slot(ingestion="observation",
                    classes=frozenset({"discharge series"}),
                    record=True, number=True),
    OBSERVE: Slot(ingestion="observation",
                  classes=frozenset({"water quality sample", "bed material",
                                     "groundwater level"}),
                  record=True, number=True),
    LINE: Slot(ingestion="line",
               draw=("polyline", "line",
                     "Draw the line this reading is measured along")),
    EXTENT: Slot(ingestion="extent",
                 draw=("rectangle", "",
                       "Draw the box this question is asked inside")),
    # A table of weather is read by the composite that expands it onto the run's
    # own clock, so nothing reads it on the way in.
    WEATHER: Slot(classes=frozenset({"weather forcing"})),
    # A sea state is a record of several columns too, but every one of them
    # becomes a keyword the deck states, so the turn from the record's words to
    # the engine's is this slot's ingestion.
    WAVE: Slot(ingestion="wave", classes=frozenset({"wave series"}), record=True),
})


def role_of(name: str) -> str:
    """The role a row called ``name`` plays, or "" for a plain row."""
    return str(name) if str(name) in SLOTS else ""


def needs_of(role: str, kind: str) -> tuple[tuple[str, str, str], ...]:
    """The rows a slot of this ROLE declares beside the one its own class
    matched, for a value of this KIND.

    Only the domain declares any; the slot states the need and never fetches."""
    if str(role) != DOMAIN:
        return ()
    from .domain import needs_beside

    return needs_beside(kind)


def ingest_slot(role: str, value: Any, *, label: str = "",
                **coercion: Any) -> Any:
    """One slot's value, through the ingestion its ROLE reads.

    ``coercion`` is what the RUN told the slot; an ingestion is handed only the keys
    IT declares. A role nothing reads returns the value as it came."""
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

    ``None`` when there is nothing to ask (no session, no canvas offer, a declined drawing)."""
    slot = SLOTS.get(str(role))
    if slot is None or slot.draw is None:
        return None
    geometry, purpose, prompt = slot.draw
    drawn = await draw_on_canvas(geometry, tool=tool, param=param,
                                 prompt=prompt, purpose=purpose,
                                 input_mode=input_mode)
    return None if drawn is None else ingest_slot(role, drawn, label=param)


async def draw_on_canvas(geometry: str, *, tool: str, param: str, prompt: str,
                         purpose: str | None = None,
                         input_mode: Any = None) -> Any:
    """The one canvas ask: the drawn value as it came, or ``None`` - outside a
    live ``user_gated`` session, or when nothing was drawn."""
    from trid3nt_server.inputs.gate.input_review import resolve_input_gate_mode
    from trid3nt_server.render.pipeline_emitter import current_emitter

    if resolve_input_gate_mode(input_mode) != "user_gated" \
            or current_emitter() is None:
        return None
    from trid3nt_server.inputs.gate.draw_input import gate_draw_input

    outcome = await gate_draw_input(tool_name=tool, param=param,
                                    geometry=geometry, purpose=purpose,
                                    prompt=prompt)
    if not outcome.drawn:
        logger.info("%s: %s was not drawn (%s)", tool, param, outcome.reason)
        return None
    return outcome.value
