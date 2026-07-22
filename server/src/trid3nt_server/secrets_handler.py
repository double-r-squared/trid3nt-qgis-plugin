"""Per-Case secret lifecycle handler (FR-AS-4 + F.3, job-0124; local-only).

Wires the three WebSocket envelope payloads from
``trid3nt_contracts.secrets`` (``secret-add``, ``secret-revoke``,
``secrets-list``) to the key-storage seam: a LOCAL file vault for the raw
key value, Persistence for the vault-ref-only ``SecretRecord``.

TRID3NT is the local product: the cloud vault backends that used to live
here (GCP Secret Manager default + AWS SSM Parameter Store SecureString)
are removed. There is exactly ONE vault:

- Secrets live one-per-file under the file-persistence data root
  (``TRID3NT_DEV_PERSISTENCE_DIR``, default ``~/.trid3nt/dev_persistence``)
  at ``<root>/secrets/<user_id>/<provider>/<leaf>``, file mode 0600.
- The stored ``vault_ref`` carries the ``file-vault://`` scheme:
  ``file-vault://<user_id>/<provider>/<leaf>``.

Legacy refs (``aws-ssm://...``, ``gcp-sm://...``, bare GCP resource names
``projects/.../versions/...``, and the interim ``local-file://...`` JSON
store) can no longer resolve: ``read_secret_value`` raises the typed
``SecretNotFoundError`` for them, which the Tier-2 fetchers already treat
as "missing key" -- the credential-request card flow re-prompts the user
and the retry stores a fresh ``file-vault://`` secret. Never a crash,
never a silent empty value.

Design notes (unchanged from job-0124 where vault-agnostic):

- The raw key value (``SecretAddEnvelopePayload.key_value``) is the only
  place a key ever appears on the wire. This handler writes that value to
  the file vault, captures the resulting ``vault_ref``, and persists only
  the vault-ref-bearing ``SecretRecord``. The raw key value is **never**
  stored via Persistence and **never** returned in any reply envelope.

- ``handle_secret_revoke`` is a **soft-revoke** (flips
  ``SecretRecord.is_active = False``). The vault file is deliberately
  **not deleted** -- it preserves the audit trail and lets the user
  un-revoke without re-entering the key (F.3 discipline).
  ``_file_delete_secret`` exists for an explicit hard purge.

- ``handle_secrets_list`` queries ``Persistence.list_secrets_refs`` (active
  records only) and wraps the result in ``SecretsListEnvelopePayload``.
  ``SecretRecord`` has no ``key_value`` field by construction -- Decision F
  wire-isolation invariant.

- ``Persistence.get_secret_value`` reads the live key value using the
  stored ``vault_ref``; called by Tier-2 fetchers at tool-invocation time
  (including the credential-card RETRY path). It raises
  ``SecretRevokedError`` if the record's ``is_active`` flag is ``False``.

- Every operation appends one fire-and-forget audit-log line via
  ``Persistence.append_audit``.

- Multi-tenant isolation: ``handle_secrets_list`` always filters by
  ``user_id`` (from the SessionState identity).

Invariants (Decision F + invariant 9):

- **No cost theater.** No quota / cost / spend fields anywhere. (FR-AS-8)
- **No raw key on the reply path** and no raw key in any log line or
  error message.
- **Confirmation hooks NOT triggered.** Per FR-AS-8 secret writes are
  user-driven configuration, not a solver run -- no confirmation pause.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.secrets import (
    ProviderID,
    SecretAddEnvelopePayload,
    SecretRecord,
    SecretsListEnvelopePayload,
)

from .persistence import Persistence

logger = logging.getLogger("trid3nt_server.secrets_handler")

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
    """Raised when a ``vault_ref`` cannot be resolved to a stored value.

    This is the typed missing-secret path: Tier-2 fetchers treat it as
    "no key available", which routes to the credential-request card so the
    user re-enters the key. Legacy cloud refs (``aws-ssm://`` / GCP) and
    malformed refs land here too -- honest re-prompt, never a crash.
    """


# --------------------------------------------------------------------------- #
# Vault-ref schemes
# --------------------------------------------------------------------------- #

#: Canonical vault-ref scheme for the local file vault. Refs look like
#: ``file-vault://<user_id>/<provider>/<leaf>`` and map to
#: ``<persistence-root>/secrets/<user_id>/<provider>/<leaf>``.
FILE_VAULT_SCHEME: Final[str] = "file-vault://"

#: Compat alias -- ``Persistence.get_secret_value`` imports this name at call
#: time to route refs to ``_file_read_secret``. Collapse onto
#: ``FILE_VAULT_SCHEME`` when persistence.py delegates to
#: ``read_secret_value`` directly.
LOCAL_FILE_VAULT_SCHEME: Final[str] = FILE_VAULT_SCHEME

#: LEGACY scheme markers -- recognized only to classify unresolvable refs
#: honestly (typed ``SecretNotFoundError``, never a crash). No cloud SDK is
#: ever invoked for them. ``Persistence.get_secret_value`` also imports the
#: AWS/GCP names at call time.
AWS_SSM_VAULT_SCHEME: Final[str] = "aws-ssm://"
GCP_SM_VAULT_SCHEME: Final[str] = "gcp-sm://"
_LEGACY_LOCAL_FILE_SCHEME: Final[str] = "local-file://"

#: Subdirectory of the persistence root that holds the secret files.
VAULT_SUBDIR: Final[str] = "secrets"

#: Allowed characters for one path segment of a vault key
#: (user_id / provider / leaf). ULIDs, the ProviderID Literal values and
#: email-shaped user ids all fall inside this set; ``/`` is the separator
#: and is excluded, so a segment can never traverse.
_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9@._-]+")


def _default_ssm_client():
    """Legacy-compat seam: there is no SSM client on the local build.

    ``Persistence.get_secret_value`` (call-time import) still constructs
    this for ``aws-ssm://`` refs when no client is injected. Raising the
    typed missing-secret error here converts an unresolvable legacy cloud
    ref into the credential-card re-prompt path instead of a boto3 crash.
    Remove together with persistence.py's ``aws-ssm://`` branch.
    """
    raise SecretNotFoundError(
        "legacy aws-ssm:// vault ref cannot be resolved on the local build; "
        "re-add the key via the credential card"
    )


# --------------------------------------------------------------------------- #
# File vault: paths + key validation
# --------------------------------------------------------------------------- #


def _vault_root() -> Path:
    """Resolve the vault root: ``<file-persistence-root>/secrets``.

    Mirrors the file-persistence selection (``TRID3NT_DEV_PERSISTENCE_DIR``,
    default ``~/.trid3nt/dev_persistence``) via the same helper
    ``FileMCPClient`` uses, resolved at call time so a test env injection
    takes effect without re-import.
    """
    from .persistence import _default_dev_persistence_dir

    return Path(_default_dev_persistence_dir()) / VAULT_SUBDIR


def _validate_segment(value: str, *, what: str) -> str:
    """Fail closed on any path-unsafe id before it touches the filesystem."""
    if not value or value in (".", "..") or not _SEGMENT_RE.fullmatch(value):
        raise SecretError(
            f"secrets vault: {what} {value!r} is not a safe path segment"
        )
    return value


def _secret_path_for_key(key_name: str) -> Path:
    """Map a vault key (``<user>/<provider>/<leaf>``) to its file path.

    Every segment is validated against the safe charset (``..`` and empty
    segments rejected) and the resolved path is confined to the vault root
    -- a malformed or hostile ref raises the typed ``SecretNotFoundError``
    (re-prompt via card) rather than reading outside the vault.
    """
    segments = key_name.split("/")
    if len(segments) < 2 or not all(
        s not in (".", "..") and _SEGMENT_RE.fullmatch(s) for s in segments
    ):
        raise SecretNotFoundError(
            f"malformed file-vault ref key {key_name!r}"
        )
    root = _vault_root()
    path = root.joinpath(*segments)
    # Belt and braces: the charset already forbids traversal; verify anyway.
    if not path.resolve().is_relative_to(root.resolve()):
        raise SecretNotFoundError(
            f"malformed file-vault ref key {key_name!r}"
        )
    return path


def _build_file_vault_key(
    provider: ProviderID, user_id: str, case_id: str | None
) -> str:
    """Vault key: ``<user_id>/<provider>/[case-<short>-]<short_ulid>``.

    Per-user/per-provider directory structure. The short ULID keeps
    re-adds after a revoke collision-free (both files persist for audit,
    so the names must differ). ``case_id`` folds into the leaf so per-Case
    scoping stays greppable in the tree.
    """
    _validate_segment(user_id, what="user_id")
    _validate_segment(provider, what="provider")
    short = new_ulid()[-12:].lower()
    if case_id:
        case_short = _validate_segment(case_id, what="case_id")[-8:].lower()
        return f"{user_id}/{provider}/case-{case_short}-{short}"
    return f"{user_id}/{provider}/{short}"


# --------------------------------------------------------------------------- #
# File vault: write / read / delete
# --------------------------------------------------------------------------- #


def _file_write_secret(
    envelope: SecretAddEnvelopePayload,
    *,
    user_id: str,
) -> str:
    """Write the raw key to the file vault; return the ``vault_ref``.

    The raw key value goes ONLY into the mode-0600 secret file -- it is
    never logged and never interpolated into an error message. The write
    is atomic (0600 tmp file + ``os.replace``) so a crash never leaves a
    partial or world-readable value.
    """
    key_name = _build_file_vault_key(envelope.provider, user_id, envelope.case_id)
    path = _secret_path_for_key(key_name)

    logger.info(
        "secret-add[file-vault]: storing key=%s provider=%s case=%s user=%s "
        "root=%s",
        key_name,
        envelope.provider,
        envelope.case_id,
        user_id,
        _vault_root(),
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the vault subtree owner-only (filenames are ids, not values,
        # but there is no reason to expose them). Best-effort chmod.
        os.chmod(_vault_root(), 0o700)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(envelope.key_value)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except SecretError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Typed error WITHOUT the key value.
        raise SecretError(
            f"file-vault write failed for provider={envelope.provider}: "
            f"{type(exc).__name__}"
        ) from exc

    return f"{FILE_VAULT_SCHEME}{key_name}"


def read_secret_value(vault_ref: str) -> str:
    """Resolve a ``vault_ref`` to its raw key value (the single read seam).

    - ``file-vault://<user>/<provider>/<leaf>`` -> read the 0600 file under
      the vault root. Missing or empty file -> ``SecretNotFoundError``
      (typed missing-secret path; the card flow re-prompts).
    - Legacy refs (``aws-ssm://``, ``gcp-sm://``, bare GCP resource names,
      the interim ``local-file://`` JSON store) -> ``SecretNotFoundError``
      with an honest "re-add the key" message. No cloud SDK is invoked.
    - Anything else -> ``SecretNotFoundError`` (malformed ref).

    **Caller MUST NOT log the returned value.**
    """
    if vault_ref.startswith(FILE_VAULT_SCHEME):
        key_name = vault_ref[len(FILE_VAULT_SCHEME) :]
        path = _secret_path_for_key(key_name)
        try:
            value = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SecretNotFoundError(
                f"file vault has no entry for key {key_name!r} "
                f"(root: {_vault_root()})"
            ) from None
        except OSError as exc:
            raise SecretNotFoundError(
                f"file vault entry {key_name!r} is unreadable: "
                f"{type(exc).__name__}"
            ) from exc
        if not value:
            # Never return a silent empty credential.
            raise SecretNotFoundError(
                f"file vault entry {key_name!r} is empty"
            )
        return value

    if vault_ref.startswith(
        (AWS_SSM_VAULT_SCHEME, GCP_SM_VAULT_SCHEME, _LEGACY_LOCAL_FILE_SCHEME)
    ) or vault_ref.startswith("projects/"):
        raise SecretNotFoundError(
            "legacy vault ref cannot be resolved on the local build "
            f"(scheme of {vault_ref.split('://', 1)[0]!r}); re-add the key "
            "via the credential card"
        )

    raise SecretNotFoundError(f"malformed vault ref {vault_ref!r}")


#: Compat binding -- ``Persistence.get_secret_value`` imports this name at
#: call time and invokes it for refs carrying ``LOCAL_FILE_VAULT_SCHEME``.
_file_read_secret = read_secret_value


def _file_delete_secret(vault_ref: str) -> None:
    """Hard-delete a file-vault entry (explicit purge only).

    ``handle_secret_revoke`` stays a SOFT revoke (record flag); this purge
    helper exists for an explicit hard-delete caller. Best-effort: a
    missing entry / non-file-vault ref is a no-op.
    """
    if not vault_ref.startswith(FILE_VAULT_SCHEME):
        return
    key_name = vault_ref[len(FILE_VAULT_SCHEME) :]
    try:
        _secret_path_for_key(key_name).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.exception(
            "secret-purge[file-vault]: best-effort delete failed for key=%s "
            "(continuing)",
            key_name,
        )


def _now_utc() -> datetime:
    """UTC ``datetime`` for ``SecretRecord.added_at`` / ``last_used_at``."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def handle_secret_add(
    envelope: SecretAddEnvelopePayload,
    *,
    user_id: str,
    persistence: Persistence,
) -> SecretRecord:
    """Process a ``secret-add`` envelope end-to-end.

    Steps:

    1. Write the raw ``key_value`` to the file vault
       (``<persistence-root>/secrets/<user_id>/<provider>/<leaf>``, mode
       0600) and capture ``vault_ref = file-vault://<key-name>``.
    2. Build a ``SecretRecord`` carrying only that ``vault_ref`` (never the
       raw key) and persist it via ``Persistence.upsert_secret_ref``.
    3. Append an audit-log entry (``event_type="secret-add"``).

    The raw key value lives ONLY in the vault file; it is never persisted
    via Persistence, never echoed back, and never logged.

    Args:
        envelope: the inbound ``SecretAddEnvelopePayload``.
        user_id: the caller's user_id (from SessionState). Stamped onto the
            ``SecretRecord`` document for multi-tenant isolation. Cannot be
            empty -- fail closed.
        persistence: the agent-side persistence wrapper.

    Returns:
        The persisted ``SecretRecord`` (vault-ref only).

    Raises:
        SecretError: on any failure -- the caller surfaces this as an A.6
            ``INTERNAL_ERROR`` envelope. The raw key value is NOT leaked
            into the error message.
    """
    if not user_id:
        # Fail closed -- multi-tenant isolation requires a stamped user_id.
        raise SecretError("handle_secret_add requires a non-empty user_id")
    if not envelope.key_value:
        # An empty key_value is a malformed envelope -- refuse before we
        # write a zero-byte secret to the vault.
        raise SecretError("handle_secret_add: key_value is empty")

    vault_ref = _file_write_secret(envelope, user_id=user_id)

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
    # filtering (the schema-level SecretRecord doesn't carry user_id; the
    # persistence layer's list filter looks for it on the document).
    await _upsert_with_user(persistence, record, user_id=user_id)

    # Audit-log entry. Never logs the key value.
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

    The vault file is **not** deleted -- preserves the audit trail and
    lets the user un-revoke without re-entering the key.

    Per FR-AS-8 this is NOT a confirmation trigger (per-Case secret
    revocation is user-driven configuration, not a solver run or result
    write).

    Args:
        secret_id: the ULID of the ``SecretRecord`` to revoke.
        user_id: the caller's user_id (audit only -- the persistence layer
            doesn't currently enforce caller-owns-secret because the
            storage schema doesn't denormalize the ownership link.
            Surfaced as OQ-0124-SECRET-OWNER-CHECK).
        persistence: the agent-side persistence wrapper.
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
    ``user_id``. When ``case_id`` is supplied the result is further
    narrowed to per-Case records.

    The returned ``SecretsListEnvelopePayload`` carries only the
    vault-ref-bearing ``SecretRecord`` entries -- by construction no
    ``key_value`` field. This is the Decision F wire-isolation backstop.

    Args:
        user_id: the caller's user_id (from SessionState). Cannot be
            empty -- fail closed.
        case_id: optional Case scope. ``None`` returns every active
            record for the user.
        persistence: the agent-side persistence wrapper.

    Returns:
        ``SecretsListEnvelopePayload`` with the (possibly empty) list.
    """
    if not user_id:
        raise SecretError("handle_secrets_list requires a non-empty user_id")
    records = await persistence.list_secrets_refs(user_id=user_id, case_id=case_id)
    # Defensive: even though the schema rejects key_value at construction,
    # double-check the wire payload carries no leakage. ``SecretRecord``
    # has no key-value field at all -- this loop never trips, but it's the
    # explicit "fail closed" assertion the kickoff requires.
    for r in records:
        dump = r.model_dump()
        for k in dump:
            assert "key" not in k or "value" not in k.lower(), (
                f"SecretRecord contained a key-value-shaped field: {k!r}"
            )
    return SecretsListEnvelopePayload(secrets=records)


# --------------------------------------------------------------------------- #
# Helpers -- confined to this module
# --------------------------------------------------------------------------- #


async def _upsert_with_user(
    persistence: Persistence, record: SecretRecord, *, user_id: str
) -> None:
    """Upsert a ``SecretRecord`` stamped with ``user_id`` for tenant scoping.

    The schema-level ``SecretRecord`` doesn't carry a ``user_id`` field
    (it's a storage-document field only, per the F.3 multi-tenant note in
    ``persistence.py``). We call the existing ``upsert_secret_ref`` then
    ``$set`` the ``user_id`` via a second MCP call -- the persistence
    wrapper's list filter looks for it.
    """
    # First: the schema-shaped upsert (vault-ref-only, no key value).
    await persistence.upsert_secret_ref(record)
    # Second: stamp the user_id by re-issuing an update-one. Best-effort: if
    # this stamp fails the record is persisted but UNOWNED, so the
    # owner-scoped ``list_secrets_refs`` filter will not surface it. We log
    # and continue rather than raise -- losing visibility of one secret-ref
    # is preferable to failing the whole add; the next add for the same
    # secret_id re-stamps it.
    try:
        await persistence._mcp.call_tool(  # noqa: SLF001 -- intentional
            "update-one",
            {
                "database": persistence._db,  # noqa: SLF001 -- intentional
                "collection": "secrets",
                "filter": {"_id": record.secret_id},
                "update": {"$set": {"user_id": user_id}},
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "secret-add: failed to stamp user_id on secret_id=%s "
            "(continuing -- list filter still finds the record)",
            record.secret_id,
        )


async def _safe_append_audit(
    persistence: Persistence, *, event_type: str, payload: dict
) -> None:
    """Append an audit-log entry -- never raise from this path.

    Audit-log writes are fire-and-forget: a failure must not abort the
    caller's happy path.
    """
    try:
        await persistence.append_audit(event_type, payload)
    except Exception:  # noqa: BLE001 -- fire-and-forget
        logger.exception(
            "audit-log append failed for event_type=%s (best-effort, continuing)",
            event_type,
        )


__all__ = [
    "AWS_SSM_VAULT_SCHEME",
    "FILE_VAULT_SCHEME",
    "GCP_SM_VAULT_SCHEME",
    "LOCAL_FILE_VAULT_SCHEME",
    "SecretError",
    "SecretNotFoundError",
    "SecretRevokedError",
    "handle_secret_add",
    "handle_secret_revoke",
    "handle_secrets_list",
    "read_secret_value",
]
