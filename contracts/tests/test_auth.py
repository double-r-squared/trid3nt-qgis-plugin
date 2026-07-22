"""Round-trip + invariant tests for the Auth handshake envelopes (job-0122).

Coverage:

1. ``test_auth_token_envelope_roundtrip`` — JSON serialize -> deserialize ->
   re-serialize is byte-identical.
2. ``test_auth_token_envelope_defaults`` — empty-token (anonymous fallback)
   construction.
3. ``test_auth_token_message_type_pinned`` — kebab-case discriminator is
   ``auth-token``.
4. ``test_auth_token_rejects_extra_fields`` — ``extra="forbid"`` keeps drift
   out.
5. ``test_auth_token_oversized_token_rejected`` — 8KB upper bound.
6. ``test_auth_ack_envelope_roundtrip`` — JSON round-trip stability.
7. ``test_auth_ack_envelope_anonymous_default_tier_free`` — anonymous
   fallback ack defaults to ``tier="free"`` and ``firebase_uid=None``.
8. ``test_auth_ack_message_type_pinned`` — kebab-case ``auth-ack``.
9. ``test_auth_ack_invariant9_no_cost_fields`` — Invariant 9: no cost /
   spend / quota / billing fields.
10. ``test_auth_ack_invalid_tier_rejected`` — open-enum-bounded ``tier``.
11. ``test_auth_ack_invalid_user_id_rejected`` — ULID discipline on
    ``user_id``.
12. ``test_auth_envelopes_exported_from_package`` — ``grace2_contracts.auth``
    is importable from the top-level package.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from grace2_contracts.auth import AuthAckEnvelope, AuthTokenEnvelope
from grace2_contracts.common import new_ulid


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ack(**overrides) -> AuthAckEnvelope:
    base = {
        "user_id": new_ulid(),
        "firebase_uid": "firebase-uid-abc-123",
        "is_anonymous": False,
        "tier": "free",
    }
    base.update(overrides)
    return AuthAckEnvelope(**base)


# --------------------------------------------------------------------------- #
# 1. auth-token round-trip
# --------------------------------------------------------------------------- #


def test_auth_token_envelope_roundtrip() -> None:
    """JSON serialize -> deserialize -> re-serialize is byte-identical."""
    tok = AuthTokenEnvelope(
        token="eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYzEyMyJ9.payload.signature",
        anonymous=False,
    )
    a = tok.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = AuthTokenEnvelope.model_validate(json.loads(text_a)).model_dump(mode="json")
    text_b = json.dumps(b, sort_keys=True)
    assert text_a == text_b, "non-idempotent round-trip"
    assert a["anonymous"] is False
    assert a["token"].startswith("eyJ")


# --------------------------------------------------------------------------- #
# 2. auth-token defaults: anonymous fallback
# --------------------------------------------------------------------------- #


def test_auth_token_envelope_defaults() -> None:
    """Default construction = empty token (triggers anonymous-fallback)."""
    tok = AuthTokenEnvelope()
    assert tok.token == ""
    assert tok.anonymous is False  # client may still NOT mark anonymous


# --------------------------------------------------------------------------- #
# 3. auth-token discriminator
# --------------------------------------------------------------------------- #


def test_auth_token_message_type_pinned() -> None:
    """The kebab-case discriminator is ``auth-token``."""
    assert AuthTokenEnvelope.MESSAGE_TYPE == "auth-token"


# --------------------------------------------------------------------------- #
# 4. auth-token extra="forbid"
# --------------------------------------------------------------------------- #


def test_auth_token_rejects_extra_fields() -> None:
    """``extra='forbid'`` catches silent contract drift."""
    base = AuthTokenEnvelope(token="abc").model_dump(mode="json")
    for forbidden in ("refresh_token", "tier", "user_id", "claims"):
        bad = {**base, forbidden: "x"}
        with pytest.raises(ValidationError, match="(?i)extra"):
            AuthTokenEnvelope.model_validate(bad)


# --------------------------------------------------------------------------- #
# 5. auth-token oversize guard
# --------------------------------------------------------------------------- #


def test_auth_token_oversized_token_rejected() -> None:
    """Token over the 8KB ceiling is rejected (max_length backstop)."""
    huge = "a" * 8193
    with pytest.raises(ValidationError):
        AuthTokenEnvelope(token=huge)


# --------------------------------------------------------------------------- #
# 6. auth-ack round-trip
# --------------------------------------------------------------------------- #


def test_auth_ack_envelope_roundtrip() -> None:
    """``auth-ack`` JSON round-trip is byte-identical."""
    ack = _ack()
    a = ack.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = AuthAckEnvelope.model_validate(json.loads(text_a)).model_dump(mode="json")
    text_b = json.dumps(b, sort_keys=True)
    assert text_a == text_b


# --------------------------------------------------------------------------- #
# 7. auth-ack anonymous fallback defaults
# --------------------------------------------------------------------------- #


def test_auth_ack_envelope_anonymous_default_tier_free() -> None:
    """Anonymous-fallback ack: no firebase_uid, anonymous=True, tier=free."""
    ack = AuthAckEnvelope(
        user_id=new_ulid(),
        firebase_uid=None,
        is_anonymous=True,
    )
    assert ack.firebase_uid is None
    assert ack.is_anonymous is True
    assert ack.tier == "free"


# --------------------------------------------------------------------------- #
# 8. auth-ack discriminator
# --------------------------------------------------------------------------- #


def test_auth_ack_message_type_pinned() -> None:
    """``auth-ack`` kebab-case discriminator."""
    assert AuthAckEnvelope.MESSAGE_TYPE == "auth-ack"


# --------------------------------------------------------------------------- #
# 9. auth-ack Invariant 9: no cost theater
# --------------------------------------------------------------------------- #


def test_auth_ack_invariant9_no_cost_fields() -> None:
    """Invariant 9 negative control: no cost / spend / quota fields admitted."""
    base = _ack().model_dump(mode="json")
    for forbidden in (
        "cost_usd",
        "monthly_spend",
        "quota_remaining",
        "subscription_cost",
        "billing_status",
        "estimated_cost",
    ):
        bad = {**base, forbidden: 0.0}
        with pytest.raises(ValidationError, match="(?i)extra"):
            AuthAckEnvelope.model_validate(bad)


# --------------------------------------------------------------------------- #
# 10. auth-ack tier open-enum bounds
# --------------------------------------------------------------------------- #


def test_auth_ack_invalid_tier_rejected() -> None:
    """``tier`` is bounded to ``free`` / ``pro`` / ``enterprise``."""
    with pytest.raises(ValidationError):
        AuthAckEnvelope(
            user_id=new_ulid(),
            firebase_uid="x",
            tier="ultra",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# 11. auth-ack user_id ULID discipline
# --------------------------------------------------------------------------- #


def test_auth_ack_invalid_user_id_rejected() -> None:
    """``user_id`` must be a syntactically valid ULID (matches User contract)."""
    with pytest.raises(ValidationError):
        AuthAckEnvelope(user_id="not-a-ulid")
    with pytest.raises(ValidationError):
        AuthAckEnvelope(user_id="")


# --------------------------------------------------------------------------- #
# 12. package-level export
# --------------------------------------------------------------------------- #


def test_auth_envelopes_exported_from_package() -> None:
    """The ``auth`` module is importable from the top-level package."""
    import grace2_contracts

    assert hasattr(grace2_contracts, "auth")
    assert grace2_contracts.auth.AuthTokenEnvelope is AuthTokenEnvelope
    assert grace2_contracts.auth.AuthAckEnvelope is AuthAckEnvelope
