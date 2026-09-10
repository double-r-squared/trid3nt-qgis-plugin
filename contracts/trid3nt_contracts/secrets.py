"""Per-Case API-key secret envelopes.

WIRE ISOLATION is the whole point: ``secret-add`` is the ONLY envelope that
ever carries a raw key value, and it is transient - written to the vault and
cleared before any log or persistence path. Everything else carries an opaque
vault reference. Keys are scoped per Case so one cannot leak across Cases.
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
    "ProviderID",
    "SecretRecord",
    "SecretsListEnvelopePayload",
    "SecretAddEnvelopePayload",
    "SecretRevokeEnvelopePayload",
    "CredentialRequestEnvelopePayload",
    "CredentialProvidedEnvelopePayload",
    "SECRET_PAYLOADS",
    "SECRET_CLIENT_TO_AGENT_PAYLOADS",
    "SECRET_AGENT_TO_CLIENT_PAYLOADS",
]


# --------------------------------------------------------------------------- #
# Provider vocabulary - a closed Literal
# --------------------------------------------------------------------------- #

# Closed Literal of providers a Case secret can be bound to. CLOSED because
# each provider has its own injection plumbing - a query param here, a header
# there, a vendor SDK elsewhere - so admitting an unknown provider here would
# let the server accept a record it cannot actually use.
ProviderID = Literal[
    # Keyed data sources with a self-serve signup page: the key is requested
    # just in time, when the upstream rejects or lacks one.
    "firms",  # NASA FIRMS active-fire (FIRMS_MAP_KEY)
    "ecmwf_cds",  # reanalysis AND tide/surge share ONE key: one provider, two
    # tools.
    "gtsm",  # an alias scope, for a caller that scopes the same key to the
    # tide/surge tool specifically; it resolves alongside ecmwf_cds.
    # Weather (Tier-2 keyed endpoints)
    "nws",
    "openweathermap",
    # LLM providers (per-Case bring-your-own-key)
    "openai",
    "anthropic",
    "google_genai",
    # Basemap providers (paid tier; per-Case scoped)
    "mapbox",
    "maptiler",
    # The name-only fallback: a credential card for ANY keyed endpoint with no
    # dedicated provider above. The name is derived from the failing tool and
    # ``signup_url`` stays None - never a fabricated URL. The key is saved under
    # this scope so the user is never left at a silent dead-end; auto-injection
    # on retry is provider-specific, so such a tool resolves its own key.
    "generic",
]


# --------------------------------------------------------------------------- #
# SecretRecord - the persisted, vault-reference-only record
# --------------------------------------------------------------------------- #


class SecretRecord(GraceModel):
    """One secret reference. The raw key value is NEVER carried here.
    Usage is recorded as ``last_used_at`` and nothing more - no call count, no
    quota, no estimated cost.
    """

    schema_version: Literal["v1"] = "v1"

    secret_id: ULIDStr
    provider: ProviderID
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


# --------------------------------------------------------------------------- #
# WebSocket envelopes
# --------------------------------------------------------------------------- #


class SecretsListEnvelopePayload(GraceModel):
    """``secrets-list``: server -> client, the secret records.
    A raw key value NEVER appears here - only vault-reference-bearing records.
    """

    MESSAGE_TYPE: ClassVar[str] = "secrets-list"

    envelope_type: Literal["secrets-list"] = "secrets-list"
    secrets: list[SecretRecord] = Field(default_factory=list)


class SecretAddEnvelopePayload(GraceModel):
    """``secret-add``: client -> server, the ONE envelope carrying a raw key.
    ``key_value`` is TRANSIENT: written to the vault on receipt and cleared
    before any log or persistence path. Persisting this payload as-is would put
    an unredacted key into storage; the repr elision below is only a back-stop.
    """

    MESSAGE_TYPE: ClassVar[str] = "secret-add"

    envelope_type: Literal["secret-add"] = "secret-add"
    provider: ProviderID
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


# --------------------------------------------------------------------------- #
# Credential-request flow - the just-in-time key prompt
# --------------------------------------------------------------------------- #


class CredentialRequestEnvelopePayload(GraceModel):
    """``credential-request``: server -> client, a key is needed NOW.
    The tool pauses; the answer takes the existing ``secret-add`` path, so this
    envelope carries NO key material in either direction. A declined prompt
    returns as a ``credential-provided`` with ``provided=False``.
    """

    MESSAGE_TYPE: ClassVar[str] = "credential-request"

    envelope_type: Literal["credential-request"] = "credential-request"
    #: Correlates request, reply and the paused tool. Echoed VERBATIM.
    request_id: ULIDStr
    provider_id: ProviderID
    #: The human name to show. A client renders whatever arrives rather than
    #: keeping its own provider -> label table.
    provider_label: str = Field(min_length=1, max_length=120)
    #: Where a key can be obtained. ``None`` when no public self-serve signup
    #: exists - never a fabricated URL; the message then explains the path.
    signup_url: str | None = Field(default=None, max_length=512)
    #: The canonical name of the secret being asked for, so the user knows
    #: exactly which credential to paste.
    secret_key_name: str = Field(min_length=1, max_length=200)
    #: The user-facing explanation of why the credential is needed right now.
    message: str = Field(min_length=1, max_length=1024)
    #: The tool that paused, so the prompt can be tied to its card and the
    #: right dispatch resumed.
    tool_name: str = Field(min_length=1, max_length=120)


class CredentialProvidedEnvelopePayload(GraceModel):
    """``credential-provided``: client -> server, the paused tool may retry.
    Carries NO key material. ``provided=False`` is the decline path: the tool is
    abandoned and narrated honestly - never a silent dead-end or an invented
    success.
    """

    MESSAGE_TYPE: ClassVar[str] = "credential-provided"

    envelope_type: Literal["credential-provided"] = "credential-provided"
    #: Resolves the exact paused tool to resume.
    request_id: ULIDStr
    #: The record the preceding ``secret-add`` minted, so its existence can be
    #: confirmed before the retry. ``None`` when ``provided=False``.
    secret_id: ULIDStr | None = None
    provided: bool = True


# --------------------------------------------------------------------------- #
# Routing registry fragments
# --------------------------------------------------------------------------- #

# Client -> server envelopes this module contributes.
SECRET_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    SecretAddEnvelopePayload.MESSAGE_TYPE: SecretAddEnvelopePayload,
    SecretRevokeEnvelopePayload.MESSAGE_TYPE: SecretRevokeEnvelopePayload,
    CredentialProvidedEnvelopePayload.MESSAGE_TYPE: (
        CredentialProvidedEnvelopePayload
    ),
}

# Server -> client envelopes this module contributes.
SECRET_AGENT_TO_CLIENT_PAYLOADS: dict[str, type[GraceModel]] = {
    SecretsListEnvelopePayload.MESSAGE_TYPE: SecretsListEnvelopePayload,
    CredentialRequestEnvelopePayload.MESSAGE_TYPE: (
        CredentialRequestEnvelopePayload
    ),
}

# Aggregate for downstream consumers that don't care about direction.
SECRET_PAYLOADS: dict[str, type[GraceModel]] = {
    **SECRET_CLIENT_TO_AGENT_PAYLOADS,
    **SECRET_AGENT_TO_CLIENT_PAYLOADS,
}
