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
    GateNotSupportedError,
    GateRefusedError,
    ModifierIllegalError,
    ParamOutOfRangeError,
    PlanValidationError,
    StepFailedError,
)
from .interpret import RunResult, interpret
from .ledger import LedgerRecord, StepLedger, invocation_key
from .params import Param, ParamNotResolved, ResolvedParam, ResolvedParams, doors
from .plan import (
    ChartSpec,
    DrawGate,
    FormGate,
    Gate,
    Plan,
    Ref,
    RenderSpec,
    RunMode,
    Step,
    Transparent,
    When,
    Within,
    Workflow,
)
from .resolver import merge_provenance, provenance_entries, resolve_params
from .validate import validate_plan

__all__ = [
    "AuthoredProducer", "Build", "ByoCoverageError", "ChartSpec", "CoversAOI",
    "Data", "DataDecl", "DeclarativeError", "Domain", "DrawGate", "Fetch",
    "FormGate", "Gate", "GateNotSupportedError", "GateRefusedError",
    "LedgerRecord", "ModifierIllegalError", "Param", "ParamNotResolved",
    "ParamOutOfRangeError", "Plan", "PlanValidationError", "Producer", "Ref",
    "ReferenceProducer", "RenderSpec", "ResolvedParam", "ResolvedParams",
    "RunMode", "RunResult", "Step", "StepFailedError", "StepLedger",
    "Transparent", "When", "Within", "Workflow", "current_domain", "doors",
    "interpret", "invocation_key", "merge_provenance", "provenance_entries",
    "render_docstring", "resolve_params", "validate_plan",
]
