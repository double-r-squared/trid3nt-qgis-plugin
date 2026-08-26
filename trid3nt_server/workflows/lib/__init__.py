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
    SuppliedCoverageError,
    SuppliedGeometryError,
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
from .interpreter import PlanNode, RunResult, expand_plan, interpret
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
    D,
    P,
    STAGES,
    ChartSpec,
    DataRef,
    DrawGate,
    FormGate,
    Gate,
    ParamRef,
    Plan,
    Ref,
    StyleSpec,
    RunMode,
    Step,
    When,
)
from .slots import Forcing, MeshPolicy, Physics, Slot, deep_freeze
from .workflow import (
    EngineOps,
    FacadeIncompleteError,
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
from .snapshot import Derivation, RunSnapshot, read_snapshot
from . import user_input
from .validate import validate_plan
from .validity import CoupledValidityError, Validity, check_validity

__all__ = [
    "AuthoredProducer", "Build", "CATEGORICAL", "ChartSpec",
    "CoupledValidityError", "CoversAOI",
    "D", "Data", "DataDecl", "DataRef", "DeclarativeError", "Derivation",
    "Derived", "Domain",
    "DrawGate", "EngineOps",
    "FacadeIncompleteError", "Fetch", "Forcing",
    "FormGate", "Gate", "GateRefusedError",
    "LeakScanTruncated", "LedgerRecord", "MeshPolicy", "ModifierIllegalError",
    "P", "Param",
    "ParamNotResolved",
    "ParamOutOfRangeError", "ParamRef", "ParamRefLeakedError",
    "ParamValues", "Physics", "Plan", "PlanNode",
    "PlanValidationError", "Producer", "RATE", "Ref", "ReferenceProducer",
    "RenderSourceMissingError", "ResampleSpec", "ResolvedParam",
    "ResolvedParams",
    "RunMode", "RunResult", "RunSnapshot",
    "STAGES", "STATE", "Slot", "Step", "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "StyleSpec",
    "StepLedger",
    "TemporalGapError", "TemporalShapeError", "TemporalSpec",
    "TemporalUnitsError", "Transformed", "UnitsSpec",
    "Validity", "When", "WireArgsError",
    "Workflow", "check_validity", "convert_units", "current_domain",
    "deep_freeze", "doors",
    "expand_plan",
    "interpret",
    "invocation_key",
    "merge_provenance", "provenance_entries", "read_snapshot",
    "rederive_revised",
    "register_workflow",
    "render_docstring", "reseat_revised", "resolve_params", "transform_series",
    "transform_value", "user_input", "validate_plan",
]
