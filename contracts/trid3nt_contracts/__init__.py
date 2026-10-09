"""The types that cross a package boundary - one definition, imported everywhere.

Every model subclasses ``ContractModel`` (``extra="forbid"``, UTC-``Z`` datetimes)
and the canonical wire form is ``model_dump(mode="json")``; the ``_id``-aliased
collection documents additionally take ``by_alias=True``.
"""

from __future__ import annotations

from . import (
    auth,
    case,
    chart_contracts,
    collections,
    coverage,
    execution,
    gate_spec,
    message,
    payload_warning,
    processing_contracts,
    secrets,
    tool_registry,
    ws,
)
from .chart_contracts import (
    ChartEmissionPayload,
    SessionChartRecord,
)
from .common import (
    BBox,
    EngineRunArgsMixin,
    ContractModel,
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
)
from .message import (
    Message,
    Part,
    ToolCall,
    ToolDeclaration,
    ToolResponse,
)
from .ws import (
    LayerRequestPayload,
    LayerResponsePayload,
)
from .processing_contracts import (
    CodeExecRequestPayload,
    ProcessingRequestPayload,
    ProcessingResponsePayload,
)

__version__ = "0.1.4"
SCHEMA_VERSION = "v1"

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    # modules
    "auth",
    "ws",
    "collections",
    "coverage",
    "case",
    "chart_contracts",
    "execution",
    "gate_spec",
    "message",
    "payload_warning",
    "processing_contracts",
    "secrets",
    "tool_registry",
    # the adapters' message IR
    "Message",
    "Part",
    "ToolCall",
    "ToolDeclaration",
    "ToolResponse",
    # chart emission
    "ChartEmissionPayload",
    "SessionChartRecord",
    # the borrowed-provider pair
    "LayerRequestPayload",
    "LayerResponsePayload",
    # the session processing pair and the code approval card
    "CodeExecRequestPayload",
    "ProcessingRequestPayload",
    "ProcessingResponsePayload",
    # common primitives
    "ContractModel",
    "ULIDStr",
    "BBox",
    "Lon",
    "Lat",
    "TimeRange",
    "TemporalMode",
    "EngineRunArgsMixin",
    "InputBasis",
    "SyntheticInput",
    "render_assumptions_line",
    "new_ulid",
    "now_utc",
]
