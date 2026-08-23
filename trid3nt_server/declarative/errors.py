"""Typed errors the declarative library raises. Every one carries an
``error_code`` the tool wrapper maps straight onto the error envelope."""

from __future__ import annotations

__all__ = [
    "ByoCoverageError",
    "DeclarativeError",
    "GateNotSupportedError",
    "GateRefusedError",
    "ModifierIllegalError",
    "ParamOutOfRangeError",
    "PlanValidationError",
    "StepFailedError",
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


class GateRefusedError(DeclarativeError):
    error_code = "GATE_INPUT_REQUIRED"


class GateNotSupportedError(DeclarativeError):
    error_code = "GATE_NOT_YET_SUPPORTED"


class ByoCoverageError(DeclarativeError):
    error_code = "BYO_COVERAGE_MISMATCH"


class StepFailedError(DeclarativeError):
    """A declared step's runner raised. ``cause`` keeps the engine's own typed error."""

    error_code = "STEP_FAILED"

    def __init__(self, message: str, *, error_code: str | None = None,
                 step: str | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message, error_code=error_code)
        self.step = step
        self.cause = cause
