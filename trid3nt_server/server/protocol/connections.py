"""Session-connection registry: the per-session live-socket set and the
session-supersede reap.

Self-contained - it reads only the module-local registry - so a caller holds no
other connection state."""

from __future__ import annotations

import logging

from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")


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


