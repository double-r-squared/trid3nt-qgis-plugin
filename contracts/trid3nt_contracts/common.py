"""Shared primitives every other contract in this package builds on.

Ids are ULIDs - 26 chars, Crockford base32, time-sortable, URL-safe. A ``bbox``
is always ``[minLon, minLat, maxLon, maxLat]`` in EPSG:4326. Datetimes
serialize with a ``Z`` suffix, and ``model_dump(mode="json")`` is the canonical
wire and storage form.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationInfo,
    field_validator,
    model_validator,
)
from ulid import ULID

__all__ = [
    "GraceModel",
    "ULIDStr",
    "BBox",
    "Lon",
    "Lat",
    "new_ulid",
    "now_utc",
    "TimeRange",
    "TemporalMode",
    "EngineRunArgsMixin",
    "InputBasis",
    "InputConsequence",
    "SyntheticInput",
    "FallbackConsequence",
    "FallbackActivation",
    "render_fallback_line",
    "render_assumptions_line",
]


# --------------------------------------------------------------------------- #
# ULID
# --------------------------------------------------------------------------- #


def new_ulid() -> str:
    """Generate a fresh ULID string (26 chars, Crockford base32, time-sortable)."""
    return str(ULID())


def _validate_ulid(value: str) -> str:
    """Reject anything that is not a syntactically valid ULID string."""
    # ULID.from_str raises ValueError on malformed input, which surfaces as a
    # pydantic validation error.
    ULID.from_str(value)
    return value


#: A string id that must be a valid ULID. Stored/serialized as a plain string.
ULIDStr = Annotated[str, AfterValidator(_validate_ulid)]


# --------------------------------------------------------------------------- #
# Datetime
# --------------------------------------------------------------------------- #


def now_utc() -> datetime:
    """Timezone-aware current UTC time (the default for ``*_at`` fields)."""
    return datetime.now(timezone.utc)


def _serialize_dt_z(value: datetime) -> str:
    """Serialize a datetime to ISO-8601 with a ``Z`` suffix (UTC).

    Naive datetimes are treated as UTC. Aware datetimes are converted to UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    # isoformat() on a UTC-aware datetime yields "+00:00"; normalize to "Z".
    return value.isoformat().replace("+00:00", "Z")


#: A datetime that always serializes to an ISO-8601 ``Z`` string on the wire.
UTCDatetime = Annotated[datetime, PlainSerializer(_serialize_dt_z, return_type=str)]


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

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


#: Bounding box, always [minLon, minLat, maxLon, maxLat] in EPSG:4326.
BBox = Annotated[tuple[float, float, float, float], AfterValidator(_validate_bbox)]


# --------------------------------------------------------------------------- #
# Base model
# --------------------------------------------------------------------------- #


class GraceModel(BaseModel):
    """Canonical base for every contract model.
    ``extra="forbid"``: an unknown field is a DEFECT, never silently dropped, so
    forward-compatible growth goes through open Literals and additive fields.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        ser_json_timedelta="iso8601",
    )


# --------------------------------------------------------------------------- #
# Shared types
# --------------------------------------------------------------------------- #


class TimeRange(GraceModel):
    """A UTC start/end interval."""

    start: UTCDatetime
    end: UTCDatetime


# --------------------------------------------------------------------------- #
# Engine run-args mixin
# --------------------------------------------------------------------------- #

#: The run's temporal solve mode. ``"steady"`` is a single stationary solve;
#: ``"transient"`` a time-stepping solve that emits an animation. Growth is by
#: an additive Literal member plus an alias, never by arbitrary keys.
TemporalMode = Literal["steady", "transient"]


#: Synonyms mapped onto the canonical ``TemporalMode`` BEFORE the Literal check,
#: so the FIRST attempt validates instead of costing a retry. An UNKNOWN string
#: passes through UNCHANGED, so a genuinely invalid value still raises.
_TEMPORAL_MODE_ALIASES: dict[str, str] = {
    # steady-state / stationary synonyms.
    "steady": "steady",
    "steady-state": "steady",
    "steady_state": "steady",
    "steadystate": "steady",
    "stationary": "steady",
    "static": "steady",
    "equilibrium": "steady",
    # transient / time-varying synonyms.
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


class EngineRunArgsMixin(GraceModel):
    """ADDITIVE, DEFAULT-OFF base for the per-engine ``*RunArgs`` models.
    Every field defaults to the behaviour before it existed, so a model that
    adopts the mixin and sets nothing serializes and behaves byte-identically.
    """

    temporal_mode: TemporalMode = "steady"
    #: Evenly spaced animation output frames.
    output_frames: int = Field(default=24, ge=1)
    #: Per-engine physics overrides. ``None`` is no overrides; what validates
    #: the keys is the engine's own keyword catalog, not this model.
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


# --------------------------------------------------------------------------- #
# Structured input provenance
# --------------------------------------------------------------------------- #

#: Where a single physical model input came from. A demo default is NOT the same
#: thing as a value fetched from real data, supplied by the user, interpreted
#: from the request, or computed from other inputs, and this is the distinction
#: that keeps the two from being narrated alike.
InputBasis = Literal[
    "fetched",
    "user",
    "prompt_interpreted",
    "default_demo",
    "derived",
    # a MEASURED published inversion (e.g. a USGS finite-fault slip model) -- the
    # strongest site-specific class: real measured data, not a scaling law/default.
    "measured_inversion",
    # a SCENARIO source built on REAL published geometry but a HYPOTHETICAL rupture
    # (e.g. a USGS Slab2 subduction-interface + a target-Mw tapered slip) -- LOUDLY a
    # "what if", never confusable with a real event. The interface geometry is real;
    # the earthquake is not.
    "scenario_slab2",
]


#: What a demo default's WRONGNESS costs. The discriminator the input-review
#: gate keys on to decide whether an unresolved ``default_demo`` value may
#: proceed or must REFUSE in auto mode:
#:   - ``physics``: a physics-consequential world value (material property,
#:     forcing magnitude, boundary/source term, friction/decay, invented terrain
#:     or geometry). A ``default_demo`` with this consequence REFUSES in auto - a
#:     wrong value silently ruins the simulation, so it never runs on an invention.
#:   - ``scenario``: the user's QUESTION (magnitude, return period, source
#:     location, a what-if forcing, a duration). A demo default here is a starting
#:     assumption, not a claim about the world - it proceeds, labeled.
#:   - ``numerical``: a solver setting (resolution, timestep, turbulence closure,
#:     calibration constant). Not a world-claim - proceeds, labeled.
#:   - ``aoi``: a default area/domain extent when the user named no place. Not a
#:     physics invention - proceeds, labeled.
InputConsequence = Literal["physics", "scenario", "numerical", "aoi"]

#: Stamped on an older persisted entry that carried ``basis="default_demo"``
#: with no ``consequence``: a tolerant read coerces it to ``scenario`` - proceed,
#: never a spurious refusal on history - and records why here.
_HISTORY_CONSEQUENCE_NOTE = "consequence backfilled=scenario (pre-law-9 record)"


class SyntheticInput(GraceModel):
    """One structured provenance entry for a physical model input.
    ADDITIVE and default-empty: an empty list means "no declared provenance",
    NEVER "all real".
    """

    #: The canonical parameter name.
    param: str
    #: The value actually used. ``None`` when it is not surfaced.
    value: float | int | str | None = None
    units: str | None = None
    basis: InputBasis
    #: REQUIRED when ``basis == "default_demo"``: the gate cannot decide
    #: refuse-vs-proceed from ``basis`` alone, because a demo default may be an
    #: invented physics value or a harmless scenario assumption.
    consequence: InputConsequence | None = None
    #: The fetcher or dataset name, for a fetched or derived basis.
    real_source_if_any: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _require_consequence_for_demo(self, info: ValidationInfo) -> "SyntheticInput":
        """A ``default_demo`` entry MUST carry a ``consequence`` tag; construction
        without one cannot succeed, because an untagged invented default is one
        the gate cannot refuse. A read opting in via
        ``context={"tolerant_history": True}`` backfills ``scenario`` with a note
        instead of raising, so loading an older record never crashes.
        """
        if self.basis == "default_demo" and self.consequence is None:
            tolerant = bool(info.context and info.context.get("tolerant_history"))
            if tolerant:
                object.__setattr__(self, "consequence", "scenario")
                note = (self.note + "; " if self.note else "") + _HISTORY_CONSEQUENCE_NOTE
                object.__setattr__(self, "note", note)
                return self
            raise ValueError(
                "SyntheticInput(basis='default_demo') requires an explicit "
                "consequence tag (physics|scenario|numerical|aoi): an unresolved "
                "demo default must be classified so the input-review gate can "
                "refuse an invented physics value in auto mode (law 9). "
                f"param={self.param!r}"
            )
        return self


#: Which rung of a declared fallback ladder served a request, and what swapping
#: to it COSTS. ``primary`` is the declared first choice and ``user_supplied``
#: the caller's own data - neither is a degradation. The three degradation
#: classes are what the loudness floor keys on: ``same_data`` (another mirror or
#: endpoint of the SAME dataset) walks silently, ``cross_dataset`` (a different
#: dataset, method or resolution) narrates loudly and gates in user_gated mode,
#: ``synthetic`` (a value with no real data source) ALWAYS gates with a labeled
#: default of refuse. ``enhancement`` is the fourth non-degradation: a source
#: BETTER than the primary - finer, more local - laid under part of a request.
#: It is reported so a reader can account for the whole result, but it costs
#: nothing, so the floor ignores it and a call site cannot "permit" it.
FallbackConsequence = Literal[
    "primary", "user_supplied", "enhancement",
    "same_data", "cross_dataset", "synthetic",
]


class FallbackActivation(GraceModel):
    """One rung of a declared fallback ladder that actually served a request."""

    #: The ladder's owner - a tool name or a seam id.
    capability: str
    rung: str
    consequence: FallbackConsequence
    #: The FRACTION of the request this rung served: 1.0 for a whole-request
    #: swap, a share when several rungs painted one mosaic together.
    coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    #: What the alternative IS, in the words the user reads.
    note: str | None = None


def render_fallback_line(activations: Any) -> str | None:
    """Render fallback activations into ONE narration line, or None when clean.
    A run with no degradation renders nothing: the line exists to say what was
    SWAPPED. Takes ``FallbackActivation`` objects or their ``model_dump`` dicts."""
    if not activations:
        return None

    def _field(a: Any, name: str) -> Any:
        return a.get(name) if isinstance(a, dict) else getattr(a, name, None)

    rows = [a for a in activations if float(_field(a, "coverage") or 0.0) > 0.0]
    if not any(
        _field(a, "consequence") in ("same_data", "cross_dataset", "synthetic")
        for a in rows
    ):
        return None
    parts = []
    for a in rows:
        cov = float(_field(a, "coverage") or 0.0)
        share = f"{cov * 100:.0f}% " if cov < 0.999 else ""
        parts.append(f"{share}{_field(a, 'rung')} [{_field(a, 'consequence')}]")
    capability = _field(rows[0], "capability")
    return f"Fallback ladder ({capability}): " + " + ".join(parts) + "."


def render_assumptions_line(entries: Any) -> str | None:
    """Render a ``synthetic_inputs`` list into ONE narration line, ``None`` when
    empty. Grouped by provenance so demo defaults are named plainly rather than
    tabulated. Takes ``SyntheticInput`` objects or their ``model_dump`` dicts."""
    if not entries:
        return None

    def _field(e: Any, name: str) -> Any:
        return e.get(name) if isinstance(e, dict) else getattr(e, name, None)

    def _one(e: Any) -> str:
        param = _field(e, "param")
        value = _field(e, "value")
        units = _field(e, "units")
        val_txt = ""
        if value is not None:
            val_txt = f"={value}" + (f" {units}" if units else "")
        return f"{param}{val_txt}"

    fetched, demo, scenario, other = [], [], [], []
    for e in entries:
        basis = _field(e, "basis")
        if basis == "scenario_slab2":
            scenario.append(e)
        elif basis in ("fetched", "derived", "measured_inversion"):
            fetched.append(e)
        elif basis == "default_demo":
            demo.append(e)
        else:
            other.append(e)

    segments: list[str] = []
    if fetched:
        srcs = sorted({
            str(_field(e, "real_source_if_any"))
            for e in fetched
            if _field(e, "real_source_if_any")
        })
        src_txt = f" (via {', '.join(srcs)})" if srcs else ""
        segments.append(
            "site-derived: " + ", ".join(_one(e) for e in fetched) + src_txt
        )
    if scenario:
        segments.append(
            "SCENARIO (hypothetical rupture on real published geometry, NOT a real "
            "event): " + ", ".join(_one(e) for e in scenario)
        )
    if demo:
        segments.append(
            "demo defaults (NOT site-specific): "
            + ", ".join(_one(e) for e in demo)
        )
    if other:
        segments.append("user/prompt: " + ", ".join(_one(e) for e in other))
    return "Input provenance -- " + "; ".join(segments) + "."
