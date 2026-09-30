"""Declared workflows: a template is PARAMS + DATA over one module.

A fill puts a value into every input; a run writes, solves and reads.
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
    NamelessFailureError,
    ParamOutOfRangeError,
    PlanValidationError,
    StepFailedError,
    WorkflowParkedError,
)
from .journal import cut_coverage, journal_note, run_coverage
from .levers import lever
from .params import (
    Param,
    ParamValues,
    ResolvedParam,
    ResolvedParams,
    doors,
    param_rows,
)
from .workflow import (
    RunResult,
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
    "CATEGORICAL",
    "CoversAOI",
    "Data", "DataDecl", "DeclarativeError",
    "Domain",
    "GateRefusedError",
    "Param",
    "ParamOutOfRangeError",
    "ParamValues",
    "PlanValidationError", "Producer", "RATE",
    "ResolvedParam",
    "ResolvedParams",
    "RunResult", "Series",
    "NamelessFailureError", "STATE", "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "TemporalGapError", "TemporalShapeError",
    "TemporalUnitsError", "ToolWord",
    "WireArgsError",
    "Workflow", "WorkflowParkedError", "convert_units",
    "current_domain",
    "data_rows", "doors",
    "cut_coverage", "journal_note", "lever", "run_coverage",
    "merge_provenance", "param_rows", "provenance_entries",
    "register_workflow",
    "render_docstring", "resolve_params",
    "tool",
]
