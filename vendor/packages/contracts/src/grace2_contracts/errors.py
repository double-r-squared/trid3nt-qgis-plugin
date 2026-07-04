"""Shared typed errors that cross specialist boundaries (job-0114-schema).

Errors that an atomic tool raises BEFORE issuing any network call —
input-shape problems the caller mis-formed — share a common typed model so
both the agent service (catching to format a chat response) and the web
client (rendering ``tool-call-failed`` envelopes) can branch on a closed
``code`` discriminator instead of string-parsing exception messages.

This module currently owns ``ToolInputError`` only (the FR-DC-6
fail-fast input-validation surface). Other typed-error families may be
added here in the future as they earn a cross-boundary need.

Convention:
- ``ToolInputError`` is a pydantic ``GraceModel`` — the shape, not the
  Python exception. Tools raise their own ``Exception`` subclass and
  populate a ``ToolInputError`` instance on it (or attach it to the
  ``tool-call-failed`` envelope) so the wire form is well-typed.
- Input errors are **never retryable** (``retryable: Literal[False]``).
  A bad bbox or missing required arg is the caller's bug; retrying won't
  fix it. Retry policy for network errors lives elsewhere.

Invariants this module touches:
- **Invariant 1 (Determinism boundary)** — error ``code`` is a closed
  ``Literal``, never LLM-judged free text. The agent narrator branches
  on the literal, not on the message.
- **Invariant 9 (No cost theater)** — no cost / retry-cost fields anywhere
  on the error model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import GraceModel

__all__ = [
    "ToolInputErrorCode",
    "TOOL_INPUT_ERROR_CODES",
    "ToolInputError",
]


#: Closed enum of ``ToolInputError`` codes. Members:
#:
#: - ``BBOX_REQUIRED`` — the tool was called with ``bbox=None`` but the
#:   tool's ``AtomicToolMetadata.supports_global_query`` is ``False``.
#:   The caller must supply a bounding box.
#: - ``INVALID_ARG`` — a required argument was missing, wrong type, or
#:   failed a tool-specific validator (e.g. out-of-range integer, unknown
#:   enum literal, malformed date string).
#: - ``BAD_FORMAT`` — the request was syntactically well-formed but
#:   semantically invalid given the tool's domain (e.g. a polygon with
#:   self-intersections, a time window with ``start >= end``).
ToolInputErrorCode = Literal["BBOX_REQUIRED", "INVALID_ARG", "BAD_FORMAT"]

#: Tuple form for parametrized tests + tool-side assertions.
TOOL_INPUT_ERROR_CODES: tuple[str, ...] = (
    "BBOX_REQUIRED",
    "INVALID_ARG",
    "BAD_FORMAT",
)


class ToolInputError(GraceModel):
    """Typed input-validation error raised by atomic tools BEFORE any I/O.

    Tools that detect a malformed input (missing bbox when not
    ``supports_global_query``, out-of-range arg, malformed polygon, etc.)
    populate this model and surface it on the failed tool-call envelope.
    The shape — not the exception itself — is what crosses specialist
    boundaries: the agent service catches the tool's Python exception and
    serializes a ``ToolInputError`` payload onto ``tool-call-failed``.

    Fields:

    - ``code`` — closed ``Literal`` discriminator
      (``BBOX_REQUIRED`` / ``INVALID_ARG`` / ``BAD_FORMAT``). Consumers
      branch on this; the human-readable ``message`` is for chat display.
    - ``message`` — human-readable description of what went wrong.
      Free-text; the agent narrator may paraphrase it but the closed
      ``code`` is the deterministic surface.
    - ``retryable`` — pinned to ``False``. Input errors never become
      not-errors on retry; this field exists so error-handling code
      written against the ``ToolInputError`` shape branches on the same
      ``retryable`` discriminator other error families use (network
      errors, rate limits) without a special-case.

    Notes on usage:

    - This is a pydantic ``GraceModel``, not a Python ``Exception``. Tools
      typically raise their own exception type (carrying a
      ``ToolInputError`` instance as an attribute) so call sites get both
      a Python-native traceback AND a wire-typed payload.
    - The cross-field rule is enforced at construction: ``retryable``
      defaults to ``False`` and the type system pins it there
      (``Literal[False]``), so a misconfigured caller writing
      ``retryable=True`` fails ``ValidationError`` at construction time.
    """

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
