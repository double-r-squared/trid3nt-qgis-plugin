"""Typed errors the declarative library raises. Every one carries an
``error_code`` the tool wrapper maps straight onto the error envelope."""

from __future__ import annotations

__all__ = [
    "DeclarativeError",
    "GateRefusedError",
    "LeakScanTruncated",
    "ModifierIllegalError",
    "ParamOutOfRangeError",
    "ParamRefLeakedError",
    "PlanValidationError",
    "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "WorkflowParkedError",
]


class DeclarativeError(RuntimeError):
    """Base for every declarative-library failure; carries a typed ``error_code``."""

    error_code = "DECLARATIVE_ERROR"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class PlanValidationError(DeclarativeError):
    error_code = "PLAN_INVALID"


class ModifierIllegalError(DeclarativeError):
    error_code = "MODIFIER_ILLEGAL"


class ParamOutOfRangeError(DeclarativeError):
    error_code = "PARAM_OUT_OF_RANGE"


class ParamRefLeakedError(DeclarativeError):
    """An unsubstituted ``ParamRef`` reached a persisted record or a returned result.
    Always a bug, never data: a ref that survives to disk means a declaration
    escaped binding."""

    error_code = "PARAM_REF_LEAKED"


class LeakScanTruncated(UserWarning):
    """The ParamRef leak scan hit its node budget, so a surface is only PART checked.
    Not an error, and never silent: the warning names the surfaces it could not
    finish, because a scan that stopped looking is not a scan that found nothing."""


class GateRefusedError(DeclarativeError):
    error_code = "GATE_INPUT_REQUIRED"


class SuppliedCoverageError(DeclarativeError):
    error_code = "SUPPLIED_COVERAGE_MISMATCH"


class SuppliedGeometryError(DeclarativeError):
    """A supplied artifact is not the SHAPE the slot it fills declares.

    Raised at the front door, before any reader is handed the artifact."""

    error_code = "SUPPLIED_GEOMETRY_MISMATCH"


class StepFailedError(DeclarativeError):
    """A declared step's runner raised. ``cause`` keeps the engine's own typed error."""

    error_code = "STEP_FAILED"

    def __init__(self, message: str, *, error_code: str | None = None,
                 step: str | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message, error_code=error_code)
        self.step = step
        self.cause = cause


class WorkflowParkedError(DeclarativeError):
    """A template that is DECLARED but off the model surface was invoked.
    Parking is a state the declaration carries: the plan still validates at import,
    the tool is simply never registered, and this refusal names the reason."""

    error_code = "TEMPLATE_PARKED"
