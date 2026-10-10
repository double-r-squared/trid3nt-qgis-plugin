"""The session request seam: a tool that runs in the user's QGIS session emits a
``processing-request`` on the plugin wire and waits for the ``processing-response``. The wait is
bounded and owner-checked like every other card gate. No live session, a wait that runs out, and
a response carrying an error are each a typed refusal the model narrates; nothing invents a result."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from trid3nt_contracts import new_ulid
from trid3nt_contracts.processing_contracts import ProcessingRequestPayload

from trid3nt_server.inputs.gate.pending import _PENDING_PROCESSING
from trid3nt_server.server.config import _env_float

if TYPE_CHECKING:
    from trid3nt_contracts.processing_contracts import ProcessingResponsePayload

logger = logging.getLogger("trid3nt_server.server.processing")

__all__ = [
    "SessionProcessingError",
    "SessionUnavailableError",
    "SessionProcessingTimeoutError",
    "SessionProcessingFailedError",
    "run_in_session",
]

def _timeout_s() -> float:
    """Wall clock a session run may take before the call resolves as a timeout
    (``TRID3NT_SESSION_PROCESSING_TIMEOUT_S``, default 600): an algorithm over a
    large raster runs for minutes, and the cap keeps a turn from hanging."""
    return _env_float("TRID3NT_SESSION_PROCESSING_TIMEOUT_S", 600.0)


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
    _PENDING_PROCESSING.register(emitter.session_id, request_id, fut)
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
        _PENDING_PROCESSING.pop(request_id, None)
    if response.status != "ok":
        raise SessionProcessingFailedError(
            response.error or f"the QGIS session reported an error on {kind} "
            f"request {request_id!r} without a message"
        )
    return response
