"""Actionability classifier for tool-dispatch errors (observability/retention

batch item 3). ONE classifier both the circuit breaker (retry-budget
exemption) and ``summarize_tool_result`` (envelope routing) import -- no
duplicated heuristic.

Closed three-way discriminator over the EXISTING error taxonomy -- conservative
by design, no fetcher rewrites:

- ``"agent"``    -- upstream 4xx-arg/429/5xx/timeout: every typed
  tool exception (declares an ``error_code``), plus the untyped transient/
  arg-shape primitives ``adapter._classify_error`` already treats as an
  agent-visible retry signal (``TimeoutError`` / ``ConnectionError`` /
  ``OSError`` / ``ValueError`` / ``TypeError`` / ``KeyError`` /
  ``AttributeError``). Routing: UNCHANGED -- rich verbatim
  ``function_response``, the model retries with corrected args or narrates
  (current behavior).
- ``"user"``     -- missing-credential/auth-config: reuses the credential
  registry's EXISTING shape-detector (``is_credential_shaped_error``) so this
  bucket tracks the exact same signal that already pauses a turn for a
  ``credential-request`` card. Routing: the function_response carries a
  concise narration directive (what happened + what the user can do), not the
  raw exception text.
- ``"operator"`` -- a NARROW, explicit set of "this should never happen"
  internal-bug exception types (``AssertionError`` / ``NotImplementedError`` /
  ``pydantic.ValidationError`` on our own constructed objects) -- a true
  contract violation, distinct from a normal (even untyped) upstream/tool
  failure. Routing: a terse, honest acknowledgment reaches the model
  ("internal error, logged"); full detail stays in the log (the dispatch
  site's ``logger.exception``) and the telemetry ``error_code``.
  Operator-class failures must NOT consume the per-tool circuit-breaker's
  retry budget (``ToolCircuitBreaker.record_failure`` exempts them the same
  way it already exempts client/arg errors).

Conservative by construction: an UNTYPED, UNRECOGNIZED exception that is
NEITHER a credential signal NOR one of the explicit internal-bug types above
(e.g. a bare ``RuntimeError("dem fetch upstream 503")`` with no ``error_code``
attribute -- common in tests and a few hand-rolled fetchers) stays
``"agent"``, mirroring ``adapter._classify_error``'s OWN catch-all
("everything else -> retryable"). This is deliberate: the operator bucket
must never silently swallow a message today's suite/consumers expect to see
verbatim.

Priority order: an EXPLICIT class-level ``actionability`` attribute (the
contracts / router-errors / transport-errors surfaces this batch bakes it
onto) wins outright -- it is the tool's own, authoritative classification.
Only an exception with NO such attribute falls through to the credential /
typed-error-code / untyped-primitive / internal-bug heuristics below.
"""

from __future__ import annotations

from typing import Literal

Actionability = Literal["agent", "user", "operator"]

__all__ = ["Actionability", "classify_actionability"]

#: Untyped exceptions ``adapter._classify_error`` already treats as a
#: retryable / agent-visible signal (network-transient or model arg-shape).
#: Mirrored here so the SAME untyped exceptions read "agent", not "operator".
_AGENT_CLASS_UNTYPED: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)

#: The narrow, explicit "this should never happen" internal-bug family.
#: Deliberately small -- anything NOT in this set falls through to the
#: agent-visible catch-all (see module docstring).
_OPERATOR_CLASS_TYPES: tuple[type[BaseException], ...] = (
    AssertionError,
    NotImplementedError,
)


def classify_actionability(tool_name: str, error: BaseException) -> Actionability:
    """Classify a tool-dispatch exception into ``{"agent", "user", "operator"}``.

    Never raises -- an internal classification fault degrades to the safe
    default ``"agent"`` (unchanged current behavior) rather than blocking
    dispatch.
    """
    try:
        # 1. An explicit class-level actionability is authoritative.
        explicit = getattr(error, "actionability", None)
        if explicit in ("agent", "user", "operator"):
            return explicit  # type: ignore[return-value]

        # 2. Credential-shaped signal (any tool, typed or not) -> user. Reuses
        #    the EXISTING credential-request detector.
        from trid3nt_server.credentials.credential_registry import (
            is_credential_shaped_error,
        )

        if is_credential_shaped_error(tool_name, error):
            return "user"

        # 3. Any OTHER typed tool exception (declares its own
        #    error_code) -- the existing agent-visible retry surface.
        code_attr = getattr(error, "error_code", None)
        if isinstance(code_attr, str) and code_attr:
            return "agent"

        # 4. Untyped network / arg-shape primitives -- also agent-visible.
        if isinstance(error, _AGENT_CLASS_UNTYPED):
            return "agent"

        # 5. A narrow, explicit internal-bug family -- a true contract
        #    violation, never a normal tool/upstream failure shape.
        if isinstance(error, _OPERATOR_CLASS_TYPES):
            return "operator"
        try:
            import pydantic

            if isinstance(error, pydantic.ValidationError):
                return "operator"
        except Exception:  # noqa: BLE001 -- pydantic always present here, but defensive
            pass

        # 6. Everything else -- an untyped, unrecognized exception (e.g. a
        #    bare RuntimeError) -- mirrors adapter._classify_error's OWN
        #    catch-all: agent-visible/retryable, unchanged behavior.
        return "agent"
    except Exception:  # noqa: BLE001 -- classification must never itself fail
        return "agent"
