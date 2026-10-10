"""Per-Case API-key secret envelopes.

Wire isolation: ``secret-add`` is the only envelope that carries a raw key value, and it is transient - cached for the
session and cleared before any log or persistence path. Everything else carries an opaque vault reference. A key is
scoped by the credential name its row declares, so the rows are the closed set.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .common import (
    ContractModel,
    ULIDStr,
    UTCDatetime,
)

__all__ = [
    "SecretRecord",
    "SecretsListEnvelopePayload",
    "SecretAddEnvelopePayload",
    "LANGUAGE_MODEL_CREDENTIAL",
    "SECRET_PAYLOADS",
    "SECRET_CLIENT_TO_AGENT_PAYLOADS",
    "SECRET_AGENT_TO_CLIENT_PAYLOADS",
]



class SecretRecord(ContractModel):
    """One secret reference. The raw key value is NEVER carried here.
    Usage is recorded as ``last_used_at`` and nothing more - no call count, no
    quota, no estimated cost.
    """

    schema_version: Literal["v1"] = "v1"

    secret_id: ULIDStr
    #: The credential name a row declares in its ``auth.credential`` block.
    provider: str = Field(min_length=1, max_length=120)
    #: None makes the record user-level, a cross-Case default; set, it scopes the key to one Case.
    case_id: ULIDStr | None = None
    #: Opaque vault path; the scheme is not validated, so another vault backend needs no contract change.
    vault_ref: str = Field(min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=200)
    added_at: UTCDatetime
    last_used_at: UTCDatetime | None = None
    #: Soft-revoke flag: a revoke does not delete the vault entry, so the audit trail survives; lookups filter on True.
    is_active: bool = True




#: The credential name for the language model's own key; every other name is one a row declares.
LANGUAGE_MODEL_CREDENTIAL = "llm"


class SecretsListEnvelopePayload(ContractModel):
    """``secrets-list``: server -> client, the secret records.
    A raw key value NEVER appears here - only vault-reference-bearing records.
    """

    MESSAGE_TYPE: ClassVar[str] = "secrets-list"

    envelope_type: Literal["secrets-list"] = "secrets-list"
    secrets: list[SecretRecord] = Field(default_factory=list)


class SecretAddEnvelopePayload(ContractModel):
    """``secret-add``: client -> server, the ONE envelope carrying a raw key.
    ``key_value`` is TRANSIENT - vaulted on receipt and cleared before any log
    or persistence path. The repr elision below is only a back-stop."""

    MESSAGE_TYPE: ClassVar[str] = "secret-add"

    envelope_type: Literal["secret-add"] = "secret-add"
    #: The credential name the key is stored under; one push serves every row naming it.
    provider: str = Field(min_length=1, max_length=120)
    case_id: ULIDStr | None = None
    label: str | None = Field(default=None, max_length=200)
    # The only field that carries a raw secret on the wire. ``repr`` is elided in ``__repr_args__`` because
    # ``Field(repr=False)`` does not reliably suppress repr for a required field. Length-bounded against a paste-buffer mishap.
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


SECRET_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[ContractModel]] = {
    SecretAddEnvelopePayload.MESSAGE_TYPE: SecretAddEnvelopePayload,
}

SECRET_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[ContractModel]] = {
    SecretsListEnvelopePayload.MESSAGE_TYPE: SecretsListEnvelopePayload,
}

SECRET_PAYLOADS: dict[str, type[ContractModel]] = {
    **SECRET_CLIENT_TO_AGENT_PAYLOADS,
    **SECRET_AGENT_TO_CLIENT_PAYLOADS,
}
