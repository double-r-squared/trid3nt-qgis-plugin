"""Pending user-interaction registries for the WebSocket server.

The tool-choice gate: a module-level dict keyed by an unguessable ULID
``request_id`` tagged with the owning ``session_id``, so a cross-session reply is
refused."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from .session.state import SessionState

logger = logging.getLogger("trid3nt_server.server")


# Pending tool-choice registry: keyed by the unguessable ULID request_id plus
# the owning session_id, so a reply arriving on a sibling WebSocket connection
# of the same session still resolves the paused turn.

_PENDING_TOOL_CHOICES: dict[str, tuple[str, "asyncio.Future"]] = {}


def _register_pending_tool_choice(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_TOOL_CHOICES[request_id] = (session_id, fut)


def _pop_pending_tool_choice(request_id: str) -> None:
    _PENDING_TOOL_CHOICES.pop(request_id, None)


def _resolve_pending_tool_choice(session_id: str, payload: Any) -> bool:
    """Complete the pending tool-candidates gate for ``payload['request_id']``,
    returning True when a live future was resolved; the payload is parsed
    defensively as a loose dict."""
    if not isinstance(payload, dict):
        return False
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return False
    entry = _PENDING_TOOL_CHOICES.get(request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "tool-choice request_id=%s owned by session=%s but resolved-by=%s; "
            "ignoring",
            request_id,
            owner_session,
            session_id,
        )
        return False
    if fut.done():
        return False
    fut.set_result(dict(payload))
    return True
