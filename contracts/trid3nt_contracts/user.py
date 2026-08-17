"""User identity envelope (Auth/Users track stub, sprint-12-mega Wave 1.5).

A User document represents an account. The shape lives here so the agent's
persistence layer has a typed contract to ``upsert_user`` / ``get_user_by_id``
against. In the local single-user build there is one fixed user; the multi-user
Auth/Users track (Firebase / Identity Platform wiring) is not part of this
build.

This is a **forward-looking stub**: only the fields the persistence layer
demands at Wave 1.5 are populated. The full Auth track will additively grow
the schema (e.g. ``last_login_at``, ``roles``, ``signing_provider``); the
``extra="forbid"`` discipline ensures any silent drift gets caught at the
next pass through ``model_validate``.

Invariants this module is responsible for:

- **9. No cost theater.** No quota / usage / spend fields anywhere on the
  User record. Cost surfacing is forbidden everywhere.
- **Forward-compat via additive growth.** The Auth/Users track adds fields
  without renaming existing ones; ``schema_version="v1"`` is the bump-anchor
  if the shape ever has to break.

SRS references:
- (``sessions``) — the User contract is the per-user
  cross-Case parent of session records (a ``sessions`` document scopes to a
  ``user_id`` + ``case_id``).
- §F.3 (per-Case secrets) — the ``case_id``-scoped ``SecretRecord``
  ultimately roots in a ``User`` once Auth lands; the ``user_id`` link is
  the join key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import (
    GraceModel,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    "User",
]


class User(GraceModel):
    """An authenticated user.

    Fields:

    - ``user_id`` — ULID; the canonical id used by every join (Case ownership,
      session scoping, per-user secrets). In the local build this is the fixed
      single-user constant.
    - ``email`` — verified email from the Auth provider. Optional (some
      providers ship no email by default; the Auth track gates this).
    - ``display_name`` — free-text display name (≤200 chars). Optional;
      defaults to the email local-part client-side when null.
    - ``created_at`` — ISO-8601 UTC creation timestamp.
    - ``is_active`` — soft-deactivate flag. Defaults True. When False the
      agent service refuses dispatch on any Case owned by this user.
    - ``prefs`` — opaque preferences dict (per-user UI prefs, theme, etc.).
      Kept open — the client owns the enumeration of preference keys; here we
      just provide the persistence slot. Unknown keys on a persisted row load
      unchanged (open dict, no per-key validation).

    Invariant 9: no cost / quota / spend fields anywhere. Usage tracking
    lives on individual ``SecretRecord``s (``last_used_at`` only).
    """

    schema_version: Literal["v1"] = "v1"

    user_id: ULIDStr
    email: str | None = Field(default=None, max_length=320)  # RFC 5321 limit
    display_name: str | None = Field(default=None, max_length=200)
    created_at: UTCDatetime
    is_active: bool = True
    prefs: dict = Field(default_factory=dict)
    # Marks an anonymous (no-IdP) User. In the local single-user build the one
    # fixed user is provisioned with ``is_anonymous=True`` so the auth-ack keeps
    # the client handshake unchanged. Default ``False`` (unchanged). (:
    # an anonymous User has no credential to leak.)
    is_anonymous: bool = False
