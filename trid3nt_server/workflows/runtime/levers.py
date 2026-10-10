"""The levers the RUNTIME declares once, for every template that runs on it.

The scenario moment, resolution, run sizing and elevation axis are the runtime's; a template declares only its own question's inputs.
A template needing a different DEFAULT declares its own row and that row wins.
An engine dictionary keyword is not a lever: the deck states it and the user overrides it by name.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Sequence

from .errors import CoresUnavailable, PlanValidationError
from .params import Param, doors

__all__ = ["BOX_CORES", "LEVERS", "LEVER_NAMES", "VERTICAL_FRAME",
           "cores_asked", "lever", "run_frame", "with_levers"]

#: NAVD88 is what the national terrain, bathymetry and gauges this substrate reaches are published on.
VERTICAL_FRAME = "NAVD88"

#: Cores this box can partition a solve across; a count past it is refused by name.
BOX_CORES: int = os.cpu_count() or 1

#: The declared levers, in the order a card reads them.
LEVERS: tuple[Param, ...] = (
    Param(name="mesh_resolution_m", door=doors.SCENARIO, optional=True,
          type=float, units="m", consequence="numerical", user_lever=True,
          derived_when_absent=(
              "the finest edge is the finest cell of the input data the mesh "
              "is built over - the bed raster's own cell"),
          desc="Target element edge or cell length the domain is resolved at. "
               "The granularity is the USER's lever and nothing refuses one: "
               "absent, the mesh resolves the finest data it stands on, and "
               "the card states the node count it comes to"),
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
    # NUMERICAL, not physics: the axis elevations are placed on, with a published national default;
    # a source that cannot reach this frame refuses by name.
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


LEVER_NAMES: tuple[str, ...] = tuple(row.name for row in LEVERS)


def lever(name: str, **stated: Any) -> Param:
    """The runtime's lever with THIS question's opinion of it - nothing else.

    A question states only the difference; the lever carries the rest."""
    found = next((row for row in LEVERS if row.name == name), None)
    if found is None:
        raise PlanValidationError(
            f"{name!r} is not a runtime lever; the runtime declares "
            f"{list(LEVER_NAMES)}. Declare the value as a Param of the question "
            "that asks it.")
    return replace(found, **stated)


def run_frame(params: Any) -> str:
    """The vertical frame THIS run counts elevations from; a run stating none stands on the lever's default."""
    stated = params.value_of("vertical_frame") if params is not None else None
    return str(stated or VERTICAL_FRAME)


def with_levers(declared: Sequence[Param],
                taken: Sequence[str] = ()) -> tuple[Param, ...]:
    """``declared`` plus each NAMED runtime lever it does not state for itself.

    A template's own row wins and keeps its position; a lever nothing takes is never seated."""
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
