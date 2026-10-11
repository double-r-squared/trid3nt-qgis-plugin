"""Shared primitives every other contract in this package builds on.

Ids are ULIDs - 26 chars, Crockford base32, time-sortable, URL-safe. A ``bbox``
is always ``[minLon, minLat, maxLon, maxLat]`` in EPSG:4326. Datetimes
serialize with a ``Z`` suffix, and ``model_dump(mode="json")`` is the canonical
wire and storage form.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)
from ulid import ULID

__all__ = [
    "ContractModel",
    "ULIDStr",
    "BBox",
    "Lon",
    "Lat",
    "new_ulid",
    "now_utc",
    "TimeRange",
    "TemporalMode",
    "EngineRunArgsMixin",
    "ORIGIN_LABELS",
    "Origin",
    "SyntheticInput",
    "render_assumptions_line",
]




def new_ulid() -> str:
    """Generate a fresh ULID string (26 chars, Crockford base32, time-sortable)."""
    return str(ULID())


def _validate_ulid(value: str) -> str:
    """Reject anything that is not a syntactically valid ULID string."""
    # A malformed id raises ValueError, surfacing as a pydantic validation error.
    ULID.from_str(value)
    return value


ULIDStr = Annotated[str, AfterValidator(_validate_ulid)]




def now_utc() -> datetime:
    """Timezone-aware current UTC time (the default for ``*_at`` fields)."""
    return datetime.now(timezone.utc)


def _serialize_dt_z(value: datetime) -> str:
    """Serialize a datetime to ISO-8601 with a ``Z`` suffix; naive datetimes are treated as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    # isoformat() on a UTC-aware datetime yields "+00:00"; normalize to "Z".
    return value.isoformat().replace("+00:00", "Z")


UTCDatetime = Annotated[datetime, PlainSerializer(_serialize_dt_z, return_type=str)]



Lon = Annotated[float, Field(ge=-180.0, le=180.0)]
Lat = Annotated[float, Field(ge=-90.0, le=90.0)]


def _validate_bbox(value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Enforce EPSG:4326 ordering: [minLon, minLat, maxLon, maxLat]."""
    min_lon, min_lat, max_lon, max_lat = value
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise ValueError(f"bbox longitudes out of range [-180, 180]: {value!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ValueError(f"bbox latitudes out of range [-90, 90]: {value!r}")
    if min_lon > max_lon:
        raise ValueError(f"bbox minLon {min_lon} > maxLon {max_lon}: {value!r}")
    if min_lat > max_lat:
        raise ValueError(f"bbox minLat {min_lat} > maxLat {max_lat}: {value!r}")
    return value


BBox = Annotated[tuple[float, float, float, float], AfterValidator(_validate_bbox)]




class ContractModel(BaseModel):
    """Canonical base for every contract model.
    ``extra="forbid"``: an unknown field is a DEFECT, never silently dropped, so
    forward-compatible growth goes through open Literals and additive fields.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        ser_json_timedelta="iso8601",
    )




class TimeRange(ContractModel):
    """A UTC start/end interval."""

    start: UTCDatetime
    end: UTCDatetime



#: Growth is by an additive Literal member plus an alias, never by arbitrary keys.
TemporalMode = Literal["steady", "transient"]


#: Synonyms mapped before the Literal check so the first attempt validates; an unknown string passes through unchanged.
_TEMPORAL_MODE_ALIASES: dict[str, str] = {
    "steady": "steady",
    "steady-state": "steady",
    "steady_state": "steady",
    "steadystate": "steady",
    "stationary": "steady",
    "static": "steady",
    "equilibrium": "steady",
    "transient": "transient",
    "nonstationary": "transient",
    "non-stationary": "transient",
    "non_stationary": "transient",
    "unsteady": "transient",
    "time-varying": "transient",
    "time_varying": "transient",
    "timevarying": "transient",
    "dynamic": "transient",
    "time-stepping": "transient",
    "time_stepping": "transient",
}


class EngineRunArgsMixin(ContractModel):
    """ADDITIVE, DEFAULT-OFF base for the per-engine ``*RunArgs`` models.
    Every field defaults to the behaviour before it existed, so a model that
    adopts the mixin and sets nothing serializes and behaves byte-identically.
    """

    temporal_mode: TemporalMode = "steady"
    output_frames: int = Field(default=24, ge=1)
    #: None is no overrides; the engine's own keyword catalog validates the keys.
    advanced_physics: dict[str, Any] | None = None

    @field_validator("temporal_mode", mode="before")
    @classmethod
    def _normalize_temporal_mode(cls, value: Any) -> Any:
        """Map a synonym onto the canonical ``TemporalMode`` BEFORE the Literal
        check. A non-string or unknown string passes through UNCHANGED, so a
        genuinely invalid value still raises the honest Literal error."""
        if not isinstance(value, str):
            return value
        key = value.strip().lower()
        return _TEMPORAL_MODE_ALIASES.get(key, key)



class Origin(str, Enum):
    """Where a run input's value came from. A closed set; the name that makes it checkable rides beside it."""

    USER = "user"
    DERIVED = "derived"
    PRODUCER = "producer"
    TEMPLATE = "template"


#: How each origin reads in a narration line and on a review card.
ORIGIN_LABELS: dict[Origin, str] = {
    Origin.USER: "user",
    Origin.PRODUCER: "produced",
    Origin.DERIVED: "derived",
    Origin.TEMPLATE: "template value, not a site measurement",
}


class SyntheticInput(ContractModel):
    """One structured provenance entry for a physical model input.
    ADDITIVE and default-empty: an empty list means "no declared provenance",
    NEVER "all real".
    """

    param: str
    value: float | int | str | None = None
    units: str | None = None
    basis: Origin
    real_source_if_any: str | None = None
    note: str | None = None


def render_assumptions_line(entries: Any) -> str | None:
    """Render a ``synthetic_inputs`` list into ONE narration line, ``None`` when
    empty. Grouped by origin so a template's starting value is named plainly
    rather than tabulated. Takes ``SyntheticInput`` objects or their ``model_dump`` dicts."""
    if not entries:
        return None

    def _field(e: Any, name: str) -> Any:
        return e.get(name) if isinstance(e, dict) else getattr(e, name, None)

    def _one(e: Any) -> str:
        value, units = _field(e, "value"), _field(e, "units")
        source = _field(e, "real_source_if_any")
        stated = "" if value is None else f"={value}" + (f" {units}" if units else "")
        return f"{_field(e, 'param')}{stated}" + (f" (by {source})" if source else "")

    segments = [
        f"{label}: " + ", ".join(_one(e) for e in entries
                                 if Origin(_field(e, "basis")) is origin)
        for origin, label in ORIGIN_LABELS.items()
        if any(Origin(_field(e, "basis")) is origin for e in entries)]
    return "Input provenance -- " + "; ".join(segments) + "."
