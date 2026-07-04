"""GRACE-2 shared contracts (SRS v0.3 Appendices A-D + FR-PHC-2 + solver shapes).

Single source of truth for every type that crosses a specialist boundary:
- ``ws``: WebSocket protocol - envelope + every message type (Appendix A).
- ``envelope``: AssessmentEnvelope + flood subtype (Appendix B).
- ``impact_envelope``: ImpactEnvelope - Pelicun post-processor output
  contract (Appendix B.6c).
- ``event``: EventMetadata + ClaimSet/NumericClaim + intensity union (Appendix C).
- ``collections``: the five MongoDB collection schemas + vector index configs
  + TTL config (Appendix D).
- ``catalog``: CatalogEntry - the public_hazard_catalog.yaml entry (FR-PHC-2).
- ``case``: Case persistence envelopes (CaseSummary/CaseChatMessage/
  CaseSessionState) + Case-lifecycle WebSocket envelopes (FR-MP-6).
- ``execution``: ModelSetup / ExecutionHandle / RunResult / LayerURI (FR-TA-2).
- ``tool_metadata``: tool-docstring metadata + ``tool_category`` conventions
  (FR-TA-3, FR-AS-3) - convention only; ``agent`` owns the registry code.

All models subclass ``GraceModel`` (``extra="forbid"``, UTC-``Z`` datetimes).
The canonical wire form is ``model_dump(mode="json")`` (add ``by_alias=True``
for the ``_id``-aliased collection documents; see ``collections.MONGO_DUMP_KWARGS``).
"""

from __future__ import annotations

from . import (
    auth,
    case,
    case_results,
    catalog,
    chart_contracts,
    collections,
    envelope,
    errors,
    event,
    execution,
    impact_envelope,
    modflow_contracts,
    payload_warning,
    publish_manifest,
    region_choice,
    sandbox_contracts,
    secrets,
    swan_contracts,
    swmm_contracts,
    tool_metadata,
    tool_registry,
    user,
    ws,
)
from .case_results import (
    CaseOneResult,
    DerivedEventParam,
    EventIngestProvenance,
    EventIngestResult,
)
from .chart_contracts import (
    ChartEmissionPayload,
    SessionChartRecord,
)
from .common import (
    BBox,
    EngineRunArgsMixin,
    GraceModel,
    Lat,
    Lon,
    TemporalMode,
    TimeRange,
    ULIDStr,
    new_ulid,
    now_utc,
)
from .geoclaw_contracts import GeoClawDepthLayerURI, GeoClawRunArgs
from .modflow_contracts import (
    ASRLayerURI,
    BudgetPartitionLayerURI,
    CaptureZoneLayerURI,
    DewaterLayerURI,
    DrawdownLayerURI,
    HydroperiodLayerURI,
    MODFLOWRunArgs,
    MoundingLayerURI,
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
    SaltwaterWedgeLayerURI,
    SeepageLayerURI,
    SpeciesSpec,
)
from .publish_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PublishManifest,
    PublishManifestBandStats,
    PublishManifestLayer,
    parse_publish_manifest,
)
from .swan_contracts import SwanRunArgs, SwanWaveBoundary, WaveFieldLayerURI
from .swmm_contracts import SWMMDepthLayerURI, SWMMRunArgs
from .sandbox_contracts import (
    CodeExecRequestPayload,
    CodeExecResultPayload,
    CodeExecStatus,
)

__version__ = "0.1.0"
SCHEMA_VERSION = "v1"

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    # modules
    "auth",
    "ws",
    "envelope",
    "impact_envelope",
    "errors",
    "event",
    "collections",
    "catalog",
    "case",
    "case_results",
    "chart_contracts",
    "execution",
    "geoclaw_contracts",
    "modflow_contracts",
    "payload_warning",
    "publish_manifest",
    "region_choice",
    "sandbox_contracts",
    "secrets",
    "swan_contracts",
    "swmm_contracts",
    "tool_metadata",
    "tool_registry",
    "user",
    # case-workflow results
    "CaseOneResult",
    "DerivedEventParam",
    "EventIngestProvenance",
    "EventIngestResult",
    # MODFLOW groundwater contracts (sprint-13)
    "MODFLOWRunArgs",
    "PlumeLayerURI",
    "SeepageLayerURI",
    # MODFLOW Wave-3 multi_species transport (sprint-18 Wave-3)
    "SpeciesSpec",
    "MultiSpeciesPlumeResult",
    "DrawdownLayerURI",
    "DewaterLayerURI",
    "BudgetPartitionLayerURI",
    # MODFLOW Wave-2 archetype layers (sprint-18 Wave-2: MAR / ASR / wetland)
    "MoundingLayerURI",
    "ASRLayerURI",
    "HydroperiodLayerURI",
    # MODFLOW Wave-4 PRT capture-zone vector layer (capture_zone / wellhead_protection)
    "CaptureZoneLayerURI",
    # MODFLOW Wave-5 variable-density saltwater intrusion layer (saltwater_intrusion)
    "SaltwaterWedgeLayerURI",
    # SWMM quasi-2D urban-flood contracts (sprint-16 P1)
    "SWMMRunArgs",
    "SWMMDepthLayerURI",
    # GeoClaw (Clawpack) shallow-water inundation contracts (sprint-17)
    "GeoClawRunArgs",
    "GeoClawDepthLayerURI",
    # SWAN (Simulating WAves Nearshore) spectral wave-field contracts (Phase 1)
    "SwanRunArgs",
    "SwanWaveBoundary",
    "WaveFieldLayerURI",
    # worker -> agent publish-manifest reader (SFINCS postprocess offload Phase 4)
    "MANIFEST_SCHEMA_VERSION",
    "PublishManifest",
    "PublishManifestBandStats",
    "PublishManifestLayer",
    "parse_publish_manifest",
    # chart-emission contracts (sprint-13 conversational analysis layer)
    "ChartEmissionPayload",
    "SessionChartRecord",
    # python-sandbox code-exec contracts (sprint-13 conversational analysis layer)
    "CodeExecRequestPayload",
    "CodeExecResultPayload",
    "CodeExecStatus",
    # common primitives
    "GraceModel",
    "ULIDStr",
    "BBox",
    "Lon",
    "Lat",
    "TimeRange",
    "TemporalMode",
    "EngineRunArgsMixin",
    "new_ulid",
    "now_utc",
]
