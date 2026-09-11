"""Pending user-interaction registries for the WebSocket server.

Two independent request/response gates, tool-choice and credential, share one
shape: a module-level dict keyed by an unguessable ULID ``request_id`` tagged
with the owning ``session_id``, so a cross-session reply is refused."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from trid3nt_contracts.secrets import CredentialProvidedEnvelopePayload

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


#
# A keyed tool dispatch that hits a missing or invalid credential pauses on a
# future keyed by the credential ``request_id`` after emitting the request
# envelope; the inbound reply, which may arrive on a sibling connection of the
# same session, resolves it and the dispatch retries the tool. Tagged with the
# owning session_id so a cross-session reply is refused.
_PENDING_CREDENTIALS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_credential(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_CREDENTIALS[request_id] = (session_id, fut)


def _pop_pending_credential(request_id: str) -> None:
    _PENDING_CREDENTIALS.pop(request_id, None)


def _resolve_pending_credential(
    session_id: str, provided: "CredentialProvidedEnvelopePayload"
) -> bool:
    """Complete the pending credential future for ``provided.request_id``, True
    when a live future was resolved; an unknown, already-resolved or
    cross-session request_id is refused."""
    entry = _PENDING_CREDENTIALS.get(provided.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "credential-provided REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            provided.request_id,
        )
        return False
    if fut.done():
        _PENDING_CREDENTIALS.pop(provided.request_id, None)
        return False
    fut.set_result(provided)
    _PENDING_CREDENTIALS.pop(provided.request_id, None)
    return True
