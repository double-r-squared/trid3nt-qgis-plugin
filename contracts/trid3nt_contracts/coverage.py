"""What a data source COVERS, and the ranked list a slot's match produces.

A coverage row is a fetcher's only statement of what it holds: the class of
thing it measures, where, over what time, at what cell, counted from what zero,
and the unit of each value column it publishes. A slot states a NEED of the same
vocabulary and the match reads the two; nothing here knows about a template.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, model_validator

from .common import GraceModel

__all__ = [
    "DATA_CLASSES",
    "PROVENANCE_KINDS",
    "DataClass",
    "Coverage",
    "CoverageExtent",
    "CoverageWindow",
    "SourceChoice",
    "SourceOption",
]

#: THE COARSE VOCABULARY a slot's need and a source's coverage are both stated
#: in. Coarse on purpose: it is the one question a mechanical filter can answer
#: about a source without reading its bytes - is this the kind of thing the slot
#: is asking for. A finer class would make the filter a taxonomy argument, and a
#: source that measures two things carries two rows rather than a hybrid word.
DATA_CLASSES = (
    "bathymetry",
    "terrain",
    "hydrography",
    "bed material",
    "water level series",
    "discharge series",
    "precipitation series",
    "weather forcing",
    "water quality sample",
    "land cover",
    "soil",
    "aquifer property",
    "groundwater level",
    "fuels",
    "ignition",
    "seismic source",
    "exposure",
)

DataClass = Literal[
    "bathymetry",
    "terrain",
    "hydrography",
    "bed material",
    "water level series",
    "discharge series",
    "precipitation series",
    "weather forcing",
    "water quality sample",
    "land cover",
    "soil",
    "aquifer property",
    "groundwater level",
    "fuels",
    "ignition",
    "seismic source",
    "exposure",
]


#: One degree of latitude, in kilometres - what a ring distance is read in.
_KM_PER_DEGREE = 111.32


class CoverageExtent(GraceModel):
    """WHERE a source holds anything, as coarsely as the source itself states it.

    ``rings`` are closed lon/lat outlines the place filter tests a point against;
    ``service`` means the catalogue answers coverage and only the probe can, so
    the filter passes the source through to it."""

    #: ``surface`` covers every point inside the rings; ``stations`` holds
    #: discrete sites inside them, so a point near one is a candidate and the
    #: probe decides; ``service`` states no geometry at all.
    kind: Literal["surface", "stations", "service"]
    #: Closed lon/lat rings, coarse. Empty ONLY on ``service``.
    rings: list[list[tuple[float, float]]] = Field(default_factory=list)
    #: What the rings are, in the source's own words - the reader's check that
    #: the outline is the dataset's and not a guess.
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _validate_rings(self) -> "CoverageExtent":
        """A geometry-bearing extent needs a ring; ``service`` must carry none."""
        if self.kind == "service":
            if self.rings:
                raise ValueError(
                    "extent.kind=service reads coverage off the catalogue and "
                    "states no rings of its own")
            return self
        if not self.rings:
            raise ValueError(f"extent.kind={self.kind} states no rings")
        for ring in self.rings:
            if len(ring) < 4:
                raise ValueError(
                    f"a coverage ring needs at least four points; got {len(ring)}")
        return self

    def covers(self, lon: float, lat: float) -> bool:
        """Is this point inside any ring? ``service`` covers everywhere - the
        probe is what answers for it."""
        if self.kind == "service":
            return True
        return any(_in_ring(ring, float(lon), float(lat)) for ring in self.rings)

    def distance_km(self, lon: float, lat: float) -> float:
        """How far this point lies from what the source holds, in kilometres.

        Zero inside the rings and off a ``service``, which answers for itself;
        outside, the distance to the nearest ring, which for a station set is
        how far the nearest station can be."""
        if self.covers(lon, lat):
            return 0.0
        return min(_ring_distance_km(ring, float(lon), float(lat))
                   for ring in self.rings)


def _ring_distance_km(ring: list[tuple[float, float]], lon: float,
                      lat: float) -> float:
    """The distance from a point to one closed ring, on a local flat earth.

    Coarse on purpose: a coverage ring is a coarse outline, and a distance read
    off it is a fact about the outline rather than a measured range."""
    scale = math.cos(math.radians(lat))
    best = float("inf")
    for (x0, y0), (x1, y1) in zip(ring, list(ring[1:]) + [ring[0]]):
        best = min(best, _segment_distance(
            (lon - x0) * scale, lat - y0, (x1 - x0) * scale, y1 - y0))
    return best * _KM_PER_DEGREE


def _segment_distance(px: float, py: float, sx: float, sy: float) -> float:
    """The distance from the origin to the segment reaching ``(sx, sy)`` from
    ``(-px, -py)``, in degrees of latitude."""
    span = sx * sx + sy * sy
    along = 0.0 if span == 0.0 else max(0.0, min(1.0, (px * sx + py * sy) / span))
    return math.hypot(px - along * sx, py - along * sy)


def _in_ring(ring: list[tuple[float, float]], lon: float, lat: float) -> bool:
    """Ray cast against one closed lon/lat ring."""
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, list(ring[1:]) + [ring[0]]):
        if (y0 > lat) != (y1 > lat) and \
                lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


#: HOW a source came by its numbers, ranked: an instrument record beats a
#: prediction and a prediction beats a model grid. It is the first fact the
#: match sorts on, because a gauge at the place answers a question a modelled
#: cell over it only estimates.
PROVENANCE_KINDS = ("measured", "predicted", "modelled")

ProvenanceKind = Literal["measured", "predicted", "modelled"]


class CoverageWindow(GraceModel):
    """WHEN a source holds anything.

    A SERIES source's window is hard: a run asking outside it has no record, and
    the refusal says so. A STATIC surface never fails it - a 2014 survey answers
    a 2026 question - and its ``latest`` is the recency the sort ranks on."""

    #: The source publishes a record per instant. False = one surface, measured
    #: once and standing until it is re-measured.
    series: bool
    #: ISO dates the source's own records open and close at. ``None`` on either
    #: side is unbounded - back to the record's beginning, forward to now.
    earliest: str | None = None
    latest: str | None = None
    #: The longest window ONE call may ask for, from the service's own cap.
    max_span_days: float | None = None
    #: The interval between records, in the source's words ("6-min samples").
    cadence: str = Field(default="", max_length=120)


class Coverage(GraceModel):
    """One source's ONLY statement of what it covers.

    Stated from the dataset's own documentation, never inferred from a fetched
    body: a guessed extent is indistinguishable from a measured one to every
    reader downstream, and this row is what decides which source fills a slot."""

    data_class: DataClass
    #: Whether these numbers were measured, predicted or modelled.
    kind: ProvenanceKind
    extent: CoverageExtent
    window: CoverageWindow
    #: How far one STATION serves, in kilometres - the distance at which its
    #: record still speaks for the place. Only a station set states one: a
    #: surface answers everywhere inside its rings and a service answers for
    #: itself.
    reach_km: float | None = Field(default=None, gt=0.0)
    #: The finest cell the source publishes, in metres. ``None`` where the
    #: source is not a grid - a station set, a point sample, a polygon.
    resolution_m: float | None = Field(default=None, gt=0.0)
    #: The zero this source's elevations are counted from, in its own words.
    #: ``None`` where it publishes none, which the match flags and the offset
    #: row refuses at.
    datum: str | None = None
    #: The unit of EACH value column the record carries, by column name. A slot
    #: reads the column it needs and converts to the keyword's unit or refuses;
    #: an absent column here is a unit nobody stated, which also refuses.
    units: dict[str, str] = Field(default_factory=dict)
    #: WHICH column carries what, in the record's own column names: the value a
    #: reading is taken from, the window it reported over, and the elevation of
    #: the zero that value is counted from. Named here because a slot that
    #: states a need cannot name a column of a source it did not choose; every
    #: name must be one the units above state a unit for.
    value_column: str = ""
    series_column: str = ""
    above_column: str = ""

    @model_validator(mode="after")
    def _validate_reach(self) -> "Coverage":
        """A reach is a station's, and every station set states one."""
        if self.extent.kind == "stations" and self.reach_km is None:
            raise ValueError(
                "a station set states reach_km, the distance one station "
                "serves; without it the place filter cannot say whether the "
                "nearest station speaks for this place")
        if self.extent.kind != "stations" and self.reach_km is not None:
            raise ValueError(
                f"extent.kind={self.extent.kind} states a reach; a reach is the "
                "distance a STATION serves and nothing else has stations")
        return self

    @model_validator(mode="after")
    def _validate_columns(self) -> "Coverage":
        """A named column is one this row states a unit for."""
        for role, column in (("value_column", self.value_column),
                             ("series_column", self.series_column),
                             ("above_column", self.above_column)):
            if column and column not in self.units:
                raise ValueError(
                    f"{role}={column!r} names a column this row states no unit "
                    "for; a column read in no stated unit is a number nobody "
                    "measured")
        return self


class SourceOption(GraceModel):
    """One row of the ranked list: a source and the four facts it is picked on."""

    fetcher: str = Field(min_length=1)
    #: Rendered facts, not numbers to re-derive: how the numbers were come by,
    #: how far the place is from what the source holds, the cell, how current
    #: the record is, the zero it counts from, and where it reaches.
    kind: str = Field(default="", max_length=40)
    distance: str = Field(default="", max_length=120)
    resolution: str = Field(default="", max_length=120)
    recency: str = Field(default="", max_length=120)
    datum: str = Field(default="", max_length=120)
    extent: str = Field(default="", max_length=300)
    #: Why the filter dropped this row, "" on a survivor. A refusal NAMES what
    #: each filter excluded, so an excluded row travels with its reason.
    excluded: str = Field(default="", max_length=300)


class SourceChoice(GraceModel):
    """THE RANKED LIST, one object in three views.

    The card renders it with the pick highlighted, the tool result carries it
    only on a tie, and the sheet stores the pick and its reason. ``sentence`` is
    authored once: the sentence the model is given IS the sentence the card
    shows."""

    #: The slot this list filled, by its own name.
    slot: str = Field(min_length=1)
    #: The class the slot asked for.
    need: str = Field(min_length=1)
    #: Survivors in rank order first, then the excluded, at most five rows.
    rows: list[SourceOption] = Field(default_factory=list, max_length=5)
    #: The fetcher that filled the slot; "" on a refusal.
    picked: str = Field(default="", max_length=120)
    sentence: str = Field(default="", max_length=600)
    #: Several survivors ranked equal on every fact the sort reads: the model or
    #: the user picks by row, and the tool result carries the list.
    tie: bool = False
    #: The run-level statement that loosened a filter, "" where none did. It
    #: shows as the user's choice, never as the match's own judgement.
    loosened: str = Field(default="", max_length=300)
    #: The run NAMED this source for this slot. The list is still the list -
    #: what the sort would have taken stays on it - and the pick reads as the
    #: user's choice rather than the match's.
    picked_by_user: bool = False
