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
    "errors",
    "collections",
    "catalog",
    "case",
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
    # publish manifest: worker -> agent
    "MANIFEST_SCHEMA_VERSION",
    "PublishManifest",
    "PublishManifestBandStats",
    "PublishManifestLayer",
    "parse_publish_manifest",
    # outputs.json manifest: writer + tolerant reader
    "OUTPUTS_MANIFEST_SCHEMA_VERSION",
    "OUTPUT_KINDS",
    "OutputEntry",
    "OutputsManifest",
    "parse_outputs_manifest",
    # chart emission
    "ChartEmissionPayload",
    "SessionChartRecord",
    # python-sandbox code exec
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
