"""Owner-checked pending replies: every block-and-wait seam parks one future here.

A registry is process-global and keyed by an unguessable id the request carries,
so a reply arriving on a sibling WebSocket connection of the same session still
resolves the waiting turn, and a reply from any other session is refused. Each
kind of reply has its own registry so ids never collide across kinds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.inputs.gate.pending")

__all__ = [
    "PendingReplies",
    "_PENDING_CONFIRMATIONS",
    "_PENDING_SPATIAL_INPUTS",
    "_PENDING_TOOL_CHOICES",
    "_PENDING_PROCESSING",
    "_PENDING_LAYER",
]


class PendingReplies(dict[str, tuple[str, asyncio.Future]]):
    """id -> (owner_session_id, future) for one kind of reply; ``resolve`` and
    ``fail`` complete a live future only for its owner and drop the entry."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def register(self, session_id: str, key: str, fut: asyncio.Future) -> None:
        self[key] = (session_id, fut)

    def _claim(self, session_id: str, key: str) -> asyncio.Future | None:
        entry = self.get(key)
        if entry is None:
            return None
        owner_session, fut = entry
        if owner_session != session_id:
            logger.warning(
                "%s REFUSED: session=%s is not the owner (owner=%s) for id=%s",
                self.kind, session_id, owner_session, key,
            )
            return None
        self.pop(key, None)
        return None if fut.done() else fut

    def resolve(self, session_id: str, key: str, value: Any) -> bool:
        fut = self._claim(session_id, key)
        if fut is None:
            return False
        fut.set_result(value)
        return True

    def fail(self, session_id: str, key: str, exc: BaseException) -> bool:
        fut = self._claim(session_id, key)
        if fut is None:
            return False
        fut.set_exception(exc)
        return True


#: warning_id / code_exec_id of a confirmation card.
_PENDING_CONFIRMATIONS = PendingReplies("tool-payload-confirmation")
#: request_id of a draw surface; a draw with no reply resolves to None on timeout.
_PENDING_SPATIAL_INPUTS = PendingReplies("spatial-input-response")
#: request_id of a tool-candidates card.
_PENDING_TOOL_CHOICES = PendingReplies("tool-choice")
#: request_id of a processing-request run in the user's QGIS session.
_PENDING_PROCESSING = PendingReplies("processing-response")
#: the row's own key, so a response only resolves the layer request that sent it.
_PENDING_LAYER = PendingReplies("layer-response")
