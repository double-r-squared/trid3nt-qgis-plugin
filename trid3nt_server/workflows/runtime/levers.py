"""The levers the RUNTIME declares once, for every template that runs on it.

A template declares no Param that is not its own question's input. What moment
the scenario is read at, how finely the domain is resolved, what the run is sized
on and the axis its elevations are counted from are the runtime's, so twelve
templates state them once here; a template that needs a different DEFAULT
declares its own row and that row wins. A value the engine's own dictionary
carries is NOT a lever: the deck states that keyword and the user overrides it by
the keyword's name.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Sequence

from .errors import CoresUnavailable, PlanValidationError
from .params import Param, doors

__all__ = ["BOX_CORES", "LEVERS", "LEVER_NAMES", "VERTICAL_FRAME",
           "cores_asked", "lever", "run_frame", "with_levers"]

#: The vertical frame a run counts elevations from where nothing states another.
#: NAVD88 is what the national terrain, the bathymetry and the gauges this
#: substrate reaches are published on.
VERTICAL_FRAME = "NAVD88"

#: The cores this box can partition a solve across. A count past it is refused
#: by name: a solve quietly cut to fit runs on a partition nobody was told about.
BOX_CORES: int = os.cpu_count() or 1

#: The declared levers, in the order a card reads them. Each one is a value the
#: skeleton or the mesh front reads, never a value a question asks about.
LEVERS: tuple[Param, ...] = (
    Param(name="mesh_resolution_m", door=doors.SCENARIO, default=14.0,
          bounds=(3.0, 5000.0), units="m", consequence="numerical",
          user_lever=True,
          desc="Target element edge or cell length the domain is resolved at. "
               "The granularity is the USER's lever: nothing derives an "
               "edge from a channel nobody surveyed, so the number the run "
               "meshes at is either yours or this labeled default"),
    Param(name="event_time", door=doors.QUESTION, optional=True,
          consequence="scenario",
          derived_when_absent=(
              "a surface is read at the most recent cycle its own source has "
              "published, and no series source is asked at all - a record is a "
              "reading at a time, and this run stated none"),
          desc="The moment the scenario is read at - an ISO date or datetime "
               "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ), from phrasing like "
               "'during last Tuesday's storm'. Each source keeps its own "
               "retention, and a request deeper than one refuses typed"),
    Param(name="cores", door=doors.CONSTANT, optional=True, type=int,
          bounds=(1.0, float(BOX_CORES)), consequence="numerical",
          derived_when_absent=(
              "the solve is partitioned across the number of processors the "
              "engine's own dictionary states for this module"),
          desc="How many cores the solve is partitioned across. Absent, the "
               "module's own processors keyword stands; a count past this "
               "box's cores is refused rather than cut down, and an engine "
               "that solves on one core says so on the card"),
    # NUMERICAL, not physics: this is the AXIS every elevation is placed on, and
    # its default is a published national frame rather than a number nobody
    # measured - what law 9 refuses in auto mode is an invented world, and a
    # source that cannot reach this frame refuses here by name instead.
    Param(name="vertical_frame", door=doors.CONSTANT, default=VERTICAL_FRAME,
          consequence="numerical",
          desc="Vertical datum this run counts every elevation from - the bed "
               "under it and the level over it. A source published on another "
               "frame reaches this one through a measured offset, and a pair "
               "nobody publishes an offset between refuses by name"),
)


def cores_asked(stated: Any) -> int | None:
    """The core count this run STATES, checked against the box - ``None`` for none.

    ``None`` is not one core: it leaves the module's own processors keyword
    standing, which is the only place a default for it lives."""
    if stated is None or (isinstance(stated, str) and not stated.strip()):
        return None
    try:
        asked = int(str(stated).strip())
    except (TypeError, ValueError):
        raise CoresUnavailable(
            f"cores {stated!r} is not a whole number of cores.") from None
    if asked < 1:
        raise CoresUnavailable(f"cores must be at least 1; {asked} was asked for.")
    if asked > BOX_CORES:
        raise CoresUnavailable(
            f"cores {asked} is past the {BOX_CORES} this box has. Ask for "
            f"{BOX_CORES} or fewer - a count this box cannot seat is refused "
            "rather than cut down to fit.")
    return asked


#: Every lever by name - what a declaration that takes all of them states.
LEVER_NAMES: tuple[str, ...] = tuple(row.name for row in LEVERS)


def lever(name: str, **stated: Any) -> Param:
    """The runtime's lever with THIS question's opinion of it - nothing else.

    A question whose window, edge or moment differs states the difference and the
    lever carries the rest; a row written out in full restates a type, a unit and
    a help text the runtime already has."""
    found = next((row for row in LEVERS if row.name == name), None)
    if found is None:
        raise PlanValidationError(
            f"{name!r} is not a runtime lever; the runtime declares "
            f"{list(LEVER_NAMES)}. Declare the value as a Param of the question "
            "that asks it.")
    return replace(found, **stated)


def run_frame(params: Any) -> str:
    """The vertical frame THIS run counts elevations from.

    Every slot that ingests an elevation is read against one frame, so it is the
    runtime's and not a row any question writes; a run that states none stands on
    the lever's own default."""
    stated = params.value_of("vertical_frame") if params is not None else None
    return str(stated or VERTICAL_FRAME)


def with_levers(declared: Sequence[Param],
                taken: Sequence[str] = ()) -> tuple[Param, ...]:
    """``declared`` plus each NAMED runtime lever it does not state for itself.

    A template's own row WINS and keeps its position: the lever is a declaration
    it no longer has to restate, never an override of one that differs. A lever
    nothing takes is never seated - a param with no reader is not a feature."""
    stated = {prm.name for prm in declared}
    wanted = set(taken)
    unknown = sorted(wanted - {lever.name for lever in LEVERS})
    if unknown:
        raise PlanValidationError(
            f"{unknown} is not a runtime lever; the runtime declares "
            f"{list(LEVER_NAMES)}. Declare the value as a Param of the question "
            "that asks it.")
    return tuple(declared) + tuple(lever for lever in LEVERS
                                   if lever.name in wanted
                                   and lever.name not in stated)
