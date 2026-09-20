"""CALIBRATION: the subsystem the OBSERVE slot reveals.

Like fetching, meshing and restyling, calibration is a subsystem whose surface
appears only when it can operate: a run whose observe slot the match filled - a
gauge record within reach and window, high-water marks over the domain - can be
calibrated, and only then is there anything to offer. What lives here is the
pairing the observe slot coerces its record through and the skill metrics over
those pairs; nothing here owns a loop's mathematics, and nothing here is a tool.
"""

from __future__ import annotations

__all__ = ["CalibrationError"]


class CalibrationError(RuntimeError):
    """A calibration refusal: its error code, its message, its retryability."""

    error_code: str = ""
    retryable: bool = False

    def __init__(self, error_code: str, message: str, *,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
