"""Session-scoped pending-confirmation registry for the block-and-wait gates.

Every gate registers into the SAME dict the inbound handler resolves: process-global,
keyed by an unguessable ULID, and refused from a non-owning session.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload

logger = logging.getLogger("trid3nt_server.gates.pending")

__all__ = [
    "_PENDING_CONFIRMATIONS",
    "_register_pending_confirmation",
    "_pop_pending_confirmation",
    "_resolve_pending_confirmation",
]

# warning_id / code_exec_id -> (owner_session_id, future). The
# ``tool-payload-confirmation`` handler resolves a pending gate on a SESSION
# match rather than a connection match, since the client can open multiple
# WebSocket connections per browser session.
_PENDING_CONFIRMATIONS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_confirmation(
    session_id: str, warning_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_CONFIRMATIONS[warning_id] = (session_id, fut)


def _pop_pending_confirmation(warning_id: str) -> None:
    _PENDING_CONFIRMATIONS.pop(warning_id, None)


def _resolve_pending_confirmation(
    session_id: str, conf: "PayloadConfirmationEnvelopePayload"
) -> bool:
    """Complete the pending gate future for ``conf.warning_id``.

    False when the id is unknown or already resolved, and when the confirming
    session is not the owner -- cross-session confirmation is refused."""
    entry = _PENDING_CONFIRMATIONS.get(conf.warning_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "tool-payload-confirmation REFUSED: session=%s is not the owner "
            "(owner=%s) for warning_id=%s",
            session_id,
            owner_session,
            conf.warning_id,
        )
        return False
    if fut.done():
        _PENDING_CONFIRMATIONS.pop(conf.warning_id, None)
        return False
    fut.set_result(conf)
    _PENDING_CONFIRMATIONS.pop(conf.warning_id, None)
    return True
