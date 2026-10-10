"""Turn wire plumbing: envelope construction and the session-safe send.

Every turn, gate and handler path reaches the wire through here: a typed
envelope, a send that falls forward to a live sibling socket, the raw-JSON
terminal frames, and the connection-liveness heartbeat."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.ws import Envelope, ErrorPayload

from trid3nt_server.model.adapters.adapter import MAX_TURN_ITERATIONS
from trid3nt_server.render.pipeline_emitter import current_turn_case
from trid3nt_server.server.protocol.connections import _SESSION_WS_CONNECTIONS

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from trid3nt_server.server.session.state import SessionState

logger = logging.getLogger("trid3nt_server.server")


def _new_envelope(message_type: str, session_id: str, payload: Any) -> str:
    """Construct and validate an Envelope and return its JSON wire form; the
    ``case_id`` is stamped from the turn's binding so a live envelope routes to
    its owning Case, and is None outside a turn."""
    env = Envelope(
        type=message_type,
        session_id=session_id,
        case_id=current_turn_case(),
        payload=payload,
    )
    return env.model_dump_json()


async def _send_error(
    websocket: "ServerConnection",
    session_id: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> None:
    payload = ErrorPayload(error_code=code, message=message, retryable=retryable)
    # Route through the session-aware safe send: an error aimed at a just-dropped socket must reach
    # a surviving sibling when one exists, and must NEVER raise into the caller, or the turn-failure
    # path skips its terminal-card persist.
    await _session_safe_send(
        websocket, session_id, _new_envelope("error", session_id, payload)
    )


# A protocol-level PING is handled transparently by a client's socket and never surfaces as a
# message, so the server's pings do NOT reset a client's inbound-frame timer; a stalled reply to
# its own keepalive then force-reconnects it (a reconnect storm). The DATA-frame heartbeat interval
# stays far inside a client's ping-plus-pong window.
HEARTBEAT_INTERVAL_SECONDS: float = 12.0


async def _heartbeat_loop(
    websocket: "ServerConnection",
    session_id: str,
) -> None:
    """Send a lightweight ``heartbeat`` DATA frame every interval until
    cancelled; it carries only a timestamp and is never Case-tagged, being a
    pure transport-liveness frame."""
    # A per-send failure on a half-closed socket is swallowed, so one transient
    # write error never tears the loop down early; the handler ends on the real
    # close and cancels this task.
    import asyncio
    import json as _json

    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            await websocket.send(
                _json.dumps(
                    {
                        "type": "heartbeat",
                        "id": new_ulid(),
                        "ts": now_utc().isoformat().replace("+00:00", "Z"),
                        "session_id": session_id,
                        "case_id": None,
                        "payload": {
                            "ts": now_utc().isoformat().replace("+00:00", "Z"),
                        },
                    }
                )
            )
        except asyncio.CancelledError:
            # Clean shutdown from the handler's finally: propagate, so the
            # awaiting canceller observes completion.
            raise
        except Exception:  # noqa: BLE001 -- transport liveness; never tear down
            # A half-closed socket send fails; the handler loop will end on the
            # real ConnectionClosed and cancel this task. Swallow + keep ticking
            # so a single transient write hiccup does not kill the heartbeat.
            logger.debug(
                "heartbeat send failed session=%s", session_id, exc_info=True
            )


# Mid-turn sends must survive a dead captured socket: fall back across any
# other live socket for the session rather than aborting the turn.


async def _session_safe_send(
    websocket: "ServerConnection | None",
    session_id: str,
    message: str,
) -> bool:
    """Send ``message`` on the captured socket, falling back to any live socket
    of ``session_id``; never raises, and returns True when a send landed."""
    if websocket is not None:
        try:
            await websocket.send(message)
            return True
        except Exception:  # noqa: BLE001 -- captured socket may be dead
            pass
    for conn in list(_SESSION_WS_CONNECTIONS.get(session_id, ())):
        if conn is websocket:
            continue
        try:
            await conn.send(message)
            return True
        except Exception:  # noqa: BLE001 -- sibling may be mid-close too
            continue
    logger.debug(
        "session-safe-send: no live socket for session=%s (frame dropped; "
        "persisted rows remain the replay backstop)",
        session_id,
    )
    return False


async def _send_loop_exhausted(
    websocket: "ServerConnection",
    session_id: str,
    reason_code: str = "MAX_ITERATIONS_REACHED",
    message: str | None = None,
) -> None:
    """Emit the ``loop_exhausted`` envelope: the iteration cap by default, or a per-turn guard's
    own code and message."""
    # ``retryable=False``: the agent already consumed its turns, so the user
    # rephrases or narrows scope. Best-effort - a wire failure is logged, never
    # raised, so the terminal chunk still fires.
    import json as _json

    if message is None:
        message = (
            f"Agent reached max iteration limit ({MAX_TURN_ITERATIONS}) "
            "before completing the request. "
            "Try rephrasing your request with a narrower scope."
        )
    try:
        await _session_safe_send(
            websocket,
            session_id,
            _json.dumps(
                {
                    "type": "loop_exhausted",
                    "session_id": session_id,
                    "payload": {
                        "status": "loop_exhausted",
                        "error_code": reason_code,
                        "message": message,
                        "retryable": False,
                    },
                }
            ),
        )
        logger.info(
            "loop_exhausted envelope sent session=%s reason=%s",
            session_id,
            reason_code,
        )
    except Exception:  # noqa: BLE001 -- observability; never break the reply path
        logger.exception(
            "loop_exhausted envelope send failed session=%s reason=%s",
            session_id,
            reason_code,
        )


async def _emit_turn_complete(
    websocket: "ServerConnection",
    state: "SessionState",
    *,
    pipeline_id: str | None = None,
    final_state: str | None = None,
) -> None:
    """Emit the end-of-turn idle signal, so a client force-completes any card still rendering as
    running."""
    # A terminal pipeline frame can be written onto a just-dropped socket and lost, leaving a card
    # spinning after its tool finished. Raw JSON, because the typed envelope forbids extra fields
    # and this payload has no contract model; the Case tag routes it to the owning Case's stream.
    import json as _json

    try:
        env = {
            "type": "turn-complete",
            "id": new_ulid(),
            "ts": now_utc().isoformat().replace("+00:00", "Z"),
            "session_id": state.session_id,
            "case_id": current_turn_case(),
            "payload": {
                "envelope_type": "turn-complete",
                "pipeline_id": pipeline_id,
                "final_state": final_state,
            },
        }
        await websocket.send(_json.dumps(env))
        logger.debug(
            "turn-complete emitted session=%s case=%s pipeline=%s final=%s",
            state.session_id,
            env["case_id"],
            pipeline_id,
            final_state,
        )
    except Exception:  # noqa: BLE001 -- idle signal; never break the reply path
        logger.debug(
            "turn-complete emit failed session=%s", state.session_id,
            exc_info=True,
        )

