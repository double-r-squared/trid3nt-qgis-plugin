"""Per-Case secret lifecycle handler (FR-AS-4 + §F.3, job-0124).

Wires the three WebSocket envelope payloads from
``grace2_contracts.secrets`` (``secret-add``, ``secret-revoke``,
``secrets-list``) to the actual key-storage seam: a **vault backend** for the
raw key value, **MongoDB (via Persistence)** for the vault-ref-only
``SecretRecord``.

The vault backend is selected at call time (mirroring
``sandbox_runner._is_local_mode`` + the DynamoDB persistence selection):

- **AWS** (``GRACE2_STORAGE_BACKEND`` in {s3, aws}) — AWS SSM Parameter Store
  ``SecureString`` (KMS-encrypted at rest). This is the prod EC2 stack path;
  the GCP Secret Manager path is dead there (no GCP ADC on EC2 — the demo
  blocker NATE hit 2026-06-17).
- **GCP** (default / unset backend) — GCP Secret Manager. Unchanged from
  job-0124 so there is no regression on the GCP stack.

Reads route on the ``vault_ref`` SCHEME (``aws-ssm://`` vs GCP resource name),
not the env — a key written under one backend still resolves if the env later
flips; the env only chooses the WRITE backend.

Design notes (per the kickoff + agent.md):

- The raw key value (``SecretAddEnvelopePayload.key_value``) is the only
  place a key ever appears on the wire. This handler writes that value to
  GCP Secret Manager, captures the resulting ``vault_ref``
  (``projects/.../secrets/.../versions/latest``), and persists only the
  vault-ref-bearing ``SecretRecord``. The raw key value is **never** stored
  in MongoDB and **never** returned in any reply envelope.

- ``handle_secret_revoke`` is a **soft-revoke** (flips
  ``SecretRecord.is_active = False`` in MongoDB). The Secret Manager entry
  is deliberately **not deleted** — it preserves the audit trail and lets
  the user un-revoke without re-entering the key (§F.3 discipline).

- ``handle_secrets_list`` queries ``Persistence.list_secrets_refs`` (active
  records only by default) and wraps the result in
  ``SecretsListEnvelopePayload``. The reply payload carries
  ``SecretRecord`` entries which by construction have no ``key_value``
  field — Decision F wire-isolation invariant.

- ``Persistence.get_secret_value`` (added in the same job-0124 scope) reads
  the live key value from Secret Manager using the stored ``vault_ref``.
  Called by Tier-2 fetchers at tool-invocation time; raises
  ``SecretRevokedError`` if the record's ``is_active`` flag is ``False``.

- Every operation appends one fire-and-forget audit-log line via
  ``Persistence.append_audit`` (Decision F + §F.3 audit trail).

- Multi-tenant isolation: ``handle_secrets_list`` always filters by
  ``user_id`` (from the SessionState authenticated identity). User A's
  ``secret-add`` writes the ``SecretRecord`` with ``user_id=A``; User B's
  ``secrets-list`` never sees those records because the persistence-layer
  filter narrows on the caller's id.

Invariants this module is responsible for (Decision F + invariant 9):

- **No cost theater.** No quota / cost / spend fields on any envelope or
  audit-log entry. (FR-AS-8)
- **No raw key on the reply path.** ``handle_secret_add``'s reply
  (``SecretsListEnvelopePayload``) carries only the ``SecretRecord`` (vault
  ref only). The ``key_value`` field is consumed by this handler and never
  echoed.
- **Confirmation hooks NOT triggered.** Per FR-AS-8 the two solver triggers
  are (1) any solver execution and (2) any MongoDB write **beyond** the
  agent's session records. Per-Case secret writes (``secrets`` collection)
  are user-driven configuration of the same session — not a solver run, not
  a result-bearing write. They proceed without a ``confirmation-request``
  pause. This matches the Case-lifecycle commands which are also not
  confirmation-gated.

SRS references:
- Appendix F.3 (``docs/srs/F-data-sources-discovery-secrets.md``) — the
  per-Case secrets architecture.
- FR-AS-4 (LLM-facing DB path via Persistence/MCP).
- FR-AS-8 (confirmation triggers — secrets writes are NOT a trigger).
- Decision F (wire isolation — raw key never persisted to MongoDB).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from grace2_contracts.common import new_ulid
from grace2_contracts.secrets import (
    ProviderID,
    SecretAddEnvelopePayload,
    SecretRecord,
    SecretsListEnvelopePayload,
)

from .persistence import Persistence

logger = logging.getLogger("grace2_agent.secrets_handler")

# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class SecretError(RuntimeError):
    """Base for secret-handler failures."""


class SecretRevokedError(SecretError):
    """Raised when ``get_secret_value`` is called on a revoked record.

    Tier-2 fetchers catch this and surface a recoverable A.6 error code
    (the user can re-enable the key or add a new one).
    """


class SecretNotFoundError(SecretError):
    """Raised when ``get_secret_value`` is called on a missing record."""


# --------------------------------------------------------------------------- #
# GCP Secret Manager client protocol — duck-typed so tests can pass a mock
# --------------------------------------------------------------------------- #

# Default GCP project for the Secret Manager backend. Resolved at handler
# construction time; the env var matches the existing ``adapter.py`` pattern
# so a single project setting drives the whole agent service. Override with
# ``GRACE2_SECRETS_GCP_PROJECT`` if the secrets project is split from the
# Vertex AI project (the v0.1 deployment puts them in the same project).
DEFAULT_GCP_PROJECT: Final[str] = (
    os.environ.get("GRACE2_SECRETS_GCP_PROJECT")
    or os.environ.get("GOOGLE_CLOUD_PROJECT")
    or "grace-2-hazard-prod"
)

# --------------------------------------------------------------------------- #
# Backend selection (GCP Secret Manager [default] vs AWS SSM Parameter Store)
# --------------------------------------------------------------------------- #
#
# ROOT CAUSE this addresses (NATE 2026-06-17 secrets demo): the prod stack is
# AWS (EC2, no GCP ADC), but ``handle_secret_add`` wrote to GCP Secret Manager
# and ``get_secret_value`` read from it — the GCP SDK then failed with
# "Application Default Credentials were not found". Same GCP-on-AWS dead-path
# class as the sandbox (Cloud Run) and MS-buildings (abfs).
#
# FIX: on the AWS storage backend (``GRACE2_STORAGE_BACKEND`` ∈ {s3,aws} —
# mirroring ``sandbox_runner._is_local_mode`` + the DynamoDB persistence
# selection) route the secret VALUE to AWS SSM Parameter Store SecureString
# (KMS-encrypted at rest, cheapest of the AWS options). The GCP path stays the
# default on GCP (unset / other backend) so there is no regression there.
#
# Vault-ref scheme: we prefix the AWS ``vault_ref`` with ``aws-ssm://`` and the
# GCP path stays the bare resource name (legacy ``gcp-sm://`` also tolerated).
# Reads route on the *ref scheme* (not the env), so a key written under one
# backend still resolves even if the env later flips — the env only chooses the
# WRITE backend. ``GRACE2_STORAGE_BACKEND`` is read at call time so a deploy /
# test injection takes effect without re-import.

#: Vault-ref scheme prefix for AWS SSM Parameter Store SecureStrings.
AWS_SSM_VAULT_SCHEME: Final[str] = "aws-ssm://"

#: Vault-ref scheme prefix for the LOCAL file vault (fingerprint audit L8,
#: NATE 2026-07-08). The TRID3NT local build (``GRACE2_SOLVER_BACKEND=
#: local-docker``, file persistence, no GCP ADC / AWS SSM) used to fall
#: through to the GCP Secret Manager write path -- a dead cloud branch that
#: failed with a GCP ADC error the first time a user submitted a
#: credential-request card locally. Local writes now land in a mode-0600 JSON
#: file next to the file-persistence store; reads route on this scheme.
LOCAL_FILE_VAULT_SCHEME: Final[str] = "local-file://"

#: Filename of the local secrets vault, created under the file-persistence
#: dir (``GRACE2_DEV_PERSISTENCE_DIR``, default ``~/.grace2/dev_persistence``).
LOCAL_VAULT_FILENAME: Final[str] = "secrets_vault.json"

#: Vault-ref scheme prefix for the (default) GCP Secret Manager path. Legacy
#: records may carry a bare resource name (no scheme); both are tolerated.
GCP_SM_VAULT_SCHEME: Final[str] = "gcp-sm://"

#: SSM parameter-name root for GRACE-2 per-user/per-provider secrets. The EC2
#: instance-role IAM policy is scoped to ``arn:aws:ssm:<region>:<acct>:parameter
#: /grace2/secrets/*`` so this prefix is load-bearing for least-privilege.
AWS_SSM_PARAM_PREFIX: Final[str] = "/grace2/secrets"


def _aws_secret_backend_selected() -> bool:
    """True when secret values must route to AWS SSM (not GCP Secret Manager).

    Selection mirrors ``sandbox_runner._is_local_mode`` and the DynamoDB
    persistence selection: the AWS stack is signalled by
    ``GRACE2_STORAGE_BACKEND`` ∈ {s3, aws}. Unset / any other value keeps the
    GCP Secret Manager default (no regression on GCP). Read at call time so a
    deploy / test env injection takes effect.
    """
    backend = (os.environ.get("GRACE2_STORAGE_BACKEND") or "").strip().lower()
    return backend in ("s3", "aws")


def _local_file_vault_selected() -> bool:
    """True when secret values must route to the LOCAL file vault (L8).

    The canonical is-local seam (same one ``server._local_compute_lane`` and
    the gate-card wording fixes use): the solver dispatch backend --
    ``GRACE2_SOLVER_BACKEND=local-docker`` -> ``tools.solver.solver_backend()``
    returns ``local-docker``. The TRID3NT local build pins it; the cloud stack
    never sets it, so the cloud SSM/GCP write paths stay byte-identical.
    Checked FIRST so a local box never writes a credential to a cloud vault.
    Read at call time so a test env injection takes effect without re-import.
    """
    from .tools.solver import SOLVER_BACKEND_LOCAL_DOCKER, solver_backend

    return solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER


def _aws_region() -> str:
    """Resolve the AWS region exactly like ``dynamo_backend`` / ``bedrock_adapter``.

    ``AWS_REGION`` wins, then ``AWS_DEFAULT_REGION``, then the migration's home
    region ``us-west-2``. boto3 itself walks the standard credential chain
    (env / ~/.aws / EC2 instance role).
    """
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _default_ssm_client():  # pragma: no cover — exercised live
    """Construct a live AWS SSM client.

    Lazy-imported so CI / unit tests without boto3 still load this module (the
    GCP client import is lazy for the same reason). Tests pass a mock client.
    """
    import boto3

    return boto3.client("ssm", region_name=_aws_region())


def _build_ssm_param_name(
    provider: ProviderID, user_id: str, case_id: str | None
) -> str:
    """Generate an SSM parameter name for a fresh per-user/per-provider secret.

    Shape: ``/grace2/secrets/<user_id>/<provider>/<short_ulid>`` — under the
    IAM-scoped ``/grace2/secrets/*`` prefix so the EC2 role grant stays
    least-privilege. The short ULID is the collision discriminator (a user may
    re-enter a key after revoking — both parameters persist for audit, so the
    names must differ). When ``case_id`` is present we fold it into the path
    segment so per-Case scoping is greppable in the parameter hierarchy.

    SSM parameter names allow ``[A-Za-z0-9_.\\-/]`` — the ULID crockford-base32
    alphabet and the provider Literal both fall inside that set; we lowercase
    the ULID fragment to match the GCP secret-id convention.
    """
    short = new_ulid()[-12:].lower()
    # Defensive: SSM names cannot contain characters outside the allowed set;
    # user_id / case_id are ULIDs (safe), provider is a closed Literal (safe).
    user_seg = user_id
    if case_id:
        case_short = case_id[-8:].lower()
        return f"{AWS_SSM_PARAM_PREFIX}/{user_seg}/{provider}/case-{case_short}-{short}"
    return f"{AWS_SSM_PARAM_PREFIX}/{user_seg}/{provider}/{short}"


def _build_secret_id(provider: ProviderID, case_id: str | None) -> str:
    """Generate a Secret Manager secret-id for a fresh per-Case secret.

    Shape: ``case-<case_id>-<provider>-<short_ulid>`` (case-scoped) or
    ``user-<provider>-<short_ulid>`` (user-level when ``case_id`` is None).
    The full ULID is the discriminator that ensures collisions cannot
    happen between two adds in the same Case for the same provider (the
    user might re-enter the key after revoking — both records persist for
    audit, so the IDs must differ).

    Secret Manager IDs must match ``[A-Za-z0-9_-]{1,255}`` — the ULID
    crockford-base32 alphabet falls inside that range and we substitute
    nothing.
    """
    short = new_ulid()[-12:].lower()
    if case_id:
        # Truncate the case_id ULID for brevity — the short fragment still
        # uniquely identifies the per-Case scoping for audit grep.
        case_short = case_id[-8:].lower()
        return f"case-{case_short}-{provider}-{short}"
    return f"user-{provider}-{short}"


def _now_utc() -> datetime:
    """UTC ``datetime`` for ``SecretRecord.added_at`` / ``last_used_at``."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Secret Manager client construction (lazy)
# --------------------------------------------------------------------------- #


def _default_secret_manager_client():  # pragma: no cover — exercised live
    """Construct a live Secret Manager client.

    GCP is decommissioned: ``google-cloud-secret-manager`` is no longer an agent
    dependency, so there is no default GCP client to build. The live vault is
    AWS SSM (``GRACE2_STORAGE_BACKEND`` ∈ {s3, aws} → ``_aws_*`` path). The GCP
    write/read functions remain only for unit tests that inject a duck-typed
    ``secret_manager_client``; production never reaches this builder.
    """
    raise RuntimeError(
        "GCP Secret Manager is decommissioned; the live secrets vault is AWS "
        "SSM Parameter Store. Set GRACE2_STORAGE_BACKEND=s3 (the production "
        "default) so secret writes route to AWS SSM."
    )


# --------------------------------------------------------------------------- #
# Vault write/delete — per-backend (GCP Secret Manager / AWS SSM)
# --------------------------------------------------------------------------- #


def _gcp_write_secret(
    envelope: SecretAddEnvelopePayload,
    *,
    user_id: str,
    secret_manager_client=None,
    gcp_project: str | None = None,
) -> str:
    """Write the raw key to GCP Secret Manager; return the ``vault_ref``.

    Steps (the original job-0124 GCP path, unchanged behaviour):
      1. ``create_secret`` the parent resource.
      2. ``add_secret_version`` with the raw key as payload.
      3. Normalize the version name to ``.../versions/latest``.

    Never logs ``key_value``.
    """
    project = gcp_project or DEFAULT_GCP_PROJECT
    secret_id = _build_secret_id(envelope.provider, envelope.case_id)
    parent = f"projects/{project}"

    client = secret_manager_client or _default_secret_manager_client()

    logger.info(
        "secret-add[gcp]: creating secret_id=%s provider=%s case=%s user=%s",
        secret_id,
        envelope.provider,
        envelope.case_id,
        user_id,
    )
    create_secret_kwargs = {
        "parent": parent,
        "secret_id": secret_id,
        "secret": {"replication": {"automatic": {}}},
    }
    # Live Secret Manager SDK accepts both ``request=`` and kwargs;
    # the kwargs path matches the mock client surface in tests.
    client.create_secret(request=create_secret_kwargs)

    version_kwargs = {
        "parent": f"{parent}/secrets/{secret_id}",
        "payload": {"data": envelope.key_value.encode("utf-8")},
    }
    add_version_response = client.add_secret_version(request=version_kwargs)
    # The live SDK returns a ``SecretVersion`` proto with a ``name`` attr
    # like ``projects/.../secrets/.../versions/1``. We normalize to
    # ``.../versions/latest`` so subsequent ``get_secret_value`` calls always
    # read the freshest version.
    versioned_name = getattr(add_version_response, "name", None) or (
        f"{parent}/secrets/{secret_id}/versions/1"
    )
    return versioned_name.rsplit("/versions/", 1)[0] + "/versions/latest"


def _aws_write_secret(
    envelope: SecretAddEnvelopePayload,
    *,
    user_id: str,
    ssm_client=None,
) -> str:
    """Write the raw key to AWS SSM Parameter Store as a SecureString.

    Returns the ``vault_ref`` (``aws-ssm://<parameter-name>``). The value is
    stored ``Type="SecureString"`` so SSM encrypts it at rest with the account
    KMS key (or a CMK if ``GRACE2_SECRETS_KMS_KEY_ID`` is set). The raw key is
    passed only as the ``put_parameter`` ``Value`` and is NEVER logged.

    Raises ``SecretError`` (no key leakage in the message) on any boto3 error.
    """
    param_name = _build_ssm_param_name(
        envelope.provider, user_id, envelope.case_id
    )
    client = ssm_client or _default_ssm_client()

    logger.info(
        "secret-add[aws-ssm]: putting param=%s provider=%s case=%s user=%s",
        param_name,
        envelope.provider,
        envelope.case_id,
        user_id,
    )
    put_kwargs: dict = {
        "Name": param_name,
        "Value": envelope.key_value,
        "Type": "SecureString",
        # Overwrite=False — the short-ULID name is collision-free per add, so
        # a name clash would signal a bug, not an intended re-add.
        "Overwrite": False,
    }
    # Optional customer-managed KMS key; absent => SSM uses the account default
    # ``alias/aws/ssm`` key. Either way the value is KMS-encrypted at rest.
    kms_key_id = os.environ.get("GRACE2_SECRETS_KMS_KEY_ID")
    if kms_key_id:
        put_kwargs["KeyId"] = kms_key_id

    try:
        client.put_parameter(**put_kwargs)
    except Exception as exc:  # noqa: BLE001
        # Surface a typed error WITHOUT echoing the key value. The boto3
        # exception text never contains the value, but we don't interpolate
        # ``envelope.key_value`` here regardless.
        raise SecretError(
            f"AWS SSM put_parameter failed for provider={envelope.provider}: "
            f"{type(exc).__name__}"
        ) from exc

    return f"{AWS_SSM_VAULT_SCHEME}{param_name}"


def _aws_delete_secret(vault_ref: str, *, ssm_client=None) -> None:
    """Hard-delete an SSM SecureString parameter (used by hard-revoke).

    ``handle_secret_revoke`` is a SOFT revoke for the MongoDB record; the AWS
    parameter delete is invoked only when a caller explicitly requests a hard
    purge (``hard=True``). Best-effort: a missing parameter is not an error.
    """
    if not vault_ref.startswith(AWS_SSM_VAULT_SCHEME):
        return
    param_name = vault_ref[len(AWS_SSM_VAULT_SCHEME) :]
    client = ssm_client or _default_ssm_client()
    try:
        client.delete_parameter(Name=param_name)
    except Exception:  # noqa: BLE001
        logger.exception(
            "secret-revoke[aws-ssm]: delete_parameter best-effort failed for "
            "param=%s (continuing)",
            param_name,
        )


# --------------------------------------------------------------------------- #
# Vault write/read/delete — LOCAL file vault (fingerprint audit L8)
# --------------------------------------------------------------------------- #


def _local_vault_path(vault_dir: "Path | None" = None) -> Path:
    """Resolve the local vault file path (persistence-dir / secrets_vault.json).

    Mirrors the file-persistence selection: the vault sits next to the
    ``FileMCPClient`` collections under ``GRACE2_DEV_PERSISTENCE_DIR``
    (default ``~/.grace2/dev_persistence``), resolved at call time via the
    same helper the persistence layer uses. ``vault_dir`` is a test seam.
    """
    if vault_dir is not None:
        return Path(vault_dir) / LOCAL_VAULT_FILENAME
    from .persistence import _default_dev_persistence_dir

    return Path(_default_dev_persistence_dir()) / LOCAL_VAULT_FILENAME


def _read_vault_store(path: Path) -> dict[str, str]:
    """Load the vault dict (``key-name -> raw value``); missing file -> {}."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    store = json.loads(raw)
    return store if isinstance(store, dict) else {}


def _write_vault_store(path: Path, store: dict[str, str]) -> None:
    """Atomically write the vault dict with mode 0600 (owner read/write only).

    tmp-file + ``os.replace`` (POSIX-atomic on the same filesystem, the same
    discipline as ``FileMCPClient``); the tmp file is CREATED 0600 via
    ``os.open`` so the raw values are never world-readable, even transiently,
    and the final path is re-chmodded 0600 in case a pre-existing file had
    looser bits.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=0, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _build_file_vault_key(
    provider: ProviderID, user_id: str, case_id: str | None
) -> str:
    """Vault-dict key: ``<user_id>/<provider>/[case-<short>-]<short_ulid>``.

    Same shape as the SSM parameter name minus the IAM-scoped
    ``/grace2/secrets`` prefix (no IAM locally). The short ULID keeps re-adds
    after a revoke collision-free, mirroring both cloud builders.
    """
    short = new_ulid()[-12:].lower()
    if case_id:
        case_short = case_id[-8:].lower()
        return f"{user_id}/{provider}/case-{case_short}-{short}"
    return f"{user_id}/{provider}/{short}"


def _file_write_secret(
    envelope: SecretAddEnvelopePayload,
    *,
    user_id: str,
    vault_dir: "Path | None" = None,
) -> str:
    """Write the raw key to the LOCAL file vault; return the ``vault_ref``.

    Local-build branch of the vault fork (``local-file://<key-name>``). The
    raw key value goes ONLY into the 0600 vault file -- it is never logged and
    never interpolated into an error message. ``vault_dir`` is a test seam
    (tests point it at a tmpdir; production resolves the persistence dir).
    """
    key_name = _build_file_vault_key(envelope.provider, user_id, envelope.case_id)
    path = _local_vault_path(vault_dir)

    logger.info(
        "secret-add[local-file]: storing key=%s provider=%s case=%s user=%s "
        "vault=%s",
        key_name,
        envelope.provider,
        envelope.case_id,
        user_id,
        path,
    )
    try:
        store = _read_vault_store(path)
        store[key_name] = envelope.key_value
        _write_vault_store(path, store)
    except Exception as exc:  # noqa: BLE001
        # Typed error WITHOUT the key value (same discipline as the SSM branch).
        raise SecretError(
            f"local file-vault write failed for provider={envelope.provider}: "
            f"{type(exc).__name__}"
        ) from exc

    return f"{LOCAL_FILE_VAULT_SCHEME}{key_name}"


def _file_read_secret(vault_ref: str, *, vault_dir: "Path | None" = None) -> str:
    """Read a raw key value back from the LOCAL file vault by ``vault_ref``.

    Called by ``Persistence.get_secret_value`` when the stored ref carries the
    ``local-file://`` scheme (reads route on the ref scheme, exactly like the
    SSM/GCP branches). **Caller MUST NOT log the returned value.**
    """
    key_name = vault_ref[len(LOCAL_FILE_VAULT_SCHEME) :]
    path = _local_vault_path(vault_dir)
    store = _read_vault_store(path)
    value = store.get(key_name)
    if value is None:
        raise SecretNotFoundError(
            f"local file-vault has no entry for key {key_name!r} "
            f"(vault file: {path})"
        )
    return str(value)


def _file_delete_secret(vault_ref: str, *, vault_dir: "Path | None" = None) -> None:
    """Hard-delete a local vault entry (parity with ``_aws_delete_secret``).

    ``handle_secret_revoke`` stays a SOFT revoke (Mongo/file record flag);
    this purge helper exists for an explicit hard-delete caller. Best-effort:
    a missing entry / missing vault file is not an error.
    """
    if not vault_ref.startswith(LOCAL_FILE_VAULT_SCHEME):
        return
    key_name = vault_ref[len(LOCAL_FILE_VAULT_SCHEME) :]
    path = _local_vault_path(vault_dir)
    try:
        store = _read_vault_store(path)
        if key_name in store:
            del store[key_name]
            _write_vault_store(path, store)
    except Exception:  # noqa: BLE001
        logger.exception(
            "secret-revoke[local-file]: best-effort delete failed for key=%s "
            "(continuing)",
            key_name,
        )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def handle_secret_add(
    envelope: SecretAddEnvelopePayload,
    *,
    user_id: str,
    persistence: Persistence,
    secret_manager_client=None,
    gcp_project: str | None = None,
    ssm_client=None,
) -> SecretRecord:
    """Process a ``secret-add`` envelope end-to-end.

    Steps:

    1. Write the raw ``key_value`` to the selected vault backend and capture
       the ``vault_ref``:
         - **LOCAL** (``GRACE2_SOLVER_BACKEND=local-docker`` -- the TRID3NT
           local build): mode-0600 ``secrets_vault.json`` under the
           file-persistence dir; ``vault_ref="local-file://<key-name>"``.
         - **AWS** (``GRACE2_STORAGE_BACKEND`` ∈ {s3, aws}): ``put_parameter``
           a ``SecureString`` under ``/grace2/secrets/<user>/<provider>/<ulid>``
           (KMS-encrypted at rest); ``vault_ref="aws-ssm://<name>"``.
         - **GCP** (default / unset backend): ``secrets.create`` +
           ``secrets.add_version``; ``vault_ref`` =
           ``projects/<project>/secrets/<id>/versions/latest``.
    2. Build a ``SecretRecord`` carrying only that ``vault_ref`` (never the raw
       key) and persist it via ``Persistence.upsert_secret_ref`` (Decision F
       backstop refuses any field shaped like a key value).
    3. Append an audit-log entry (``event_type="secret-add"``).

    The raw key value lives ONLY in the vault (GCP SM / AWS SSM SecureString);
    it is never persisted plaintext to MongoDB and never logged.

    The handler returns the persisted ``SecretRecord``. The caller
    (``server.py``) wraps it in a fresh ``SecretsListEnvelopePayload`` and
    sends to the client. The raw ``key_value`` field on the inbound
    envelope is **never** echoed back, **never** persisted to MongoDB, and
    **never** logged.

    Args:
        envelope: the inbound ``SecretAddEnvelopePayload``.
        user_id: the authenticated caller's user_id (from SessionState).
            Stamped onto the ``SecretRecord`` for multi-tenant isolation.
            Cannot be empty — fail closed.
        persistence: the agent-side Mongo wrapper (added the secret-record
            CRUD methods in job-0115).
        secret_manager_client: optional pre-constructed Secret Manager
            client. Tests pass a mock; production passes None and we lazy-
            construct a live one. Only used on the GCP backend.
        gcp_project: override the default project (``DEFAULT_GCP_PROJECT``).
        ssm_client: optional pre-constructed AWS SSM client (boto3). Tests
            pass a mock; production passes None and we lazy-construct one.
            Only used on the AWS backend (``GRACE2_STORAGE_BACKEND`` ∈
            {s3, aws}).

    Returns:
        The persisted ``SecretRecord`` (vault-ref only).

    Raises:
        SecretError: on any failure — the caller surfaces this as an A.6
            ``INTERNAL_ERROR`` envelope. The raw key value is NOT leaked
            into the error message.
    """
    if not user_id:
        # Fail closed — multi-tenant isolation requires a stamped user_id.
        raise SecretError("handle_secret_add requires a non-empty user_id")
    if not envelope.key_value:
        # An empty key_value is a malformed envelope — refuse before we
        # write a zero-byte secret version to the vault.
        raise SecretError("handle_secret_add: key_value is empty")

    # Backend fork: LOCAL file vault when solves run on this machine
    # (``GRACE2_SOLVER_BACKEND=local-docker`` -- checked first so a local box
    # never writes a credential to a cloud vault; fingerprint audit L8), AWS
    # SSM Parameter Store (SecureString) on the AWS stack, GCP Secret Manager
    # everywhere else. We never surface key_value into a log line on any
    # branch.
    if _local_file_vault_selected():
        vault_ref = _file_write_secret(envelope, user_id=user_id)
    elif _aws_secret_backend_selected():
        vault_ref = _aws_write_secret(
            envelope, user_id=user_id, ssm_client=ssm_client
        )
    else:
        vault_ref = _gcp_write_secret(
            envelope,
            user_id=user_id,
            secret_manager_client=secret_manager_client,
            gcp_project=gcp_project,
        )

    # 3. Build and persist the SecretRecord.
    record = SecretRecord(
        secret_id=new_ulid(),
        provider=envelope.provider,
        case_id=envelope.case_id,
        vault_ref=vault_ref,
        label=envelope.label,
        added_at=_now_utc(),
        last_used_at=None,
        is_active=True,
    )
    # Stamp the user_id onto the persisted document for multi-tenant
    # filtering. ``Persistence.upsert_secret_ref`` stores the
    # ``SecretRecord.model_dump()`` plus our supplied user_id; the schema
    # itself does not carry user_id (forward-compat field), but the
    # persistence layer's list filter looks for it.
    await _upsert_with_user(persistence, record, user_id=user_id)

    # 4. Append an audit-log entry. Never logs the key value.
    await _safe_append_audit(
        persistence,
        event_type="secret-add",
        payload={
            "user_id": user_id,
            "case_id": envelope.case_id,
            "provider": envelope.provider,
            "secret_id": record.secret_id,
            "vault_ref": vault_ref,
            "label": envelope.label,
        },
    )

    return record


async def handle_secret_revoke(
    secret_id: str,
    *,
    user_id: str,
    persistence: Persistence,
) -> None:
    """Soft-revoke a secret (sets ``SecretRecord.is_active = False``).

    The GCP Secret Manager entry is **not** deleted — preserves the audit
    trail and lets the user un-revoke without re-entering the key.

    Per FR-AS-8 this is NOT a confirmation trigger (per-Case secret
    revocation is user-driven configuration, not a solver run or result
    write).

    Args:
        secret_id: the ULID of the ``SecretRecord`` to revoke.
        user_id: the authenticated caller's user_id (audit only — the
            persistence layer doesn't currently enforce caller-owns-secret
            because the storage schema doesn't denormalize the ownership
            link. Surfaced as OQ-0124-SECRET-OWNER-CHECK).
        persistence: the agent-side Mongo wrapper.
    """
    if not secret_id:
        raise SecretError("handle_secret_revoke requires a non-empty secret_id")

    await persistence.revoke_secret(secret_id)
    await _safe_append_audit(
        persistence,
        event_type="secret-revoke",
        payload={"user_id": user_id, "secret_id": secret_id},
    )
    logger.info(
        "secret-revoke: marked secret_id=%s inactive (user=%s)",
        secret_id,
        user_id,
    )


async def handle_secrets_list(
    *,
    user_id: str,
    case_id: str | None = None,
    persistence: Persistence,
) -> SecretsListEnvelopePayload:
    """List active secret references for the caller.

    Multi-tenant isolation: ``Persistence.list_secrets_refs`` filters on
    ``user_id`` (plus backward-compat for pre-Auth records without the
    field). When ``case_id`` is supplied the result is further narrowed
    to per-Case records — user-level records are excluded from a
    Case-scoped list to keep the UX surface tight.

    The returned ``SecretsListEnvelopePayload`` carries only the
    vault-ref-bearing ``SecretRecord`` entries — by construction no
    ``key_value`` field. This is the Decision F wire-isolation backstop.

    Args:
        user_id: the authenticated caller's user_id (from SessionState).
            Cannot be empty — fail closed.
        case_id: optional Case scope. ``None`` returns every active
            record for the user.
        persistence: the agent-side Mongo wrapper.

    Returns:
        ``SecretsListEnvelopePayload`` with the (possibly empty) list.
    """
    if not user_id:
        raise SecretError("handle_secrets_list requires a non-empty user_id")
    records = await persistence.list_secrets_refs(user_id=user_id, case_id=case_id)
    # Defensive: even though the schema rejects key_value at construction,
    # double-check the wire payload carries no leakage. ``SecretRecord``
    # has no key-value field at all — this loop never trips, but it's the
    # explicit "fail closed" assertion the kickoff requires.
    for r in records:
        dump = r.model_dump()
        for k in dump:
            assert "key" not in k or "value" not in k.lower(), (
                f"SecretRecord contained a key-value-shaped field: {k!r}"
            )
    return SecretsListEnvelopePayload(secrets=records)


# --------------------------------------------------------------------------- #
# Helpers — confined to this module (don't expand Persistence's public API
# more than the kickoff specifies)
# --------------------------------------------------------------------------- #


async def _upsert_with_user(
    persistence: Persistence, record: SecretRecord, *, user_id: str
) -> None:
    """Upsert a ``SecretRecord`` stamped with ``user_id`` for tenant scoping.

    The schema-level ``SecretRecord`` doesn't carry a ``user_id`` field
    (it's a forward-compat field on the *storage* document only, per the
    §F.3 multi-tenant note in ``persistence.py``). We call the existing
    ``upsert_secret_ref`` then ``$set`` the ``user_id`` via a second MCP
    call — the persistence wrapper's list filter looks for either
    ``user_id`` or the legacy ``owner_user_id``.

    We could (and may, in a future job) expose a ``user_id``-aware
    ``upsert_secret_ref`` directly; the kickoff explicitly says additive
    Persistence changes only, and the only required new method is
    ``get_secret_value``. So this stays in the handler module.
    """
    # First: the schema-shaped upsert (vault-ref-only, no key value).
    await persistence.upsert_secret_ref(record)
    # Second: stamp the user_id by re-issuing an update-one. Best-effort: if
    # this stamp fails the record is persisted but UNOWNED, so the owner-scoped
    # ``list_secrets_refs`` filter (job-0252 removed the ``$exists:false``
    # backward-compat clause that used to surface unowned rows to every user)
    # will not surface it. We log and continue rather than raise — losing
    # visibility of one secret-ref is preferable to failing the whole add; the
    # next add for the same secret_id re-stamps it.
    try:
        await persistence._mcp.call_tool(  # noqa: SLF001 — intentional
            "update-one",
            {
                "database": persistence._db,  # noqa: SLF001 — intentional
                "collection": "secrets",
                "filter": {"_id": record.secret_id},
                "update": {"$set": {"user_id": user_id}},
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "secret-add: failed to stamp user_id on secret_id=%s "
            "(continuing — list filter still finds the record)",
            record.secret_id,
        )


async def _safe_append_audit(
    persistence: Persistence, *, event_type: str, payload: dict
) -> None:
    """Append an audit-log entry — never raise from this path.

    Audit-log writes are fire-and-forget: a failure must not abort the
    caller's happy path. ``Persistence.append_audit`` is already async +
    MCP-routed; we wrap it in try/except so any MCP wobble doesn't turn
    a successful secret-add into a user-visible error.
    """
    try:
        await persistence.append_audit(event_type, payload)
    except Exception:  # noqa: BLE001 — fire-and-forget
        logger.exception(
            "audit-log append failed for event_type=%s (best-effort, continuing)",
            event_type,
        )


__all__ = [
    "AWS_SSM_PARAM_PREFIX",
    "AWS_SSM_VAULT_SCHEME",
    "DEFAULT_GCP_PROJECT",
    "GCP_SM_VAULT_SCHEME",
    "SecretError",
    "SecretNotFoundError",
    "SecretRevokedError",
    "handle_secret_add",
    "handle_secret_revoke",
    "handle_secrets_list",
]
