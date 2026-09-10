"""Session-connection registry: the per-session live-socket set and the
session-supersede reap.

Self-contained - it reads only the module-local registry - so a caller holds no
other connection state."""

from __future__ import annotations

import logging

from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

SESSION_SUPERSEDED_CLOSE_CODE = 4408

_SESSION_WS_CONNECTIONS: "dict[str, set[ServerConnection]]" = {}


def _register_session_connection(
    session_id: str, websocket: "ServerConnection"
) -> None:
    """Record ``websocket`` as a live connection of ``session_id``; set
    semantics make a re-register a no-op."""
    if not session_id:
        return
    _SESSION_WS_CONNECTIONS.setdefault(session_id, set()).add(websocket)


def _deregister_session_connection(
    session_id: str, websocket: "ServerConnection"
) -> None:
    """Drop ``websocket`` from ``session_id``'s live-connection set; an emptied
    bucket is pruned so the registry cannot grow unbounded."""
    if not session_id:
        return
    bucket = _SESSION_WS_CONNECTIONS.get(session_id)
    if bucket is None:
        return
    bucket.discard(websocket)
    if not bucket:
        _SESSION_WS_CONNECTIONS.pop(session_id, None)


def session_connection_count(session_id: str) -> int:
    """Number of live connections currently tracked for ``session_id``: never
    negative, and 0 for an unknown session."""
    return len(_SESSION_WS_CONNECTIONS.get(session_id, ()))


async def _reap_prior_session_connections(
    session_id: str, keeper: "ServerConnection"
) -> int:
    """DISABLED: returns 0 without closing anything. The reap would close every
    PRIOR socket of ``session_id`` except ``keeper``, and the code below is kept
    for a re-enable under a policy that survives the dual-socket design."""
    # The eager per-session reap is incompatible with the dual-socket design,
    # where two sockets share one session_id: it closed the legitimate sibling
    # and killed its mid-stream turn. Re-enable ONLY under a policy that
    # preserves the socket pair and never closes a socket whose session has an
    # in-flight turn or solve.
    return 0
    bucket = _SESSION_WS_CONNECTIONS.get(session_id)
    if not bucket:
        return 0
    # Snapshot + exclude the keeper by identity BEFORE any close so we can never
    # target the resuming connection's own live socket.
    priors = [c for c in bucket if c is not keeper]
    reaped = 0
    for prior in priors:
        # Drop from the registry first so a re-entrant reap (a near-simultaneous
        # resume) cannot double-target the same stale socket.
        bucket.discard(prior)
        try:
            await prior.close(
                code=SESSION_SUPERSEDED_CLOSE_CODE,
                reason="superseded by a newer session connection",
            )
            reaped += 1
        except Exception:  # noqa: BLE001 - best-effort; never break the resume
            # The prior socket is already closing/closed; still count it gone.
            reaped += 1
    if not bucket:
        _SESSION_WS_CONNECTIONS.pop(session_id, None)
    if reaped:
        logger.info(
            "session-resume reaped %d prior socket(s) session=%s remaining=%d",
            reaped,
            session_id,
            session_connection_count(session_id),
        )
    return reaped
