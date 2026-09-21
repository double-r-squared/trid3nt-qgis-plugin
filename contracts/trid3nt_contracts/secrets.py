"""Per-Case API-key secret envelopes.

WIRE ISOLATION is the whole point: ``secret-add`` is the ONLY envelope that
ever carries a raw key value, and it is transient - cached for the session and
cleared before any log or persistence path. Everything else carries an opaque
vault reference. A key is scoped by the credential NAME its source row declares,
so the rows are the closed set and this module restates none of them.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .common import (
    GraceModel,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    "SecretRecord",
    "SecretsListEnvelopePayload",
    "SecretAddEnvelopePayload",
    "SecretRevokeEnvelopePayload",
    "SECRET_PAYLOADS",
    "SECRET_CLIENT_TO_AGENT_PAYLOADS",
    "SECRET_AGENT_TO_CLIENT_PAYLOADS",
]



class SecretRecord(GraceModel):
    """One secret reference. The raw key value is NEVER carried here.
    Usage is recorded as ``last_used_at`` and nothing more - no call count, no
    quota, no estimated cost.
    """

    schema_version: Literal["v1"] = "v1"

    secret_id: ULIDStr
    #: The credential name a source row declares in its ``auth.credential``
    #: block. The rows are the closed set, so nothing is restated here.
    provider: str = Field(min_length=1, max_length=120)
    #: ``None`` makes the record user-level, a cross-Case default; set, it
    #: scopes the key to a single Case.
    case_id: ULIDStr | None = None
    #: Opaque vault path. The scheme is deliberately NOT validated here, so an
    #: alternative vault backend needs no contract change.
    vault_ref: str = Field(min_length=1, max_length=512)
    #: Free-text user label. Bounded to keep a stored document tame.
    label: str | None = Field(default=None, max_length=200)
    added_at: UTCDatetime
    #: Last successful use, ``None`` if never used. Stamped at invocation time.
    last_used_at: UTCDatetime | None = None
    #: Soft-revoke flag. A revoke flips this and does NOT delete the vault
    #: entry, so the audit trail survives; lookups filter on True.
    is_active: bool = True




class SecretsListEnvelopePayload(GraceModel):
    """``secrets-list``: server -> client, the secret records.
    A raw key value NEVER appears here - only vault-reference-bearing records.
    """

    MESSAGE_TYPE: ClassVar[str] = "secrets-list"

    envelope_type: Literal["secrets-list"] = "secrets-list"
    secrets: list[SecretRecord] = Field(default_factory=list)


class SecretAddEnvelopePayload(GraceModel):
    """``secret-add``: client -> server, the ONE envelope carrying a raw key.
    ``key_value`` is TRANSIENT - vaulted on receipt and cleared before any log
    or persistence path. The repr elision below is only a back-stop."""

    MESSAGE_TYPE: ClassVar[str] = "secret-add"

    envelope_type: Literal["secret-add"] = "secret-add"
    #: The credential name the key is stored under: the name the source rows
    #: that need this key state, so one push serves every row naming it.
    provider: str = Field(min_length=1, max_length=120)
    #: ``None`` for a user-level secret rather than a Case-scoped one.
    case_id: ULIDStr | None = None
    label: str | None = Field(default=None, max_length=200)
    # The only field that ever carries a raw secret on the wire. ``repr`` is
    # elided in ``__repr_args__`` rather than via ``Field(repr=False)``, which
    # does not reliably suppress repr for a required field. Length-bounded so a
    # paste-buffer mishap does not ship an entire clipboard.
    key_value: str = Field(default="", max_length=2048)

    def __repr_args__(self) -> list[tuple[str | None, object]]:
        """Elide ``key_value`` from the default repr.
        The FIELD stays visible so the wire shape is still debuggable; the value
        becomes a sentinel whose angle brackets no real key can produce."""
        args = list(super().__repr_args__())
        return [
            (name, "<redacted>" if name == "key_value" else value)
            for name, value in args
        ]


class SecretRevokeEnvelopePayload(GraceModel):
    """``secret-revoke``: client -> server, SOFT-revoke one secret.
    The vault entry is not deleted, so the audit trail survives and the user can
    un-revoke without re-entering the key; lookups filter revoked keys out.
    """

    MESSAGE_TYPE: ClassVar[str] = "secret-revoke"

    envelope_type: Literal["secret-revoke"] = "secret-revoke"
    secret_id: ULIDStr




# Client -> server envelopes this module contributes.
SECRET_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    SecretAddEnvelopePayload.MESSAGE_TYPE: SecretAddEnvelopePayload,
    SecretRevokeEnvelopePayload.MESSAGE_TYPE: SecretRevokeEnvelopePayload,
}

# Server -> client envelopes this module contributes.
SECRET_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    SecretsListEnvelopePayload.MESSAGE_TYPE: SecretsListEnvelopePayload,
}

# Aggregate for downstream consumers that don't care about direction.
SECRET_PAYLOADS: dict[str, type[GraceModel]] = {
    **SECRET_CLIENT_TO_AGENT_PAYLOADS,
    **SECRET_AGENT_TO_CLIENT_PAYLOADS,
}
