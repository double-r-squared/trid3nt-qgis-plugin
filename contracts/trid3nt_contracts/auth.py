"""The two envelopes of the WebSocket connect handshake.

``auth-token`` carries the access token up, ``auth-ack`` the fixed session
identity back. The raw token NEVER appears on the ack and is never persisted:
only the ``user_id`` the session is scoped to reaches the client.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from .common import (
    ContractModel,
    ULIDStr,
)

__all__ = [
    "AdvertisedEndpoints",
    "AuthTokenEnvelope",
    "AuthAckEnvelope",
]




class AdvertisedEndpoints(ContractModel):
    """Base URLs for the daemon's sibling services, ridden on the ``auth-ack``.
    Best-effort and wholly optional: a client that reads ``None`` falls back to
    its own configured defaults rather than failing the connect."""

    #: Object-store (MinIO) http base, e.g. ``http://<host>:9000``; None when not derivable and no env override.
    data_base: str | None = Field(default=None, max_length=2048)

    #: Agent read-only HTTP base, e.g. ``http://<host>:8766``; None when not derivable and no env override.
    http_base: str | None = Field(default=None, max_length=2048)




class AuthTokenEnvelope(ContractModel):
    """``auth-token`` (client -> agent): the access token, sent before any
    other client envelope. It is the whole gate - an absent or empty token is
    refused, never a fallback into some lesser identity."""

    MESSAGE_TYPE: ClassVar[str] = "auth-token"

    #: The daemon's shared access token: consumed at verification and discarded, never persisted or re-emitted. Bounded at 8KB.
    token: str = Field(default="", max_length=8192)




class AuthAckEnvelope(ContractModel):
    """``auth-ack`` (agent -> client): the session identity, sent exactly once
    per connect and only after the token verified. Every later envelope is
    implicitly scoped to this ``user_id``, and no cost, quota or spend field
    ever lands here."""

    MESSAGE_TYPE: ClassVar[str] = "auth-ack"

    user_id: ULIDStr

    #: Server-advertised sibling endpoints; None when not advertised, and a client treats it as best-effort.
    endpoints: AdvertisedEndpoints | None = Field(default=None)
