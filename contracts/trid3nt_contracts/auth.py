"""The two envelopes of the WebSocket connect handshake.

``auth-token`` carries the access token up, ``auth-ack`` the fixed session
identity back. The raw token NEVER appears on the ack and is never persisted:
only the ``user_id`` the session is scoped to reaches the client.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from .common import (
    GraceModel,
    ULIDStr,
)

__all__ = [
    "AdvertisedEndpoints",
    "AuthTokenEnvelope",
    "AuthAckEnvelope",
]




class AdvertisedEndpoints(GraceModel):
    """Base URLs for the daemon's sibling services, ridden on the ``auth-ack``.
    Best-effort and wholly optional: a client that reads ``None`` falls back to
    its own configured defaults rather than failing the connect."""

    #: Object-store (MinIO) http base, e.g. ``http://<host>:9000``. None when
    #: the server cannot derive it and no env override is set.
    data_base: str | None = Field(default=None, max_length=2048)

    #: Agent read-only HTTP base, e.g. ``http://<host>:8766``. None when the
    #: server cannot derive it and no env override is set.
    http_base: str | None = Field(default=None, max_length=2048)




class AuthTokenEnvelope(GraceModel):
    """``auth-token`` (client -> agent): the access token, sent before any
    other client envelope. It is the whole gate - an absent or empty token is
    refused, never a fallback into some lesser identity."""

    MESSAGE_TYPE: ClassVar[str] = "auth-token"

    #: The daemon's shared access token. Consumed at verification and discarded
    #: - never persisted, never re-emitted. Bounded at 8KB: far above the
    #: minted token, far below a DOS.
    token: str = Field(default="", max_length=8192)




class AuthAckEnvelope(GraceModel):
    """``auth-ack`` (agent -> client): the session identity, sent exactly once
    per connect and only after the token verified. Every later envelope is
    implicitly scoped to this ``user_id``, and no cost, quota or spend field
    ever lands here."""

    MESSAGE_TYPE: ClassVar[str] = "auth-ack"

    #: The fixed session id (ULID) this connection is scoped to.
    user_id: ULIDStr

    #: Optional server-advertised sibling endpoints (object store + agent
    #: HTTP). ``None`` when the server does not advertise; a client treats it
    #: as best-effort. Additive and default-``None``, so the wire shape stays
    #: byte-identical against a server or client that ignores it.
    endpoints: AdvertisedEndpoints | None = Field(default=None)
