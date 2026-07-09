"""Unit + integration tests for ``grace2_agent.secrets_handler`` (job-0124).

Coverage (per the kickoff: ≥8 unit + 1 integration):

Unit (mocked Secret Manager + Persistence):
1. ``test_secret_add_writes_vault_and_persists_record`` — full add lifecycle.
2. ``test_secret_add_never_logs_or_echoes_key_value`` — Decision F leak check.
3. ``test_get_secret_value_returns_original_key`` — round-trip via vault.
4. ``test_get_secret_value_raises_on_revoked`` — typed SecretRevokedError.
5. ``test_secrets_list_no_key_value_field`` — wire-payload audit.
6. ``test_secret_add_appends_audit_log`` — audit-log row created.
7. ``test_secret_revoke_appends_audit_log`` — revoke audit-log row created.
8. ``test_multi_tenant_isolation_list`` — user A's list excludes user B's records.
9. ``test_secret_add_empty_user_id_fail_closed`` — multi-tenant guardrail.
10. ``test_secret_add_empty_key_value_fail_closed`` — never write a zero-byte version.

Integration:
11. ``test_full_lifecycle_add_list_use_revoke_list`` — add -> list -> use ->
    revoke -> list-again, end-to-end with mocked Secret Manager.

Live (env-gated, GRACE2_TEST_LIVE_SECRETS=1):
12. ``test_live_secret_manager_roundtrip_or_skip`` — real Secret Manager
    add/get/revoke against a test GCP project.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest

from grace2_agent.persistence import (
    SECRETS_COLLECTION,
    Persistence,
)
from grace2_agent.secrets_handler import (
    SecretRevokedError,
    handle_secret_add,
    handle_secret_revoke,
    handle_secrets_list,
)
from grace2_contracts.common import new_ulid
from grace2_contracts.secrets import (
    SecretAddEnvelopePayload,
    SecretRecord,
    SecretsListEnvelopePayload,
)

# Reuse the MockMCPClient from the Persistence test suite — same shape.
from .test_persistence import MockMCPClient


# --------------------------------------------------------------------------- #
# Mock Secret Manager client
# --------------------------------------------------------------------------- #


class _FakeSecretPayload:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakeSecretVersion:
    def __init__(self, name: str, payload: _FakeSecretPayload) -> None:
        self.name = name
        self.payload = payload


class MockSecretManagerClient:
    """Drop-in replacement for ``SecretManagerServiceClient``.

    Implements ``create_secret``, ``add_secret_version``, and
    ``access_secret_version`` — the exact three calls the handler uses.
    Records every call so tests can assert routing.
    """

    def __init__(self) -> None:
        # parent -> secret_id -> list[bytes] (version payloads, latest last)
        self._store: dict[str, dict[str, list[bytes]]] = {}
        self.calls: list[tuple[str, dict]] = []

    def create_secret(self, request: dict) -> dict:
        self.calls.append(("create_secret", dict(request)))
        parent = request["parent"]
        secret_id = request["secret_id"]
        self._store.setdefault(parent, {}).setdefault(secret_id, [])
        return {"name": f"{parent}/secrets/{secret_id}"}

    def add_secret_version(self, request: dict):
        self.calls.append(("add_secret_version", dict(request)))
        parent = request["parent"]  # projects/X/secrets/<secret_id>
        # parse "projects/X/secrets/Y" -> ("projects/X", "Y")
        project_part, _, secret_id = parent.rpartition("/secrets/")
        data = request["payload"]["data"]
        versions = self._store.setdefault(project_part, {}).setdefault(
            secret_id, []
        )
        versions.append(data)
        version_number = len(versions)
        name = f"{parent}/versions/{version_number}"
        return _FakeSecretVersion(name=name, payload=_FakeSecretPayload(data))

    def access_secret_version(self, request: dict):
        self.calls.append(("access_secret_version", dict(request)))
        # name shape: projects/X/secrets/Y/versions/{N|latest}
        name = request["name"]
        prefix, _, version_sel = name.rpartition("/versions/")
        project_part, _, secret_id = prefix.rpartition("/secrets/")
        versions = self._store.get(project_part, {}).get(secret_id, [])
        if not versions:
            raise RuntimeError(f"mock: no versions for {name!r}")
        if version_sel == "latest":
            data = versions[-1]
        else:
            data = versions[int(version_sel) - 1]
        return _FakeSecretVersion(name=name, payload=_FakeSecretPayload(data))


class MockSSMClient:
    """Drop-in replacement for ``boto3.client("ssm")``.

    Implements ``put_parameter``, ``get_parameter`` (with ``WithDecryption``),
    and ``delete_parameter`` — the exact three calls the AWS secret backend
    uses. Records every call so tests can assert routing + KMS encryption.
    """

    def __init__(self) -> None:
        # param-name -> {"Value": str, "Type": str, "KeyId": str | None}
        self._store: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []

    def put_parameter(self, **kwargs):  # noqa: ANN003
        self.calls.append(("put_parameter", dict(kwargs)))
        name = kwargs["Name"]
        if name in self._store and not kwargs.get("Overwrite", False):
            raise RuntimeError(f"mock: parameter {name!r} already exists")
        self._store[name] = {
            "Value": kwargs["Value"],
            "Type": kwargs.get("Type"),
            "KeyId": kwargs.get("KeyId"),
        }
        return {"Version": 1, "Tier": "Standard"}

    def get_parameter(self, **kwargs):  # noqa: ANN003
        self.calls.append(("get_parameter", dict(kwargs)))
        name = kwargs["Name"]
        if name not in self._store:
            raise RuntimeError(f"mock: parameter {name!r} not found")
        return {"Parameter": {"Name": name, "Value": self._store[name]["Value"]}}

    def delete_parameter(self, **kwargs):  # noqa: ANN003
        self.calls.append(("delete_parameter", dict(kwargs)))
        self._store.pop(kwargs["Name"], None)
        return {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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


def _make_persistence_and_secret_mgr() -> tuple[
    Persistence, MockMCPClient, MockSecretManagerClient
]:
    mcp = MockMCPClient()
    p = Persistence(mcp)
    sm = MockSecretManagerClient()
    return p, mcp, sm


# --------------------------------------------------------------------------- #
# Unit tests
# --------------------------------------------------------------------------- #


def test_secret_add_writes_vault_and_persists_record() -> None:
    """Full add lifecycle: Secret Manager + MongoDB both touched correctly."""
    p, mcp, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    envelope = _env_add(provider="ebird", key="ebird-key-abc-123")

    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p,
            secret_manager_client=sm, gcp_project="test-project",
        )
    )

    # Returned a vault-ref-only SecretRecord (no key_value field).
    assert isinstance(record, SecretRecord)
    assert record.provider == "ebird"
    assert record.case_id == envelope.case_id
    assert record.is_active is True
    assert record.vault_ref.startswith("projects/test-project/secrets/")
    assert record.vault_ref.endswith("/versions/latest")

    # Secret Manager: create_secret + add_secret_version both invoked.
    sm_methods = [c[0] for c in sm.calls]
    assert "create_secret" in sm_methods
    assert "add_secret_version" in sm_methods

    # The raw key value made it into the vault.
    create_kwargs = next(c[1] for c in sm.calls if c[0] == "create_secret")
    assert create_kwargs["parent"] == "projects/test-project"
    version_kwargs = next(
        c[1] for c in sm.calls if c[0] == "add_secret_version"
    )
    assert version_kwargs["payload"]["data"] == b"ebird-key-abc-123"

    # MongoDB: secrets collection has the SecretRecord, audit_log has the entry.
    secrets_calls = [
        (n, a) for n, a in mcp.calls
        if a.get("collection") == SECRETS_COLLECTION
    ]
    assert secrets_calls, "no MCP calls to secrets collection"
    # At least one upsert (update-one + upsert=True)
    upserts = [
        a for n, a in secrets_calls
        if n == "update-one" and a.get("upsert") is True
    ]
    assert upserts


def test_secret_add_never_logs_or_echoes_key_value(caplog) -> None:
    """Decision F leak check: the raw key value must not appear in logs."""
    p, _, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    sentinel_key = "SUPER-SECRET-LEAK-SENTINEL-XYZ-987"
    envelope = _env_add(key=sentinel_key)

    with caplog.at_level("DEBUG", logger="grace2_agent.secrets_handler"):
        _run(
            handle_secret_add(
                envelope, user_id=user_id, persistence=p,
                secret_manager_client=sm,
            )
        )

    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel_key not in full_log, (
        f"key_value leaked into log output: {full_log!r}"
    )


def test_get_secret_value_returns_original_key() -> None:
    """Round-trip: add a secret, then read the value back via Persistence."""
    p, _, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    envelope = _env_add(key="round-trip-test-value")

    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p,
            secret_manager_client=sm, gcp_project="rt-project",
        )
    )

    fetched = _run(
        p.get_secret_value(record, secret_manager_client=sm)
    )
    assert fetched == "round-trip-test-value"


def test_get_secret_value_raises_on_revoked() -> None:
    """A revoked secret yields SecretRevokedError before touching the vault."""
    p, _, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    envelope = _env_add(key="will-be-revoked")
    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p,
            secret_manager_client=sm,
        )
    )
    # Soft-revoke the record.
    revoked = record.model_copy(update={"is_active": False})

    with pytest.raises(SecretRevokedError):
        _run(p.get_secret_value(revoked, secret_manager_client=sm))


def test_secrets_list_no_key_value_field() -> None:
    """The reply payload's SecretRecord entries carry only the vault_ref."""
    p, _, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    case_id = new_ulid()

    _run(
        handle_secret_add(
            _env_add(provider="ebird", key="k1", case_id=case_id),
            user_id=user_id, persistence=p, secret_manager_client=sm,
        )
    )
    _run(
        handle_secret_add(
            _env_add(provider="iucn_red_list", key="k2", case_id=case_id),
            user_id=user_id, persistence=p, secret_manager_client=sm,
        )
    )

    payload = _run(
        handle_secrets_list(
            user_id=user_id, case_id=case_id, persistence=p,
        )
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


def test_secret_add_appends_audit_log() -> None:
    """An ``audit_log`` insert lands per secret-add."""
    p, mcp, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    _run(
        handle_secret_add(
            _env_add(), user_id=user_id, persistence=p,
            secret_manager_client=sm,
        )
    )

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


def test_secret_revoke_appends_audit_log() -> None:
    """secret-revoke flips is_active=False and writes an audit-log row."""
    p, mcp, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    record = _run(
        handle_secret_add(
            _env_add(), user_id=user_id, persistence=p,
            secret_manager_client=sm,
        )
    )

    _run(
        handle_secret_revoke(
            record.secret_id, user_id=user_id, persistence=p,
        )
    )

    # The list-active-only call returns 0 records after revoke.
    payload = _run(
        handle_secrets_list(user_id=user_id, persistence=p)
    )
    assert len(payload.secrets) == 0

    audit_inserts = [
        a for n, a in mcp.calls
        if n == "insert-one" and a.get("collection") == "audit_log"
    ]
    # at least secret-add + secret-revoke
    event_types = [a["document"]["event_type"] for a in audit_inserts]
    assert "secret-revoke" in event_types


def test_multi_tenant_isolation_list() -> None:
    """User A's secret-list excludes records added by User B."""
    p, _, sm = _make_persistence_and_secret_mgr()
    user_a = new_ulid()
    user_b = new_ulid()

    _run(
        handle_secret_add(
            _env_add(provider="ebird", key="a-key"),
            user_id=user_a, persistence=p, secret_manager_client=sm,
        )
    )
    _run(
        handle_secret_add(
            _env_add(provider="iucn_red_list", key="b-key"),
            user_id=user_b, persistence=p, secret_manager_client=sm,
        )
    )

    a_list = _run(handle_secrets_list(user_id=user_a, persistence=p))
    b_list = _run(handle_secrets_list(user_id=user_b, persistence=p))

    a_providers = {s.provider for s in a_list.secrets}
    b_providers = {s.provider for s in b_list.secrets}
    assert a_providers == {"ebird"}
    assert b_providers == {"iucn_red_list"}


def test_secret_add_empty_user_id_fail_closed() -> None:
    """An empty user_id raises before any vault write — multi-tenant guardrail."""
    p, _, sm = _make_persistence_and_secret_mgr()
    with pytest.raises(Exception):
        _run(
            handle_secret_add(
                _env_add(), user_id="", persistence=p,
                secret_manager_client=sm,
            )
        )
    # Critically: no Secret Manager call was made.
    assert not sm.calls


def test_secret_add_empty_key_value_fail_closed() -> None:
    """An empty key_value raises before touching Secret Manager."""
    p, _, sm = _make_persistence_and_secret_mgr()
    envelope = SecretAddEnvelopePayload(
        provider="ebird",
        case_id=new_ulid(),
        label="empty key test",
        key_value="",
    )
    with pytest.raises(Exception):
        _run(
            handle_secret_add(
                envelope, user_id=new_ulid(), persistence=p,
                secret_manager_client=sm,
            )
        )
    assert not sm.calls


# --------------------------------------------------------------------------- #
# AWS SSM Parameter Store backend (NATE 2026-06-17 demo blocker fix)
# --------------------------------------------------------------------------- #


@pytest.fixture
def _aws_backend(monkeypatch):
    """Select the AWS secret backend for the duration of a test."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "aws")
    yield


def test_aws_secret_add_writes_to_ssm_securestring(_aws_backend) -> None:
    """On the AWS backend, secret-add writes a KMS-encrypted SecureString."""
    p, mcp, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    user_id = new_ulid()
    envelope = _env_add(provider="ebird", key="ebird-aws-key-xyz")

    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p, ssm_client=ssm,
        )
    )

    # vault_ref is the aws-ssm:// scheme pointing under /grace2/secrets/<user>.
    assert record.vault_ref.startswith("aws-ssm:///grace2/secrets/")
    assert user_id in record.vault_ref
    assert "ebird" in record.vault_ref

    # SSM put_parameter was called as a SecureString (KMS-encrypted at rest).
    puts = [a for n, a in ssm.calls if n == "put_parameter"]
    assert len(puts) == 1
    assert puts[0]["Type"] == "SecureString"
    assert puts[0]["Value"] == "ebird-aws-key-xyz"
    assert puts[0]["Name"].startswith("/grace2/secrets/")

    # MongoDB SecretRecord persisted (vault-ref only — Decision F backstop).
    secrets_calls = [
        a for n, a in mcp.calls
        if a.get("collection") == SECRETS_COLLECTION
        and n == "update-one" and a.get("upsert") is True
    ]
    assert secrets_calls


def test_aws_get_secret_value_round_trips_via_ssm(_aws_backend) -> None:
    """Round-trip: add to SSM, then read it back with WithDecryption=True."""
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    user_id = new_ulid()
    envelope = _env_add(key="aws-round-trip-value")

    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p, ssm_client=ssm,
        )
    )
    fetched = _run(p.get_secret_value(record, ssm_client=ssm))
    assert fetched == "aws-round-trip-value"

    # The read decrypted the SecureString.
    gets = [a for n, a in ssm.calls if n == "get_parameter"]
    assert gets and gets[-1]["WithDecryption"] is True


def test_aws_get_secret_value_raises_on_revoked(_aws_backend) -> None:
    """A revoked AWS-backed record raises before touching SSM."""
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    user_id = new_ulid()
    record = _run(
        handle_secret_add(
            _env_add(key="aws-will-revoke"), user_id=user_id,
            persistence=p, ssm_client=ssm,
        )
    )
    revoked = record.model_copy(update={"is_active": False})
    # Fresh SSM client to prove we never call get_parameter.
    ssm2 = MockSSMClient()
    with pytest.raises(SecretRevokedError):
        _run(p.get_secret_value(revoked, ssm_client=ssm2))
    assert not ssm2.calls


def test_aws_backend_never_logs_key(_aws_backend, caplog) -> None:
    """Decision F leak check on the AWS path: raw key never appears in logs."""
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    sentinel = "AWS-LEAK-SENTINEL-KEY-55512"
    with caplog.at_level("DEBUG", logger="grace2_agent.secrets_handler"):
        _run(
            handle_secret_add(
                _env_add(key=sentinel), user_id=new_ulid(),
                persistence=p, ssm_client=ssm,
            )
        )
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel not in full_log


def test_aws_backend_honors_custom_kms_key(_aws_backend, monkeypatch) -> None:
    """A configured CMK is passed to put_parameter as KeyId."""
    monkeypatch.setenv("GRACE2_SECRETS_KMS_KEY_ID", "alias/grace2-secrets")
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    _run(
        handle_secret_add(
            _env_add(key="cmk-key"), user_id=new_ulid(),
            persistence=p, ssm_client=ssm,
        )
    )
    put = next(a for n, a in ssm.calls if n == "put_parameter")
    assert put["KeyId"] == "alias/grace2-secrets"


def test_backend_selection_honors_storage_backend(monkeypatch) -> None:
    """Selection helper routes AWS for {s3,aws}; GCP (False) otherwise/unset."""
    from grace2_agent.secrets_handler import _aws_secret_backend_selected

    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    assert _aws_secret_backend_selected() is False  # unset => GCP default
    for v in ("aws", "s3", "AWS", "S3", " aws "):
        monkeypatch.setenv("GRACE2_STORAGE_BACKEND", v)
        assert _aws_secret_backend_selected() is True
    for v in ("gcp", "", "gcs", "local"):
        monkeypatch.setenv("GRACE2_STORAGE_BACKEND", v)
        assert _aws_secret_backend_selected() is False


def test_gcp_path_unchanged_when_backend_unset(monkeypatch) -> None:
    """With no GRACE2_STORAGE_BACKEND, secret-add still uses GCP Secret Manager."""
    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    p, _, sm = _make_persistence_and_secret_mgr()
    # If the AWS branch were taken with no ssm_client it would lazy-import boto3
    # and fail to find a real param store; passing a GCP mock proves GCP routing.
    record = _run(
        handle_secret_add(
            _env_add(key="gcp-default-key"), user_id=new_ulid(),
            persistence=p, secret_manager_client=sm, gcp_project="gcp-proj",
        )
    )
    assert record.vault_ref.startswith("projects/gcp-proj/secrets/")
    assert record.vault_ref.endswith("/versions/latest")
    sm_methods = [c[0] for c in sm.calls]
    assert "create_secret" in sm_methods and "add_secret_version" in sm_methods
    # Round-trip read also stays on the GCP path.
    assert _run(p.get_secret_value(record, secret_manager_client=sm)) == (
        "gcp-default-key"
    )


def test_read_routes_by_vault_ref_scheme_not_env(monkeypatch) -> None:
    """A key written under AWS still resolves even if the env later flips.

    Reads route on the vault_ref scheme, not GRACE2_STORAGE_BACKEND.
    """
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "aws")
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    record = _run(
        handle_secret_add(
            _env_add(key="scheme-routed"), user_id=new_ulid(),
            persistence=p, ssm_client=ssm,
        )
    )
    # Flip the env to GCP — the aws-ssm:// ref must STILL read from SSM.
    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    assert _run(p.get_secret_value(record, ssm_client=ssm)) == "scheme-routed"


# --------------------------------------------------------------------------- #
# LOCAL file vault (fingerprint audit L8, NATE 2026-07-08)
# --------------------------------------------------------------------------- #
#
# The TRID3NT local build (GRACE2_SOLVER_BACKEND=local-docker, file
# persistence, no GCP ADC / AWS IAM) used to fall through to the GCP Secret
# Manager write path. Secret writes now land in a mode-0600
# ``secrets_vault.json`` under the persistence dir; reads route on the
# ``local-file://`` ref scheme. Cloud lanes stay byte-identical (the AWS/GCP
# tests above run with GRACE2_SOLVER_BACKEND unset).


@pytest.fixture
def _local_vault(monkeypatch, tmp_path):
    """Select the LOCAL file-vault backend against a tmp persistence dir."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setenv("GRACE2_DEV_PERSISTENCE_DIR", str(tmp_path))
    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    yield tmp_path


def test_local_secret_add_writes_file_vault_mode_0600(_local_vault) -> None:
    """Local add lands in secrets_vault.json (0600), never a cloud client."""
    import json as _json
    import stat as _stat

    p, mcp, sm = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    user_id = new_ulid()
    envelope = _env_add(provider="ebird", key="local-vault-key-abc")

    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p,
            secret_manager_client=sm, ssm_client=ssm,
        )
    )

    # vault_ref carries the local scheme + the user/provider key shape.
    assert record.vault_ref.startswith("local-file://")
    assert user_id in record.vault_ref
    assert "ebird" in record.vault_ref

    # Neither cloud client was touched (mocks were available and ignored).
    assert not sm.calls
    assert not ssm.calls

    # The vault file exists under the tmp persistence dir, is owner-only
    # (0600), and holds the raw value keyed by the ref's key-name.
    vault = _local_vault / "secrets_vault.json"
    assert vault.exists()
    mode = _stat.S_IMODE(vault.stat().st_mode)
    assert mode == 0o600, f"vault mode is {oct(mode)}, expected 0o600"
    store = _json.loads(vault.read_text(encoding="utf-8"))
    key_name = record.vault_ref[len("local-file://"):]
    assert store[key_name] == "local-vault-key-abc"

    # MongoDB SecretRecord persisted (vault-ref only -- Decision F backstop).
    secrets_calls = [
        a for n, a in mcp.calls
        if a.get("collection") == SECRETS_COLLECTION
        and n == "update-one" and a.get("upsert") is True
    ]
    assert secrets_calls


def test_local_get_secret_value_round_trips_via_file(_local_vault) -> None:
    """Round-trip: add locally, read back with NO cloud client available."""
    p, _, _ = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    record = _run(
        handle_secret_add(
            _env_add(key="local-round-trip-value"), user_id=user_id,
            persistence=p,
        )
    )
    # No secret_manager_client / ssm_client passed: the local-file:// branch
    # must resolve before either cloud lazy-constructor is reached.
    fetched = _run(p.get_secret_value(record))
    assert fetched == "local-round-trip-value"


def test_local_get_secret_value_raises_on_revoked(_local_vault) -> None:
    p, _, _ = _make_persistence_and_secret_mgr()
    record = _run(
        handle_secret_add(
            _env_add(key="local-will-revoke"), user_id=new_ulid(),
            persistence=p,
        )
    )
    revoked = record.model_copy(update={"is_active": False})
    with pytest.raises(SecretRevokedError):
        _run(p.get_secret_value(revoked))


def test_local_missing_vault_entry_raises_typed_error(_local_vault) -> None:
    from grace2_agent.secrets_handler import SecretNotFoundError

    p, _, _ = _make_persistence_and_secret_mgr()
    ghost = SecretRecord(
        secret_id=new_ulid(),
        provider="ebird",
        case_id=new_ulid(),
        vault_ref="local-file://nobody/ebird/deadbeefdead",
        label="ghost",
        added_at=datetime.now(timezone.utc),
        last_used_at=None,
        is_active=True,
    )
    with pytest.raises(SecretNotFoundError):
        _run(p.get_secret_value(ghost))


def test_local_vault_never_logs_key(_local_vault, caplog) -> None:
    """Decision F leak check on the local path: raw key never in the logs."""
    p, _, _ = _make_persistence_and_secret_mgr()
    sentinel = "LOCAL-LEAK-SENTINEL-KEY-77821"
    with caplog.at_level("DEBUG"):
        record = _run(
            handle_secret_add(
                _env_add(key=sentinel), user_id=new_ulid(), persistence=p,
            )
        )
        _run(p.get_secret_value(record))
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel not in full_log


def test_local_vault_wins_over_storage_backend(monkeypatch, tmp_path) -> None:
    """local-docker beats GRACE2_STORAGE_BACKEND=s3: a local box never writes
    a credential to AWS SSM. (Cloud is unaffected -- it never runs
    local-docker.)"""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_DEV_PERSISTENCE_DIR", str(tmp_path))
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    record = _run(
        handle_secret_add(
            _env_add(key="precedence-key"), user_id=new_ulid(),
            persistence=p, ssm_client=ssm,
        )
    )
    assert record.vault_ref.startswith("local-file://")
    assert not ssm.calls


def test_cloud_lane_byte_identical_when_backend_aws_batch(monkeypatch) -> None:
    """GRACE2_SOLVER_BACKEND=aws-batch (the cloud stack) + storage=aws still
    routes to SSM exactly as before the file-vault fork."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "aws")
    p, _, _ = _make_persistence_and_secret_mgr()
    ssm = MockSSMClient()
    record = _run(
        handle_secret_add(
            _env_add(key="cloud-unchanged-key"), user_id=new_ulid(),
            persistence=p, ssm_client=ssm,
        )
    )
    assert record.vault_ref.startswith("aws-ssm:///grace2/secrets/")
    puts = [a for n, a in ssm.calls if n == "put_parameter"]
    assert len(puts) == 1 and puts[0]["Type"] == "SecureString"


def test_local_file_delete_secret_purges_entry(_local_vault) -> None:
    """Hard-purge parity helper removes the entry; other entries survive."""
    import json as _json

    from grace2_agent.secrets_handler import _file_delete_secret

    p, _, _ = _make_persistence_and_secret_mgr()
    rec_a = _run(
        handle_secret_add(
            _env_add(provider="ebird", key="keep-me"), user_id=new_ulid(),
            persistence=p,
        )
    )
    rec_b = _run(
        handle_secret_add(
            _env_add(provider="openweathermap", key="purge-me"),
            user_id=new_ulid(), persistence=p,
        )
    )
    _file_delete_secret(rec_b.vault_ref)
    vault = _local_vault / "secrets_vault.json"
    store = _json.loads(vault.read_text(encoding="utf-8"))
    assert rec_a.vault_ref[len("local-file://"):] in store
    assert rec_b.vault_ref[len("local-file://"):] not in store
    # Non-local refs are ignored (best-effort no-op).
    _file_delete_secret("aws-ssm:///grace2/secrets/u/p/x")


# --------------------------------------------------------------------------- #
# Integration: full lifecycle
# --------------------------------------------------------------------------- #


def test_full_lifecycle_add_list_use_revoke_list() -> None:
    """End-to-end: add -> list (1) -> use (round-trip) -> revoke -> list (0)."""
    p, _, sm = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    case_id = new_ulid()
    envelope = _env_add(provider="openweathermap", key="lifecycle-test-key",
                        case_id=case_id)

    # 1. Add
    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p,
            secret_manager_client=sm,
        )
    )

    # 2. List — one active record
    lst1 = _run(
        handle_secrets_list(
            user_id=user_id, case_id=case_id, persistence=p,
        )
    )
    assert len(lst1.secrets) == 1
    assert lst1.secrets[0].secret_id == record.secret_id

    # 3. Use — Tier-2-fetcher-style read
    value = _run(p.get_secret_value(record, secret_manager_client=sm))
    assert value == "lifecycle-test-key"

    # 4. Revoke
    _run(
        handle_secret_revoke(
            record.secret_id, user_id=user_id, persistence=p,
        )
    )

    # 5. List again — 0 active records
    lst2 = _run(
        handle_secrets_list(
            user_id=user_id, case_id=case_id, persistence=p,
        )
    )
    assert len(lst2.secrets) == 0

    # Vault entry intact (audit trail) — direct check via the mock store
    # the record's vault_ref still resolves to the key value, because
    # revoke is soft. We bypass the is_active guard by constructing a
    # synthetic active-shaped record pointing at the same vault_ref.
    audit_resurrect = SecretRecord(
        secret_id=record.secret_id,
        provider=record.provider,
        case_id=record.case_id,
        vault_ref=record.vault_ref,
        added_at=datetime.now(timezone.utc),
        is_active=True,
    )
    value_after_revoke = _run(
        p.get_secret_value(audit_resurrect, secret_manager_client=sm)
    )
    assert value_after_revoke == "lifecycle-test-key", (
        "vault entry must persist for audit trail"
    )


# --------------------------------------------------------------------------- #
# Live (env-gated) test
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    os.environ.get("GRACE2_TEST_LIVE_SECRETS") != "1",
    reason="live Secret Manager test requires GRACE2_TEST_LIVE_SECRETS=1",
)
def test_live_secret_manager_roundtrip() -> None:  # pragma: no cover — live
    """Live: add a test secret to a test GCP project; verify the round-trip.

    Requires:
    - ``GRACE2_TEST_LIVE_SECRETS=1``
    - ``GRACE2_SECRETS_GCP_PROJECT`` (or ``GOOGLE_CLOUD_PROJECT``) pointing
      at a project the test SA can write Secret Manager entries to.
    - ADC via ``GOOGLE_APPLICATION_CREDENTIALS``.
    """
    p, _, _ = _make_persistence_and_secret_mgr()
    user_id = new_ulid()
    envelope = _env_add(
        provider="ebird", key=f"live-test-{new_ulid()[:12]}"
    )

    record = _run(
        handle_secret_add(
            envelope, user_id=user_id, persistence=p,
        )
    )
    assert record.is_active is True
    # Read back via the live Secret Manager.
    value = _run(p.get_secret_value(record))
    assert value == envelope.key_value

    # Revoke.
    _run(
        handle_secret_revoke(
            record.secret_id, user_id=user_id, persistence=p,
        )
    )
    # The MongoDB record now has is_active=False (verified by listing).
    lst = _run(handle_secrets_list(user_id=user_id, persistence=p))
    revoked_ids = {s.secret_id for s in lst.secrets}
    assert record.secret_id not in revoked_ids
