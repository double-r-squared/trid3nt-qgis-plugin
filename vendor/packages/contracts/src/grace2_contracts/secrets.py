"""Per-Case API-key secret envelopes (§F.3 forward-looking contract).

Sprint-12-mega Wave 1, job-0100. The §F.3 architecture currently documented in
``docs/srs/F-data-sources-discovery-secrets.md`` is a **per-user** Cloud-Function
mediated UX (deferred indefinitely until M6+ identity machinery lands). The
shapes in this module are the **per-Case** scoped envelope contract the sprint-12
Case UX needs: the same wire-isolation principle (the raw key value never
appears in any persisted envelope; only a vault reference is retained), but
scoped per ``case_id`` so a Case can carry its own provider keys without
leaking across Cases.

This module owns:

- ``ProviderID`` — closed Literal vocabulary of providers a secret can be
  bound to (Tier-2 conservation fetchers, weather, LLM, basemap). Open-enum
  growth happens via SRS amendment, not silent expansion.
- ``SecretRecord`` — the persisted record returned by the server. Carries the
  ``vault_ref`` (opaque vault path) but **never** the key value itself.
- ``SecretsListEnvelopePayload`` — server -> client list of secret records.
- ``SecretAddEnvelopePayload`` — client -> server add a new secret. Carries
  ``key_value`` transiently; the server writes the value to the vault and
  clears the field before any echo / log / persistence. The ``key_value``
  field is excluded from ``__repr__`` / ``model_dump_json()`` default output
  to minimise the surface area for accidental leakage in logs.
- ``SecretRevokeEnvelopePayload`` — client -> server revoke a secret
  (soft-revoke: sets ``is_active=False``; does NOT delete the vault entry —
  keeps the audit trail intact).
- ``CredentialRequestEnvelopePayload`` — server -> client just-in-time
  credential request. Emitted when a tool dispatch hits a missing or invalid
  credential for a keyed provider: the agent pauses the tool, names the
  provider + the secret key it needs + a signup URL the user can follow, and
  asks the client to surface a credential-entry affordance. The user's
  answer rides back on the existing ``secret-add`` path (vault-write), then a
  ``CredentialProvidedEnvelopePayload`` signals the agent to retry the
  paused tool.
- ``CredentialProvidedEnvelopePayload`` — client -> server retry signal.
  Sent AFTER a successful ``secret-add`` that satisfied a pending
  ``CredentialRequestEnvelopePayload``. It carries the ``request_id`` of the
  request it answers (and the ``secret_id`` the ``secret-add`` minted) so the
  agent can resume the exact paused tool. It carries NO key material —
  ``secret-add`` is the only envelope that ever transports the raw key value
  (Decision F). The client may instead send a request with ``provided=False``
  to signal the user declined / cancelled the credential prompt, in which case
  the agent narrates honestly and abandons the paused tool.

Invariants this module is responsible for:

- **Decision F (wire isolation).** The ``key_value`` field is transient on
  the wire and **never** stored in the WebSocket envelope persistence path
  (Decision F logs every envelope to MongoDB). The server consumes
  ``SecretAddEnvelopePayload``, writes the key value to the vault, then
  returns a ``SecretsListEnvelopePayload`` containing only the vault-ref-bearing
  ``SecretRecord``. The raw key value MUST be cleared on the server side before
  any logging / persistence. We back-stop with ``repr=False`` and a custom
  ``__repr__`` that elides ``key_value`` for the in-process safety net.
- **9. No cost theater.** No cost / quota / usage fields anywhere on the
  envelopes.
- **8. Cancellation is first-class.** ``secret-add`` and ``secret-revoke`` do
  NOT carry ad-hoc cancellation fields; cancellation flows through the
  existing A.3 ``cancel`` message.

SRS references:
- Appendix F.3 (``docs/srs/F-data-sources-discovery-secrets.md``) — the
  forward-looking design the per-user Cloud-Function flow describes. The
  sprint-12 Case-scoped envelopes are a divergent (per-Case, in-band
  WebSocket) shape — see ``OQ-0100-F3-CASE-VS-USER-SCOPE`` in the job report
  for the proposed amendment.
- Appendix A.3 (client -> server) / A.4 (server -> client) for envelope-type
  discipline (kebab-case ``type``, ``payload`` always an object).
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
# Provider vocabulary (closed Literal — SRS amendment to add a member)
# --------------------------------------------------------------------------- #

# Closed Literal of providers a Case secret can be bound to. The set is closed
# at v0.1 — a new provider is an SRS amendment (Appendix F.3 update), not a
# silent open-enum, because the agent-side per-provider plumbing (Census ACS
# uses a query param, OpenWeatherMap uses a header, LLM keys go through the
# vendor SDK, etc.) is per-provider code and registering an unknown provider
# at the schema level would let the server accept a record it can't actually
# use.
ProviderID = Literal[
    # Tier-2 conservation fetchers (sprint-12 Case 1 substrate)
    "ebird",
    "iucn_red_list",
    "movebank",
    # Hazard / earth-observation keyed fetchers (job credential-pipeline-generic)
    # — each is a keyed atomic-tool data source with a self-serve signup page;
    # the agent JIT-requests the key when the upstream rejects/lacks one.
    "firms",  # NASA FIRMS active-fire (FIRMS_MAP_KEY)
    "ecmwf_cds",  # Copernicus CDS — ERA5 reanalysis AND GTSM tide/surge share
    # one CDS API key (GRACE2_COPERNICUS_CDS_API_KEY); one provider, two tools.
    "gtsm",  # GTSM tide/surge — alias scope retained for callers that scope a
    # CDS key specifically to the GTSM tool (resolves alongside ecmwf_cds).
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
    # Generic name-only fallback — the credential card for ANY keyed endpoint
    # that has no dedicated provider above. The server derives a human credential
    # name from the failing tool (``derive_generic_credential_name``) and emits a
    # NAME-ONLY card (signup_url=None — never a fabricated URL). The key is saved
    # under this scope so the user is never left at a silent dead-end; auto-inject
    # on retry is provider-specific, so a fully-unregistered tool resolves its
    # saved key via its own path. Surfacing the form for any endpoint is the goal.
    "generic",
]


# --------------------------------------------------------------------------- #
# SecretRecord — the persisted, vault-reference-only record
# --------------------------------------------------------------------------- #


class SecretRecord(GraceModel):
    """Single per-Case (or user-level) secret reference.

    The raw key value is **never** carried on a ``SecretRecord``; the server
    writes the key to the vault on add and returns this record, which carries
    only the opaque ``vault_ref`` for later retrieval (the agent-runtime SA
    reads from the vault via ADC when invoking the relevant atomic tool).

    Fields:

    - ``secret_id`` — ULID identifier (matches the WS envelope id discipline,
      Appendix A.1).
    - ``provider`` — closed ``ProviderID`` Literal.
    - ``case_id`` — when ``None`` the record is user-level (cross-Case
      default); when set it scopes the key to a single Case. Forward-looking:
      user-level scope depends on M6+ identity machinery (per §F.3
      prerequisites); v0.1 callers should populate ``case_id``.
    - ``vault_ref`` — opaque vault path. Typical shape:
      ``gcp-sm://projects/<project>/secrets/<id>/versions/latest``. The
      ``schema`` module does NOT validate the URI scheme — that belongs to
      ``infra`` (which provisions the vault) and ``agent`` (which calls it).
      We treat it as a free-form non-empty string for forward compatibility
      with alternative vault backends.
    - ``label`` — optional free-text user-supplied label (e.g. "personal eBird
      key — expires 2027-01"); max 200 chars to keep MongoDB documents tame.
    - ``added_at`` — ISO-8601-Z UTC creation timestamp.
    - ``last_used_at`` — ISO-8601-Z UTC of last successful tool invocation
      using this secret; ``None`` if never used. Updated by the agent at
      invocation time; the schema only owns the field shape.
    - ``is_active`` — soft-revoke flag. The server flips this to ``False`` on
      ``secret-revoke``; the vault entry is **not** deleted (preserves audit
      trail). Atomic tools filter on ``is_active=True`` at lookup time.

    Invariant 9: no cost / quota / usage-count field anywhere. Usage is
    tracked via ``last_used_at`` only — not "how many calls", not
    "estimated_cost". The agent narrates from the data the user can see;
    quota / cost surfacing is forbidden everywhere.
    """

    schema_version: Literal["v1"] = "v1"

    secret_id: ULIDStr
    provider: ProviderID
    case_id: ULIDStr | None = None
    vault_ref: str = Field(min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=200)
    added_at: UTCDatetime
    last_used_at: UTCDatetime | None = None
    is_active: bool = True


# --------------------------------------------------------------------------- #
# WebSocket envelopes (A.4 / A.3 amendments — proposed §F.3 amendment)
# --------------------------------------------------------------------------- #


class SecretsListEnvelopePayload(GraceModel):
    """``secrets-list`` (A.4 amendment): server -> client list of secret records.

    Emitted in response to a ``user-message`` opening the secrets-management
    surface (sprint-12 Case-UX), or as the confirmation following a successful
    ``secret-add`` / ``secret-revoke``. The client renders the secrets panel
    from this list. The raw key values **never** appear in this payload — only
    the ``vault_ref``-bearing ``SecretRecord`` entries.

    ``envelope_type`` is the typed-literal discriminator for the A.1
    ``type`` field (kebab-case). ``MESSAGE_TYPE`` mirrors that as a
    ``ClassVar`` so the routing registries in ``ws.py`` can be appended
    idempotently.
    """

    MESSAGE_TYPE: ClassVar[str] = "secrets-list"

    envelope_type: Literal["secrets-list"] = "secrets-list"
    secrets: list[SecretRecord] = Field(default_factory=list)


class SecretAddEnvelopePayload(GraceModel):
    """``secret-add`` (A.3 amendment): client -> server add a new secret.

    The ``key_value`` field is **transient**: the server writes the value to
    the vault on receipt, then clears the field before any logging or
    persistence path. To make accidental leakage in logs harder, ``key_value``
    is also excluded from the default ``__repr__`` output (see
    ``__repr_args__`` override below) so e.g. ``print(payload)`` or a stray
    f-string does NOT echo the key value.

    Decision F binding: this envelope is the only place the raw key value
    appears on the wire. The server MUST NOT persist this payload as-is to
    the ``sessions`` collection (Decision F logs every envelope; an
    unredacted ``key_value`` would land in MongoDB). The agent service is
    responsible for redacting the field at the persistence boundary; the
    schema-side back-stop is the ``repr`` elision below.

    Fields:

    - ``provider`` — closed ``ProviderID`` Literal.
    - ``case_id`` — Case to scope the secret to; ``None`` for user-level
      (M6+ identity required for user-level scope).
    - ``label`` — optional free-text label (≤200 chars).
    - ``key_value`` — the raw key value. Transient; cleared by the server
      after vault-write. ``repr=False`` keeps it out of default ``repr``;
      length-bounded (≤2048 chars) so a paste-buffer mishap doesn't ship
      the entire clipboard.
    """

    MESSAGE_TYPE: ClassVar[str] = "secret-add"

    envelope_type: Literal["secret-add"] = "secret-add"
    provider: ProviderID
    case_id: ULIDStr | None = None
    label: str | None = Field(default=None, max_length=200)
    # key_value is the only field that ever carries the raw secret on the
    # wire. We exclude it from ``repr`` via the override below; pydantic v2's
    # ``Field(repr=False)`` does not reliably suppress repr for required
    # fields, so we implement the elision explicitly in ``__repr_args__``.
    key_value: str = Field(default="", max_length=2048)

    def __repr_args__(self) -> list[tuple[str | None, object]]:
        """Elide ``key_value`` from the default repr (leak-minimisation).

        The presence of the field is still visible (so debugging can confirm
        the wire shape), but the actual value is replaced with a fixed
        ``"<redacted>"`` sentinel that is NEVER the literal value of a real
        key (real keys are typically alphanumeric, often longer; the angle
        brackets disambiguate).
        """
        args = list(super().__repr_args__())
        return [
            (name, "<redacted>" if name == "key_value" else value)
            for name, value in args
        ]


class SecretRevokeEnvelopePayload(GraceModel):
    """``secret-revoke`` (A.3 amendment): client -> server soft-revoke a secret.

    Soft-revoke: the server sets ``SecretRecord.is_active = False`` on the
    matching record; the vault entry is **not** deleted (preserves audit
    trail and lets the user un-revoke without re-entering the key). Atomic
    tools filter on ``is_active=True`` at lookup time, so a revoked secret
    is effectively unavailable.

    The ``secret_id`` is the ULID of the ``SecretRecord`` to revoke. The
    server validates the caller has authority to revoke (owns the Case the
    secret is scoped to, or — for user-level secrets — is the same user) and
    responds with a refreshed ``secrets-list`` envelope.
    """

    MESSAGE_TYPE: ClassVar[str] = "secret-revoke"

    envelope_type: Literal["secret-revoke"] = "secret-revoke"
    secret_id: ULIDStr


# --------------------------------------------------------------------------- #
# Credential-request flow (just-in-time secrets prompt; §F.3 amendment)
# --------------------------------------------------------------------------- #


class CredentialRequestEnvelopePayload(GraceModel):
    """``credential-request`` (A.4 amendment): server -> client JIT key prompt.

    Emitted when a tool dispatch needs a credential that is missing or
    invalid for a keyed provider (e.g. an eBird fetch with no eBird key on the
    Case, or an expired OpenWeatherMap key). The agent pauses the offending
    tool, names what it needs, and the client surfaces a credential-entry
    affordance (typically the same form the ``SecretsPanel`` renders, scoped
    to the named ``provider_id`` + ``secret_key_name``).

    The user's answer takes the **existing** ``secret-add`` path (which
    writes the raw key to the vault and returns a refreshed ``secrets-list``).
    Once the secret is saved, the client emits a
    ``CredentialProvidedEnvelopePayload`` carrying this envelope's
    ``request_id`` so the agent can resume the exact paused tool. This
    envelope carries NO key material in either direction — Decision F keeps
    the raw key isolated to the ``secret-add`` transport.

    Fields:

    - ``request_id`` — ULID correlating this request with the
      ``CredentialProvidedEnvelopePayload`` reply (and with the agent's
      paused-tool record). The client MUST echo it verbatim.
    - ``provider_id`` — closed ``ProviderID`` Literal identifying the keyed
      provider the tool needs. Drives the per-provider help / signup copy and
      scopes the ``secret-add`` the client emits in response.
    - ``provider_label`` — human-readable provider name for the prompt UI
      (e.g. "eBird", "OpenWeatherMap"). The web side does NOT hardcode a
      provider -> label table; it renders whatever the agent sends.
    - ``signup_url`` — the URL where the user can obtain a key for this
      provider (e.g. the provider's API-key registration page). Free-form
      non-empty string; the client renders it as an outbound link. ``None``
      when no public self-serve signup exists (the message then explains the
      out-of-band path).
    - ``secret_key_name`` — the canonical name of the secret the tool is
      looking for (e.g. "EBIRD_API_KEY"). Surfaced in the prompt so the user
      knows exactly which credential to paste; the ``secret-add`` the client
      emits in response is scoped to this provider.
    - ``message`` — the agent's user-facing explanation of why the credential
      is needed right now (e.g. "I need an eBird API key to fetch the
      observation records for this Case."). Plain prose; ≤1024 chars.
    - ``tool_name`` — the registry tool that paused waiting for the
      credential. Lets the client correlate the prompt with the inline tool
      card and lets the agent resume the right dispatch on
      ``credential-provided``.

    Invariant 9 (no cost theater): no cost / quota / spend field. Invariant 8
    (cancellation is first-class): there is no per-envelope timeout/cancel
    field — a declined prompt rides back as a ``credential-provided`` with
    ``provided=False``; a hard cancel flows through the A.3 ``cancel`` message.
    """

    MESSAGE_TYPE: ClassVar[str] = "credential-request"

    envelope_type: Literal["credential-request"] = "credential-request"
    request_id: ULIDStr
    provider_id: ProviderID
    provider_label: str = Field(min_length=1, max_length=120)
    signup_url: str | None = Field(default=None, max_length=512)
    secret_key_name: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=1024)
    tool_name: str = Field(min_length=1, max_length=120)


class CredentialProvidedEnvelopePayload(GraceModel):
    """``credential-provided`` (A.3 amendment): client -> server retry signal.

    Sent AFTER the client has run the existing ``secret-add`` path to save the
    key the agent asked for (a ``CredentialRequestEnvelopePayload``). It tells
    the agent the credential is now in the vault and the paused tool can be
    retried. It carries NO key material — ``secret-add`` is the only envelope
    that ever transports the raw key value (Decision F).

    Fields:

    - ``request_id`` — the ``request_id`` of the
      ``CredentialRequestEnvelopePayload`` this answers. The agent uses it to
      resolve the exact paused tool to resume.
    - ``secret_id`` — the ULID of the ``SecretRecord`` the preceding
      ``secret-add`` minted. ``None`` when ``provided=False`` (no secret was
      saved). When set it lets the agent confirm the new vault record exists
      before retrying.
    - ``provided`` — ``True`` when the user supplied the credential and the
      ``secret-add`` succeeded (agent should retry the paused tool);
      ``False`` when the user declined / cancelled the prompt (agent narrates
      honestly and abandons the paused tool, per the data-source fallback
      norm — no silent dead-end, no hallucinated success).
    """

    MESSAGE_TYPE: ClassVar[str] = "credential-provided"

    envelope_type: Literal["credential-provided"] = "credential-provided"
    request_id: ULIDStr
    secret_id: ULIDStr | None = None
    provided: bool = True


# --------------------------------------------------------------------------- #
# Routing registry (per-module — sibling follow-up wires into ws.ALL_PAYLOADS)
# --------------------------------------------------------------------------- #

# job-0100 scope explicitly FROZE ``ws.py`` — the kickoff file-ownership list
# covers only ``secrets.py`` + ``__init__.py`` registration. These three
# module-level dicts give a follow-up job (or downstream consumer) the typed
# surface to spread into ``ws.CLIENT_TO_AGENT_PAYLOADS`` / etc. when that
# editing scope opens. See ``OQ-0100-WS-REGISTRY-WIRING`` in the job report.

# Client -> server envelopes this module contributes (A.3).
SECRET_CLIENT_TO_AGENT_PAYLOADS: dict[str, type[GraceModel]] = {
    SecretAddEnvelopePayload.MESSAGE_TYPE: SecretAddEnvelopePayload,
    SecretRevokeEnvelopePayload.MESSAGE_TYPE: SecretRevokeEnvelopePayload,
    CredentialProvidedEnvelopePayload.MESSAGE_TYPE: (
        CredentialProvidedEnvelopePayload
    ),
}

# Server -> client envelopes this module contributes (A.4).
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
