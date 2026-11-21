"""Declared workflows: a template is one body of INPUTS over one module.

A fill puts a value into every input; a run writes, solves and reads.
"""

from __future__ import annotations

from .data import (
    Producer,
    ToolWord,
    tool,
)
from .docstring import render_docstring
from .errors import (
    SuppliedCoverageError,
    SuppliedGeometryError,
    DeclarativeError,
    GateRefusedError,
    NamelessFailureError,
    DeclarationError,
    StepFailedError,
)
from .workflow import (
    RunResult,
    Workflow,
    register_workflow,
)
from .provenance import (
    merge_provenance,
    provenance_entries,
)

__all__ = [
    "DeclarativeError",
    "GateRefusedError",
    "DeclarationError", "Producer",
    "RunResult",
    "NamelessFailureError", "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "ToolWord",
    "Workflow",
    "merge_provenance", "provenance_entries",
    "register_workflow",
    "render_docstring",
    "tool",
]
