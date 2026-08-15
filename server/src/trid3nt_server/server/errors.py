"""Typed error taxonomy for the WebSocket server dispatch path.

These are
pure exception types: each carries an ``error_code`` (a wire code / marker) and
a ``retryable`` flag that ``summarize_tool_result`` harvests so a gate refusal
reaches the model as a structured function_response and the turn completes
honestly. No session or module state -- moved verbatim (behavior-preserving),
docstrings swept to model-neutral wording per the comments-are-constraints norm.
"""

from __future__ import annotations


class ToolNotFoundError(RuntimeError):
    """Raised when ``_invoke_tool_via_emitter`` receives a tool name that is
    not registered in ``TOOL_REGISTRY``.

    ``retryable=False``: the model cannot retry its way to a registration it
    invented -- it must revise its call (use a different tool, narrate that
    it cannot help, or ask for clarification).

    The ``valid_tools`` attribute carries the first 20 registered names so
    the function-response payload gives the model a correction hint without
    blowing the response character budget.
    """

    error_code: str = "TOOL_NOT_FOUND"
    retryable: bool = False

    def __init__(self, tool_name: str, valid_tools: list[str]) -> None:
        # Limit to first 20 names to stay within _FUNCTION_RESPONSE_CHAR_BUDGET.
        hint = valid_tools[:20]
        super().__init__(
            f"tool {tool_name!r} not in TOOL_REGISTRY; "
            f"valid tools (first 20): {hint}"
        )
        self.tool_name = tool_name
        self.valid_tools = hint


class PayloadWarningCancelledError(RuntimeError):
    """Raised when the payload-warning gate skips dispatch because the user
    chose ``cancel`` or the gate timed out.

    ``retryable=False``: the user explicitly declined; the model should narrate
    the cancellation honestly and not re-issue the same call without narrower
    scope.
    """

    error_code: str = "PAYLOAD_WARNING_CANCELLED"
    retryable: bool = False

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"tool {tool_name!r} dispatch cancelled via payload-warning gate "
            "(user chose 'cancel' or gate timed out)"
        )
        self.tool_name = tool_name


class CodeExecConfirmationCancelledError(RuntimeError):
    """Raised when the ``code_exec_request`` confirm gate denies the run
    because the user chose ``cancel`` or the gate timed out.

    Running arbitrary Python is a consequential action; the gate fails closed.
    ``retryable=False``: the user explicitly declined to run THIS code -- the
    model should narrate the decline honestly and not re-issue the identical
    snippet without the user changing course.
    """

    error_code: str = "CODE_EXEC_CANCELLED"
    retryable: bool = False

    def __init__(self, code_exec_id: str) -> None:
        super().__init__(
            f"code_exec_request {code_exec_id!r} cancelled at the confirm gate "
            "(user chose 'cancel' or gate timed out); the sandbox did not run"
        )
        self.code_exec_id = code_exec_id


class CodeExecApprovalTimeoutError(RuntimeError):
    """Raised when the ``code-exec-request`` approval card was never answered.

    Distinct from :class:`CodeExecConfirmationCancelledError` (an explicit
    user decision): here NOBODY answered the card within the approval
    window -- e.g. a client with no handler for the envelope leaves the
    parked tool call waiting forever.

    ``retryable=False``: re-issuing the identical snippet would just park on
    another unanswered card; the model should narrate that the approval card was
    not answered and let the user decide how to proceed. ``summarize_tool_result``
    harvests ``error_code`` + ``retryable`` so this reaches the model as a
    structured function_response and the turn completes.
    """

    error_code: str = "CODE_EXEC_APPROVAL_TIMEOUT"
    retryable: bool = False

    def __init__(self, code_exec_id: str, timeout_s: float) -> None:
        super().__init__(
            f"code_exec_request {code_exec_id!r} approval card was not answered "
            f"within {timeout_s:.0f}s (no confirmation arrived from the user "
            "interface); the sandbox did not run. Tell the user their approval "
            "was required but never received, and do not re-issue the identical "
            "snippet unless they ask to retry."
        )
        self.code_exec_id = code_exec_id
        self.timeout_s = timeout_s


class SolverConfirmationCancelledError(RuntimeError):
    """Raised when a solver confirm gate denies the dispatch.

    A solver run is a consequence: the user must
    approve the derived forcing parameters before the model executes. Cancel,
    timeout, and disconnect all fail closed. ``retryable=False`` so the model
    narrates the decline honestly instead of re-dispatching the same run.
    """

    error_code: str = "SOLVER_CONFIRMATION_CANCELLED"
    retryable: bool = False

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"{tool_name} declined at the parameter-confirmation gate "
            "(user chose 'cancel' or the gate timed out); the solver did not run"
        )
        self.tool_name = tool_name


class SpatialInputInvalidResponseError(Exception):
    """A spatial-input-response arrived but failed structural validation.

    Carries the typed error the paused ``request_spatial_input`` turn surfaces
    to the model (honesty floor: a malformed reply degrades to a typed error, NOT
    a silent success and NOT a hung turn that drains the read TTL). Raised into
    the pending future by ``_fail_pending_spatial_input`` so the awaiting
    dispatch coroutine returns IN-BAND immediately instead of blocking until
    ``default_timeout_seconds`` then degrading to ``SPATIAL_INPUT_TIMEOUT``
    (the untagged-barrier mismatch).
    """

    def __init__(self, error_code: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
