"""Pending user-interaction registries for the WebSocket server.

Two independent request/response gates (tool-choice + credential), both sharing
the same shape: a module-level dict keyed by an unguessable-ULID ``request_id``
tagged with the owning ``session_id``, so a reply arriving on a sibling
WebSocket connection of the same session still resolves the paused turn, and a
cross-session reply is refused. ``register`` / ``pop`` / ``resolve`` are pure
registry operations. ``_core`` re-imports these names by name so bare-global
references and monkeypatch targets on ``trid3nt_server.server.<name>`` resolve
as the monolith's did. The ``logging.getLogger`` name matches ``_core`` so log
records are indistinguishable across the split.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from trid3nt_contracts.secrets import CredentialProvidedEnvelopePayload

    from ._core import SessionState

logger = logging.getLogger("trid3nt_server.server")


# --------------------------------------------------------------------------- #
# Pending tool-choice registry: keyed by the unguessable ULID
# request_id + owning session_id, so a reply arriving on a sibling WebSocket
# connection of the same session still resolves the paused turn.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Pending tool-choice registry: module-level, keyed by the
# unguessable ULID request_id + owning session_id, so a reply arriving on a
# sibling WebSocket connection of the same session still resolves the paused
# turn.
# --------------------------------------------------------------------------- #

_PENDING_TOOL_CHOICES: dict[str, tuple[str, "asyncio.Future"]] = {}


def _register_pending_tool_choice(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_TOOL_CHOICES[request_id] = (session_id, fut)


def _pop_pending_tool_choice(request_id: str) -> None:
    _PENDING_TOOL_CHOICES.pop(request_id, None)


def _resolve_pending_tool_choice(session_id: str, payload: Any) -> bool:
    """Complete the pending tool-candidates gate for ``payload['request_id']``.

    The payload is a LOOSE dict on purpose -- the contracts lane declares the
    ``tool-choice`` model; until integration we parse defensively. Returns
    True when a live future was resolved.
    """
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


# --------------------------------------------------------------------------- #
# Session-scoped pending-CREDENTIAL registry
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_CONFIRMATIONS`` (the payload-warning / code-exec / solver
# gate registry) but for the credential-request flow: when a keyed tool
# dispatch hits a missing/invalid credential the dispatch coroutine pauses on
# a future keyed by the credential ``request_id``, having emitted a
# ``credential-request`` envelope. The inbound ``credential-provided``
# handler (which may arrive on a different WebSocket connection of the same
# session) resolves the future, and the paused dispatch retries the tool
# (which now reads the freshly-pushed session-cache key). Tagged with the
# owning session_id so a cross-session credential-provided is refused.
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
    """Complete the pending credential future for ``provided.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- the request_id is an unguessable ULID, but the string
    compare is cheap defense-in-depth, matching ``_resolve_pending_confirmation``).
    """
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
