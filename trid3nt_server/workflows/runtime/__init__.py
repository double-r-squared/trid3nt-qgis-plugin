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
from .errors import (
    SuppliedCoverageError,
    SuppliedGeometryError,
    DeclarativeError,
    GateRefusedError,
    NamelessFailureError,
    ParamOutOfRangeError,
    DeclarationError,
    StepFailedError,
)
from .levers import lever
from .params import (
    Param,
    ResolvedParam,
    ResolvedParams,
    param_rows,
)
from .workflow import (
    RunResult,
    Workflow,
    register_workflow,
)
from .resolver import (
    merge_provenance,
    provenance_entries,
)

__all__ = [
    "Accepts", "AcceptsDeclarationError",
    "CoversAOI",
    "Data", "DataDecl", "DeclarativeError",
    "GateRefusedError",
    "Param",
    "ParamOutOfRangeError",
    "DeclarationError", "Producer",
    "ResolvedParam",
    "ResolvedParams",
    "RunResult",
    "NamelessFailureError", "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "ToolWord",
    "Workflow",
    "data_rows",
    "lever",
    "merge_provenance", "param_rows", "provenance_entries",
    "register_workflow",
    "render_docstring",
    "tool",
]
