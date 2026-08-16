"""Turn wire plumbing -- the envelope construction + session-safe send
primitives the turn engine emits through.

The leaf transport layer every turn/gate/handler path uses to reach the wire:
build a typed ``Envelope`` (``_new_envelope``), send it across the captured
socket with fall-forward to any live sibling socket of the session
(``_session_safe_send``, the mid-turn-survives-a-dead-socket seam), and the
small family of raw-JSON terminal frames (``error`` / ``loop_exhausted`` /
``agent-abort`` / ``turn-complete`` / ``cache-status``) plus the connection
liveness ``heartbeat``. These are pure leaves: they reference only external
contracts, the sibling ``.protocol`` connection registry, and each other -- no
``SessionState`` behavior, no ``_core`` back-import -- so they extract as a unit
with no import cycle.

``_core`` re-imports every name here so its bare-global references (the handler,
the resume/auth/case emit paths, the turn driver) resolve unchanged; the package
facade re-exposes them at ``trid3nt_server.server.<name>`` and propagates
monkeypatch writes to this module (it is in ``_EXTRACTION_MODULES``).

Deliberately NOT here (stays in ``_core``, flagged for a future pass): the gate
coroutines (``_maybe_gate_on_payload_warning`` / ``_gate_on_code_exec`` /
``_gate_on_solver_confirm`` / ``_gate_with_turn_memory``) and the turn driver
(``_dispatch_model_turn_and_persist`` / ``_stream_model_reply``). The gates are
bound to the ``_gate_wait_timeout`` source-inspection seam that
``test_gate_timeout_local`` counts in ``_core`` AND that seam is SHARED with the
credential/region/spatial emit-wait gates that stay in ``_core`` -- so the gate
family is not cleanly separable. The driver's dense orchestration (it calls
~30 ``_core``-resident persist/emit/dispatch helpers) would force a
``_core`` <-> ``turn`` import cycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.ws import Envelope, ErrorPayload

from ..adapters.adapter import MAX_TURN_ITERATIONS
from ..emission.pipeline_emitter import current_turn_case
from .protocol import _SESSION_WS_CONNECTIONS

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from ..adapters.adapter import UsageMetadataEvent
    from .session import SessionState

logger = logging.getLogger("trid3nt_server.server")


def _new_envelope(message_type: str, session_id: str, payload: Any) -> str:
    """Construct + validate an Envelope and return its JSON wire form.

    Stamps ``case_id`` from the turn's ContextVar binding so the web routes
    live envelopes to the owning Case's stream. None outside a turn --
    lifecycle envelopes are untagged.
    """
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
    # F1 (2026-07-08): route through the session-aware safe send. An error
    # reply aimed at a just-dropped socket must reach the session's surviving
    # sibling socket when one exists, and must NEVER raise into the caller --
    # pre-fix, the turn-failure path's _send_error re-raised ConnectionClosed
    # and skipped the terminal-failure-card persist entirely.
    await _session_safe_send(
        websocket, session_id, _new_envelope("error", session_id, payload)
    )


# WS-30s STORM FIX (server data heartbeat): the browser ``WebSocket`` API
# handles server PROTOCOL-level PING control-frames transparently and NEVER
# surfaces them to ``onmessage``, so the server's ``ping_interval=20`` pings do
# NOT reset the client's inbound-frame timer (ws.ts ``noteInboundActivity``
# fires only on a DATA frame). Between turns the ONLY data frame the client sees
# is its own keepalive's ``session-state`` reply; if that reply is slow or stalls
# (a reconnect re-runs the active-case layer replay + vector densify), the
# client's pong deadline expires and it force-reconnects -> the reconnect re-runs
# the replay -> stalls again -> a self-sustaining ~30s reconnect storm in which
# the user's prompts never reach the turn handler.
#
# Fix: per WS connection, a background task sends a lightweight ``heartbeat`` DATA
# frame every ``HEARTBEAT_INTERVAL_SECONDS`` (well under the client's
# ~25s ping + 10s pong-timeout window) so the client's ``onmessage`` fires and
# its inbound-activity timer is reset on a cheap server clock that is independent
# of the (possibly-slow) resume reply. ws.ts already (a) calls
# ``noteInboundActivity()`` on EVERY inbound frame BEFORE any type parsing and
# (b) routes an unknown ``heartbeat`` type to a no-op ``default:`` (console.debug
# only) -- so NO web change is required for the client to tolerate + benefit from
# it. The interval is deliberately shorter than the client's 25s keepalive so a
# heartbeat lands inside every pong window even on a busy loop.
HEARTBEAT_INTERVAL_SECONDS: float = 12.0


async def _heartbeat_loop(
    websocket: "ServerConnection",
    session_id: str,
) -> None:
    """Send a lightweight ``heartbeat`` DATA frame every interval until cancelled.

    WS-30s STORM FIX (primary): the server PING control-frames never reach the
    browser ``onmessage`` handler, so they cannot keep the client's
    inbound-activity / pong-deadline timer alive. This per-connection task sends a
    tiny ``heartbeat`` envelope on a server clock (every
    ``HEARTBEAT_INTERVAL_SECONDS``) so the client always sees a fresh DATA frame
    well inside its pong window -- breaking the reconnect storm regardless of how
    slow the session-resume reply is.

    Built as a raw-JSON envelope (the same pattern ``_emit_turn_complete`` /
    ``_send_loop_exhausted`` use) so no schema-lane payload model is required; the
    payload carries only a server timestamp. NOT stamped with a Case tag -- it is
    a pure transport-liveness frame, never routed to a Case stream.

    Cancelled cleanly by the handler's ``finally`` on EVERY disconnect path. A
    per-send wire failure (the socket may be mid-close) is swallowed so a transient
    write error never tears down the loop early; a ``ConnectionClosed`` ends the
    ``async for``-driven handler which then cancels this task.
    """
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
            # Clean shutdown from the handler ``finally`` -- propagate so the
            # awaiting canceller observes completion (NATE: cancel cleanly).
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
    """Send ``message`` on the captured socket, falling back to any live
    socket of ``session_id``. Never raises; returns True when a send landed.
    """
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
) -> None:
    """Emit the distinct ``loop_exhausted`` envelope.

    Fires when the multi-turn loop hits ``MAX_TURN_ITERATIONS`` without a
    natural termination (no tool-call-free turn). Raw-JSON envelope typed
    ``"loop_exhausted"`` -- distinct from the generic ``"error"`` type -- so
    the UI can render "Agent ran out of steps" rather than a generic
    failure indicator.

    Wire shape:
        {
          "type": "loop_exhausted",
          "session_id": str,
          "payload": {
            "status": "loop_exhausted",
            "error_code": "MAX_ITERATIONS_REACHED",
            "message": "Agent reached max iteration limit (N) before completing the request.",
            "retryable": False
          }
        }

    The ``payload.error_code`` key is SCREAMING_SNAKE_CASE and lives in the
    ``loop_exhausted`` typed envelope, not the ``error`` envelope, so clients
    can distinguish "tool chain too long" from an upstream LLM failure.
    ``retryable=False`` because the agent already consumed all its turns;
    the user should rephrase or narrow scope.

    Best-effort: a wire failure is logged but not re-raised so the terminal
    agent-message-chunk can still fire.
    """
    import json as _json

    try:
        payload = {
            "status": "loop_exhausted",
            "error_code": "MAX_ITERATIONS_REACHED",
            "message": (
                f"Agent reached max iteration limit ({MAX_TURN_ITERATIONS}) "
                "before completing the request. "
                "Try rephrasing your request with a narrower scope."
            ),
            "retryable": False,
        }
        await _session_safe_send(
            websocket,
            session_id,
            _json.dumps(
                {
                    "type": "loop_exhausted",
                    "session_id": session_id,
                    "payload": payload,
                }
            ),
        )
        logger.info(
            "loop_exhausted envelope sent session=%s max_iter=%d",
            session_id,
            MAX_TURN_ITERATIONS,
        )
    except Exception:  # noqa: BLE001 -- observability; never break the reply path
        logger.exception(
            "loop_exhausted envelope send failed session=%s", session_id
        )


async def _send_agent_abort(
    websocket: "ServerConnection",
    session_id: str,
    reason_code: str,
    message: str,
) -> None:
    """Emit the runaway-agent abort envelope.

    Sent when a per-turn guard fires (step cap, wall-clock, or loop watchdog)
    to stop a runaway turn before it can wedge the shared box. Reuses the
    ``loop_exhausted`` typed wire envelope but carries the specific guard
    ``error_code`` and an honest message (honesty floor: state exactly why
    the turn stopped, never a fabricated success). Best-effort: a wire
    failure is logged, never re-raised, so the turn still terminates and
    releases busy.
    """
    import json as _json

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
        logger.warning(
            "agent-abort session=%s reason=%s", session_id, reason_code
        )
    except Exception:  # noqa: BLE001 -- observability; never break the reply path
        logger.exception(
            "agent-abort envelope send failed session=%s reason=%s",
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
    """Emit the end-of-turn ``turn-complete`` signal so the client
    force-completes any card still rendering ``running``.

    A tool/turn's terminal ``pipeline-state`` frame can be written onto a
    just-dropped socket and lost, leaving a card spinning after its tool
    actually finished. This idle marker is emitted at the end of every turn
    and re-emitted on session-resume so no card hangs past turn end.

    Wire shape (matches ``TurnCompletePayload`` exactly -- both fields
    optional, a bare ``{}`` is a valid whole-turn idle):
        {"type": "turn-complete", "session_id": ..., "case_id": <turn case>,
         "payload": {"envelope_type": "turn-complete",
                     "pipeline_id": <str|null>, "final_state": <str|null>}}

    Built as a raw-JSON envelope (not via ``_new_envelope``) because the
    typed ``Envelope.payload`` is a ``GraceModel`` with ``extra="forbid"``
    and this payload model is not yet in ``trid3nt_contracts``; same
    raw-JSON pattern ``_send_loop_exhausted`` uses. ``case_id`` is stamped
    from the turn's ContextVar tag so this fans out session-wide and routes
    by ``case_id`` to the owning Case's stream, exactly like ``solve-progress``.

    Best-effort: a wire failure (the socket may already be half-closed) is
    logged, never raised -- the persisted tool-card terminal state is the
    durable replay backstop, and session-resume re-emits this signal anyway.
    """
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


async def _emit_cache_status(
    websocket: "ServerConnection",
    state: "SessionState",
    usage: "UsageMetadataEvent",
) -> None:
    """Emit a ``cache-status`` envelope so the UI can render live cache hit rate.

    Forwarded once per model stream after the ``UsageMetadataEvent`` lands.
    Payload shape:

        {
            "cache_hit":     bool,
            "cached_tokens": int,
            "total_tokens":  int,
            "prompt_tokens": int | null,
            "candidates_tokens": int | null,
            "model_cache_ref": str | null   (the provider-side cache handle in use this turn),
        }

    Intentionally raw-JSON (no contract model): observability surface, not
    a wire-API contract. A wire-side failure is logged but never raised --
    cache-status reporting must not break the agent loop.
    """
    import json as _json

    try:
        payload = {
            "cache_hit": bool(usage.cache_hit),
            "cached_tokens": int(usage.cached_content_token_count or 0),
            "total_tokens": int(usage.total_token_count or 0),
            "prompt_tokens": usage.prompt_token_count,
            "candidates_tokens": usage.candidates_token_count,
            "model_cache_ref": state.model_cache_ref,
        }
        await _session_safe_send(websocket, state.session_id,
            _json.dumps(
                {
                    "type": "cache-status",
                    "session_id": state.session_id,
                    "payload": payload,
                }
            )
        )
    except Exception:  # noqa: BLE001 -- observability, never bubble up
        logger.exception(
            "cache-status emission failed session=%s", state.session_id
        )
