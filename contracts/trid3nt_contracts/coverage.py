"""What a data source COVERS, and the ranked list a slot's match produces.

A row is a fetcher's only statement of what it holds: the class of
thing it measures, where, over what time, at what cell, counted from what zero,
and the field map its records are read by. A slot states a NEED of the same
vocabulary and the match reads the two; nothing here knows about a template.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, model_validator

from .common import ContractModel

__all__ = [
    "DATA_CLASSES",
    "PROVENANCE_KINDS",
    "DataClass",
    "FieldMap",
    "UnitField",
    "MEMBERS",
    "Member",
    "NUMERIC_MEMBERS",
    "PER_RECORD",
    "Rule",
    "SERIES_MEMBERS",
    "Coverage",
    "CoverageExtent",
    "CoveragePoint",
    "CoverageWindow",
    "SourceChoice",
    "SourceOption",
]

#: Coarse vocabulary for a slot's need and a source's coverage: the one question a mechanical filter can
#: answer without reading bytes. A source measuring two things carries two rows, not a hybrid word.
DATA_CLASSES = (
    "bathymetry",
    "channel survey",
    "terrain",
    "hydrography",
    "bed material",
    "water level series",
    "discharge series",
    "precipitation series",
    "wave series",
    "weather forcing",
    "cloud cover",
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
    "channel survey",
    "terrain",
    "hydrography",
    "bed material",
    "water level series",
    "discharge series",
    "precipitation series",
    "wave series",
    "weather forcing",
    "cloud cover",
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


#: Kilometres per degree of latitude.
_KM_PER_DEGREE = 111.32

#: A zero the record carries per feature; the slot reads it off the feature and refuses a feature carrying none.
PER_RECORD = "record"


class CoveragePoint(ContractModel):
    """One STATION a source holds a record at, in the source's own words.

    The id is what the source is called by to reach this station, so a probe can
    name it; the datum is this station's own zero where the service states one
    per station rather than one for the network."""

    id: str = Field(min_length=1, max_length=120)
    lon: float
    lat: float
    datum: str | None = None


class CoverageExtent(ContractModel):
    """WHERE a source holds anything, as coarsely as the source itself states it.

    ``rings`` are closed lon/lat outlines the place filter tests a point against;
    a station set states its stations instead - listed, read from a hook, or
    discovered by box - and ``service`` means the catalogue answers coverage and
    only the probe can, so the filter passes the source through to it."""

    #: ``stations``: a point within reach of one is a candidate and the probe decides; ``service`` states no geometry.
    kind: Literal["surface", "stations", "service"]
    #: Closed lon/lat rings, coarse. Empty ONLY on ``service``.
    rings: list[list[tuple[float, float]]] = Field(default_factory=list)
    note: str = Field(default="", max_length=300)
    points: list[CoveragePoint] = Field(default_factory=list)
    #: A ``<source>.<point>`` hook naming the station listing, for a set too large to state.
    read_from: str = Field(default="", max_length=120)
    #: A network with no bounded listing: reach applies to the station the probe discovers, and an empty probe drops the source.
    discover: Literal["", "bbox"] = ""

    @model_validator(mode="after")
    def _validate_extent(self) -> "CoverageExtent":
        """A geometry-bearing extent needs a ring; ``service`` must carry none;
        and a station set names its stations one of the three ways."""
        if self.kind != "stations" and (self.points or self.read_from
                                        or self.discover):
            raise ValueError(
                f"extent.kind={self.kind} names stations; only a station set has "
                "them")
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
        if self.kind == "stations" and not (self.points or self.read_from
                                            or self.discover):
            raise ValueError(
                "a station set lists its stations - points, read_from or "
                "discover - because a ring alone says a place is in the region "
                "and nothing about whether a station stands anywhere near it")
        return self

    def covers(self, lon: float, lat: float) -> bool:
        """Is this point inside any ring? ``service`` covers everywhere - the
        probe is what answers for it."""
        if self.kind == "service":
            return True
        return any(_in_ring(ring, float(lon), float(lat)) for ring in self.rings)

    def listed(self) -> list[CoveragePoint]:
        """The stations this extent actually holds: the listing clipped to the
        rings, which is how one network's listing serves a row drawn over part
        of it."""
        if not self.rings:
            return list(self.points)
        return [p for p in self.points if self.covers(p.lon, p.lat)]

    def nearest(self, lon: float, lat: float) -> CoveragePoint | None:
        """The listed station closest to this point, or ``None`` where the set
        states no stations to name."""
        listed = self.listed()
        if not listed:
            return None
        scale = math.cos(math.radians(float(lat)))
        return min(listed, key=lambda p: math.hypot(
            (p.lon - float(lon)) * scale, p.lat - float(lat)))

    def distance_km(self, lon: float, lat: float) -> float:
        """How far this point lies from what the source holds, in km: zero off a ``service``, the nearest
        station for a listed set, otherwise against the rings (zero inside)."""
        if self.kind == "service":
            return 0.0
        station = self.nearest(float(lon), float(lat))
        if station is not None:
            scale = math.cos(math.radians(float(lat)))
            return math.hypot((station.lon - float(lon)) * scale,
                              station.lat - float(lat)) * _KM_PER_DEGREE
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


#: The names code reads a fetched record by. A source maps each it publishes onto its own field or a cited
#: number; a member it does not map is one it does not report, and a read of it refuses.
MEMBERS = ("value", "series", "time", "above", "above_accuracy", "accuracy",
           "height", "period", "from_direction", "site", "site_name", "quantity", "depth", "datum",
           "air_temperature", "dew_point", "relative_humidity", "wind_speed", "wind_from",
           "solar_radiation", "pressure", "cloud_cover", "cloud_cover_accuracy", "rain")

MemberName = Literal["value", "series", "time", "above", "above_accuracy", "accuracy",
                     "height", "period", "from_direction", "site", "site_name", "quantity", "depth",
                     "datum", "air_temperature", "dew_point", "relative_humidity", "wind_speed",
                     "wind_from", "solar_radiation", "pressure", "cloud_cover", "cloud_cover_accuracy", "rain"]

#: Members holding a number (a series holds numbers), so each states the unit it is counted in.
NUMERIC_MEMBERS = frozenset(MEMBERS) - {"time", "site", "site_name", "quantity", "datum"}

#: Members naming a record's window - the source's own text of stamped readings, a sea-state quantity's included.
SERIES_MEMBERS = frozenset({"series", "height", "period", "from_direction"})


class UnitField(ContractModel):
    """A unit the record carries beside its value, by the record's own field name."""

    field: str = Field(min_length=1)


class Rule(ContractModel):
    """A published accuracy: the larger of an absolute bound and a fraction of the value read."""

    absolute: float = Field(default=0.0, ge=0.0)
    relative: float = Field(default=0.0, ge=0.0)
    unit: str = Field(min_length=1)


class Member(ContractModel):
    """One member as a source states it, in exactly one of four forms: its own ``field``; a ``field``
    read through a ``table`` of its codes; a ``constant``; or an accuracy rule (``absolute`` /
    ``relative``). Every form but a plain field carries the ``cite`` it was read from."""

    #: The record's own field; a dotted name reads a nested one.
    field: str = ""
    #: The source's code -> our word, a cited number, or a cited rule.
    table: dict[str, str | float | Rule] | None = None
    constant: float | None = None
    absolute: float | None = Field(default=None, ge=0.0)
    relative: float | None = Field(default=None, ge=0.0)
    #: The unit the number is counted in: stated here, or a field of the record.
    unit: str | UnitField = ""
    #: A window member whose field holds JSON text of the source's own reading objects names the keys of
    #: each reading that carry its instant and its number.
    time_key: str = ""
    value_key: str = ""
    cite: str = Field(default="", max_length=600)

    @property
    def rule(self) -> Rule | None:
        if self.absolute is None and self.relative is None:
            return None
        return Rule(absolute=self.absolute or 0.0, relative=self.relative or 0.0,
                    unit=str(self.unit))

    @model_validator(mode="after")
    def _validate_form(self) -> "Member":
        """One form; a table reads a field; a number nobody can trace refuses."""
        ruled = self.absolute is not None or self.relative is not None
        if sum((bool(self.field), self.constant is not None, ruled)) != 1:
            raise ValueError(
                "a member is exactly one of a field (with or without a table), a constant, "
                "or an absolute/relative rule")
        if self.table is not None and not self.field:
            raise ValueError("a table translates a field's codes, so it names the field")
        cited = self.table is not None or not self.field
        if cited and not self.cite.strip():
            raise ValueError(
                "a constant, a table or a rule states the publication it was read from as "
                "cite; a number nobody can trace is an invented one")
        if not cited and self.cite:
            raise ValueError("a plain field carries no cite: the source itself is its origin")
        if ruled and not (isinstance(self.unit, str) and self.unit):
            raise ValueError("a rule states its own unit, never a record's field")
        if bool(self.time_key) != bool(self.value_key):
            raise ValueError("a window of reading objects names both keys: the instant's and the number's")
        if self.time_key and not self.field:
            raise ValueError("reading keys belong to a field holding the window")
        return self


def validate_fields(fields: dict[str, Member]) -> None:
    """A numeric member states its unit: on itself, a field of the record, or each table row's rule."""
    for name, member in fields.items():
        if member.time_key and name not in SERIES_MEMBERS:
            raise ValueError(f"fields.{name} names reading keys, which only a window member holds")
        if name not in NUMERIC_MEMBERS or member.unit:
            continue
        if member.table and all(isinstance(row, Rule) for row in member.table.values()):
            continue
        raise ValueError(
            f"fields.{name} states no unit; a number read in no stated unit is a number "
            "nobody measured")


FieldMap = dict[MemberName, Member]


#: How a source came by its numbers, ranked: an instrument record beats a prediction beats a model grid; the match sorts on it first.
PROVENANCE_KINDS = ("measured", "predicted", "modelled")

ProvenanceKind = Literal["measured", "predicted", "modelled"]


class CoverageWindow(ContractModel):
    """WHEN a source holds anything.

    A SERIES source's window is hard: a run asking outside it has no record, and
    the refusal says so. A STATIC surface never fails it - a 2014 survey answers
    a 2026 question - and its ``latest`` is the recency the sort ranks on."""

    #: False is one surface, measured once and standing until re-measured.
    series: bool
    #: ISO dates the records open and close at; None on a side is unbounded.
    earliest: str | None = None
    latest: str | None = None
    #: The longest window ONE call may ask for, from the service's own cap.
    max_span_days: float | None = None
    cadence: str = Field(default="", max_length=120)
    #: The request param that asks a series source for its whole record: set true when the
    #: window is a span, left off for a moment, which the source answers with its latest reading.
    series_param: str = ""

    @model_validator(mode="after")
    def _validate_series_param(self) -> "CoverageWindow":
        """Only a series has a record to switch on."""
        if self.series_param and not self.series:
            raise ValueError(
                f"series_param={self.series_param!r} on a window that is not a "
                "series: a surface has no record to ask for")
        return self


class Coverage(ContractModel):
    """One source's ONLY statement of what it covers.

    Stated from the dataset's own documentation, never inferred from a fetched
    body: a guessed extent is indistinguishable from a measured one to every
    reader downstream, and this row is what decides which source fills a slot."""

    data_class: DataClass
    kind: ProvenanceKind
    extent: CoverageExtent
    window: CoverageWindow
    #: Kilometres one station serves; only a station set states one.
    reach_km: float | None = Field(default=None, gt=0.0)
    #: The finest cell in metres; None where the source is not a grid.
    resolution_m: float | None = Field(default=None, gt=0.0)
    #: The zero elevations are counted from, as one frame name the offset service converts (the loader refuses prose).
    #: None where the dataset publishes none; :data:`PER_RECORD` where each feature carries its own.
    datum: str | None = None
    #: Request values this row is fetched under, by param name, so the source answers with this row.
    #: ``need:<attribute>`` maps a need's generic attribute (``span_km``, ``seed_point``) onto the param this source states it in.
    ask: dict[str, str] = Field(default_factory=dict)
    #: This source's own word for each published variable, by the variable's name; a variable with no word here
    #: is a measurement the source does not take, and the match excludes it.
    vocabulary: dict[str, str] = Field(default_factory=dict)
    #: The members this row reads differently from its source's map; after load, the source's map
    #: with these over it.
    fields: FieldMap = Field(default_factory=dict)

    def word_for(self, variable: str) -> str:
        """This source's own name for a published variable, "" where it has none."""
        return str(self.vocabulary.get(str(variable), ""))

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
    def _validate_fields(self) -> "Coverage":
        validate_fields(dict(self.fields))
        return self


class SourceOption(ContractModel):
    """One row of the ranked list: a source and the four facts it is picked on."""

    fetcher: str = Field(min_length=1)
    #: Rendered facts, not numbers to re-derive.
    kind: str = Field(default="", max_length=40)
    distance: str = Field(default="", max_length=120)
    resolution: str = Field(default="", max_length=120)
    recency: str = Field(default="", max_length=120)
    datum: str = Field(default="", max_length=120)
    extent: str = Field(default="", max_length=300)
    #: Why the filter dropped this row; "" on a survivor.
    excluded: str = Field(default="", max_length=300)


class SourceChoice(ContractModel):
    """THE RANKED LIST, one object in three views.

    The card renders it with the pick highlighted, the tool result carries it
    only on a tie, and the sheet stores the pick and its reason. ``sentence`` is
    authored once: the sentence the model is given IS the sentence the card
    shows."""

    slot: str = Field(min_length=1)
    need: str = Field(min_length=1)
    #: Survivors in rank order first, then the excluded, at most five rows.
    rows: list[SourceOption] = Field(default_factory=list, max_length=5)
    #: The fetcher that filled the slot; "" on a refusal.
    picked: str = Field(default="", max_length=120)
    sentence: str = Field(default="", max_length=600)
    #: Survivors ranked equal on every fact the sort reads: the model or the user picks by row.
    tie: bool = False
    #: The run-level statement that loosened a filter, "" where none did; it shows as the user's choice.
    loosened: str = Field(default="", max_length=300)
    #: The run named this source for this slot; the sort's own pick stays on the list.
    picked_by_user: bool = False
