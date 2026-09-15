"""BOUNDARY RUNS: named stretches of the domain's edge, and what each carries.

A run is two Points on the edge and a TYPE. A template or a producer states zero
or more of them; everything the runs do not name is solid wall, so a closed body
states none and its mesh has only walls. Nothing here knows an engine: what a
role means to a deck is the deck's, and what a run means to a mesh is the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .point import Point
from .user_input import UserInputError, lonlat_point

__all__ = ["BoundaryRun", "RUN_TYPES", "WALL", "boundary_runs", "roles_from_runs"]

logger = logging.getLogger("trid3nt_server.inputs.boundary")

_CODE = "BOUNDARY_RUN_INVALID"

#: What a stretch of the edge carries. ``wall`` is the default and is never
#: prescribed: it is what the edge already is where no run names it.
WALL = "wall"
RUN_TYPES: tuple[str, ...] = (WALL, "inflow", "outflow", "open")


@dataclass(frozen=True, slots=True)
class BoundaryRun:
    """One named stretch of the domain's edge: where it starts, where it ends,
    and what it carries."""

    start: Point
    end: Point
    type: str = WALL
    name: str | None = None

    def __post_init__(self) -> None:
        if self.type not in RUN_TYPES:
            raise UserInputError(
                f"a boundary run carries one of {list(RUN_TYPES)}; "
                f"{self.type!r} is not one of them.", code=_CODE)

    @property
    def face(self) -> dict[str, Any]:
        """This run as the two-ended FACE a mesh names a stretch of its edge by."""
        return {"type": "LineString",
                "coordinates": [[self.start.lon, self.start.lat],
                                [self.end.lon, self.end.lat]]}


def boundary_runs(value: Any, *, label: str = "boundary runs",
                  code: str = _CODE) -> tuple[BoundaryRun, ...]:
    """THE ingestion: drawn pairs, typed rows, or a producer's own runs.

    Nothing is ``()`` and that is an ANSWER - a closed body states no run."""
    if value is None:
        return ()
    if isinstance(value, BoundaryRun):
        return (value,)
    declared = getattr(value, "runs", None)
    if declared is not None and not isinstance(value, (Mapping, Sequence)):
        # A typed SLOT value states the runs its producer measured: a domain
        # cut between two faces carries them, a drawn outline carries none.
        return boundary_runs(declared, label=label, code=code)
    if isinstance(value, Mapping):
        if value.get("type") == "FeatureCollection":
            return tuple(_row(f, label, code)
                         for f in (value.get("features") or ()))
        runs = value.get("runs")
        if runs is not None:
            return boundary_runs(runs, label=label, code=code)
        return (_row(value, label, code),)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(run for item in value
                     for run in boundary_runs(item, label=label, code=code))
    raise UserInputError(
        f"{label} takes runs of the domain's edge - two points and a type "
        f"each - and {value!r} is not one.", code=code)


def roles_from_runs(runs: Iterable[BoundaryRun]) -> dict[str, list[dict[str, Any]]]:
    """``{role: [face, ...]}`` for the runs that PRESCRIBE something.

    A wall run is dropped: the edge is wall wherever nothing names it, so
    prescribing one would be the mesh asked to impose what it already is."""
    roles: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        if run.type == WALL:
            continue
        roles.setdefault(run.type, []).append(run.face)
    return roles


def _row(value: Any, label: str, code: str) -> BoundaryRun:
    """One declared run, whichever of its shapes it arrived in."""
    if isinstance(value, BoundaryRun):
        return value
    if not isinstance(value, Mapping):
        pair = list(value) if isinstance(value, Sequence) else []
        if len(pair) != 2:
            raise UserInputError(
                f"a {label} row is two points and a type; got {value!r}.",
                code=code)
        return BoundaryRun(start=_point(pair[0], label, code),
                           end=_point(pair[1], label, code))
    props = dict(value.get("properties") or {})
    geometry = value.get("geometry")
    kind = str(props.get("type") or value.get("type") or WALL)
    if kind not in RUN_TYPES and isinstance(geometry, Mapping):
        # A drawn feature's own GeoJSON ``type`` is "Feature"; the run's type is
        # a property on it, and an unnamed one is the wall the edge already is.
        kind = WALL
    name = props.get("name") or value.get("name")
    if isinstance(geometry, Mapping):
        coords = list(geometry.get("coordinates") or ())
        if len(coords) < 2:
            raise UserInputError(
                f"a {label} row's line carries {len(coords)} vertices; a run is "
                "the stretch between two points on the edge.", code=code)
        return BoundaryRun(start=_point(coords[0], label, code),
                           end=_point(coords[-1], label, code),
                           type=kind, name=_text(name))
    start, end = value.get("start"), value.get("end")
    if start is None or end is None:
        raise UserInputError(
            f"a {label} row names its two ends as 'start' and 'end'; got "
            f"{sorted(value)}.", code=code)
    return BoundaryRun(start=_point(start, label, code),
                       end=_point(end, label, code), type=kind, name=_text(name))


def _point(value: Any, label: str, code: str) -> Point:
    """One end of a run, as a Point - the same pair every slot reads."""
    if isinstance(value, Point):
        return value
    if isinstance(value, Mapping):
        value = value.get("coordinates") or [value.get("lon"), value.get("lat")]
    pair = lonlat_point(value, label=label, code=code)
    if pair is None:
        raise UserInputError(f"a {label} end carries no coordinates.", code=code)
    return Point(float(pair[0]), float(pair[1]))


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
