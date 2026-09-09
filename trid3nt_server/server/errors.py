"""Typed error taxonomy for the WebSocket server dispatch path.

Each type carries an ``error_code`` and a ``retryable`` flag that the result
summarizer harvests, so a gate refusal reaches the model as a structured
function response and the turn completes honestly."""

from __future__ import annotations


class ToolNotFoundError(RuntimeError):
    """Raised when a dispatched tool name is not registered; ``retryable=False``,
    because no retry reaches a registration that does not exist. ``valid_tools``
    carries the first 20 registered names as a correction hint."""

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
    """Raised when the payload-warning gate skips dispatch on a cancel or a
    timeout; ``retryable=False``, so the model narrates the cancellation rather
    than re-issuing the same call at the same scope."""

    error_code: str = "PAYLOAD_WARNING_CANCELLED"
    retryable: bool = False

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"tool {tool_name!r} dispatch cancelled via payload-warning gate "
            "(user chose 'cancel' or gate timed out)"
        )
        self.tool_name = tool_name


class CodeExecConfirmationCancelledError(RuntimeError):
    """Raised when the ``code_exec_request`` confirm gate denies the run on a
    cancel or a timeout; the gate fails closed and ``retryable=False``, since the
    user declined to run THIS code."""

    error_code: str = "CODE_EXEC_CANCELLED"
    retryable: bool = False

    def __init__(self, code_exec_id: str) -> None:
        super().__init__(
            f"code_exec_request {code_exec_id!r} cancelled at the confirm gate "
            "(user chose 'cancel' or gate timed out); the sandbox did not run"
        )
        self.code_exec_id = code_exec_id


class CodeExecApprovalTimeoutError(RuntimeError):
    """Raised when the ``code-exec-request`` approval card was never answered -
    nobody decided, unlike an explicit cancel. ``retryable=False``: a re-issue
    would park on another unanswered card."""

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
    """Raised when a solver confirm gate denies the dispatch; cancel, timeout and
    disconnect all fail closed and ``retryable=False``, so the model narrates the
    decline instead of re-dispatching the same run."""

    error_code: str = "SOLVER_CONFIRMATION_CANCELLED"
    retryable: bool = False

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"{tool_name} declined at the parameter-confirmation gate "
            "(user chose 'cancel' or the gate timed out); the solver did not run"
        )
        self.tool_name = tool_name


class SpatialInputInvalidResponseError(Exception):
    """A spatial-input-response arrived but failed structural validation, so the
    paused turn returns a typed error IN-BAND rather than a silent success or a
    turn hung until the read TTL drains."""

    def __init__(self, error_code: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
