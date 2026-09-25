"""Typed errors the declarative library raises. Every one carries an
``error_code`` the tool wrapper maps straight onto the error envelope."""

from __future__ import annotations

from trid3nt_server.errors import DeclarativeError

__all__ = [
    "ContinuationRefused",
    "CoresUnavailable",
    "DeclarativeError",
    "GateRefusedError",
    "LeakScanTruncated",
    "ModifierIllegalError",
    "NamelessFailureError",
    "ParamOutOfRangeError",
    "ParamRefLeakedError",
    "PlanValidationError",
    "said",
    "StepFailedError",
    "SuppliedCoverageError",
    "SuppliedGeometryError",
    "WorkflowParkedError",
]


class PlanValidationError(DeclarativeError):
    error_code = "PLAN_INVALID"


class ContinuationRefused(DeclarativeError):
    """``continue_from`` names a run with no solved result."""

    error_code = "CONTINUATION_UNSOLVED"


class CoresUnavailable(DeclarativeError):
    """A run asked to be partitioned across more cores than the box has.

    Refused by name, never clamped: a count cut down to fit is a partition the
    caller was never told about."""

    error_code = "CORES_UNAVAILABLE"


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


def said(exc: BaseException) -> str:
    """What a raised exception SAYS, or its type where it says nothing.

    An exception raised with no arguments stringifies to nothing, and a failure
    whose sentence is empty is a run that stopped in silence."""
    return str(exc).strip() or f"{type(exc).__name__} (raised saying nothing)"


class NamelessFailureError(DeclarativeError):
    """A step failure was raised carrying no sentence saying what stopped it.

    A run that stops in silence leaves the packet with a red step and nothing to
    read off it, so the nameless refusal is itself refused here."""

    error_code = "FAILURE_UNNAMED"


class StepFailedError(DeclarativeError):
    """A declared step's runner raised. ``cause`` keeps the engine's own typed error."""

    error_code = "STEP_FAILED"

    def __init__(self, message: str, *, error_code: str | None = None,
                 step: str | None = None, cause: BaseException | None = None) -> None:
        # The sentence is what the packet says stopped the run; a failure that
        # states none cannot be recorded at all.
        if not str(message or "").strip():
            raise NamelessFailureError(
                f"a step failure was raised for {step!r} carrying no sentence; "
                "every failure names WHY it stopped - a typed code and a "
                "sentence - and one caused by an upstream service says so.")
        super().__init__(message, error_code=error_code)
        self.step = step
        self.cause = cause


class WorkflowParkedError(DeclarativeError):
    """A template that is DECLARED but off the model surface was invoked.
    Parking is a state the declaration carries: the plan still validates at import,
    the tool is simply never registered, and this refusal names the reason."""

    error_code = "TEMPLATE_PARKED"
