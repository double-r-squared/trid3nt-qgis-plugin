"""Compute / clip / extract / vector-edit / chart derive tools (flat).

``DeriveError`` is the one refusal shape they share: the code a caller branches
on, the sentence a reader acts on, and whether calling again could answer
differently.
"""

from __future__ import annotations

__all__ = ["DeriveError"]


class DeriveError(RuntimeError):
    """A derive's typed refusal: its error code, its message, its retryability."""

    error_code: str = ""
    retryable: bool = False

    def __init__(self, error_code: str, message: str, *,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
