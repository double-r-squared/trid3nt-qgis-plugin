"""Per-provider credential registry: the metadata a credential card needs.

Wire isolation -- NO key material lives here, only the label, signup url and
credential name needed to ASK for a key.
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

# Provider-id for the NAME-ONLY generic credential card emitted when a
# credential-shaped failure comes from a tool that is NOT in this registry: a
# credential NAME plus a secret-entry form, rather than letting the model
# free-text a possibly-fake signup URL. This id is NOT a real provider scope and
# carries no signup_url. The server emits the generic card only when this id is
# a valid wire ``ProviderID``; otherwise it surfaces the original typed error --
# it NEVER fabricates a URL.
GENERIC_PROVIDER_ID = "generic"


@dataclass(frozen=True)
class CredentialProvider:
    """One keyed provider's just-in-time credential-request metadata.
    ``provider_id`` is a plain ``str`` so the registry stays import-light; the
    server validates it against the live ``ProviderID`` at envelope-build time."""

    provider_id: str
    label: str
    signup_url: str | None
    secret_key_name: str
    default_message: str


# Provider registry -- ALL keyed atomic-tool data sources. Every provider_id is
# a member of the closed ``ProviderID`` Literal in ``trid3nt_contracts.secrets``
# so the saved key lands under the same scope the resolver's session cache
# re-reads on retry. Each ``secret_key_name`` is the SAME env-var name the
# tool's ``_resolve_*_key`` reads as its env fallback, so the user-facing name
# and the code path agree.

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
    # Copernicus CDS -- ONE key (TRID3NT_COPERNICUS_CDS_API_KEY) serves BOTH the
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
}


# Tool → provider mapping. A tool name resolves to the provider whose key it
# needs. ERA5 and GTSM both route to the shared ``ecmwf_cds`` CDS provider.

TOOL_PROVIDER: dict[str, str] = {
    "fetch_firms_active_fire": "firms",
    "fetch_era5_reanalysis": "ecmwf_cds",
    "fetch_gtsm_tide_surge": "ecmwf_cds",
}


# Per-tool auth/credential error-code set. The server treats a dispatch
# failure whose ``error_code`` is in this tool's set (OR whose exception is the
# tool's credential-error class, OR whose error matches the generic credential
# heuristics in ``is_credential_error``) as a "needs a key" signal: pause +
# emit ``credential-request`` + retry on provided. Each set lists the tool's
# explicit ``*_AUTH_ERROR`` / ``*_MISSING_KEY`` typed-error codes; the generic
# pattern matcher in ``is_credential_error`` is the catch-all for codes/bodies
# that don't appear here (e.g. a 401 surfaced under an UPSTREAM code).

TOOL_AUTH_ERROR_CODES: dict[str, frozenset[str]] = {
    "fetch_firms_active_fire": frozenset(
        {"FIRMS_AUTH_ERROR", "FIRMS_MISSING_KEY"}
    ),
    "fetch_era5_reanalysis": frozenset(
        {"ERA5_AUTH_ERROR", "ERA5_MISSING_KEY"}
    ),
    "fetch_gtsm_tide_surge": frozenset(
        {"GTSM_AUTH_ERROR", "GTSM_MISSING_KEY"}
    ),
}


# Generic "needs an API key" detection helpers (provider-agnostic). These back
# ``is_credential_error`` so a credential failure is caught regardless of which
# tool raised it or whether the tool authored an explicit ``*_AUTH_ERROR`` code.

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
    # Config-missing family -- a credential-shaped failure whose message names a
    # missing or incomplete credentials CONFIG rather than the literal words
    # "api key". Kept narrow and specific so a generic upstream or outage
    # message does NOT trip the gate.
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
    """True when ``error_code`` matches a credential pattern.
    A code ending in ``_AUTH_ERROR`` or ``_MISSING_KEY``, or containing any of
    :data:`_CREDENTIAL_CODE_SUBSTRINGS`."""
    if not isinstance(error_code, str) or not error_code:
        return False
    ec = error_code.upper()
    if ec.endswith("_AUTH_ERROR") or ec.endswith("_MISSING_KEY"):
        return True
    return any(sub in ec for sub in _CREDENTIAL_CODE_SUBSTRINGS)


def _http_status_is_credential(error: BaseException) -> bool:
    """True when a typed error surfaces an HTTP 401/403.
    Reads ``status_code`` / ``http_status`` / ``status``, so an UPSTREAM-coded
    error carrying a 401/403 still classifies as credential."""
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
    ``None`` means the tool is not key-requiring, or its provider is not
    registered: no credential-request, and the typed error flows through."""
    pid = TOOL_PROVIDER.get(tool_name)
    if pid is None:
        return None
    return CREDENTIAL_PROVIDERS.get(pid)


def is_credential_error(tool_name: str, error: BaseException) -> bool:
    """True when ``error`` from ``tool_name`` reads as a missing or invalid
    credential AND the tool has a registered provider; the shape test itself is
    :func:`is_credential_shaped_error`."""
    # HONEST, NO FABRICATION: only a tool with a registered provider returns
    # True here. A credential-shaped error from a tool with no provider is left
    # to the provider-agnostic path, which surfaces a NAME-ONLY card rather than
    # inventing a provider or a signup URL.
    if provider_for_tool(tool_name) is None:
        return False
    return is_credential_shaped_error(tool_name, error)


def is_credential_shaped_error(tool_name: str, error: BaseException) -> bool:
    """True when ``error`` looks like a missing or invalid credential.
    Provider-AGNOSTIC: unlike :func:`is_credential_error` it does not require the
    tool to have a registered provider."""
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
            "MovebankAuthError",
            "IUCNAuthError",
        )
    ):
        return True
    return False


def derive_generic_credential_name(tool_name: str) -> str:
    """Human credential name derived from a tool name alone.
    Strips a leading fetch/get-style verb, upper-cases short all-letter tokens
    and title-cases the rest; NEVER empty, so ``secret_key_name`` is satisfiable."""
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
    """Build a NAME-ONLY generic ``CredentialProvider`` for an unregistered tool.
    ``signup_url`` is always ``None`` -- never a fabricated URL -- and
    ``provider_id`` is the non-scoping :data:`GENERIC_PROVIDER_ID` sentinel."""
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
