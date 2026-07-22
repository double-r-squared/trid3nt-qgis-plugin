"""Unit + integration tests for ``grace2_agent.secrets_handler`` (file vault).

TRID3NT is the local product: the cloud vaults (GCP Secret Manager, AWS SSM
Parameter Store) are removed. Secrets are stored one-per-file under the
file-persistence data root at ``<root>/secrets/<user_id>/<provider>/<leaf>``
(mode 0600) and referenced as ``file-vault://<user>/<provider>/<leaf>``.

Coverage:

Store / read-back / delete:
1.  ``test_secret_add_writes_file_vault_and_persists_record``
2.  ``test_get_secret_value_returns_original_key`` -- round-trip through the
    UNCHANGED ``Persistence.get_secret_value`` routing (the card-retry seam).
3.  ``test_secret_re_add_is_collision_free``
4.  ``test_file_delete_secret_purges_entry``
5.  ``test_env_flags_do_not_reroute_writes`` -- no cloud fork remains.

Missing-secret card flow (typed, never crash / silent empty):
6.  ``test_missing_vault_entry_raises_typed_error``
7.  ``test_legacy_cloud_refs_raise_typed_missing_secret``
8.  ``test_legacy_aws_ref_via_persistence_hits_typed_path``
9.  ``test_malformed_ref_raises_typed_error`` (incl. traversal attempts)
10. ``test_get_secret_value_raises_on_revoked``

Wire / isolation invariants (Decision F):
11. ``test_secret_add_never_logs_or_echoes_key_value``
12. ``test_secrets_list_no_key_value_field``
13. ``test_secret_add_appends_audit_log``
14. ``test_secret_revoke_appends_audit_log``
15. ``test_multi_tenant_isolation_list``
16. ``test_secret_add_empty_user_id_fail_closed``
17. ``test_secret_add_empty_key_value_fail_closed``
18. ``test_secret_add_rejects_pathy_user_id``

Integration:
19. ``test_full_lifecycle_add_list_use_revoke_list``
"""

from __future__ import annotations

import asyncio
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grace2_agent.persistence import (
    SECRETS_COLLECTION,
    Persistence,
)
from grace2_agent.secrets_handler import (
    FILE_VAULT_SCHEME,
    SecretError,
    SecretNotFoundError,
    SecretRevokedError,
    _file_delete_secret,
    handle_secret_add,
    handle_secret_revoke,
    handle_secrets_list,
    read_secret_value,
)
from grace2_contracts.common import new_ulid
from grace2_contracts.secrets import (
    SecretAddEnvelopePayload,
    SecretRecord,
    SecretsListEnvelopePayload,
)

# Reuse the MockMCPClient from the Persistence test suite -- same shape.
from .test_persistence import MockMCPClient


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _vault_env(monkeypatch, tmp_path):
    """Point the file vault at a tmp persistence root for every test."""
    monkeypatch.setenv("GRACE2_DEV_PERSISTENCE_DIR", str(tmp_path))
    yield tmp_path


def _env_add(provider: str = "ebird", key: str = "test-ebird-key-DO-NOT-LOG",
             case_id: str | None = None) -> SecretAddEnvelopePayload:
    return SecretAddEnvelopePayload(
        provider=provider,  # type: ignore[arg-type]
        case_id=case_id or new_ulid(),
        label=f"test {provider} key",
        key_value=key,
    )


def _run(coro):
    return asyncio.run(coro)


def _make_persistence() -> tuple[Persistence, MockMCPClient]:
    mcp = MockMCPClient()
    return Persistence(mcp), mcp


def _vault_file_for(record: SecretRecord, root: Path) -> Path:
    key_name = record.vault_ref[len(FILE_VAULT_SCHEME):]
    return root / "secrets" / Path(*key_name.split("/"))


def _ghost_record(vault_ref: str) -> SecretRecord:
    return SecretRecord(
        secret_id=new_ulid(),
        provider="ebird",
        case_id=new_ulid(),
        vault_ref=vault_ref,
        label="ghost",
        added_at=datetime.now(timezone.utc),
        last_used_at=None,
        is_active=True,
    )


# --------------------------------------------------------------------------- #
# Store / read-back / delete
# --------------------------------------------------------------------------- #


def test_secret_add_writes_file_vault_and_persists_record(_vault_env) -> None:
    """Full add lifecycle: 0600 secret file + vault-ref-only SecretRecord."""
    p, mcp = _make_persistence()
    user_id = new_ulid()
    envelope = _env_add(provider="ebird", key="ebird-key-abc-123")

    record = _run(handle_secret_add(envelope, user_id=user_id, persistence=p))

    # Returned a vault-ref-only SecretRecord (no key_value field).
    assert isinstance(record, SecretRecord)
    assert record.provider == "ebird"
    assert record.case_id == envelope.case_id
    assert record.is_active is True

    # Ref shape: file-vault://<user>/<provider>/<leaf>.
    assert record.vault_ref.startswith(FILE_VAULT_SCHEME)
    key_name = record.vault_ref[len(FILE_VAULT_SCHEME):]
    segs = key_name.split("/")
    assert segs[0] == user_id
    assert segs[1] == "ebird"
    assert len(segs) == 3

    # The secret file exists under <root>/secrets/<user>/<provider>/, is
    # owner-only (0600), and holds exactly the raw value.
    vault_file = _vault_file_for(record, _vault_env)
    assert vault_file.exists()
    mode = stat.S_IMODE(vault_file.stat().st_mode)
    assert mode == 0o600, f"vault file mode is {oct(mode)}, expected 0o600"
    assert vault_file.read_text(encoding="utf-8") == "ebird-key-abc-123"

    # Persistence: secrets collection got the upsert (vault-ref only).
    upserts = [
        a for n, a in mcp.calls
        if a.get("collection") == SECRETS_COLLECTION
        and n == "update-one" and a.get("upsert") is True
    ]
    assert upserts, "no secrets-collection upsert recorded"


def test_get_secret_value_returns_original_key(_vault_env) -> None:
    """Round-trip THROUGH Persistence.get_secret_value (the card-retry seam)."""
    p, _ = _make_persistence()
    record = _run(
        handle_secret_add(
            _env_add(key="round-trip-test-value"), user_id=new_ulid(),
            persistence=p,
        )
    )
    fetched = _run(p.get_secret_value(record))
    assert fetched == "round-trip-test-value"
    # And via the module's own read seam.
    assert read_secret_value(record.vault_ref) == "round-trip-test-value"


def test_secret_re_add_is_collision_free(_vault_env) -> None:
    """Re-adding for the same user/provider yields distinct refs + files."""
    p, _ = _make_persistence()
    user_id = new_ulid()
    case_id = new_ulid()
    rec_a = _run(
        handle_secret_add(
            _env_add(provider="ebird", key="first", case_id=case_id),
            user_id=user_id, persistence=p,
        )
    )
    rec_b = _run(
        handle_secret_add(
            _env_add(provider="ebird", key="second", case_id=case_id),
            user_id=user_id, persistence=p,
        )
    )
    assert rec_a.vault_ref != rec_b.vault_ref
    assert read_secret_value(rec_a.vault_ref) == "first"
    assert read_secret_value(rec_b.vault_ref) == "second"


def test_file_delete_secret_purges_entry(_vault_env) -> None:
    """Hard-purge helper removes only its entry; legacy refs are a no-op."""
    p, _ = _make_persistence()
    rec_keep = _run(
        handle_secret_add(
            _env_add(provider="ebird", key="keep-me"), user_id=new_ulid(),
            persistence=p,
        )
    )
    rec_purge = _run(
        handle_secret_add(
            _env_add(provider="openweathermap", key="purge-me"),
            user_id=new_ulid(), persistence=p,
        )
    )
    _file_delete_secret(rec_purge.vault_ref)
    assert not _vault_file_for(rec_purge, _vault_env).exists()
    assert read_secret_value(rec_keep.vault_ref) == "keep-me"
    with pytest.raises(SecretNotFoundError):
        read_secret_value(rec_purge.vault_ref)
    # Non-file-vault refs are ignored (best-effort no-op).
    _file_delete_secret("aws-ssm:///grace2/secrets/u/p/x")


def test_env_flags_do_not_reroute_writes(_vault_env, monkeypatch) -> None:
    """No cloud fork remains: cloud-ish env flags still write the file vault."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    p, _ = _make_persistence()
    record = _run(
        handle_secret_add(
            _env_add(key="always-local"), user_id=new_ulid(), persistence=p,
        )
    )
    assert record.vault_ref.startswith(FILE_VAULT_SCHEME)
    assert _vault_file_for(record, _vault_env).exists()
    assert _run(p.get_secret_value(record)) == "always-local"


# --------------------------------------------------------------------------- #
# Missing-secret card flow (typed, never crash / silent empty)
# --------------------------------------------------------------------------- #


def test_missing_vault_entry_raises_typed_error(_vault_env) -> None:
    """A dangling file-vault ref -> SecretNotFoundError (card re-prompt)."""
    p, _ = _make_persistence()
    ghost = _ghost_record("file-vault://nobody/ebird/deadbeefdead")
    with pytest.raises(SecretNotFoundError):
        _run(p.get_secret_value(ghost))
    with pytest.raises(SecretNotFoundError):
        read_secret_value(ghost.vault_ref)


def test_legacy_cloud_refs_raise_typed_missing_secret(_vault_env) -> None:
    """aws-ssm:// / GCP / interim local-file:// refs -> typed missing-secret.

    Honest handling: none of these can resolve on the local build. The read
    seam raises SecretNotFoundError (fetchers treat it as missing key -> the
    credential-request card re-prompts) -- never a crash, never an empty
    string, and no cloud SDK import.
    """
    legacy_refs = [
        "aws-ssm:///grace2/secrets/user/ebird/abc123",
        "gcp-sm://projects/p/secrets/s/versions/latest",
        "projects/p/secrets/s/versions/latest",  # bare GCP resource name
        "local-file://user/ebird/abc123",  # interim JSON-store scheme
    ]
    for ref in legacy_refs:
        with pytest.raises(SecretNotFoundError):
            read_secret_value(ref)


def test_legacy_aws_ref_via_persistence_hits_typed_path(_vault_env) -> None:
    """An aws-ssm:// ref read through Persistence.get_secret_value stays typed.

    persistence.py still routes aws-ssm:// refs to a default-client builder;
    on the local build that builder raises SecretNotFoundError instead of
    constructing boto3 -- the record re-prompts via the card, no crash.
    """
    p, _ = _make_persistence()
    ghost = _ghost_record("aws-ssm:///grace2/secrets/user/ebird/abc123")
    with pytest.raises(SecretNotFoundError):
        _run(p.get_secret_value(ghost))


def test_malformed_ref_raises_typed_error(_vault_env) -> None:
    """Malformed / hostile refs -> typed error; traversal never escapes."""
    outside = _vault_env / "outside.txt"
    outside.write_text("not-a-secret", encoding="utf-8")
    bad_refs = [
        "",
        "bogus://x/y/z",
        "file-vault://",
        "file-vault://single-segment",
        "file-vault://user//leaf",  # empty segment
        "file-vault://../outside.txt",
        "file-vault://user/../../outside.txt",
        "file-vault://user/ebird/../../../outside.txt",
    ]
    for ref in bad_refs:
        with pytest.raises((SecretNotFoundError, SecretError)):
            read_secret_value(ref)


def test_get_secret_value_raises_on_revoked(_vault_env) -> None:
    """A revoked record yields SecretRevokedError before touching the vault."""
    p, _ = _make_persistence()
    record = _run(
        handle_secret_add(
            _env_add(key="will-be-revoked"), user_id=new_ulid(), persistence=p,
        )
    )
    revoked = record.model_copy(update={"is_active": False})
    with pytest.raises(SecretRevokedError):
        _run(p.get_secret_value(revoked))
    # The vault file itself is untouched by a soft revoke.
    assert _vault_file_for(record, _vault_env).exists()


# --------------------------------------------------------------------------- #
# Wire / isolation invariants (Decision F)
# --------------------------------------------------------------------------- #


def test_secret_add_never_logs_or_echoes_key_value(_vault_env, caplog) -> None:
    """Decision F leak check: the raw key value never appears in logs."""
    p, _ = _make_persistence()
    sentinel_key = "SUPER-SECRET-LEAK-SENTINEL-XYZ-987"
    with caplog.at_level("DEBUG"):
        record = _run(
            handle_secret_add(
                _env_add(key=sentinel_key), user_id=new_ulid(), persistence=p,
            )
        )
        _run(p.get_secret_value(record))
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel_key not in full_log, (
        f"key_value leaked into log output: {full_log!r}"
    )
    # And the ref itself carries no key material.
    assert sentinel_key not in record.vault_ref


def test_secrets_list_no_key_value_field(_vault_env) -> None:
    """The reply payload's SecretRecord entries carry only the vault_ref."""
    p, _ = _make_persistence()
    user_id = new_ulid()
    case_id = new_ulid()

    _run(
        handle_secret_add(
            _env_add(provider="ebird", key="k1", case_id=case_id),
            user_id=user_id, persistence=p,
        )
    )
    _run(
        handle_secret_add(
            _env_add(provider="iucn_red_list", key="k2", case_id=case_id),
            user_id=user_id, persistence=p,
        )
    )

    payload = _run(
        handle_secrets_list(user_id=user_id, case_id=case_id, persistence=p)
    )
    assert isinstance(payload, SecretsListEnvelopePayload)
    assert len(payload.secrets) == 2

    # Wire-payload audit: no field named "key_value" anywhere.
    wire_dict = payload.model_dump(mode="json")
    for record in wire_dict["secrets"]:
        for k in record.keys():
            assert k != "key_value", f"key_value field surfaced: {record!r}"
        # And neither k1 nor k2 appears as a value anywhere.
        for v in record.values():
            assert v != "k1" and v != "k2"


def test_secret_add_appends_audit_log(_vault_env) -> None:
    """An ``audit_log`` insert lands per secret-add."""
    p, mcp = _make_persistence()
    user_id = new_ulid()
    _run(handle_secret_add(_env_add(), user_id=user_id, persistence=p))

    audit_inserts = [
        a for n, a in mcp.calls
        if n == "insert-one" and a.get("collection") == "audit_log"
    ]
    assert audit_inserts, "no audit_log insert recorded"
    doc = audit_inserts[0]["document"]
    assert doc["event_type"] == "secret-add"
    assert doc["payload"]["user_id"] == user_id
    # The audit-log payload includes vault_ref and provider but NOT key_value.
    assert "vault_ref" in doc["payload"]
    assert "key_value" not in doc["payload"]


def test_secret_revoke_appends_audit_log(_vault_env) -> None:
    """secret-revoke flips is_active=False and writes an audit-log row."""
    p, mcp = _make_persistence()
    user_id = new_ulid()
    record = _run(handle_secret_add(_env_add(), user_id=user_id, persistence=p))

    _run(handle_secret_revoke(record.secret_id, user_id=user_id, persistence=p))

    # The list-active-only call returns 0 records after revoke.
    payload = _run(handle_secrets_list(user_id=user_id, persistence=p))
    assert len(payload.secrets) == 0

    audit_inserts = [
        a for n, a in mcp.calls
        if n == "insert-one" and a.get("collection") == "audit_log"
    ]
    event_types = [a["document"]["event_type"] for a in audit_inserts]
    assert "secret-revoke" in event_types


def test_multi_tenant_isolation_list(_vault_env) -> None:
    """User A's secret-list excludes records added by User B."""
    p, _ = _make_persistence()
    user_a = new_ulid()
    user_b = new_ulid()

    _run(
        handle_secret_add(
            _env_add(provider="ebird", key="a-key"),
            user_id=user_a, persistence=p,
        )
    )
    _run(
        handle_secret_add(
            _env_add(provider="iucn_red_list", key="b-key"),
            user_id=user_b, persistence=p,
        )
    )

    a_list = _run(handle_secrets_list(user_id=user_a, persistence=p))
    b_list = _run(handle_secrets_list(user_id=user_b, persistence=p))

    assert {s.provider for s in a_list.secrets} == {"ebird"}
    assert {s.provider for s in b_list.secrets} == {"iucn_red_list"}


def test_secret_add_empty_user_id_fail_closed(_vault_env) -> None:
    """An empty user_id raises before any vault write."""
    p, _ = _make_persistence()
    with pytest.raises(SecretError):
        _run(handle_secret_add(_env_add(), user_id="", persistence=p))
    # Critically: nothing was written to the vault.
    assert not (_vault_env / "secrets").exists()


def test_secret_add_empty_key_value_fail_closed(_vault_env) -> None:
    """An empty key_value raises before touching the vault."""
    p, _ = _make_persistence()
    envelope = SecretAddEnvelopePayload(
        provider="ebird",
        case_id=new_ulid(),
        label="empty key test",
        key_value="",
    )
    with pytest.raises(SecretError):
        _run(handle_secret_add(envelope, user_id=new_ulid(), persistence=p))
    assert not (_vault_env / "secrets").exists()


def test_secret_add_rejects_pathy_user_id(_vault_env) -> None:
    """A path-unsafe user_id fails closed; nothing lands outside the vault."""
    p, _ = _make_persistence()
    for bad_user in ("../evil", "a/b", "..", "user id with spaces"):
        with pytest.raises(SecretError):
            _run(
                handle_secret_add(
                    _env_add(key="never-stored"), user_id=bad_user,
                    persistence=p,
                )
            )
    assert not (_vault_env / "secrets").exists()


# --------------------------------------------------------------------------- #
# Integration: full lifecycle
# --------------------------------------------------------------------------- #


def test_full_lifecycle_add_list_use_revoke_list(_vault_env) -> None:
    """End-to-end: add -> list (1) -> use (round-trip) -> revoke -> list (0)."""
    p, _ = _make_persistence()
    user_id = new_ulid()
    case_id = new_ulid()
    envelope = _env_add(provider="openweathermap", key="lifecycle-test-key",
                        case_id=case_id)

    # 1. Add
    record = _run(handle_secret_add(envelope, user_id=user_id, persistence=p))

    # 2. List -- one active record
    lst1 = _run(
        handle_secrets_list(user_id=user_id, case_id=case_id, persistence=p)
    )
    assert len(lst1.secrets) == 1
    assert lst1.secrets[0].secret_id == record.secret_id

    # 3. Use -- Tier-2-fetcher-style read
    assert _run(p.get_secret_value(record)) == "lifecycle-test-key"

    # 4. Revoke
    _run(handle_secret_revoke(record.secret_id, user_id=user_id, persistence=p))

    # 5. List again -- 0 active records
    lst2 = _run(
        handle_secrets_list(user_id=user_id, case_id=case_id, persistence=p)
    )
    assert len(lst2.secrets) == 0

    # Vault entry intact (audit trail) -- revoke is soft. We bypass the
    # is_active guard by constructing a synthetic active-shaped record
    # pointing at the same vault_ref.
    audit_resurrect = SecretRecord(
        secret_id=record.secret_id,
        provider=record.provider,
        case_id=record.case_id,
        vault_ref=record.vault_ref,
        added_at=datetime.now(timezone.utc),
        is_active=True,
    )
    assert _run(p.get_secret_value(audit_resurrect)) == "lifecycle-test-key", (
        "vault entry must persist for audit trail"
    )
