"""Declarative workflows: a workflow is PARAMS + DATA + a pure ``plan(p, d)``.

The plan is a value; the interpreter walks it.
"""

from __future__ import annotations

from .accepts import Accepts, AcceptsDeclarationError
from .data import (
    CoversAOI,
    Data,
    DataDecl,
    Producer,
    ToolWord,
    data_rows,
    tool,
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
    StepFailedError,
    WorkflowParkedError,
)
from .interpreter import PlanNode, RunResult, expand_plan, interpret
from .journal import journal_note
from .ledger import LedgerRecord, StepLedger, invocation_key
from .params import (
    Derived,
    Param,
    ParamNotResolved,
    ParamValues,
    ResolvedParam,
    ResolvedParams,
    doors,
    param_rows,
)
from .plan import (
    ChartSpec,
    DataRef,
    ParamRef,
    Plan,
    Ref,
    Row,
    RawKeywords,
    RunMode,
    Step,
    body_rows,
)
from .workflow import (
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
from .validate import validate_plan
from .validity import CoupledValidityError, Validity, check_validity

__all__ = [
    "Accepts", "AcceptsDeclarationError",
    "CATEGORICAL", "ChartSpec",
    "CoupledValidityError", "CoversAOI",
    "Data", "DataDecl", "DataRef", "DeclarativeError", "Derivation",
    "Derived", "Domain",
    "GateRefusedError",
    "LeakScanTruncated", "LedgerRecord", "ModifierIllegalError",
    "Param",
    "ParamNotResolved",
    "ParamOutOfRangeError", "ParamRef", "ParamRefLeakedError",
    "ParamValues", "Plan", "PlanNode",
    "PlanValidationError", "Producer", "RATE", "Ref",
    "ResampleSpec", "ResolvedParam",
    "ResolvedParams",
    "RawKeywords", "Row", "RunMode", "RunResult", "RunSnapshot",
    "STATE", "Step", "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "StepLedger",
    "TemporalGapError", "TemporalShapeError", "TemporalSpec",
    "TemporalUnitsError", "ToolWord", "Transformed", "UnitsSpec",
    "Validity", "WireArgsError",
    "Workflow", "WorkflowParkedError", "check_validity", "convert_units",
    "current_domain",
    "body_rows", "data_rows", "doors",
    "expand_plan",
    "interpret",
    "invocation_key",
    "journal_note",
    "merge_provenance", "param_rows", "provenance_entries",
    "read_snapshot",
    "rederive_revised",
    "register_workflow",
    "render_docstring", "reseat_revised", "resolve_params", "transform_series",
    "tool", "transform_value", "validate_plan",
]
