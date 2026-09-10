"""``AssessmentEnvelope`` - the central output shape.

One shape across memory (pydantic), wire (JSON) and storage. The base and
``BaseMetrics`` are hazard-agnostic: hazard specifics live only in the subtype
payload ``hazard_type`` selects, and every number a narrative cites is a typed
field here rather than free text. No cost field appears anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator

from .common import (
    BBox,
    FallbackActivation,
    GraceModel,
    Lat,
    Lon,
    TimeRange,
    ULIDStr,
    UTCDatetime,
)

if TYPE_CHECKING:
    # ``LegendKey`` lives in a module that imports this one, so a runtime
    # import here would be circular. The ``ResultLayer.legend`` annotation
    # stays a string forward-ref, resolved lazily at first validation.
    from .execution import LegendKey

__all__ = [
    "HazardType",
    "EnvelopeType",
    "ForcingSummary",
    "ResultLayer",
    "TemporalConfig",
    "DataSource",
    "Provenance",
    "CatalogReference",
    "BaseMetrics",
    "FloodMetrics",
    "CriticalFacility",
    "FloodPayload",
    "AssessmentEnvelope",
]


# Open enum: a new engine registers a new hazard without a breaking change.
# Flood is the only fully typed subtype; the rest ride permissive dicts.
HazardType = Literal["flood", "groundwater", "wildfire", "seismic", "spill"]
EnvelopeType = Literal["modeled", "discovered"]


# --------------------------------------------------------------------------- #
# Supporting types
# --------------------------------------------------------------------------- #


class ForcingSummary(GraceModel):
    """Boundary-condition summary for a modeled envelope (None for discovery)."""

    forcing_type: Literal[
        "storm_surge",
        "pluvial_synthetic",
        "fluvial_synthetic",
        "news_derived",
        "user_supplied",
    ]
    source: str  # human-readable
    parameters: dict  # forcing-specific; validated by the owning workflow
    inputs_uri: str | None = None  # the forcing data file, if any


class TemporalConfig(GraceModel):
    """WMS-T temporal config for a time-varying layer."""

    start: UTCDatetime
    end: UTCDatetime
    step_seconds: int = Field(gt=0)


class ResultLayer(GraceModel):
    """A renderable result layer.
    Field-for-field alignable with the ``load-layer`` map command so a client
    renders it without translating. Rasters are COG, vectors FlatGeobuf."""

    layer_id: str  # stable id; used in map-command messages
    name: str  # human-readable display name
    layer_type: Literal["raster", "vector"]
    uri: str  # canonical object-store location
    style: dict[str, Any] | None = None  # the DECLARED style row
    temporal: TemporalConfig | None = None  # present iff layer is time-varying
    role: Literal["primary", "context", "input"]
    units: str | None = None  # e.g., "meters", "m/s", or None for categorical
    # The declared style resolved against this layer - concrete range and the
    # .qml the map loads. None until the layer is published.
    legend: "LegendKey | None" = None
    # Which rungs of a declared fallback ladder produced this layer's inputs.
    # Empty means "no ladder governs this", never "nothing was substituted".
    fallbacks: list[FallbackActivation] = Field(default_factory=list)
    fallback_note: str | None = None  # one-line narration of what was swapped


class DataSource(GraceModel):
    """A single upstream data source, as a typed (not prose) provenance record."""

    name: str  # e.g., "USGS 3DEP"
    uri: str  # the actual data file used
    accessed_at: UTCDatetime


class Provenance(GraceModel):
    """Structured provenance: attribution is generated from these, not written."""

    data_sources: list[DataSource] = Field(default_factory=list)
    article_ids: list[ULIDStr] = Field(default_factory=list)  # if news-derived
    event_id: ULIDStr | None = None  # the event id, if news-derived


class CatalogReference(GraceModel):
    """Denormalized reference to a curated catalog entry. Discovery only."""

    catalog_entry_id: str  # the ``CatalogEntry.id`` this came from
    title: str  # denormalized for narrative use
    agency: str  # denormalized for narrative use
    access_url: str  # the URL fetched for this layer
    license: str  # license text or URL


class BaseMetrics(GraceModel):
    """Empty base. The envelope's top-level ``metrics`` stays empty BY DESIGN:
    the real numbers live in the hazard subtype payload, which is what keeps
    the envelope itself hazard-agnostic."""


# --------------------------------------------------------------------------- #
# Flood subtype - the only fully typed one
# --------------------------------------------------------------------------- #


class CriticalFacility(GraceModel):
    """A flooded critical facility, as typed numbers a narrative can cite."""

    name: str
    category: Literal["school", "hospital", "fire_station", "police", "other"]
    coordinates: tuple[Lon, Lat]  # [lon, lat], EPSG:4326
    max_depth_m: float


class FloodMetrics(BaseMetrics):
    """Structured flood metrics. Every number the narrative cites lives here."""

    # Spatial extent of impact
    flooded_area_km2: float = Field(ge=0.0)

    # Depth statistics, computed over flooded cells only
    max_depth_m: float
    mean_depth_m: float
    p95_depth_m: float  # 95th percentile

    # Velocity, if the run computed it
    max_velocity_m_s: float | None = None

    # Affected assets, optional based on which fetchers ran
    affected_buildings_count: int | None = None
    # Counts keyed by a depth-band label, e.g. "0.5-1m".
    affected_buildings_by_depth: dict[str, int] | None = None
    affected_critical_facilities: list[CriticalFacility] | None = None
    population_exposed: int | None = None

    # Solver provenance
    solver_version: str
    grid_resolution_m: float = Field(gt=0.0)
    simulation_duration_hours: int = Field(gt=0)


class FloodPayload(GraceModel):
    """Flood hazard subtype payload. Populated iff ``hazard_type == 'flood'``."""

    metrics: FloodMetrics


# --------------------------------------------------------------------------- #
# Top-level envelope
# --------------------------------------------------------------------------- #


class AssessmentEnvelope(GraceModel):
    """The central output structure.
    Exactly one subtype field is populated - the one matching ``hazard_type`` -
    and ``envelope_type`` is independent of which hazard that is."""

    schema_version: Literal["v1"] = "v1"

    # Identity
    envelope_id: ULIDStr
    project_id: ULIDStr
    session_id: ULIDStr

    # Mode discriminator
    envelope_type: EnvelopeType

    # Classification
    hazard_type: HazardType
    workflow_name: str

    # Spatial and temporal extent
    bbox: BBox
    crs: str = "EPSG:4326"
    time_range: TimeRange | None = None  # event time; None for a discovery

    # Forcing summary (modeled only; None for discovered)
    forcing: ForcingSummary | None = None

    # Catalog reference (discovered only; None for modeled)
    catalog_entries: list[CatalogReference] | None = None

    # Outputs
    layers: list[ResultLayer] = Field(default_factory=list)
    metrics: BaseMetrics = Field(default_factory=BaseMetrics)

    # Provenance
    provenance: Provenance

    # Lifecycle
    created_at: UTCDatetime
    completed_at: UTCDatetime
    solver_run_ids: list[ULIDStr] = Field(default_factory=list)  # empty for discovered

    # Subtype payloads, discriminated by hazard_type. Only flood is typed; the
    # rest stay permissive dicts, validated at the workflow layer, until their
    # engines land and earn a model.
    flood: FloodPayload | None = None
    groundwater: dict | None = None
    wildfire: dict | None = None
    seismic: dict | None = None
    spill: dict | None = None

    @model_validator(mode="after")
    def _exactly_one_subtype_matching_hazard(self) -> "AssessmentEnvelope":
        """Exactly the ``hazard_type`` subtype payload is populated; rest None."""
        subtypes = {
            "flood": self.flood,
            "groundwater": self.groundwater,
            "wildfire": self.wildfire,
            "seismic": self.seismic,
            "spill": self.spill,
        }
        populated = [name for name, value in subtypes.items() if value is not None]
        if populated != [self.hazard_type]:
            raise ValueError(
                "exactly one subtype payload must be populated and it must match "
                f"hazard_type={self.hazard_type!r}; populated subtypes={populated!r}"
            )
        return self
