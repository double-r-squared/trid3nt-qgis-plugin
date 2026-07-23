"""Auth handshake envelopes for the WebSocket connect flow (Appendix H, FR-AS-5).

Wave 2 of sprint-12-mega lands Firebase Authentication into the WebSocket
connect handshake. Per Appendix H.5 the agent verifies a Firebase ID token on
connect, resolves it to a ``UserDocument._id`` via the FR-MP-1 Persistence
interface (job-0115), and binds the resolved user to the session context so
every subsequent envelope is user-scoped.

This module defines the **two envelopes** the auth handshake uses:

- ``AuthTokenEnvelope`` (client → agent, type ``auth-token``) — the client
  sends its Firebase ID token immediately after WebSocket connect. The token
  is the credential; verification happens agent-side.
- ``AuthAckEnvelope`` (agent → client, type ``auth-ack``) — the agent
  confirms the resolved authenticated user id and whether the user is
  anonymous. Sent once per connect after either successful ``verify_id_token``
  or anonymous-fallback provisioning (job-0122 scope).

Both shapes carry small, forward-looking surfaces — the H.4 ``tier`` claim is
included on the ack so the client can drive tier-gated UI without a second
round-trip. The H.5 ``token-refresh`` envelope is deferred to a follow-up job
when token-refresh wiring lands.

Invariants this module is responsible for:

- **Invariant 9 (no cost theater).** No cost / spend / quota fields on either
  envelope. ``tier`` is a capability claim, not a cost surface (Appendix H.4).
- **Decision F (wire isolation).** The raw token NEVER appears in
  ``AuthAckEnvelope`` — the agent verifies + discards; only the resolved
  ``user_id`` + ``firebase_uid`` + ``tier`` flow back to the client.
- **Forward-compat via additive growth.** New tier-claim fields,
  organization_id, roles[] all land additively when the enterprise SKU
  upgrade ships (Appendix H.4).

SRS references:

- Appendix H.1 — Firebase Authentication as the identity provider.
- Appendix H.3 — Anonymous → authenticated upgrade (``is_anonymous`` flag).
- Appendix H.4 — Custom claims for tier gating (``tier`` field).
- Appendix H.5 — Session validation: ``verify_id_token`` resolves to
  ``UserDocument._id`` via Persistence.
- Appendix A.5 — Connection lifecycle (the handshake sits here once landed).
- Appendix A.6 — ``AUTH_TOKEN_EXPIRED`` / ``AUTH_TOKEN_INVALID`` /
  ``TIER_INSUFFICIENT`` error codes (forward-looking; this module pins the
  envelope shapes so the codes have somewhere to surface).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .common import (
    GraceModel,
    ULIDStr,
)

__all__ = [
    "TierClaim",
    "AdvertisedEndpoints",
    "AuthTokenEnvelope",
    "AuthAckEnvelope",
]


# --------------------------------------------------------------------------- #
# Tier claim (H.4)
# --------------------------------------------------------------------------- #

#: Tier capability claim per Appendix H.4. v0.1 default ``"free"``;
#: ``"pro"`` / ``"enterprise"`` reserved for the v0.2+ commercial track.
#: No cost surface — this is a capability claim.
TierClaim = Literal["free", "pro", "enterprise"]


# --------------------------------------------------------------------------- #
# Server-advertised sibling endpoints (remote-daemon access, 2026-07)
# --------------------------------------------------------------------------- #


class AdvertisedEndpoints(GraceModel):
    """Server-advertised base URLs for the daemon's sibling services.

    Remote-daemon access: a client (QGIS plugin / browser) that is configured
    with ONLY the WebSocket server URL learns where the sibling surfaces live
    directly from the connect handshake, so no second setting is required. The
    server rides this object on the ``auth-ack`` -- the FIRST envelope the
    client parses -- so the endpoints are known before any layer / data fetch.

    - ``data_base`` -- the object-store (MinIO) http base, e.g.
      ``http://<host>:9000``. Clients translate ``s3://bucket/key`` layer
      URIs to path-style http against this base.
    - ``http_base`` -- the agent's read-only HTTP surface (tool catalog etc.),
      e.g. ``http://<host>:8766``.

    The server DERIVES both from the connection's own local address (so a
    client dialing ``100.x.x.x:8765`` over the tailnet gets ``100.x.x.x`` back
    automatically) plus the known ports, OR from the
    ``TRID3NT_ADVERTISED_DATA_BASE`` / ``TRID3NT_ADVERTISED_HTTP_BASE`` env
    overrides when set.

    Both fields are optional and the whole object is optional on the ack
    (defaults ``None``): an old server / stub that never sets it, and an old
    client that never reads it, are byte-identical on the wire. Clients MUST
    treat it as best-effort and fall back to their own configured defaults
    when it is absent.
    """

    #: Object-store (MinIO) http base, e.g. ``http://<host>:9000``. None when
    #: the server cannot derive it and no env override is set.
    data_base: str | None = Field(default=None, max_length=2048)

    #: Agent read-only HTTP base, e.g. ``http://<host>:8766``. None when the
    #: server cannot derive it and no env override is set.
    http_base: str | None = Field(default=None, max_length=2048)


# --------------------------------------------------------------------------- #
# Client → Agent: auth-token (Appendix H.5)
# --------------------------------------------------------------------------- #


class AuthTokenEnvelope(GraceModel):
    """``auth-token`` (client → agent): the Firebase ID token for verification.

    The client sends this envelope immediately after WebSocket connect, before
    any other client→agent envelope. The agent calls
    ``firebase_admin.auth.verify_id_token(token)`` to resolve the Firebase
    ``uid`` (and the tier custom-claim if present), then looks up or
    auto-provisions the corresponding ``UserDocument`` via the FR-MP-1
    Persistence interface (job-0115).

    Wave 2 scope (job-0122):
    - ``token`` is a non-empty string — the JWT issued by Firebase Auth.
    - ``anonymous`` may be sent as a hint by the client (e.g. when it
      explicitly signed in anonymously). The agent does NOT trust this hint
      blindly — verification flows from the JWT claims.
    - Empty / missing ``token`` triggers the anonymous-fallback path
      (server creates ephemeral User without firebase_uid).

    Decision F: the raw token is consumed by the agent and discarded after
    verification — it is NEVER persisted (Mongo) and NEVER re-emitted on the
    wire (the ack carries only the resolved identity, not the credential).
    """

    MESSAGE_TYPE: ClassVar[str] = "auth-token"

    #: The Firebase ID token (JWT). Empty string triggers anonymous fallback.
    #: Upper-bounded at 8KB — well above any real JWT, well below any DOS
    #: vector. Firebase JWTs are typically 800-1500 bytes.
    token: str = Field(default="", max_length=8192)

    #: Client-side hint that this is an anonymous sign-in. The agent verifies
    #: against the JWT claims; this field is informational only.
    anonymous: bool = False

    #: job-0172 Part C: sticky anonymous identity hint. When the connect path
    #: has no Firebase token AND this field carries a ULID, the agent looks
    #: up the matching ``UserDocument`` and re-binds the existing anonymous
    #: User if (a) the User exists and (b) ``user.is_anonymous`` is True.
    #: Otherwise the agent mints a fresh anonymous User. The client
    #: persists this id in localStorage on first connect and replays it on
    #: every reconnect, so a browser refresh re-binds the same User and the
    #: user's Cases stay visible across reloads.
    #:
    #: NEVER trusted for Firebase-verified Users — when ``token`` verifies,
    #: this hint is ignored entirely (the JWT claims are the credential).
    anonymous_user_id: ULIDStr | None = Field(default=None)


# --------------------------------------------------------------------------- #
# Agent → Client: auth-ack (Appendix H.5)
# --------------------------------------------------------------------------- #


class AuthAckEnvelope(GraceModel):
    """``auth-ack`` (agent → client): confirmation of the resolved identity.

    Sent exactly once per WebSocket connect, after the agent has either:

    1. Verified a Firebase ID token and resolved (or auto-provisioned) the
       matching ``UserDocument`` — ``is_anonymous=False``,
       ``firebase_uid`` set.
    2. Fallen through to the anonymous-fallback path (no token, invalid
       token, or 5-second token-arrival timeout) — ``is_anonymous=True``,
       ``firebase_uid=None``.

    Either way the client now knows its authenticated ``user_id`` for the
    session — every subsequent envelope is implicitly scoped to this user.

    Wave 2 scope (job-0122):
    - ``user_id`` is the ULID-shaped ``UserDocument._id`` (per Appendix H.2
      and the ``User`` contract).
    - ``firebase_uid`` is the resolved Firebase UID (None on anonymous
      fallback).
    - ``is_anonymous`` mirrors the H.3 fallback path.
    - ``tier`` is the H.4 capability claim, default ``"free"``. The web
      client uses this to drive tier-gated UI without a second round-trip.

    Invariant 9: no cost / quota / spend field. ``tier`` is capability, not
    cost.
    """

    MESSAGE_TYPE: ClassVar[str] = "auth-ack"

    #: The resolved ``UserDocument._id`` (ULID) for this session.
    user_id: ULIDStr

    #: The Firebase UID; ``None`` for anonymous-fallback users.
    firebase_uid: str | None = Field(default=None, max_length=256)

    #: True if this is an anonymous-fallback user (no Firebase verification).
    is_anonymous: bool = False

    #: H.4 tier capability claim. v0.1 default ``"free"``.
    tier: TierClaim = "free"

    #: Remote-daemon access (2026-07): optional server-advertised sibling
    #: endpoints (object store + agent HTTP). ``None`` on old servers / stubs
    #: -- clients treat it as best-effort and fall back to their own configured
    #: defaults when absent. Additive + default-None, so ``extra="forbid"`` and
    #: the on-the-wire shape stay backward-compatible.
    endpoints: AdvertisedEndpoints | None = Field(default=None)
