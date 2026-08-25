"""Declarative workflows: a workflow is PARAMS + DATA + a pure ``plan(p, d)``.

The plan is a value; the interpreter walks it. See
``docs/design/declarative-workflows.md``.
"""

from __future__ import annotations

from .data import (
    AuthoredProducer,
    Build,
    CoversAOI,
    Data,
    DataDecl,
    Fetch,
    Producer,
    ReferenceProducer,
)
from .docstring import render_docstring
from .domain import Domain, current_domain
from .errors import (
    ByoCoverageError,
    DeclarativeError,
    GateRefusedError,
    LeakScanTruncated,
    ModifierIllegalError,
    ParamOutOfRangeError,
    ParamRefLeakedError,
    PlanValidationError,
    RenderSourceMissingError,
    StepFailedError,
)
from .interpreter import RunResult, interpret
from .ledger import LedgerRecord, StepLedger, invocation_key
from .params import (
    Derived,
    Param,
    ParamNotResolved,
    ParamValues,
    ResolvedParam,
    ResolvedParams,
    doors,
)
from .plan import (
    STAGES,
    ChartSpec,
    DrawGate,
    FormGate,
    Gate,
    ParamRef,
    Plan,
    Ref,
    RenderSpec,
    RunMode,
    Step,
    When,
)
from .slots import Forcing, MeshPolicy, Physics, Slot
from .workflow import (
    DataRefs,
    EngineOps,
    FacadeIncompleteError,
    UndeclaredDataError,
    WireArgsError,
    Workflow,
    register_workflow,
)
from .temporal import (
    CATEGORICAL,
    RATE,
    STATE,
    ResampleSpec,
    TemporalGapError,
    TemporalShapeError,
    TemporalSpec,
    TemporalUnitsError,
    Transformed,
    UnitsSpec,
    convert_units,
    transform_series,
    transform_value,
)
from .resolver import (
    merge_provenance,
    provenance_entries,
    rederive_revised,
    reseat_revised,
    resolve_params,
)
from .validate import validate_plan

__all__ = [
    "AuthoredProducer", "Build", "ByoCoverageError", "CATEGORICAL", "ChartSpec",
    "CoversAOI",
    "Data", "DataDecl", "DataRefs", "DeclarativeError", "Derived", "Domain",
    "DrawGate", "EngineOps",
    "FacadeIncompleteError", "Fetch", "Forcing",
    "FormGate", "Gate", "GateRefusedError",
    "LeakScanTruncated", "LedgerRecord", "MeshPolicy", "ModifierIllegalError",
    "Param",
    "ParamNotResolved",
    "ParamOutOfRangeError", "ParamRef", "ParamRefLeakedError",
    "ParamValues", "Physics", "Plan",
    "PlanValidationError", "Producer", "RATE", "Ref", "ReferenceProducer",
    "RenderSourceMissingError", "RenderSpec", "ResampleSpec", "ResolvedParam",
    "ResolvedParams",
    "RunMode", "RunResult", "STAGES", "STATE", "Slot", "Step", "StepFailedError",
    "StepLedger",
    "TemporalGapError", "TemporalShapeError", "TemporalSpec",
    "TemporalUnitsError", "Transformed", "UndeclaredDataError", "UnitsSpec",
    "When", "WireArgsError",
    "Workflow", "convert_units", "current_domain", "doors", "interpret",
    "invocation_key",
    "merge_provenance", "provenance_entries", "rederive_revised",
    "register_workflow",
    "render_docstring", "reseat_revised", "resolve_params", "transform_series",
    "transform_value", "validate_plan",
]
