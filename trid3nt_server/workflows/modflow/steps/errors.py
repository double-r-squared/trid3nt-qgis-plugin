"""The typed failures the shared MODFLOW archetype steps raise.

Each carries an ``error_code`` the interpreter copies onto ``StepFailedError``,
so the tool's error envelope is the engine's own code rather than a generic one.
"""

from __future__ import annotations

__all__ = [
    "ModflowAoiInputError",
    "ModflowArchetypeRunError",
    "ModflowPhysicsInputRequired",
    "ModflowStepError",
]


class ModflowStepError(RuntimeError):
    """Base for the shared MODFLOW step family."""

    error_code = "MODFLOW_STEP_ERROR"
    retryable = False

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code


class ModflowAoiInputError(ModflowStepError):
    """No usable AOI: neither a geocodable place nor an explicit point."""

    error_code = "MODFLOW_AOI_INPUT_INVALID"


class ModflowPhysicsInputRequired(ModflowStepError):
    """A physics-consequential aquifer property could not be resolved (law 9).

    Shares its code with the declarative library's gate refusal so a caller
    routes on the reason and not on which surface refused.
    """

    error_code = "PHYSICS_INPUT_REQUIRED"


class ModflowArchetypeRunError(ModflowStepError):
    """The archetype solve failed, or returned something that is not its layer."""

    error_code = "MODFLOW_ARCHETYPE_RUN_FAILED"
