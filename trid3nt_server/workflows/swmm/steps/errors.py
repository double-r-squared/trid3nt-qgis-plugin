"""The typed failures the shared SWMM step family raises.

Each carries an ``error_code`` the interpreter copies onto ``StepFailedError``,
so the tool's error envelope is the engine's own code rather than a generic one.
"""

from __future__ import annotations

__all__ = ["SwmmDeckError", "SwmmPhysicsInputRequired", "SwmmSolveError",
           "SwmmStepError"]


class SwmmStepError(RuntimeError):
    """Base for the shared SWMM step family."""

    error_code = "SWMM_STEP_ERROR"
    retryable = False

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class SwmmPhysicsInputRequired(SwmmStepError):
    """A physics-consequential soil/aquifer property could not be resolved (law 9)."""

    error_code = "SWMM_PHYSICS_INPUT_REQUIRED"


class SwmmDeckError(SwmmStepError):
    """The deck could not be authored from the declared values."""

    error_code = "SWMM_DECK_INVALID"


class SwmmSolveError(SwmmStepError):
    """The headless SWMM 5 engine failed on a deck this template authored."""

    error_code = "SWMM_SOLVE_FAILED"
