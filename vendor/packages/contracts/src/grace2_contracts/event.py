"""EventMetadata, ClaimSet/NumericClaim, and the intensity union (Appendix C).

``EventMetadata`` is the structured representation of a real-world hazard event
extracted from news + agency content. Produced by ``extract_event_metadata``,
stored in the ``events`` collection, consumed by ``model_news_event``.

Invariants this module is responsible for:
- **7. Claims carry provenance.** Every numerical claim is a per-source
  ``NumericClaim`` in a ``ClaimSet`` with computed consensus. ``source_type``
  is a closed ``Literal`` mapped from a curated table (engine-owned), never
  LLM-judged. ``consensus_value`` is what gets narrated.
- **Every numeric intensity field is a ``ClaimSet | None``, never a bare number**
  (Decision M, C.4). Non-numeric fields (landfall_location, breach_type,
  river_name, gauge_id, ...) stay scalar.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import (
    BBox,
    GraceModel,
    TimeRange,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    "EventType",
    "SourceType",
    "ConsensusMethod",
    "ConsensusConfidence",
    "NumericClaim",
    "ClaimSet",
    "AdminUnit",
    "EventLocation",
    "EventProvenance",
    "HurricaneIntensity",
    "TropicalStormIntensity",
    "AtmosphericRiverIntensity",
    "RainfallIntensity",
    "DamFailureIntensity",
    "StormSurgeIntensity",
    "RiverFloodIntensity",
    "FlashFloodIntensity",
    "GenericIntensity",
    "IntensityIndicators",
    "EventMetadata",
]


# Open enum (Decision G): grows as wildfire/seismic/contaminant engines land.
EventType = Literal[
    "hurricane",
    "tropical_storm",
    "atmospheric_river",
    "intense_rainfall",
    "dam_failure",
    "levee_failure",
    "storm_surge",
    "river_flood",
    "flash_flood",
    "other",
]

# Closed Literal mapped from a curated source-to-tier table (engine-owned,
# FR-HEP-2, invariant 7). Never a field the LLM free-fills with a judged tier.
SourceType = Literal[
    "agency",  # tier 1 or 2: NWS, USGS, NHC, etc.
    "major_news",  # tier 3: AP, Reuters, NYT, WaPo with direct sourcing
    "regional_news",  # tier 4: regional dailies, local TV websites
    "aggregator",  # tier 5: news aggregators, secondary reporting
    "social",  # tier 6: social/community (deferred to v0.2+)
    "other",
]

ConsensusMethod = Literal[
    "single_source",  # only one claim, no aggregation
    "median",  # median across non-outlier claims
    "authority_weighted",  # weighted by source_type tier
    "latest_authoritative",  # most recent agency claim
    "agent_synthesized",  # LLM-reasoned consensus (deep research mode)
]
ConsensusConfidence = Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- #
# Claim-set types for multi-source numerical evidence (Appendix C.3)
# --------------------------------------------------------------------------- #


class NumericClaim(GraceModel):
    """A single numerical claim from a single source, with provenance."""

    value: float
    unit: str  # canonical unit string, e.g., "kt", "mb", "ft", "inches"
    source_type: SourceType  # data-driven tier; never LLM-judged (invariant 7)
    source_id: str  # article_id or agency feed entry id
    source_url: str
    observation_time: UTCDatetime | None = None  # when measured/observed, if known
    reporting_time: UTCDatetime  # when the source reported it
    confidence: float | None = None  # source's stated confidence, if any (0..1)
    outlier_flag: bool = False  # set by aggregation logic; True if flagged


class ClaimSet(GraceModel):
    """A set of numerical claims for one quantity across sources, with consensus.

    ``consensus_value`` is the narrated number (invariant 7). Contributing
    ``claims`` stay drillable. Populated by ``aggregate_claims_across_sources``
    (engine-owned); ``schema`` owns this shape.
    """

    claims: list[NumericClaim] = Field(default_factory=list)
    consensus_value: float | None = None
    consensus_unit: str | None = None
    consensus_method: ConsensusMethod | None = None
    consensus_confidence: ConsensusConfidence | None = None
    notes: str | None = None  # agent commentary if relevant


# --------------------------------------------------------------------------- #
# Location and provenance (Appendix C.3)
# --------------------------------------------------------------------------- #


class AdminUnit(GraceModel):
    """Parsed administrative context for an event location."""

    country: str | None = None  # ISO 3166-1 alpha-2, e.g., "US"
    region: str | None = None  # state/province, e.g., "FL"
    locality: str | None = None  # city/town


class EventLocation(GraceModel):
    """Event location. At least one of ``bbox`` or ``place_name`` is required."""

    bbox: BBox | None = None
    place_name: str | None = None  # e.g., "Fort Myers, FL"
    admin_unit: AdminUnit | None = None
    geocoded: bool = False  # True iff bbox came from geocoding

    # Granularity for client-side auto-snap and padding decisions
    granularity: (
        Literal["country", "region", "state", "city", "facility", "bbox"] | None
    ) = None

    # Modeling-readiness assessment for the news pipeline / dispatcher
    precision_class: (
        Literal[
            "point_known",  # specific named facility, point coords available
            "polygon_known",  # specific neighborhood with defined boundaries
            "bbox_sufficient",  # admin area where bbox is enough for the task
            "imprecise",  # "near the river" — needs user spatial input
            "ambiguous",  # "Springfield" — needs disambiguation
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _require_bbox_or_place_name(self) -> "EventLocation":
        if self.bbox is None and self.place_name is None:
            raise ValueError("EventLocation requires at least one of bbox or place_name")
        return self


class EventProvenance(GraceModel):
    """Source attribution for an extracted event."""

    article_ids: list[ULIDStr] = Field(default_factory=list)  # contributing articles
    primary_article_id: ULIDStr  # the "main" article when synthesizing across many
    extraction_notes: str | None = None  # free-text caveats from the extractor


# --------------------------------------------------------------------------- #
# Intensity indicators, discriminated by event_type (Appendix C.4)
# --------------------------------------------------------------------------- #
# Every numeric quantity is ClaimSet | None. Non-numeric fields stay scalar.


class HurricaneIntensity(GraceModel):
    saffir_simpson: ClaimSet | None = None  # 1..5
    max_winds_kt: ClaimSet | None = None  # sustained winds at peak
    min_central_pressure_mb: ClaimSet | None = None
    landfall_location: str | None = None  # non-numeric


class TropicalStormIntensity(GraceModel):
    max_winds_kt: ClaimSet | None = None
    landfall_location: str | None = None  # non-numeric


class AtmosphericRiverIntensity(GraceModel):
    ar_category: ClaimSet | None = None  # 1..5 (Ralph et al. scale)
    ivt_kg_m_s: ClaimSet | None = None  # integrated vapor transport


class RainfallIntensity(GraceModel):
    total_inches: ClaimSet | None = None
    duration_hours: ClaimSet | None = None
    peak_hourly_inches: ClaimSet | None = None
    return_period_years: ClaimSet | None = None


class DamFailureIntensity(GraceModel):
    dam_name: str | None = None  # non-numeric
    reservoir_volume_acre_feet: ClaimSet | None = None
    breach_type: Literal["overtopping", "piping", "structural", "unknown"] | None = None


class StormSurgeIntensity(GraceModel):
    peak_surge_ft: ClaimSet | None = None
    associated_storm: str | None = None  # non-numeric


class RiverFloodIntensity(GraceModel):
    river_name: str | None = None  # non-numeric
    peak_stage_ft: ClaimSet | None = None
    flood_stage_ft: ClaimSet | None = None  # official flood-stage threshold
    gauge_id: str | None = None  # USGS NWIS gauge if extractable; non-numeric


class FlashFloodIntensity(GraceModel):
    duration_hours: ClaimSet | None = None
    cause: Literal["thunderstorm", "training_storms", "dam_break", "unknown"] | None = None


class GenericIntensity(GraceModel):
    description: str  # free-text fallback
    severity: Literal["minor", "moderate", "major", "catastrophic"] | None = None


class IntensityIndicators(GraceModel):
    """Discriminated union: exactly one field populated based on event_type."""

    hurricane: HurricaneIntensity | None = None
    tropical_storm: TropicalStormIntensity | None = None
    atmospheric_river: AtmosphericRiverIntensity | None = None
    rainfall: RainfallIntensity | None = None
    dam_failure: DamFailureIntensity | None = None
    storm_surge: StormSurgeIntensity | None = None
    river_flood: RiverFloodIntensity | None = None
    flash_flood: FlashFloodIntensity | None = None
    generic: GenericIntensity | None = None  # fallback for "other"


# event_type -> the intensity field expected to be populated. levee_failure and
# intense_rainfall have no dedicated intensity field in C.4; they map to the
# closest available field per the dispatcher (C.7): levee_failure -> dam_failure
# machinery, intense_rainfall -> rainfall. See report OQ-S3.
_EVENT_TYPE_TO_INTENSITY: dict[str, str] = {
    "hurricane": "hurricane",
    "tropical_storm": "tropical_storm",
    "atmospheric_river": "atmospheric_river",
    "intense_rainfall": "rainfall",
    "dam_failure": "dam_failure",
    "levee_failure": "dam_failure",
    "storm_surge": "storm_surge",
    "river_flood": "river_flood",
    "flash_flood": "flash_flood",
    "other": "generic",
}


# --------------------------------------------------------------------------- #
# Top-level EventMetadata (Appendix C.2)
# --------------------------------------------------------------------------- #


class EventMetadata(GraceModel):
    """Structured representation of a real-world hazard event (Appendix C.2)."""

    schema_version: Literal["v1"] = "v1"

    # Identity
    event_id: ULIDStr

    # Classification
    event_type: EventType
    confidence: float = Field(ge=0.0, le=1.0)  # extractor's self-reported confidence

    # Identity within the event domain, when available
    canonical_name: str | None = None  # e.g., "Hurricane Ian"
    canonical_id: str | None = None  # e.g., "AL092022" for ATCF storms

    # Location
    location: EventLocation

    # Time
    time_range: TimeRange
    time_classification: Literal["past", "ongoing", "forecast"]

    # Intensity (discriminated by event_type)
    intensity: IntensityIndicators

    # Source attribution
    provenance: EventProvenance

    # Search support (populated separately from extraction)
    embedding: list[float] | None = None
    embedding_model: str | None = None  # e.g., "text-embedding-005"

    # Lifecycle
    extracted_at: UTCDatetime
    extractor_version: str  # for reproducibility

    @model_validator(mode="after")
    def _intensity_matches_event_type(self) -> "EventMetadata":
        """The populated intensity field must correspond to ``event_type``.

        Other intensity fields must be ``None``. This keeps the discriminated
        union honest without forcing the dispatcher to guess which payload to
        read. ``levee_failure``/``intense_rainfall`` map onto the closest field
        (see ``_EVENT_TYPE_TO_INTENSITY`` and report OQ-S3).
        """
        expected = _EVENT_TYPE_TO_INTENSITY[self.event_type]
        present = [
            name
            for name, value in self.intensity.__dict__.items()
            if value is not None
        ]
        # Allow zero intensity payloads (extraction may find no quantities),
        # but if any is present it must be exactly the expected one.
        if present and present != [expected]:
            raise ValueError(
                f"intensity payload for event_type={self.event_type!r} must be "
                f"{expected!r} (or empty); found {present!r}"
            )
        return self
