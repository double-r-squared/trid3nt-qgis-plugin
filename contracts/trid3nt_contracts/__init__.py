"""Shared contracts (SRS v0.3 Appendices A-D + + solver shapes).

Single source of truth for every type that crosses a specialist boundary:
- ``ws``: WebSocket protocol - envelope + every message type.
- ``envelope``: AssessmentEnvelope + flood subtype.
- ``impact_envelope``: ImpactEnvelope - Pelicun post-processor output
  contract (c).
- ``event``: EventMetadata + ClaimSet/NumericClaim + intensity union.
- ``collections``: the five MongoDB collection schemas + vector index configs
  + TTL config.
- ``catalog``: CatalogEntry - the public_hazard_catalog.yaml entry.
- ``case``: Case persistence envelopes (CaseSummary/CaseChatMessage/
  CaseSessionState) + Case-lifecycle WebSocket envelopes.
- ``execution``: ModelSetup / ExecutionHandle / RunResult / LayerURI.
- ``tool_metadata``: tool-docstring metadata + ``tool_category`` conventions
 - convention only; ``agent`` owns the registry code.

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
    gate_spec,
    impact_envelope,
    payload_warning,
    publish_manifest,
    region_choice,
    sandbox_contracts,
    secrets,
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
    FallbackActivation,
    FallbackConsequence,
    GraceModel,
    InputBasis,
    Lat,
    Lon,
    SyntheticInput,
    TemporalMode,
    TimeRange,
    ULIDStr,
    new_ulid,
    now_utc,
    render_assumptions_line,
    render_fallback_line,
)
from .publish_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PublishManifest,
    PublishManifestBandStats,
    PublishManifestLayer,
    parse_publish_manifest,
)
from .outputs_manifest import (
    OUTPUTS_MANIFEST_SCHEMA_VERSION,
    OUTPUT_KINDS,
    OutputEntry,
    OutputsManifest,
    parse_outputs_manifest,
)
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
    "gate_spec",
    "payload_warning",
    "publish_manifest",
    "outputs_manifest",
    "region_choice",
    "sandbox_contracts",
    "secrets",
    "tool_metadata",
    "tool_registry",
    "user",
    # case-workflow results
    "CaseOneResult",
    "DerivedEventParam",
    "EventIngestProvenance",
    "EventIngestResult",
    # worker -> agent publish-manifest reader (SFINCS postprocess offload Phase 4)
    "MANIFEST_SCHEMA_VERSION",
    "PublishManifest",
    "PublishManifestBandStats",
    "PublishManifestLayer",
    "parse_publish_manifest",
    # emit-on-solve outputs.json manifest (writer + tolerant reader)
    "OUTPUTS_MANIFEST_SCHEMA_VERSION",
    "OUTPUT_KINDS",
    "OutputEntry",
    "OutputsManifest",
    "parse_outputs_manifest",
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
    "InputBasis",
    "SyntheticInput",
    "FallbackActivation",
    "FallbackConsequence",
    "render_fallback_line",
    "render_assumptions_line",
    "new_ulid",
    "now_utc",
]
