"""What an engine READ off a solved run, in the shape publishing consumes.

A read carries no engine, module or template word: node coordinates in lon/lat,
values, a time axis, the file an animation plays from, and the measures its
reader took while it held the field. Every read is a value."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["Deliverable", "Field", "Frames", "Read", "Series"]


@dataclass(frozen=True, kw_only=True)
class Read:
    """What one primitive measured, by name. A bare read publishes nothing."""

    measures: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class Field(Read):
    """One variable over the domain at one instant, or its envelope over time.

    ``t`` is the instant, ``None`` the envelope; a cell below ``floor`` is nodata."""

    name: str
    units: str
    lon: Any
    lat: Any
    ikle: Any
    values: Any
    t: float | None = None
    floor: float | None = None


@dataclass(frozen=True, kw_only=True)
class Series(Read):
    """One variable over time: the domain maximum at each instant, or a point's."""

    name: str
    units: str
    times: Any
    values: Any
    #: Where the series was read - ``"the domain maximum"`` or a point's name.
    at: str


@dataclass(frozen=True, kw_only=True)
class Frames(Read):
    """One variable over time, as the result file an animation plays from."""

    name: str
    units: str
    #: The result file's basename under the run prefix.
    file: str
    #: The dataset group the mesh reader binds the variable by.
    group: str
    epsg: int
    reference_time: str | None
    frames: int


@dataclass(frozen=True, kw_only=True)
class Deliverable:
    """One read and how it is published: as a layer, a chart or an animation.

    ``caption`` names the quantity in the reader's words; ``style`` is a
    declared style row, read only for a layer."""

    read: Read
    mode: str
    caption: str
    style: Mapping[str, Any] | None = None
