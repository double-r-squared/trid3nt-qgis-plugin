"""The two envelopes of the WebSocket connect handshake.

``auth-token`` carries the credential up, ``auth-ack`` the resolved identity
back. The raw credential NEVER appears on the ack and is never persisted: only
the resolved ``user_id`` reaches the client.
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
    """``auth-token`` (client -> agent): the credential, sent before any other
    client envelope. An empty ``token`` selects the anonymous path, and
    ``anonymous`` is an untrusted client hint rather than the decision."""

    MESSAGE_TYPE: ClassVar[str] = "auth-token"

    #: The identity token (JWT). Empty string selects the anonymous fallback.
    #: Consumed at verification and discarded - never persisted, never
    #: re-emitted. Bounded at 8KB: far above any real JWT, far below a DOS.
    token: str = Field(default="", max_length=8192)

    #: Client-side hint that this is an anonymous sign-in. Informational only:
    #: the decision is taken from the token's own claims.
    anonymous: bool = False




class AuthAckEnvelope(GraceModel):
    """``auth-ack`` (agent -> client): the resolved identity, sent exactly once
    per connect. Every later envelope is implicitly scoped to this ``user_id``,
    and no cost, quota or spend field ever lands here."""

    MESSAGE_TYPE: ClassVar[str] = "auth-ack"

    #: The resolved ``User`` id (ULID) this session is scoped to.
    user_id: ULIDStr

    #: True if this is an anonymous-fallback user (no identity provider).
    is_anonymous: bool = False

    #: Optional server-advertised sibling endpoints (object store + agent
    #: HTTP). ``None`` when the server does not advertise; a client treats it
    #: as best-effort. Additive and default-``None``, so the wire shape stays
    #: byte-identical against a server or client that ignores it.
    endpoints: AdvertisedEndpoints | None = Field(default=None)
