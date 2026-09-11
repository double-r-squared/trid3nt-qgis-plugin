"""Round-trip and invariant tests for the Auth handshake envelopes.

Both envelopes serialize -> deserialize -> re-serialize byte-identically, pin
their kebab-case discriminators, refuse extra fields and a malformed ULID
``user_id``, cap a token at 8 KB, and carry no cost, spend or quota field. The
anonymous ack carries no identity claim."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trid3nt_contracts.auth import AuthAckEnvelope, AuthTokenEnvelope
from trid3nt_contracts.common import new_ulid




def _ack(**overrides) -> AuthAckEnvelope:
    base = {
        "user_id": new_ulid(),
        "is_anonymous": False,
    }
    base.update(overrides)
    return AuthAckEnvelope(**base)




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




def test_auth_token_envelope_defaults() -> None:
    """Default construction = empty token (triggers anonymous-fallback)."""
    tok = AuthTokenEnvelope()
    assert tok.token == ""
    assert tok.anonymous is False  # client may still NOT mark anonymous




def test_auth_token_message_type_pinned() -> None:
    """The kebab-case discriminator is ``auth-token``."""
    assert AuthTokenEnvelope.MESSAGE_TYPE == "auth-token"




def test_auth_token_rejects_extra_fields() -> None:
    """``extra='forbid'`` catches silent contract drift."""
    base = AuthTokenEnvelope(token="abc").model_dump(mode="json")
    # ``anonymous_user_id`` is not a field on this envelope - it is rejected
    # like any other extra.
    for forbidden in ("refresh_token", "tier", "user_id", "claims", "anonymous_user_id"):
        bad = {**base, forbidden: "x"}
        with pytest.raises(ValidationError, match="(?i)extra"):
            AuthTokenEnvelope.model_validate(bad)




def test_auth_token_oversized_token_rejected() -> None:
    """Token over the 8KB ceiling is rejected (max_length backstop)."""
    huge = "a" * 8193
    with pytest.raises(ValidationError):
        AuthTokenEnvelope(token=huge)




def test_auth_ack_envelope_roundtrip() -> None:
    """``auth-ack`` JSON round-trip is byte-identical."""
    ack = _ack()
    a = ack.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = AuthAckEnvelope.model_validate(json.loads(text_a)).model_dump(mode="json")
    text_b = json.dumps(b, sort_keys=True)
    assert text_a == text_b




def test_auth_ack_envelope_anonymous() -> None:
    """Anonymous-fallback ack: no firebase_uid, anonymous=True."""
    ack = AuthAckEnvelope(
        user_id=new_ulid(),
        is_anonymous=True,
    )
    assert not hasattr(ack, "firebase_uid")
    assert not hasattr(ack, "tier")
    assert ack.is_anonymous is True




def test_auth_ack_message_type_pinned() -> None:
    """``auth-ack`` kebab-case discriminator."""
    assert AuthAckEnvelope.MESSAGE_TYPE == "auth-ack"




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




def test_auth_ack_tier_field_removed() -> None:
    """The H.4 tier claim was cut; ``extra="forbid"`` rejects any ``tier`` key."""
    with pytest.raises(ValidationError, match="(?i)extra"):
        AuthAckEnvelope(
            user_id=new_ulid(),
            tier="free",  # type: ignore[call-arg]
        )




def test_auth_ack_invalid_user_id_rejected() -> None:
    """``user_id`` must be a syntactically valid ULID (matches User contract)."""
    with pytest.raises(ValidationError):
        AuthAckEnvelope(user_id="not-a-ulid")
    with pytest.raises(ValidationError):
        AuthAckEnvelope(user_id="")




def test_auth_envelopes_exported_from_package() -> None:
    """The ``auth`` module is importable from the top-level package."""
    import trid3nt_contracts

    assert hasattr(trid3nt_contracts, "auth")
    assert trid3nt_contracts.auth.AuthTokenEnvelope is AuthTokenEnvelope
    assert trid3nt_contracts.auth.AuthAckEnvelope is AuthAckEnvelope
