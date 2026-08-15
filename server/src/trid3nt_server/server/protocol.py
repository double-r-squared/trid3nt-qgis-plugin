"""Session-connection registry -- the serve-wiring plumbing (server-refactor
wave 4, ADR 0264).

The per-session live-socket registry and the session-supersede reap: the
cleanly-separable slice of the daemon's connection plumbing. Self-contained
(reads only the module-local ``_SESSION_WS_CONNECTIONS`` dict + a logger), so it
extracts as a leaf with no ``_core`` back-import. ``_core`` re-imports these
names so the handler's ``_register_session_connection`` /
``_reap_prior_session_connections`` calls and ``_session_safe_send``'s
``_SESSION_WS_CONNECTIONS`` read resolve unchanged; the package facade re-exposes
them at ``trid3nt_server.server.<name>``.

Deliberately NOT here (entangled, flagged in ADR 0264 for the session/turn wave):
the WS handler (``_make_handler``) and ``run_server`` -- the connection loop is
inseparable from ``SessionState`` and the ``_SESSION_LIVE_TURNS`` registry, both
owned by the session wave -- and ``inflight_turn_count`` (reads
``_SESSION_LIVE_TURNS``). They stay in ``_core``, and ``run_server`` still
resolves through the facade for ``main.py``.
"""

from __future__ import annotations

import logging

from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

SESSION_SUPERSEDED_CLOSE_CODE = 4408

_SESSION_WS_CONNECTIONS: "dict[str, set[ServerConnection]]" = {}


def _register_session_connection(
    session_id: str, websocket: "ServerConnection"
) -> None:
    """Record ``websocket`` as a live connection of ``session_id`` (idempotent).

    Called once the connection's ``session_id`` is known (first inbound
    envelope routed through ``_handle_session_resume`` / the handler). Set
    semantics make a re-register a no-op.
    """
    if not session_id:
        return
    _SESSION_WS_CONNECTIONS.setdefault(session_id, set()).add(websocket)


def _deregister_session_connection(
    session_id: str, websocket: "ServerConnection"
) -> None:
    """Drop ``websocket`` from ``session_id``'s live-connection set.

    Called from the handler ``finally`` on EVERY exit path. ``discard`` never
    raises; an emptied bucket is pruned so the registry cannot grow unbounded.
    """
    if not session_id:
        return
    bucket = _SESSION_WS_CONNECTIONS.get(session_id)
    if bucket is None:
        return
    bucket.discard(websocket)
    if not bucket:
        _SESSION_WS_CONNECTIONS.pop(session_id, None)


def session_connection_count(session_id: str) -> int:
    """Number of live connections currently tracked for ``session_id``.

    Surfaced for tests (and post-mortem) so the per-session reap can be asserted
    directly. NEVER negative; 0 for an unknown session.
    """
    return len(_SESSION_WS_CONNECTIONS.get(session_id, ()))


async def _reap_prior_session_connections(
    session_id: str, keeper: "ServerConnection"
) -> int:
    """Proactively close every PRIOR socket of ``session_id`` except ``keeper``.

    Called on each session-resume handshake. The ``keeper`` (the resuming
    connection) is excluded by object identity FIRST so its own live socket is
    never closed. Returns the number of prior sockets closed; best-effort, a
    close that raises is swallowed and the stale socket is dropped from the
    registry either way so the count cannot wedge.
    """
    # Reap DISABLED: the eager per-session reap is incompatible with the
    # dual-socket design (2 sockets per session share a session_id) - it closed
    # the legitimate sibling and killed its mid-stream turn with 4408. Re-enable
    # ONLY with a policy that preserves the dual-socket pair and never closes a
    # socket whose session has an in-flight turn/solve. _register_session_connection
    # stays (cheap, useful); the code below is retained for that re-enable.
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
