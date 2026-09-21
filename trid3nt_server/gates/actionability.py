"""One three-way classifier for tool-dispatch errors: agent, user, operator.

An explicit class-level ``actionability`` attribute wins outright; an
unrecognized untyped exception stays ``"agent"``, never the operator bucket.
"""

from __future__ import annotations

from typing import Literal

Actionability = Literal["agent", "user", "operator"]

__all__ = ["Actionability", "classify_actionability"]

#: Untyped exceptions that are a retryable / agent-visible signal
#: (network-transient or model arg-shape), and so read "agent", not "operator".
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
#: Anything NOT in this set falls through to the agent-visible catch-all.
_OPERATOR_CLASS_TYPES: tuple[type[BaseException], ...] = (
    AssertionError,
    NotImplementedError,
)


def classify_actionability(tool_name: str, error: BaseException) -> Actionability:
    """Classify a tool-dispatch exception into ``{"agent", "user", "operator"}``.

    Never raises: a classification fault degrades to ``"agent"``, never blocking
    dispatch."""
    try:
        # 1. An explicit class-level actionability is authoritative.
        explicit = getattr(error, "actionability", None)
        if explicit in ("agent", "user", "operator"):
            return explicit  # type: ignore[return-value]

        # 2. Any OTHER typed tool exception (declares its own
        #    error_code) -- the agent-visible retry surface. A credential
        #    failure reaches the user bucket through step 1: the refusal and
        #    the transport's auth error both DECLARE themselves user-actionable,
        #    so nothing here has to guess a missing key from message text.
        code_attr = getattr(error, "error_code", None)
        if isinstance(code_attr, str) and code_attr:
            return "agent"

        # 3. Untyped network / arg-shape primitives -- also agent-visible.
        if isinstance(error, _AGENT_CLASS_UNTYPED):
            return "agent"

        # 4. A narrow, explicit internal-bug family -- a true contract
        #    violation, never a normal tool/upstream failure shape.
        if isinstance(error, _OPERATOR_CLASS_TYPES):
            return "operator"
        try:
            import pydantic

            if isinstance(error, pydantic.ValidationError):
                return "operator"
        except Exception:  # noqa: BLE001 -- pydantic always present here, but defensive
            pass

        # 5. Everything else -- an untyped, unrecognized exception (e.g. a
        #    bare RuntimeError) -- is agent-visible/retryable: the operator
        #    bucket must never silently swallow a message a caller expects
        #    to read verbatim.
        return "agent"
    except Exception:  # noqa: BLE001 -- classification must never itself fail
        return "agent"
