"""Typed error taxonomy for the WebSocket server dispatch path.

Each type carries an ``error_code`` and a ``retryable`` flag that the result
summarizer harvests, so a gate refusal reaches the model as a structured
function response and the turn completes honestly. A DECLINE is a separate
class from a failure - ``UserDeclinedError`` marks the user's own answer - and a
card nobody answered is neither: ``GateConfirmationTimeoutError``."""

from __future__ import annotations


class UserDeclinedError(RuntimeError):
    """The user answered a gate card with cancel, so the tool never ran.
    ``declined`` is the marker three seams read without importing this module:
    the pipeline emitter marks the step cancelled rather than failed, the result
    summarizer hands the model a declined result rather than an error, and the
    circuit breaker leaves the tool's retry budget alone. ``decline_note`` names
    the card and what it asked, in the words the model relays."""

    declined: bool = True
    retryable: bool = False

    def __init__(self, message: str, decline_note: str) -> None:
        super().__init__(message)
        self.decline_note = decline_note


class GateConfirmationTimeoutError(RuntimeError):
    """Raised when a gate card reached its deadline with nobody answering, which
    is not the user's decision: no ``declined`` marker, so the emitter fails the
    step rather than cancelling it and the summarizer hands the model an error.
    ``retryable=False``: a re-issue parks on another card nobody answers. The
    message IS the narration, and it never reads as a decline."""

    error_code: str = "CONFIRMATION_TIMEOUT"
    retryable: bool = False

    def __init__(self, card: str, subject: str, timeout_s: float) -> None:
        super().__init__(
            f"the {card} for {subject} went unanswered for {timeout_s:.0f}s, so "
            "the call did not run. Nobody responded before the deadline and the "
            "user made no choice either way. Tell them the card expired with no "
            "answer and ask whether to try again."
        )
        self.card = card
        self.subject = subject
        self.timeout_s = timeout_s


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


class PayloadWarningCancelledError(UserDeclinedError):
    """Raised when the user cancels the payload-warning card, so the gate skips
    dispatch; ``retryable=False``, so the model narrates the decision rather
    than re-issuing the same call at the same scope."""

    error_code: str = "PAYLOAD_WARNING_CANCELLED"

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"tool {tool_name!r} dispatch cancelled via payload-warning gate "
            "(the user chose 'cancel')",
            f"The user declined the payload-size warning card for {tool_name}, "
            "which asked whether to fetch a payload this large. The tool did "
            "not run.",
        )
        self.tool_name = tool_name


class CodeExecConfirmationCancelledError(UserDeclinedError):
    """Raised when the ``run_pyqgis`` confirm gate denies the run on the user's
    cancel; the gate fails closed and ``retryable=False``, since the user
    declined to run THIS code."""

    error_code: str = "CODE_EXEC_CANCELLED"

    def __init__(self, code_exec_id: str) -> None:
        super().__init__(
            f"run_pyqgis {code_exec_id!r} cancelled at the confirm gate "
            "(the user chose 'cancel'); the code did not run",
            "The user declined the code-approval card for run_pyqgis, which "
            "asked permission to run the proposed snippet in their QGIS "
            "session. The code did not run.",
        )
        self.code_exec_id = code_exec_id


class SolverConfirmationCancelledError(UserDeclinedError):
    """Raised when a solver confirm gate denies the dispatch on the user's own
    answer; a cancel and a narrow_scope the card never offered both fail closed,
    ``retryable=False``, so the model narrates the decline instead of
    re-dispatching the same run."""

    error_code: str = "SOLVER_CONFIRMATION_CANCELLED"

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"{tool_name} declined at the parameter-confirmation gate "
            "(the user chose 'cancel'); the solver did not run",
            f"The user declined the parameter-confirmation card for "
            f"{tool_name}, which asked whether to start the run with the "
            "parameters it showed. The run did not start.",
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
