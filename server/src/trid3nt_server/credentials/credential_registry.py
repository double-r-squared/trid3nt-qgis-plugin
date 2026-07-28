"""Per-provider credential registry — the agent-side credential pipeline (job: VAULT-READ).

When a keyed tool dispatch hits a missing or invalid credential, the server
pauses the tool and emits a ``credential-request`` envelope
(``trid3nt_contracts.secrets.CredentialRequestEnvelopePayload``) so the web
client can surface a just-in-time key-entry affordance. To build that envelope
the server needs, per provider:

- ``provider_id`` — the closed ``ProviderID`` Literal the ``secret-add`` reply
  is scoped to (so the saved key lands in the right per-Case slot).
- ``label`` — the human-readable provider name for the prompt UI ("NASA FIRMS").
- ``signup_url`` — where the user obtains a key.
- ``secret_key_name`` — the canonical name of the credential the tool wants
  ("FIRMS_MAP_KEY"), surfaced in the prompt so the user pastes the right thing.

This module is the single per-provider map. It is intentionally tiny and
data-only: each entry is one ``CredentialProvider`` dataclass, keyed by the
``ProviderID`` value. ALL keyed atomic-tool data sources are members:
FIRMS (``fetch_firms_active_fire``), eBird (``fetch_ebird_observations``),
Copernicus CDS — ERA5 + GTSM share one CDS key
(``fetch_era5_reanalysis`` / ``fetch_gtsm_tide_surge``), Movebank
(``fetch_movebank_tracks``), and the IUCN Red List
(``fetch_iucn_red_list_range``). Every entry mirrors the same
``secret_ref`` → ``Persistence.get_secret_value`` → env-var key-resolution
pattern, so a provider joins by adding one row here plus its tool-name →
provider mapping in ``TOOL_PROVIDER`` and its auth/missing error codes in
``TOOL_AUTH_ERROR_CODES``.

``ProviderID`` scope: every ``provider_id`` below is now a member of the
closed ``ProviderID`` Literal in ``trid3nt_contracts.secrets`` (the schema
amendment landed alongside this job), so the server's envelope builder
validates each provider_id directly — there is no longer a fallback scope.
The saved key therefore lands under the SAME provider scope the
``credential-request`` named, which is exactly the scope
``_resolve_active_secret_ref`` re-reads on retry, so the round-trip closes.
We keep ``provider_id`` typed as a plain ``str`` here only so the registry
stays import-light (it does not import the contracts Literal); the server
validates it against the live ``ProviderID`` at emit time.

Generic classification: ``is_credential_error`` detects a "needs an API key"
condition from ANY tool — not just FIRMS — via (a) ``error_code`` patterns
(``*_AUTH_ERROR`` / ``*_MISSING_KEY`` suffixes, or a code containing
``API_KEY`` / ``APIKEY`` / ``UNAUTHORIZED``), (b) an HTTP 401/403 surfaced on
the typed error, and (c) message/body text mentioning "api key" / "key
required" / "unauthorized" / "invalid key". A credential error from a tool
with NO registered provider returns ``False`` (the server narrates honestly —
it cannot request a key for an unknown provider — and never fabricates one).

Invariant 9 (no cost theater): no quota / cost / spend field anywhere here.
Decision F (wire isolation): this registry carries NO key material — only the
metadata needed to ASK for a key. The raw key always rides the ``secret-add``
transport and is read back from the vault by the tool's ``_resolve_*_key``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CredentialProvider",
    "CREDENTIAL_PROVIDERS",
    "TOOL_PROVIDER",
    "TOOL_AUTH_ERROR_CODES",
    "GENERIC_PROVIDER_ID",
    "get_provider",
    "provider_for_tool",
    "is_credential_error",
    "is_credential_shaped_error",
    "derive_generic_credential_name",
    "generic_provider_for_tool",
]

# Provider-id used for the NAME-ONLY generic credential card emitted when a
# credential-shaped failure comes from a tool that is NOT in this registry
# (NATE principle 3, 2026-06-18: still surface a card — a credential NAME + a
# secret-entry form — rather than letting the LLM free-text a possibly-fake
# signup URL). This id is NOT a real provider scope; it carries no signup_url.
# The server only emits the generic card when this id is a valid wire
# ``ProviderID`` (the schema owns that Literal); until then the server falls
# back to surfacing the original typed error — it NEVER fabricates a URL.
GENERIC_PROVIDER_ID = "generic"


@dataclass(frozen=True)
class CredentialProvider:
    """One keyed provider's just-in-time credential-request metadata.

    Fields map 1:1 onto ``CredentialRequestEnvelopePayload`` (minus the
    per-request ``request_id`` / ``message`` / ``tool_name`` the server mints
    at emit time):

    - ``provider_id`` — the value the ``secret-add`` the client emits in
      response is scoped to. Held as a plain string so this registry is
      decoupled from the ``ProviderID`` Literal's enum-rollout cadence (the
      server validates it against the live Literal at envelope-build time).
    - ``label`` — human-readable provider name for the prompt UI.
    - ``signup_url`` — where the user obtains a key (``None`` for out-of-band).
    - ``secret_key_name`` — canonical name of the credential the tool wants
      (the same env-var name the tool's ``_resolve_*_key`` reads as its env
      fallback, so the user-facing name and the code path agree).
    - ``default_message`` — fallback user-facing copy when the server has no
      tool-specific message. Kept short and honest (data-source fallback norm:
      tell the user a key is needed, no silent dead-end).
    """

    provider_id: str
    label: str
    signup_url: str | None
    secret_key_name: str
    default_message: str


# --------------------------------------------------------------------------- #
# Provider registry — ALL keyed atomic-tool data sources. Every provider_id is
# a member of the closed ``ProviderID`` Literal in ``trid3nt_contracts.secrets``
# so the saved key lands under the same scope ``_resolve_active_secret_ref``
# re-reads on retry. Each ``secret_key_name`` is the SAME env-var name the
# tool's ``_resolve_*_key`` reads as its env fallback, so the user-facing name
# and the code path agree.
# --------------------------------------------------------------------------- #

CREDENTIAL_PROVIDERS: dict[str, CredentialProvider] = {
    "firms": CredentialProvider(
        provider_id="firms",
        label="NASA FIRMS",
        signup_url="https://firms.modaps.eosdis.nasa.gov/api/map_key/",
        secret_key_name="FIRMS_MAP_KEY",
        default_message=(
            "NASA FIRMS needs a free MAP_KEY to fetch active-fire detections. "
            "Add your FIRMS MAP_KEY and I'll retry the fetch."
        ),
    ),
    "ebird": CredentialProvider(
        provider_id="ebird",
        label="eBird",
        signup_url="https://ebird.org/api/keygen",
        secret_key_name="EBIRD_API_KEY",
        default_message=(
            "eBird needs a free API key to fetch observation records. "
            "Add your eBird API key and I'll retry the fetch."
        ),
    ),
    # Copernicus CDS — ONE key (TRID3NT_COPERNICUS_CDS_API_KEY) serves BOTH the
    # ERA5 reanalysis tool and the GTSM tide/surge tool. They share this single
    # ``ecmwf_cds`` provider scope so a CDS key saved for either tool resolves
    # for both on retry.
    "ecmwf_cds": CredentialProvider(
        provider_id="ecmwf_cds",
        label="Copernicus Climate Data Store",
        signup_url="https://cds.climate.copernicus.eu/how-to-api",
        secret_key_name="TRID3NT_COPERNICUS_CDS_API_KEY",
        default_message=(
            "This dataset needs a free Copernicus Climate Data Store (CDS) API "
            "key. Add your CDS key and I'll retry the fetch."
        ),
    ),
    "movebank": CredentialProvider(
        provider_id="movebank",
        label="Movebank",
        signup_url="https://www.movebank.org/cms/movebank-login",
        secret_key_name="MOVEBANK_CREDENTIALS",
        default_message=(
            "Movebank needs your account credentials to fetch animal-tracking "
            "data. Add your Movebank login (as a JSON object with 'username' "
            "and 'password') and I'll retry the fetch."
        ),
    ),
    "iucn_red_list": CredentialProvider(
        provider_id="iucn_red_list",
        label="IUCN Red List",
        signup_url="https://api.iucnredlist.org/users/sign_up",
        secret_key_name="TRID3NT_IUCN_RED_LIST_API_KEY",
        default_message=(
            "The IUCN Red List API needs a free access token. "
            "Add your IUCN Red List token and I'll retry the fetch."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Tool → provider mapping. A tool name resolves to the provider whose key it
# needs. ERA5 and GTSM both route to the shared ``ecmwf_cds`` CDS provider.
# --------------------------------------------------------------------------- #

TOOL_PROVIDER: dict[str, str] = {
    "fetch_firms_active_fire": "firms",
    "fetch_ebird_observations": "ebird",
    "fetch_era5_reanalysis": "ecmwf_cds",
    "fetch_gtsm_tide_surge": "ecmwf_cds",
    "fetch_movebank_tracks": "movebank",
    "fetch_iucn_red_list_range": "iucn_red_list",
}


# --------------------------------------------------------------------------- #
# Per-tool auth/credential error-code set. The server treats a dispatch
# failure whose ``error_code`` is in this tool's set (OR whose exception is the
# tool's credential-error class, OR whose error matches the generic credential
# heuristics in ``is_credential_error``) as a "needs a key" signal: pause +
# emit ``credential-request`` + retry on provided. Each set lists the tool's
# explicit ``*_AUTH_ERROR`` / ``*_MISSING_KEY`` typed-error codes; the generic
# pattern matcher in ``is_credential_error`` is the catch-all for codes/bodies
# that don't appear here (e.g. a 401 surfaced under an UPSTREAM code).
# --------------------------------------------------------------------------- #

TOOL_AUTH_ERROR_CODES: dict[str, frozenset[str]] = {
    "fetch_firms_active_fire": frozenset(
        {"FIRMS_AUTH_ERROR", "FIRMS_MISSING_KEY"}
    ),
    "fetch_ebird_observations": frozenset(
        {"EBIRD_AUTH_ERROR", "EBIRD_MISSING_KEY"}
    ),
    "fetch_era5_reanalysis": frozenset(
        {"ERA5_AUTH_ERROR", "ERA5_MISSING_KEY"}
    ),
    "fetch_gtsm_tide_surge": frozenset(
        {"GTSM_AUTH_ERROR", "GTSM_MISSING_KEY"}
    ),
    "fetch_movebank_tracks": frozenset(
        {"MOVEBANK_AUTH_ERROR"}
    ),
    "fetch_iucn_red_list_range": frozenset(
        {"IUCN_AUTH_ERROR"}
    ),
}


# --------------------------------------------------------------------------- #
# Generic "needs an API key" detection helpers (provider-agnostic). These back
# ``is_credential_error`` so a credential failure is caught regardless of which
# tool raised it or whether the tool authored an explicit ``*_AUTH_ERROR`` code.
# --------------------------------------------------------------------------- #

# Substrings that, when present in an ``error_code``, mark it credential-shaped.
_CREDENTIAL_CODE_SUBSTRINGS: tuple[str, ...] = (
    "API_KEY",
    "APIKEY",
    "AUTH_ERROR",
    "MISSING_KEY",
    "UNAUTHORIZED",
    "FORBIDDEN",
)

# Phrases that, when present in the error message/body text (case-insensitive),
# mark it credential-shaped. Kept narrow + specific to avoid false positives on
# generic upstream errors.
_CREDENTIAL_TEXT_PHRASES: tuple[str, ...] = (
    "api key",
    "api-key",
    "apikey",
    "key required",
    "requires a key",
    "requires an api key",
    "needs a key",
    "needs an api key",
    "missing key",
    "missing api key",
    "no api key",
    "unauthorized",
    "invalid key",
    "invalid api key",
    "invalid map_key",
    "invalid token",
    "access token",
    "authentication required",
    "authentication failed",
    "not authorized",
    # Config-missing family — a credential-shaped failure whose message names a
    # missing/incomplete credentials CONFIG rather than the literal words "api
    # key" (LIVE BUG NATE 2026-06-18: ERA5's no-key path surfaced
    # "Missing/incomplete configuration file: /root/.cdsapirc", which matched
    # NONE of the phrases above, so no credential card fired). Kept narrow +
    # specific so a generic upstream/outage message does NOT trip the gate.
    ".cdsapirc",
    "missing/incomplete configuration",
    "missing or incomplete configuration",
    "incomplete configuration file",
    "no api key configured",
    "no api key found",
    "credentials not configured",
    "no credentials found",
    "credential not configured",
)


def _error_code_is_credential_shaped(error_code: object) -> bool:
    """True when ``error_code`` (a string) matches a credential pattern.

    Matches a code ending in ``_AUTH_ERROR`` / ``_MISSING_KEY`` (the FR-AS-11
    typed-error convention every keyed tool follows) OR containing any of the
    generic credential substrings (``API_KEY`` / ``APIKEY`` / ``UNAUTHORIZED``
    / ``FORBIDDEN``).
    """
    if not isinstance(error_code, str) or not error_code:
        return False
    ec = error_code.upper()
    if ec.endswith("_AUTH_ERROR") or ec.endswith("_MISSING_KEY"):
        return True
    return any(sub in ec for sub in _CREDENTIAL_CODE_SUBSTRINGS)


def _http_status_is_credential(error: BaseException) -> bool:
    """True when a typed error surfaces an HTTP 401/403.

    Checks the common attribute names tools attach a status under
    (``status_code`` / ``http_status`` / ``status``) so a tool that raises an
    UPSTREAM-coded error carrying a 401/403 still classifies as credential.
    """
    for attr in ("status_code", "http_status", "status"):
        val = getattr(error, attr, None)
        if isinstance(val, int) and val in (401, 403):
            return True
        if isinstance(val, str) and val.strip() in ("401", "403"):
            return True
    return False


def _message_text_is_credential(error: BaseException) -> bool:
    """True when the error message/body text reads like a missing-key signal."""
    text = str(error).lower()
    if not text:
        return False
    return any(phrase in text for phrase in _CREDENTIAL_TEXT_PHRASES)


def get_provider(provider_id: str) -> CredentialProvider | None:
    """Return the ``CredentialProvider`` for ``provider_id`` (or ``None``)."""
    return CREDENTIAL_PROVIDERS.get(provider_id)


def provider_for_tool(tool_name: str) -> CredentialProvider | None:
    """Return the ``CredentialProvider`` a tool needs a key from (or ``None``).

    A ``None`` return means the tool is not key-requiring (or its provider is
    not yet registered) — the server does NOT emit a credential-request for it
    and the dispatch error flows through the normal typed-error surface.
    """
    pid = TOOL_PROVIDER.get(tool_name)
    if pid is None:
        return None
    return CREDENTIAL_PROVIDERS.get(pid)


def is_credential_error(tool_name: str, error: BaseException) -> bool:
    """True when ``error`` from ``tool_name`` is a missing/invalid-credential signal.

    Generic across ALL keyed tools (NATE 2026-06-17: "it should not just be
    FIRMS but ANY gate where the agent gets back a body that says you need an
    api key"). Matches on ANY of:

      1. the exception's ``error_code`` being in the tool's
         ``TOOL_AUTH_ERROR_CODES`` set (the explicit per-tool list), OR
      2. the ``error_code`` being credential-SHAPED by pattern — ends in
         ``_AUTH_ERROR`` / ``_MISSING_KEY``, or contains ``API_KEY`` /
         ``APIKEY`` / ``UNAUTHORIZED`` / ``FORBIDDEN`` — so a tool that
         surfaces a 401 under, say, an ``*_UPSTREAM_ERROR`` code with a
         credential-shaped variant still classifies, OR
      3. an HTTP 401/403 attached to the typed error
         (``status_code`` / ``http_status`` / ``status``), OR
      4. the message/body text reading like a missing-key signal
         ("api key" / "key required" / "unauthorized" / "invalid key" / ...),
         OR
      5. the exception class name matching a known credential-error class
         family (defensive fallback if no code/text/status is present).

    Gating rule (HONEST, NO FABRICATION): only returns True for a tool that has
    a registered provider in ``TOOL_PROVIDER``. A credential-shaped error from a
    tool with no provider returns ``False`` here — the server then asks
    ``is_credential_shaped_error`` (provider-agnostic) whether to surface a
    NAME-ONLY generic card (NATE principle 3) instead of fabricating a
    provider/URL.
    """
    if provider_for_tool(tool_name) is None:
        return False
    # The error is credential-shaped by the same provider-agnostic checks the
    # generic path uses; the only difference here is the registered-provider
    # gate above (so a REGISTERED tool routes to its real provider card).
    return is_credential_shaped_error(tool_name, error)


def is_credential_shaped_error(tool_name: str, error: BaseException) -> bool:
    """True when ``error`` looks like a missing/invalid-credential signal.

    Provider-AGNOSTIC: unlike ``is_credential_error`` this does NOT require the
    tool to have a registered provider. It is the shared shape-detector both
    paths use:

    - ``is_credential_error`` calls it AFTER confirming the tool has a
      registered provider (→ a real per-provider card with a real signup_url).
    - the server's generic fallback (NATE principle 3) calls it for a tool with
      NO registered provider, to decide whether to surface a NAME-ONLY card
      (credential name + secret-entry form, signup_url=None) rather than letting
      the LLM narrate a possibly-fabricated URL.

    Matches on ANY of: an explicit per-tool ``TOOL_AUTH_ERROR_CODES`` code; a
    credential-SHAPED ``error_code`` (``*_AUTH_ERROR`` / ``*_MISSING_KEY`` /
    contains ``API_KEY`` / ``UNAUTHORIZED`` / ``FORBIDDEN``); an HTTP 401/403 on
    the typed error; a message/body that reads like a missing-key signal (incl.
    the config-missing family — ``.cdsapirc`` / "missing/incomplete
    configuration" / "credentials not configured"); or a known
    ``*AuthError`` / ``*MissingKeyError`` exception-class family.
    """
    # 1 + 2. error_code: explicit per-tool set, then generic pattern.
    ec = getattr(error, "error_code", None)
    codes = TOOL_AUTH_ERROR_CODES.get(tool_name)
    if codes and isinstance(ec, str) and ec in codes:
        return True
    if _error_code_is_credential_shaped(ec):
        return True

    # 3. HTTP 401/403 surfaced on the typed error.
    if _http_status_is_credential(error):
        return True

    # 4. Message / body text reads like a missing-key signal.
    if _message_text_is_credential(error):
        return True

    # 5. Defensive class-name fallback (an auth/missing-key exception that lost
    #    its error_code and carries an unhelpful message). Narrow: only the
    #    *Auth* / *MissingKey* exception class families across keyed tools.
    cls_name = type(error).__name__
    if (
        cls_name.endswith("AuthError")
        or cls_name.endswith("MissingKeyError")
        or cls_name in (
            "FirmsAuthError",
            "FirmsMissingKeyError",
            "EBirdAuthError",
            "EBirdMissingKeyError",
            "ERA5AuthError",
            "ERA5MissingKeyError",
            "GTSMAuthError",
            "GTSMMissingKeyError",
            "MovebankAuthError",
            "IUCNAuthError",
        )
    ):
        return True
    return False


def derive_generic_credential_name(tool_name: str) -> str:
    """Human credential name for a NAME-ONLY generic card (NATE principle 3).

    For a credential-shaped failure from a tool NOT in this registry, the server
    has no real provider label or ``secret_key_name`` to show — and MUST NOT
    invent a signup URL. This derives an honest, readable credential name from
    the tool name alone (the only thing we reliably know), e.g.::

        fetch_usgs_water_gauges -> "USGS Water Gauges API key"
        fetch_some_provider_data -> "Some Provider Data API key"
        weird_tool              -> "Weird Tool API key"

    The rules: strip a leading ``fetch_`` / ``get_`` / ``query_`` verb, split on
    underscores, upper-case any short all-letter token (<=4 chars, e.g. "usgs",
    "noaa", "gbif" -> "USGS", "NOAA", "GBIF") else title-case it, and append
    " API key". Always returns a non-empty string so the card's
    ``secret_key_name`` field (min_length=1) is satisfiable.
    """
    raw = (tool_name or "").strip()
    if not raw:
        return "API key"
    parts = [p for p in raw.split("_") if p]
    # Drop a leading fetch/get-style verb so the name reads as the DATA source.
    if len(parts) > 1 and parts[0].lower() in (
        "fetch", "get", "query", "load", "pull", "download", "request",
    ):
        parts = parts[1:]
    words: list[str] = []
    for p in parts:
        if p.isalpha() and len(p) <= 4:
            words.append(p.upper())
        else:
            words.append(p.capitalize())
    base = " ".join(words).strip()
    if not base:
        return "API key"
    return f"{base} API key"


def generic_provider_for_tool(tool_name: str) -> CredentialProvider:
    """Build a NAME-ONLY generic ``CredentialProvider`` (no real provider).

    Used by the server's generic-fallback path (NATE principle 3) for a
    credential-shaped failure from a tool with NO registered provider. Carries:

    - ``provider_id = GENERIC_PROVIDER_ID`` ("generic") — a non-scoping
      sentinel; the server only emits the card if this id is a valid wire
      ``ProviderID`` (schema-owned), else it surfaces the original error.
    - ``signup_url = None`` — NEVER a fabricated URL. The card shows the
      credential NAME + a secret-entry form only (NATE principle 2: no-URL
      fallback).
    - ``secret_key_name`` / ``label`` derived from the tool name.
    """
    name = derive_generic_credential_name(tool_name)
    # secret_key_name as an ENV-style token (e.g. "USGS Water Gauges API key"
    # -> "USGS_WATER_GAUGES_API_KEY") so the prompt names a concrete field.
    key_token = (
        "_".join(name.replace("/", " ").split())
        .upper()
        .replace("-", "_")
    ) or "API_KEY"
    return CredentialProvider(
        provider_id=GENERIC_PROVIDER_ID,
        label=name,
        signup_url=None,
        secret_key_name=key_token,
        default_message=(
            f"This data source needs an API key ({name}). "
            f"Add the key and I'll retry the request."
        ),
    )
