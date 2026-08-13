"""Session-scoped pending-confirmation registry (the #154 pause/resume spine).

Extracted from ``server`` so BOTH the transport-coupled gate orchestration
(``server._gate_on_solver_confirm`` / ``_gate_on_code_exec`` /
``_maybe_gate_on_payload_warning``) AND the in-tool input-review gate (ADR 0107,
``agent.gates.input_review``) register their block-and-wait futures into the SAME
dict the inbound ``tool-payload-confirmation`` handler resolves. The registry is
process-global (keyed by the unguessable ULID ``warning_id`` / ``code_exec_id``)
and per-session-owned: a confirmation from a non-owning session is refused.

``server`` re-imports these names, so ``server._PENDING_CONFIRMATIONS`` stays the
SAME dict object (tests that reach through ``server.`` are unaffected).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload

logger = logging.getLogger("trid3nt_server.agent.gates.pending")

__all__ = [
    "_PENDING_CONFIRMATIONS",
    "_register_pending_confirmation",
    "_pop_pending_confirmation",
    "_resolve_pending_confirmation",
]

# warning_id / code_exec_id -> (owner_session_id, future). The
# ``tool-payload-confirmation`` handler can resolve a pending gate as long as
# the session matches, since the client can open multiple WebSocket
# connections per browser session. Shared by every confirmation gate --
# payload warning, code-exec, solver-confirm, and the input-review gate.
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

    Returns True when a live future was resolved. False when the warning_id is
    unknown/already-resolved, or when the confirming session is not the owner
    (cross-session confirmation is refused loudly -- the warning_id is an
    unguessable ULID, but defense-in-depth costs one string compare).
    """
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
