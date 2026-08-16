"""AssessmentEnvelope and supporting types (SRS Appendix B, FR-TA-1, FR-AS-7).

The ``AssessmentEnvelope`` is the system's central output: what every hazard
engine produces, what the agent narrates from, what is embedded in the ``runs``
collection, and what feeds the UI's layer loading. One shape across in-memory
(pydantic), wire (JSON over WebSocket), and storage (MongoDB).

Invariants this module is responsible for:
- **3. Engine registration, not modification.** The base + ``BaseMetrics`` are
  hazard-agnostic; flood specifics live only in ``flood: FloodPayload | None``,
  discriminated by ``hazard_type``. Exactly one subtype field is populated.
- **1. Determinism boundary.** Every number the narrative cites is a typed field
  on ``FloodMetrics`` (or a ``ResultLayer``), never free text.
- **9. No cost theater.** No cost field appears anywhere in this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from .common import (
    BBox,
    GraceModel,
    Lat,
    Lon,
    TimeRange,
    ULIDStr,
    UTCDatetime,
)

if TYPE_CHECKING:
    # ``LegendKey`` lives in execution.py, which imports this module
    # (``TemporalConfig``). A runtime import here would be circular, so the
    # ``ResultLayer.legend`` annotation is a string forward-ref pydantic
    # resolves lazily against the ``execution`` module namespace on first
    # validation (the package is always fully importable by then).
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


# Open enum (Decision G): new engines register new hazards without a breaking
# change. v0.1 ships flood as the only fully-typed subtype.
HazardType = Literal["flood", "groundwater", "wildfire", "seismic", "spill"]
EnvelopeType = Literal["modeled", "discovered"]


# --------------------------------------------------------------------------- #
# Supporting types (Appendix B.3)
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
    source: str  # human-readable, e.g., "NHC ATCF, Hurricane Ian"
    parameters: dict  # forcing-specific; validated per workflow (engine-owned)
    inputs_uri: str | None = None  # GCS URI to forcing data file, if any


class TemporalConfig(GraceModel):
    """WMS-T temporal config for a time-varying layer."""

    start: UTCDatetime
    end: UTCDatetime
    step_seconds: int = Field(gt=0)


class ResultLayer(GraceModel):
    """A renderable result layer.

    Field-for-field alignable with ``map-command load-layer`` args
    (``layer_id``, ``style_preset``, ``temporal``) so the UI renders without
    translation. Output formats are fixed by FR-CE-4/FR-QS-3: rasters COG,
    vectors FlatGeobuf/GeoParquet. ``LayerURI`` (execution.py) is the producer
    shape that maps onto this.

    ``legend`` mirrors ``LayerURI.legend`` -- the DATA-DRIVEN render key (see
    ``execution.LegendKey``): the colormap is the semantic per-variable choice,
    the range is the REAL data range. Additive + optional -- ``None`` means
    legacy ``style_preset`` rendering, so layers without a legend render exactly
    as before.
    """

    layer_id: str  # stable id; used in map-command messages
    name: str  # human-readable display name
    layer_type: Literal["raster", "vector"]
    uri: str  # gs://... canonical location (COG / FlatGeobuf / GeoParquet)
    style_preset: str  # references the QML preset library
    temporal: TemporalConfig | None = None  # present iff layer is time-varying
    role: Literal["primary", "context", "input"]
    units: str | None = None  # e.g., "meters", "m/s", or None for categorical
    legend: "LegendKey | None" = None  # data-driven render key; None => legacy style_preset rendering


class DataSource(GraceModel):
    """A single upstream data source, as a typed (not prose) provenance record."""

    name: str  # e.g., "USGS 3DEP"
    uri: str  # the actual data file used
    accessed_at: UTCDatetime


class Provenance(GraceModel):
    """Structured provenance for a modeled or discovered envelope (invariant 7)."""

    data_sources: list[DataSource] = Field(default_factory=list)
    article_ids: list[ULIDStr] = Field(default_factory=list)  # if news-derived
    event_id: ULIDStr | None = None  # MongoDB event id, if news-derived


class CatalogReference(GraceModel):
    """Denormalized reference to a public_hazard_catalog entry (discovery only)."""

    catalog_entry_id: str  # references public_hazard_catalog.yaml entry
    title: str  # denormalized for narrative use
    agency: str  # denormalized for narrative use
    access_url: str  # the URL fetched for this layer
    license: str  # license text or URL


class BaseMetrics(GraceModel):
    """Empty base; subtype payloads carry the real metric fields (Appendix B.3).

    The envelope's top-level ``metrics`` field is ``BaseMetrics`` and stays
    empty by design — real numbers live in the hazard subtype payload
    (``flood.metrics`` etc.), enforcing invariant 3.
    """


# --------------------------------------------------------------------------- #
# Flood subtype (Appendix B.4) — the only fully-typed subtype in v0.1
# --------------------------------------------------------------------------- #


class CriticalFacility(GraceModel):
    """A flooded critical facility (invariant 1: typed, narratable number)."""

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
    affected_buildings_by_depth: dict[str, int] | None = None
    # e.g., {"0-0.5m": 412, "0.5-1m": 251, "1-2m": 132, "2m+": 52}
    affected_critical_facilities: list[CriticalFacility] | None = None
    population_exposed: int | None = None

    # Solver provenance
    solver_version: str  # e.g., "sfincs-v2.0.4"
    grid_resolution_m: float = Field(gt=0.0)
    simulation_duration_hours: int = Field(gt=0)


class FloodPayload(GraceModel):
    """Flood hazard subtype payload. Populated iff ``hazard_type == 'flood'``."""

    metrics: FloodMetrics


# --------------------------------------------------------------------------- #
# Top-level envelope (Appendix B.2)
# --------------------------------------------------------------------------- #


class AssessmentEnvelope(GraceModel):
    """The central output structure (Appendix B.2).

    For a given envelope, exactly one subtype field matching ``hazard_type`` is
    populated; the rest are ``None``. ``envelope_type`` (modeled vs discovered)
    is independent of ``hazard_type``.

    v0.1 note: only the flood subtype is fully typed (``FloodPayload``). The
    v0.2+/v0.3+ subtypes (groundwater/wildfire/seismic/spill) are carried as
    permissive ``dict | None`` slots until their engines land — matching
    Appendix B.6b, which states discovery payloads are a permissive dict
    validated at the workflow layer in v0.1. See report Open Question OQ-S2.
    """

    schema_version: Literal["v1"] = "v1"

    # Identity
    envelope_id: ULIDStr
    project_id: ULIDStr  # links to projects collection
    session_id: ULIDStr  # links to sessions collection

    # Mode discriminator
    envelope_type: EnvelopeType

    # Classification
    hazard_type: HazardType
    workflow_name: str  # e.g., "run_storm_surge_flood", "show_hazard_layer"

    # Spatial and temporal extent
    bbox: BBox
    crs: str = "EPSG:4326"
    time_range: TimeRange | None = None  # event time; None for synthetic/discovery

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

    # Subtype payloads (discriminator: hazard_type)
    flood: FloodPayload | None = None
    groundwater: dict | None = None  # v0.2+ (permissive until engine lands)
    wildfire: dict | None = None  # v0.2+ (permissive until engine lands)
    seismic: dict | None = None  # v0.3+
    spill: dict | None = None  # v0.3+

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
