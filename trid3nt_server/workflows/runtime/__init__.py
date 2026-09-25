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
    NamelessFailureError,
    ParamOutOfRangeError,
    ParamRefLeakedError,
    PlanValidationError,
    StepFailedError,
    WorkflowParkedError,
)
from .interpreter import PlanNode, RunResult, expand_plan, interpret
from .journal import cut_coverage, journal_note, run_coverage
from .ledger import LedgerRecord, StepLedger, invocation_key
from .levers import lever
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
    Continued,
    DataRef,
    ParamRef,
    Plan,
    Ref,
    RawKeywords,
    Row,
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
    Series,
    TemporalGapError,
    TemporalShapeError,
    TemporalUnitsError,
    convert_units,
)
from .resolver import (
    merge_provenance,
    provenance_entries,
    resolve_params,
)

__all__ = [
    "Accepts", "AcceptsDeclarationError",
    "CATEGORICAL", "ChartSpec",
    "CoversAOI",
    "Continued", "Data", "DataDecl", "DataRef", "DeclarativeError",
    "Derived", "Domain",
    "GateRefusedError",
    "LeakScanTruncated", "LedgerRecord", "ModifierIllegalError",
    "Param",
    "ParamNotResolved",
    "ParamOutOfRangeError", "ParamRef", "ParamRefLeakedError",
    "ParamValues", "Plan", "PlanNode",
    "PlanValidationError", "Producer", "RATE", "Ref",
    "ResolvedParam",
    "ResolvedParams",
    "RawKeywords", "Row", "RunMode", "RunResult", "Series",
    "NamelessFailureError", "STATE", "Step", "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "StepLedger",
    "TemporalGapError", "TemporalShapeError",
    "TemporalUnitsError", "ToolWord",
    "WireArgsError",
    "Workflow", "WorkflowParkedError", "convert_units",
    "current_domain",
    "body_rows", "data_rows", "doors",
    "expand_plan",
    "interpret",
    "invocation_key",
    "cut_coverage", "journal_note", "lever", "run_coverage",
    "merge_provenance", "param_rows", "provenance_entries",
    "register_workflow",
    "render_docstring", "resolve_params",
    "tool",
]
