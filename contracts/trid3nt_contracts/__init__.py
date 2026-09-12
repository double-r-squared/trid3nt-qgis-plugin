"""The types that cross a package boundary - one definition, imported everywhere.

Every model subclasses ``GraceModel`` (``extra="forbid"``, UTC-``Z`` datetimes)
and the canonical wire form is ``model_dump(mode="json")``; the ``_id``-aliased
collection documents additionally take ``by_alias=True``.
"""

from __future__ import annotations

from . import (
    auth,
    case,
    catalog,
    chart_contracts,
    collections,
    envelope,
    errors,
    execution,
    gate_spec,
    message,
    payload_warning,
    processing_contracts,
    region_choice,
    secrets,
    tool_metadata,
    tool_registry,
    user,
    ws,
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
from .message import (
    Message,
    Part,
    ToolCall,
    ToolDeclaration,
    ToolResponse,
)
from .outputs_manifest import (
    OUTPUTS_MANIFEST_SCHEMA_VERSION,
    OUTPUT_KINDS,
    OutputEntry,
    OutputsManifest,
    parse_outputs_manifest,
)
from .processing_contracts import (
    CodeExecRequestPayload,
    ProcessingRequestPayload,
    ProcessingResponsePayload,
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
    "errors",
    "collections",
    "catalog",
    "case",
    "chart_contracts",
    "execution",
    "gate_spec",
    "message",
    "payload_warning",
    "outputs_manifest",
    "processing_contracts",
    "region_choice",
    "secrets",
    "tool_metadata",
    "tool_registry",
    "user",
    # the adapters' message IR
    "Message",
    "Part",
    "ToolCall",
    "ToolDeclaration",
    "ToolResponse",
    # outputs.json manifest: writer + tolerant reader
    "OUTPUTS_MANIFEST_SCHEMA_VERSION",
    "OUTPUT_KINDS",
    "OutputEntry",
    "OutputsManifest",
    "parse_outputs_manifest",
    # chart emission
    "ChartEmissionPayload",
    "SessionChartRecord",
    # the session processing pair and the code approval card
    "CodeExecRequestPayload",
    "ProcessingRequestPayload",
    "ProcessingResponsePayload",
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
