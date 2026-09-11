"""Round-trip and leak-resistance tests for the per-Case secrets envelopes.

The persisted record is vault-ref-only and never carries a raw key; the list, add
and revoke envelopes round-trip byte-identically; a wrong ``envelope_type`` or an
unknown provider is refused; no cost, spend or quota field can be added; the add
envelope's ``repr()`` redacts ``key_value`` while attribute access keeps it."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from trid3nt_contracts import secrets
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.secrets import (
    SecretAddEnvelopePayload,
    SecretRecord,
    SecretRevokeEnvelopePayload,
    SecretsListEnvelopePayload,
)




def _record(provider: str = "firms", case_id: str | None = None) -> SecretRecord:
    """A realistic SecretRecord for round-trip tests."""
    return SecretRecord(
        secret_id=new_ulid(),
        provider=provider,  # type: ignore[arg-type]
        case_id=case_id if case_id is not None else new_ulid(),
        vault_ref=(
            "gcp-sm://projects/legacy-cloud-project/secrets/"
            "case-fk-01k-firms-key/versions/latest"
        ),
        label="personal FIRMS key",
        added_at=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
        last_used_at=None,
        is_active=True,
    )




def test_secret_record_roundtrip_idempotent() -> None:
    """JSON serialize -> deserialize -> re-serialize is byte-identical."""
    rec = _record()
    a = rec.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SecretRecord.model_validate(json.loads(text_a)).model_dump(mode="json")
    text_b = json.dumps(b, sort_keys=True)
    assert text_a == text_b, "non-idempotent round-trip"

    # Wire-shape sanity
    assert a["schema_version"] == "v1"
    assert a["provider"] == "firms"
    assert a["is_active"] is True
    assert a["added_at"].endswith("Z"), "datetime must serialize with Z suffix"
    assert a["last_used_at"] is None
    assert a["vault_ref"].startswith("gcp-sm://")




def test_secrets_list_envelope_roundtrip_idempotent() -> None:
    """The list envelope round-trips and defaults to an empty list."""
    # Default empty-list path
    empty = SecretsListEnvelopePayload()
    a_empty = empty.model_dump(mode="json")
    assert a_empty["envelope_type"] == "secrets-list"
    assert a_empty["secrets"] == []
    assert SecretsListEnvelopePayload.MESSAGE_TYPE == "secrets-list"

    # Populated path: two records, different providers
    populated = SecretsListEnvelopePayload(
        secrets=[_record("firms"), _record("ecmwf_cds")]
    )
    a = populated.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SecretsListEnvelopePayload.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert len(a["secrets"]) == 2
    assert {s["provider"] for s in a["secrets"]} == {"firms", "ecmwf_cds"}




def test_secret_add_envelope_roundtrip_idempotent() -> None:
    """The add envelope round-trips through JSON byte-identically.

    This is the wire form a client sends, which DOES carry the value; clearing
    ``key_value`` before persistence is the server's discipline, not this shape's."""
    add = SecretAddEnvelopePayload(
        provider="firms",
        case_id=new_ulid(),
        label="personal FIRMS key",
        key_value="ABCD1234EXAMPLEKEYDONOTUSE",
    )
    a = add.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SecretAddEnvelopePayload.model_validate(json.loads(text_a)).model_dump(mode="json")
    text_b = json.dumps(b, sort_keys=True)
    assert text_a == text_b

    # Default envelope_type literal
    assert a["envelope_type"] == "secret-add"
    assert SecretAddEnvelopePayload.MESSAGE_TYPE == "secret-add"
    # Defaults work without key_value (still a valid construct; server
    # validates the value at the persistence layer, not the schema).
    blank = SecretAddEnvelopePayload(provider="nws")
    assert blank.key_value == ""
    assert blank.case_id is None
    assert blank.label is None




def test_secret_revoke_envelope_roundtrip_idempotent() -> None:
    """The revoke envelope round-trips and rejects missing/empty secret_id."""
    revoke = SecretRevokeEnvelopePayload(secret_id=new_ulid())
    a = revoke.model_dump(mode="json")
    text_a = json.dumps(a, sort_keys=True)
    b = SecretRevokeEnvelopePayload.model_validate(json.loads(text_a)).model_dump(mode="json")
    assert text_a == json.dumps(b, sort_keys=True)
    assert a["envelope_type"] == "secret-revoke"
    assert SecretRevokeEnvelopePayload.MESSAGE_TYPE == "secret-revoke"

    # ULID validation rejects non-ULID strings
    with pytest.raises(ValidationError):
        SecretRevokeEnvelopePayload(secret_id="not-a-ulid")
    # And empty string
    with pytest.raises(ValidationError):
        SecretRevokeEnvelopePayload(secret_id="")




def test_secret_add_repr_redacts_key_value() -> None:
    """Default ``repr()`` MUST NOT echo the raw key value.

    It is the leak-minimisation back-stop, so dropping the ``__repr_args__`` override
    has to fail here."""
    sensitive = "REAL-LOOKING-KEY-DO-NOT-LEAK-12345"
    add = SecretAddEnvelopePayload(
        provider="firms",
        case_id=new_ulid(),
        label="personal key",
        key_value=sensitive,
    )
    r = repr(add)
    assert sensitive not in r, (
        f"key_value leaked into default repr (regression on §F.3 wire isolation): {r}"
    )
    # The redaction sentinel IS present so debugging confirms shape
    assert "<redacted>" in r, (
        f"redaction sentinel missing — repr_args override may be broken: {r}"
    )
    # And the field's presence is still visible (not silently dropped)
    assert "key_value=" in r




def test_envelope_type_literal_validation() -> None:
    """``envelope_type`` is a Literal — wrong values are rejected at validate."""
    # SecretsListEnvelopePayload
    bad = {"envelope_type": "secrets-list-bogus", "secrets": []}
    with pytest.raises(ValidationError):
        SecretsListEnvelopePayload.model_validate(bad)

    # SecretAddEnvelopePayload
    bad = {
        "envelope_type": "add-secret",  # wrong direction in the kebab-case
        "provider": "firms",
        "case_id": new_ulid(),
        "key_value": "x",
    }
    with pytest.raises(ValidationError):
        SecretAddEnvelopePayload.model_validate(bad)

    # SecretRevokeEnvelopePayload
    bad = {"envelope_type": "secret-revocation", "secret_id": new_ulid()}
    with pytest.raises(ValidationError):
        SecretRevokeEnvelopePayload.model_validate(bad)




def test_provider_id_literal_validation() -> None:
    """Unknown ``provider`` strings are rejected by the closed Literal."""
    # SecretRecord rejects unknown provider
    base = _record().model_dump(mode="json")
    bad = {**base, "provider": "frobnicate"}
    with pytest.raises(ValidationError):
        SecretRecord.model_validate(bad)

    # SecretAddEnvelopePayload rejects unknown provider
    bad_add = {
        "envelope_type": "secret-add",
        "provider": "stripe",  # not in the Tier-2/LLM/basemap vocabulary
        "case_id": new_ulid(),
        "key_value": "x",
    }
    with pytest.raises(ValidationError):
        SecretAddEnvelopePayload.model_validate(bad_add)

    # All declared providers are constructible
    for provider in (
        "firms",
        "ecmwf_cds",
        "gtsm",
        "nws",
        "openweathermap",
        "openai",
        "anthropic",
        "google_genai",
        "mapbox",
        "maptiler",
    ):
        rec = _record(provider=provider)
        assert rec.provider == provider




def test_secrets_payloads_exposed_via_module_registries() -> None:
    """All three message types land in the secrets module-level registries.

    ``SECRET_CLIENT_TO_AGENT_PAYLOADS`` / ``SECRET_AGENT_TO_CLIENT_PAYLOADS`` /
    ``SECRET_PAYLOADS`` are what the ws routing dicts splat in."""
    # Client -> agent (add, revoke)
    assert "secret-add" in secrets.SECRET_CLIENT_TO_AGENT_PAYLOADS
    assert "secret-revoke" in secrets.SECRET_CLIENT_TO_AGENT_PAYLOADS
    assert (
        secrets.SECRET_CLIENT_TO_AGENT_PAYLOADS["secret-add"]
        is SecretAddEnvelopePayload
    )
    assert (
        secrets.SECRET_CLIENT_TO_AGENT_PAYLOADS["secret-revoke"]
        is SecretRevokeEnvelopePayload
    )

    # Agent -> client (list)
    assert "secrets-list" in secrets.SECRET_AGENT_TO_CLIENT_PAYLOADS
    assert (
        secrets.SECRET_AGENT_TO_CLIENT_PAYLOADS["secrets-list"]
        is SecretsListEnvelopePayload
    )

    # Aggregated module-level registry
    for t in ("secret-add", "secret-revoke", "secrets-list"):
        assert t in secrets.SECRET_PAYLOADS, f"{t} missing from SECRET_PAYLOADS"




def test_secret_record_no_cost_field_invariant9() -> None:
    """Extra fields like cost_usd / estimated_cost are rejected by extra='forbid'."""
    base = _record().model_dump(mode="json")
    for forbidden in (
        "cost_usd",
        "estimated_cost",
        "cost_per_call",
        "quota_remaining",
        "monthly_spend",
    ):
        bad = {**base, forbidden: 0.0}
        with pytest.raises(ValidationError, match="(?i)extra"):
            SecretRecord.model_validate(bad)

    # Same for the add envelope
    add_base = SecretAddEnvelopePayload(
        provider="firms", case_id=new_ulid(), key_value="x"
    ).model_dump(mode="json")
    for forbidden in ("cost_usd", "estimated_cost"):
        bad = {**add_base, forbidden: 0.0}
        with pytest.raises(ValidationError):
            SecretAddEnvelopePayload.model_validate(bad)




def test_secret_add_keeps_key_value_accessible_for_server_write() -> None:
    """The repr elision is a leak-minimisation back-stop, not data hiding.

    It affects ``__repr__`` and ``{x!r}`` only, never attribute access or
    ``model_dump``, both of which the server needs to write the value."""
    sensitive = "SERVER-MUST-READ-THIS-KEY-123"
    add = SecretAddEnvelopePayload(
        provider="anthropic",
        case_id=new_ulid(),
        key_value=sensitive,
    )

    # 1. Attribute access still returns the raw value (server-side read path)
    assert add.key_value == sensitive

    # 2. model_dump still serialises the raw value (the wire form the server
    #    receives and the persistence-layer redactor consumes).
    dumped = add.model_dump(mode="json")
    assert dumped["key_value"] == sensitive

    # 3. model_dump_json also serialises the raw value
    json_str = add.model_dump_json()
    assert sensitive in json_str  # the wire form CARRIES the secret in transit

    # But default repr does NOT
    assert sensitive not in repr(add)
