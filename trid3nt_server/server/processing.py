"""The session request seam: a tool that runs in the user's QGIS session emits a
``processing-request`` on the plugin wire and waits for the ``processing-response``.

The wait is bounded and owner-checked like every other card gate. No live
session, a wait that runs out, and a response carrying an error are each a
typed refusal the model narrates; nothing here invents a result.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from trid3nt_contracts import new_ulid
from trid3nt_contracts.processing_contracts import ProcessingRequestPayload

if TYPE_CHECKING:
    from trid3nt_contracts.processing_contracts import ProcessingResponsePayload

logger = logging.getLogger("trid3nt_server.server.processing")

__all__ = [
    "SessionProcessingError",
    "SessionUnavailableError",
    "SessionProcessingTimeoutError",
    "SessionProcessingFailedError",
    "run_in_session",
    "_register_pending_processing",
    "_pop_pending_processing",
    "_resolve_pending_processing",
]

#: Wall clock a session run may take before the tool call resolves as a timeout.
#: An algorithm over a large raster runs for minutes; the cap keeps a turn from
#: hanging on a session that never answers.
_DEFAULT_TIMEOUT_S = 600.0


def _timeout_s() -> float:
    raw = os.environ.get("TRID3NT_SESSION_PROCESSING_TIMEOUT_S")
    try:
        value = float(raw) if raw is not None else _DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S
    return value if value > 0 else _DEFAULT_TIMEOUT_S


class SessionProcessingError(RuntimeError):
    """A session request that produced no result; the subclass says why."""

    error_code: str = "SESSION_PROCESSING_ERROR"
    retryable: bool = False


class SessionUnavailableError(SessionProcessingError):
    """No live QGIS session is bound to this turn, so nothing can run the request."""

    error_code = "SESSION_UNAVAILABLE"


class SessionProcessingTimeoutError(SessionProcessingError):
    """The session never answered within the window."""

    error_code = "SESSION_PROCESSING_TIMEOUT"


class SessionProcessingFailedError(SessionProcessingError):
    """The session ran the request and reported an error, carried verbatim."""

    error_code = "SESSION_PROCESSING_FAILED"


# request_id -> (owner_session_id, future). The response may arrive on a sibling
# connection of the same session; a cross-session response is refused.
_PENDING_PROCESSING: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_processing(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_PROCESSING[request_id] = (session_id, fut)


def _pop_pending_processing(request_id: str) -> None:
    _PENDING_PROCESSING.pop(request_id, None)


def _resolve_pending_processing(
    session_id: str, response: "ProcessingResponsePayload"
) -> bool:
    """Complete the pending future for ``response.request_id``; False for an
    unknown, already-resolved or cross-session request."""
    entry = _PENDING_PROCESSING.get(response.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "processing-response REFUSED: session=%s is not the owner (owner=%s) "
            "for request_id=%s", session_id, owner_session, response.request_id,
        )
        return False
    if fut.done():
        _PENDING_PROCESSING.pop(response.request_id, None)
        return False
    fut.set_result(response)
    _PENDING_PROCESSING.pop(response.request_id, None)
    return True


async def run_in_session(
    *,
    kind: str,
    algorithm: str | None = None,
    params: dict | None = None,
    code: str | None = None,
    code_exec_id: str | None = None,
) -> "ProcessingResponsePayload":
    """Emit one ``processing-request`` on the turn's session and WAIT for its
    response; an ``error`` response is raised as the session's own message."""
    from trid3nt_server.render.pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None:
        raise SessionUnavailableError(
            "no live QGIS session is bound to this turn; a session tool runs only "
            "through the plugin"
        )
    request_id = new_ulid()
    payload = ProcessingRequestPayload(
        request_id=request_id, kind=kind, algorithm=algorithm,
        params=params or {}, code=code, code_exec_id=code_exec_id,
    )
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_processing(emitter.session_id, request_id, fut)
    timeout_s = _timeout_s()
    try:
        await emitter.send_envelope("processing-request", payload)
        logger.info("processing-request emitted session=%s request_id=%s kind=%s "
                    "algorithm=%s", emitter.session_id, request_id, kind, algorithm)
        response = await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        raise SessionProcessingTimeoutError(
            f"the QGIS session did not answer {kind} request {request_id!r} within "
            f"{timeout_s:.0f}s; nothing ran to completion"
        ) from None
    finally:
        _pop_pending_processing(request_id)
    if response.status != "ok":
        raise SessionProcessingFailedError(
            response.error or f"the QGIS session reported an error on {kind} "
            f"request {request_id!r} without a message"
        )
    return response
