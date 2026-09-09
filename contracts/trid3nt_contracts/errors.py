"""Typed errors that cross a package boundary.

The SHAPE, not the Python exception: a closed ``code`` discriminator so a
consumer branches on a literal instead of string-parsing a message. An input
error is never retryable, and no cost or retry-cost field lives on one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import GraceModel

__all__ = [
    "ToolInputErrorCode",
    "TOOL_INPUT_ERROR_CODES",
    "ActionabilityClass",
    "ACTIONABILITY_CLASSES",
    "ToolInputError",
]

#: Closed three-way discriminator: who can ACT on a typed error, distinct from
#: ``retryable`` (whether it can be retried at all).
#:
#: - ``"agent"`` -- upstream 4xx-arg / 429 / 5xx / timeout: the raw text goes
#:   back verbatim so the model self-corrects its args or narrates.
#: - ``"user"`` -- missing credential or auth config: a concise narration
#:   directive travels, never the raw exception text.
#: - ``"operator"`` -- a contract violation or internal exception: a terse
#:   acknowledgment reaches the model, full detail stays in the log, and the
#:   failure does not consume the per-tool retry budget.
ActionabilityClass = Literal["agent", "user", "operator"]

#: Tuple form of the same three values.
ACTIONABILITY_CLASSES: tuple[str, ...] = ("agent", "user", "operator")


#: Closed enum of ``ToolInputError`` codes:
#:
#: - ``BBOX_REQUIRED`` -- called with ``bbox=None`` against a tool that does
#:   not support a global query.
#: - ``INVALID_ARG`` -- a required argument missing, wrong type, or failing a
#:   tool-specific validator.
#: - ``BAD_FORMAT`` -- syntactically well-formed but semantically invalid for
#:   the tool's domain (a self-intersecting polygon, ``start >= end``).
ToolInputErrorCode = Literal["BBOX_REQUIRED", "INVALID_ARG", "BAD_FORMAT"]

#: Tuple form of the same three codes.
TOOL_INPUT_ERROR_CODES: tuple[str, ...] = (
    "BBOX_REQUIRED",
    "INVALID_ARG",
    "BAD_FORMAT",
)


class ToolInputError(GraceModel):
    """Typed input-validation error, produced BEFORE any I/O is attempted.
    A shape, not an exception: a tool raises its own exception carrying an
    instance of this, so the caller gets a traceback AND a wire-typed payload.
    """

    # The wire form is PINNED at these three keys - external consumers type
    # against it, so a new field is a breaking change. Server-side
    # actionability for this family is always "agent" and is computed at the
    # dispatch boundary rather than carried here.
    code: ToolInputErrorCode = Field(
        description="Closed-enum discriminator for the input-error class."
    )
    message: str = Field(
        min_length=1,
        description="Human-readable description of the malformed input.",
    )
    retryable: Literal[False] = Field(
        default=False,
        description=(
            "Pinned False: input errors are never retryable. "
            "Field exists so handler code can branch on `retryable` uniformly."
        ),
    )
