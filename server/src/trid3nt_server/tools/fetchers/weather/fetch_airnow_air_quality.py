"""``fetch_airnow_air_quality`` atomic tool — EPA AirNow current AQI observations.

Wraps the U.S. EPA AirNow ``aq/data`` web service. Returns FlatGeobuf POINT
geometries of the current-hour air-quality monitor observations within a
bbox — each point carrying the reporting parameter (PM2.5 / Ozone / PM10 /
NO2 / SO2 / CO), the AQI value + AQI category, the raw concentration + unit,
and the monitor / reporting-agency identity. This fills the air-quality /
smoke-exposure gap that ties the GOES/JPSS fire demos together (a fire plume
overlaid on the AQI monitors it is degrading).

SECRET-GATED. The AirNow ``aq/data`` endpoint REQUIRES an API key
(``airnowapi.org``, free registration). UNLIKE ``fetch_usace_dams`` (which has
a public mirror to degrade to), AirNow has NO unauthenticated public endpoint
-- so when no key resolves we DO NOT fabricate a layer. We raise a typed
``AirNowMissingKeyError`` (error_code ``AIRNOW_MISSING_KEY``) which the agent's
provider-agnostic credential pipeline
(``credential_registry.is_credential_shaped_error``) recognises BY SUFFIX, so
the server surfaces a NAME-ONLY credential card (NATE principle 3) prompting
the user to enter an AirNow API key -- no per-provider registry entry required.
This mirrors ``fetch_ebird_observations`` (which likewise has no public mirror
and raises ``EBirdMissingKeyError``).

Key resolution (canonical 3-path secret loader, identical to eBird / USACE):

1. Explicit ``api_key`` kwarg (live-test / dev override).
2. ``secret_ref`` (a ``SecretRecord``) -> ``Persistence.get_secret_value`` (the
   per-Case production path -- credential card -> per-Case SSM SecureString ->
   retry).
3. ``TRID3NT_AIRNOW_API_KEY`` env var (dev convenience).

If none of the three resolve a key, ``AirNowMissingKeyError`` is raised
PRE-NETWORK -- an honest typed dead-end, never a hallucinated success. A key
that resolves but is REJECTED by the server (HTTP 401 ``Request not
authenticated.``) raises ``AirNowAuthError`` (error_code ``AIRNOW_AUTH_ERROR``)
so the user can re-enter a valid key.

Source (verified live 2026-06-27):

    https://www.airnowapi.org/aq/data/
        ?startDate=YYYY-MM-DDTHH&endDate=YYYY-MM-DDTHH
        &parameters=PM25,OZONE,PM10
        &BBOX=minLon,minLat,maxLon,maxLat
        &dataType=B            (B = both concentration AND AQI)
        &format=application/json
        &verbose=1             (include SiteName / AgencyName / AQS codes)
        &monitorType=0         (0 = permanent monitors; 2 = both perm+mobile)
        &includerawconcentrations=1
        &API_KEY=<key>

A no-key / bad-key request returns HTTP 401 with body
``{"WebServiceError":[{"Message":"Request not authenticated."}]}`` (captured
live 2026-06-27) -- that is the auth signal we map to ``AirNowAuthError``.

The ``aq/data`` verbose-JSON observation schema (stable, long-published):
``Latitude``, ``Longitude``, ``UTC``, ``Parameter``, ``Unit``, ``Value``,
``RawConcentration``, ``AQI``, ``Category``, ``SiteName``, ``AgencyName``,
``FullAQSCode``, ``IntlAQSCode``. We preserve all of these on the FlatGeobuf
and additionally derive ``AQICategoryName`` (the human-readable AQI band) and
``ParameterName`` (normalized long name) for the legend / chat narration.

Cache: ``dynamic-1h`` (AQI is a current-hour observation; the AirNow feed
updates hourly -- a top-of-hour TTL bucket matches FR-DC semantics). The
window of the query (current hour) is baked into the cache key so the next
hour misses and re-fetches.

``supports_global_query=False``: AirNow's ``aq/data`` endpoint requires a BBOX
(it has no "all monitors" mode and would be enormous + slow), so a bbox is
MANDATORY here -- a None bbox is an input error, not a global sweep.

FR-DC-3/4: routed through ``read_through`` so identical bbox+hour calls reuse
the cached FlatGeobuf. FR-AS-11: typed errors carry ``error_code`` +
``retryable``. FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_airnow_air_quality",
    "AirNowError",
    "AirNowInputError",
    "AirNowUpstreamError",
    "AirNowMissingKeyError",
    "AirNowAuthError",
    "estimate_payload_mb",
    "set_persistence_for_secrets",
    "_resolve_api_key",
    "_materialize_secret",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_validate_parameters",
    "_current_hour_window",
    "_build_airnow_url",
    "_fetch_airnow_json",
    "_records_to_fgb",
    "_aqi_category_name",
    "VALID_PARAMETERS",
    "PRESERVED_PROPERTIES",
    "AQI_CATEGORY_NAMES",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.weather.fetch_airnow_air_quality")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class AirNowError(RuntimeError):
    """Base class for fetch_airnow_air_quality failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface; ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "AIRNOW_ERROR"
    retryable: bool = True


class AirNowInputError(AirNowError):
    """Caller passed an invalid bbox / parameter / missing-bbox."""

    error_code = "AIRNOW_INPUT_ERROR"
    retryable = False


class AirNowUpstreamError(AirNowError):
    """AirNow ``aq/data`` query failed (network, 5xx, non-JSON, parse)."""

    error_code = "AIRNOW_UPSTREAM_ERROR"
    retryable = True


class AirNowMissingKeyError(AirNowError):
    """No AirNow API key resolved on any of the three lookup paths.

    Raised PRE-NETWORK. The ``_MISSING_KEY`` error-code suffix and the
    ``MissingKeyError`` class-name suffix are BOTH recognised by the agent's
    provider-agnostic credential pipeline
    (``credential_registry.is_credential_shaped_error``), so the server surfaces
    a NAME-ONLY credential card (NATE principle 3) prompting the user to enter
    an AirNow API key -- no per-provider registry entry is required. AirNow has
    NO public mirror, so unlike ``fetch_usace_dams`` we cannot degrade to
    keyless data; an honest typed dead-end is the correct behaviour.
    ``retryable=False`` -- retrying without a key is futile; the agent waits
    for the user to supply one.
    """

    error_code = "AIRNOW_MISSING_KEY"
    retryable = False


class AirNowAuthError(AirNowError):
    """A key resolved but the AirNow server REJECTED it (HTTP 401).

    AirNow returns HTTP 401 ``{"WebServiceError":[{"Message":"Request not
    authenticated."}]}`` for a missing OR invalid key (verified live
    2026-06-27). Because we only reach the network AFTER a key resolves, a 401
    here means the supplied key is wrong / expired / revoked. The
    ``_AUTH_ERROR`` suffix surfaces the credential card to re-enter a valid key.
    ``retryable=False`` -- retrying the same bad key is futile.
    """

    error_code = "AIRNOW_AUTH_ERROR"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# AirNow current-observations bounded-box web service. Verified live
# 2026-06-27 (a keyless request returns HTTP 401 + the WebServiceError body).
_AIRNOW_BASE = "https://www.airnowapi.org/aq/data/"

# Env-var fallback for the API key (resolution order: kwarg -> secret_ref ->
# this env var). UPPER_SNAKE convention shared with the credential pipeline.
_AIRNOW_KEY_ENV = "TRID3NT_AIRNOW_API_KEY"

# AirNow ``parameters`` controlled vocabulary. Keys are the case-insensitive
# user-facing aliases; values are the EXACT AirNow API token. PM2.5 / Ozone /
# PM10 are the kickoff-named primaries; NO2 / SO2 / CO round out the criteria
# pollutants AirNow reports.
VALID_PARAMETERS: dict[str, str] = {
    "pm25": "PM25",
    "pm2.5": "PM25",
    "pm10": "PM10",
    "ozone": "OZONE",
    "o3": "OZONE",
    "no2": "NO2",
    "so2": "SO2",
    "co": "CO",
}

# Long-form parameter names for the legend / narration.
_PARAMETER_LONG_NAME: dict[str, str] = {
    "PM25": "PM2.5 (fine particulate matter)",
    "PM10": "PM10 (coarse particulate matter)",
    "OZONE": "Ozone",
    "NO2": "Nitrogen dioxide",
    "SO2": "Sulfur dioxide",
    "CO": "Carbon monoxide",
}

# AirNow ``Category`` integer -> human-readable AQI band (EPA AQI scale).
AQI_CATEGORY_NAMES: dict[int, str] = {
    1: "Good",
    2: "Moderate",
    3: "Unhealthy for Sensitive Groups",
    4: "Unhealthy",
    5: "Very Unhealthy",
    6: "Hazardous",
    7: "Unavailable",
}

# Properties preserved from each AirNow observation (verbose JSON schema).
# Explicit allow-list so the FlatGeobuf column set is stable across API
# versions. The derived columns (AQICategoryName / ParameterName) are appended
# in ``_records_to_fgb``.
PRESERVED_PROPERTIES: tuple[str, ...] = (
    "Latitude",
    "Longitude",
    "UTC",
    "Parameter",
    "Unit",
    "Value",
    "RawConcentration",
    "AQI",
    "Category",
    "SiteName",
    "AgencyName",
    "FullAQSCode",
    "IntlAQSCode",
)

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_HTTP_TIMEOUT_S = 30.0

# How far back the requested observation window reaches. AirNow current obs
# lag ~1-2h behind real time as monitors report; a 3-hour window catches the
# latest available reading per monitor (AirNow returns one row per
# monitor/parameter/hour, so a wider window can return multiple hours -- we
# keep the LATEST per monitor+parameter in ``_records_to_fgb``).
_WINDOW_HOURS = 3

# Empirical: each verbose observation serializes to ~0.4 KB of FlatGeobuf
# (point geometry + ~15 scalar attributes). A metro-sized bbox returns
# 10-80 monitor-parameter rows; a multi-state bbox 200-1500.
_BYTES_PER_FEATURE_ESTIMATE = 420
_CONUS_AREA_DEG = (-65.0 - -125.0) * (50.0 - 24.0)
_CONUS_FEATURE_COUNT_ESTIMATE = 8000


# ---------------------------------------------------------------------------
# Payload estimator hook (Wave 1.5 / FR-DC-9).
# ---------------------------------------------------------------------------


def estimate_payload_mb(**args: Any) -> float:
    """FR-DC-9 / Wave-1.5 payload estimator (called by chat-warning gate).

    Scales by bbox area relative to CONUS. A missing/garbage bbox returns a
    nominal small estimate (the tool will error on a None bbox anyway, since
    AirNow requires a bbox). Advisory only -- never raises.
    """
    bbox = args.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return 0.05
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return 0.05
    area = max(0.0, (max_lon - min_lon)) * max(0.0, (max_lat - min_lat))
    if _CONUS_AREA_DEG <= 0:
        return 0.05
    fraction = min(1.0, area / _CONUS_AREA_DEG)
    est_features = max(1, int(_CONUS_FEATURE_COUNT_ESTIMATE * fraction))
    est_bytes = est_features * _BYTES_PER_FEATURE_ESTIMATE
    return float(est_bytes) / (1024 * 1024)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_airnow_air_quality",
    ttl_class="dynamic-1h",
    source_class="airnow_air_quality",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Secret-loader (canonical 3-path; mirrors fetch_ebird_observations /
# fetch_usace_dams).
# ---------------------------------------------------------------------------

_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for secret materialization.

    Called once at startup by the agent service (parallels
    ``fetch_ebird_observations.set_persistence_for_secrets``). Tests call this
    in a fixture and reset to ``None`` on teardown.
    """
    global _PERSISTENCE_FOR_SECRETS
    _PERSISTENCE_FOR_SECRETS = persistence


def _get_persistence_for_secrets() -> Any | None:
    return _PERSISTENCE_FOR_SECRETS


def _run_coro_sync(coro: Any) -> Any:
    """Run an ``asyncio`` coroutine and return its result from sync context.

    Uses ``asyncio.run`` when no loop is running (test / CLI path); falls back
    to a one-shot worker-thread loop when called from within a running loop
    (agent-runtime path). Both paths close the loop they create. Mirrors the
    eBird fetcher's bridge so the secret-loader semantics are identical.
    """
    import asyncio
    import threading

    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            result_box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            error_box["err"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "err" in error_box:
        raise error_box["err"]
    return result_box["value"]


def _materialize_secret(secret_ref: Any) -> str:
    """Bridge ``Persistence.get_secret_value`` (async) into a sync caller.

    A ``str`` ``secret_ref`` is accepted verbatim (the test surface injects a
    known key this way without standing up Persistence). Otherwise the bound
    ``Persistence`` resolves the per-Case vault reference.
    """
    if isinstance(secret_ref, str):
        return secret_ref

    persistence = _get_persistence_for_secrets()
    if persistence is None:
        raise AirNowMissingKeyError(
            "Persistence not bound; cannot resolve secret_ref for the AirNow "
            "API key. Pass api_key=... explicitly in this context."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


def _resolve_api_key(
    api_key: str | None,
    secret_ref: Any | None,
) -> str:
    """Return the live AirNow API key from one of three lookup paths.

    Priority (canonical 3-path secret loader):

    1. Explicit ``api_key`` kwarg.
    2. ``secret_ref`` (a ``SecretRecord``) -> ``Persistence.get_secret_value``
       (the per-Case production path).
    3. ``TRID3NT_AIRNOW_API_KEY`` env var (dev convenience).

    Raises:
        ``AirNowMissingKeyError`` if none of the three paths produce a key.
        AirNow has NO public mirror, so we cannot degrade -- an honest typed
        dead-end is correct.
    """
    if api_key:
        return api_key

    if secret_ref is not None:
        try:
            resolved = _materialize_secret(secret_ref)
        except AirNowMissingKeyError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as missing-key
            raise AirNowMissingKeyError(
                f"AirNow secret_ref lookup failed: {exc}"
            ) from exc
        if resolved:
            return resolved

    env_key = os.environ.get(_AIRNOW_KEY_ENV)
    if env_key:
        return env_key

    raise AirNowMissingKeyError(
        "no AirNow API key available: pass api_key=..., secret_ref=..., or "
        "set the TRID3NT_AIRNOW_API_KEY env var. Register a free key at "
        "https://docs.airnowapi.org/account/request/."
    )


# ---------------------------------------------------------------------------
# bbox + parameter helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Validate + return a 4-float bbox, or raise ``AirNowInputError``.

    AirNow's ``aq/data`` endpoint REQUIRES a bbox (there is no all-monitors
    mode), so a None bbox is an input error here, NOT a global sweep.
    """
    if bbox is None:
        raise AirNowInputError(
            "bbox is required for fetch_airnow_air_quality "
            "(AirNow aq/data has no global mode); pass "
            "(min_lon, min_lat, max_lon, max_lat)."
        )
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise AirNowInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise AirNowInputError(f"bbox values must be numeric: {bbox!r}") from exc
    vals = (min_lon, min_lat, max_lon, max_lat)
    if not all(math.isfinite(v) for v in vals):
        raise AirNowInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise AirNowInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise AirNowInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise AirNowInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    return vals


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_parameters(
    parameters: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Normalize the ``parameters`` filter to canonical AirNow API tokens.

    Accepts a single value or a list/tuple, case-insensitive. ``None`` defaults
    to the three particulate/ozone primaries (``PM25,OZONE,PM10``). Each entry
    must be a known criteria pollutant alias.

    Returns the de-duplicated canonical-token list, preserving first-seen order.
    Raises ``AirNowInputError`` on an unknown parameter.
    """
    if parameters is None:
        return ["PM25", "OZONE", "PM10"]
    if isinstance(parameters, str):
        raw = [parameters]
    elif isinstance(parameters, (list, tuple)):
        raw = list(parameters)
    else:
        raise AirNowInputError(
            f"parameters must be a str or list of str; got "
            f"{type(parameters).__name__}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise AirNowInputError(f"parameters entries must be str; got {item!r}")
        key = item.strip().lower()
        canon = VALID_PARAMETERS.get(key)
        if canon is None:
            raise AirNowInputError(
                f"parameter {item!r} is not a known AirNow pollutant; expected "
                f"one of {sorted(set(VALID_PARAMETERS.values()))}"
            )
        if canon not in out:
            out.append(canon)
    if not out:
        return ["PM25", "OZONE", "PM10"]
    return out


def _current_hour_window(
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return the ``(startDate, endDate)`` AirNow window strings (UTC, ``%Y-%m-%dT%H``).

    The end is the current top-of-hour UTC; the start reaches ``_WINDOW_HOURS``
    back so the latest available reading per monitor is captured (AirNow obs
    lag ~1-2h). Both are floored to the hour so the cache key is stable within
    the hour (matching the ``dynamic-1h`` TTL bucket).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    end_hour = now.replace(minute=0, second=0, microsecond=0)
    start_hour = end_hour - timedelta(hours=_WINDOW_HOURS)
    fmt = "%Y-%m-%dT%H"
    return start_hour.strftime(fmt), end_hour.strftime(fmt)


def _aqi_category_name(category: Any) -> str:
    """Map an AirNow ``Category`` integer to the EPA AQI band name."""
    try:
        c = int(category)
    except (TypeError, ValueError):
        return "Unavailable"
    return AQI_CATEGORY_NAMES.get(c, "Unavailable")


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_airnow_url(
    bbox: tuple[float, float, float, float],
    parameters: list[str],
    api_key: str,
    *,
    start_date: str,
    end_date: str,
    monitor_type: int = 0,
) -> tuple[str, dict[str, str]]:
    """Build the AirNow ``aq/data`` query URL + params dict.

    ``BBOX`` is the literal ``minLon,minLat,maxLon,maxLat``. ``dataType=B``
    returns BOTH concentration and AQI; ``verbose=1`` adds SiteName /
    AgencyName / AQS codes; ``includerawconcentrations=1`` adds the raw
    concentration column. ``monitorType=0`` = permanent monitors.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    params: dict[str, str] = {
        "startDate": start_date,
        "endDate": end_date,
        "parameters": ",".join(parameters),
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "dataType": "B",
        "format": "application/json",
        "verbose": "1",
        "monitorType": str(monitor_type),
        "includerawconcentrations": "1",
        "API_KEY": api_key,
    }
    return _AIRNOW_BASE, params


# ---------------------------------------------------------------------------
# AirNow HTTP fetch.
# ---------------------------------------------------------------------------


def _fetch_airnow_json(
    url: str,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    """GET the AirNow ``aq/data`` query and return the parsed observation list.

    Raises:
        ``AirNowAuthError``: HTTP 401 (the resolved key was rejected) or a
            ``WebServiceError`` envelope mentioning authentication.
        ``AirNowUpstreamError``: network / 5xx / non-JSON / unexpected shape /
            non-auth ``WebServiceError`` envelope.
    """
    safe_params = {k: ("<redacted>" if k == "API_KEY" else v) for k, v in params.items()}
    logger.info("fetch_airnow_air_quality: GET %s params=%s", url, safe_params)
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise AirNowUpstreamError(
            f"AirNow request failed url={url}: {exc}"
        ) from exc

    # HTTP 401 => the resolved key was rejected (missing/invalid). AirNow
    # returns 401 for both, but we only reach the network AFTER a key resolves,
    # so a 401 here is an INVALID key -> credential card to re-enter.
    if resp.status_code == 401:
        raise AirNowAuthError(
            f"AirNow rejected the API key (HTTP 401) url={url}: "
            f"{resp.text[:300]!r}"
        )
    if resp.status_code == 403:
        raise AirNowAuthError(
            f"AirNow rejected the API key (HTTP 403) url={url}: "
            f"{resp.text[:300]!r}"
        )
    if resp.status_code >= 400:
        raise AirNowUpstreamError(
            f"AirNow returned HTTP {resp.status_code} url={url}: "
            f"{resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise AirNowUpstreamError(
            f"AirNow returned non-JSON url={url}: {exc}; body={resp.text[:300]!r}"
        ) from exc

    # AirNow error envelope: a dict (or list-wrapped) ``WebServiceError``.
    if isinstance(body, dict) and "WebServiceError" in body:
        msg = json.dumps(body["WebServiceError"])[:300]
        if "authenticat" in msg.lower() or "api_key" in msg.lower() or "key" in msg.lower():
            raise AirNowAuthError(
                f"AirNow authentication error url={url}: {msg}"
            )
        raise AirNowUpstreamError(
            f"AirNow returned a WebServiceError envelope url={url}: {msg}"
        )

    # A successful empty result is a JSON list (possibly empty). An empty list
    # over a bbox with no reporting monitors is LEGITIMATE -- we serialize an
    # empty FGB, never raise.
    if not isinstance(body, list):
        raise AirNowUpstreamError(
            f"AirNow response is not a JSON list url={url}: "
            f"type={type(body).__name__!r} body={str(body)[:200]!r}"
        )

    return body


# ---------------------------------------------------------------------------
# Records -> FlatGeobuf conversion (latest-per-monitor+parameter).
# ---------------------------------------------------------------------------


def _records_to_fgb(records: list[dict[str, Any]]) -> bytes:
    """Convert AirNow observation records to FlatGeobuf POINT bytes.

    AirNow returns one row per monitor/parameter/hour over the requested
    window; we keep only the LATEST (max ``UTC``) row per
    (Latitude, Longitude, Parameter) so the layer shows ONE current point per
    monitor-parameter. Derived columns ``AQICategoryName`` + ``ParameterName``
    are appended. Rows without finite coordinates are dropped. Always emits a
    valid FlatGeobuf -- an empty input yields a header-only FGB.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AirNowUpstreamError(
            f"geopandas/shapely not available for FlatGeobuf conversion: {exc}"
        ) from exc

    # Dedup: keep latest UTC per (lat, lon, parameter).
    latest: dict[tuple[float, float, str], dict[str, Any]] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            lat = float(rec.get("Latitude"))
            lon = float(rec.get("Longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        param = str(rec.get("Parameter") or "")
        key = (round(lat, 6), round(lon, 6), param)
        utc = str(rec.get("UTC") or "")
        prev = latest.get(key)
        if prev is None or utc >= str(prev.get("UTC") or ""):
            latest[key] = rec

    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    for rec in latest.values():
        lat = float(rec.get("Latitude"))
        lon = float(rec.get("Longitude"))
        row_props: dict[str, Any] = {}
        for k in PRESERVED_PROPERTIES:
            v = rec.get(k)
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row_props[k] = v
        param_tok = str(rec.get("Parameter") or "")
        row_props["ParameterName"] = _PARAMETER_LONG_NAME.get(param_tok, param_tok)
        row_props["AQICategoryName"] = _aqi_category_name(rec.get("Category"))
        rows.append(row_props)
        geoms.append(Point(lon, lat))

    columns = list(PRESERVED_PROPERTIES) + ["ParameterName", "AQICategoryName"]
    if not rows:
        gdf = gpd.GeoDataFrame(
            {c: [] for c in columns},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_airnow_aq_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise AirNowUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_airnow_air_quality: FlatGeobuf = %d bytes (%d monitor-obs)",
            len(fgb_bytes),
            len(gdf),
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# End-to-end fetcher (HTTP -> records -> FGB bytes).
# ---------------------------------------------------------------------------


def _fetch_airnow_bytes(
    bbox: tuple[float, float, float, float],
    parameters: list[str],
    api_key: str,
    *,
    start_date: str,
    end_date: str,
    monitor_type: int = 0,
) -> bytes:
    """Run the AirNow fetch + conversion. Raises typed errors on failure."""
    url, params = _build_airnow_url(
        bbox,
        parameters,
        api_key,
        start_date=start_date,
        end_date=end_date,
        monitor_type=monitor_type,
    )
    records = _fetch_airnow_json(url, params)
    logger.info(
        "fetch_airnow_air_quality: %d raw observation row(s) returned", len(records)
    )
    return _records_to_fgb(records)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_airnow_air_quality(
    bbox: tuple[float, float, float, float] | None = None,
    parameters: str | list[str] | None = None,
    monitor_type: int = 0,
    api_key: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """EPA AirNow current-hour air-quality / AQI / PM2.5 monitor observations as points.

    Use this (not fetch_openaq_measurements) when the user names EPA AirNow or
    wants US/Canada/Mexico current-hour AQI. Call it even if no key is set: the
    key is auto-requested via a credential card if missing (see below), so a
    possible key prompt is NOT a reason to route elsewhere.

    What it does:
        Fetches the U.S. EPA AirNow current-hour air-quality observations for
        every reporting monitor within ``bbox`` as point features. Each point
        carries the reporting parameter (PM2.5 / Ozone / PM10 / NO2 / SO2 /
        CO), the AQI value + AQI category band, the raw concentration + unit,
        and the monitor / reporting-agency identity. Key auto-requested if
        missing: AirNow uses an API key (resolved kwarg -> per-Case
        ``secret_ref`` -> ``TRID3NT_AIRNOW_API_KEY`` env). With NO key it raises a
        credential-shaped ``AirNowMissingKeyError`` so the agent surfaces a
        credential card and retries -- it NEVER fabricates a layer (AirNow has
        no public mirror).

    When to use:
        - User asks about current air quality / AQI / smoke exposure in a
          region ("what's the air quality near the fire?", "show PM2.5
          monitors around Los Angeles").
        - A fire / smoke-plume workflow (GOES/JPSS fire, FIRMS) needs the
          ground-truth AQI monitors the plume is degrading -- overlay AirNow
          points on the fire footprint.
        - Damage / public-health context needs current pollutant levels at
          permanent EPA monitors.

    When NOT to use:
        - DO NOT use for FORECAST air quality -- this is current OBSERVED only;
          AirNow has a separate forecast feed.
        - DO NOT use for HISTORICAL air-quality archives -- use EPA AQS
          (aqs.epa.gov) for multi-year time series.
        - DO NOT use for non-US/Canada/Mexico regions -- AirNow coverage is
          North America.
        - DO NOT use for a gridded/modeled smoke RASTER -- AirNow is point
          monitors; use a satellite-derived smoke product for the plume.

    Parameters:
        bbox: REQUIRED ``(min_lon, min_lat, max_lon, max_lat)`` envelope in
            EPSG:4326. Type: 4-float tuple, lon/lat ordered min-then-max on
            each axis. Example: ``(-118.7, 33.7, -117.6, 34.3)`` for the Los
            Angeles basin. AirNow's ``aq/data`` endpoint has NO global mode, so
            a None bbox raises ``AirNowInputError`` (not a global sweep).
        parameters: Optional pollutant filter. A single value or list,
            case-insensitive, each one of ``"PM25"`` / ``"PM10"`` /
            ``"OZONE"`` / ``"NO2"`` / ``"SO2"`` / ``"CO"`` (aliases ``"PM2.5"``
            / ``"O3"`` accepted). Defaults to ``["PM25","OZONE","PM10"]`` (the
            primary criteria pollutants). Example: ``parameters="PM25"`` for
            wildfire-smoke focus.
        monitor_type: AirNow ``monitorType``: ``0`` = permanent monitors
            (default), ``2`` = permanent + mobile. Other values are passed
            through to AirNow.
        api_key: Optional explicit AirNow API key (highest-priority resolution
            path; live-test / dev override).
        secret_ref: Optional ``SecretRecord`` (from the per-Case secrets panel)
            -> resolved to the key via ``Persistence.get_secret_value`` at
            invocation time (the production path). A key that resolves but is
            rejected by AirNow raises ``AirNowAuthError`` (credential-card
            path); no key on any path raises ``AirNowMissingKeyError``.

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
        ``s3://<cache>/cache/dynamic-1h/airnow_air_quality/<key>.fgb``
        containing ``Point`` geometries (EPSG:4326), ONE per
        monitor-parameter (latest reading in the window), with columns
        ``Parameter`` / ``ParameterName``, ``AQI``, ``Category`` /
        ``AQICategoryName``, ``Value`` / ``RawConcentration`` / ``Unit``,
        ``SiteName`` / ``AgencyName`` / ``FullAQSCode``. ``layer_type="vector"``,
        ``role="primary"``, ``units="AQI"``.

    Cross-tool dependencies:
        Consumes a bbox typically derived from ``geocode_location`` /
        ``fetch_administrative_boundaries`` or a fire footprint
        (``fetch_firms_active_fire`` / ``fetch_goes_active_fire``). Pairs with
        those fire layers for the smoke-exposure overlay, and feeds
        ``compute_zonal_statistics`` / ``clip_vector_to_polygon`` for AOI-scoped
        AQI summaries.

    Cache: ``dynamic-1h`` (AQI is a current-hour observation; the AirNow feed
    updates hourly). Cache key includes the bbox (6dp), the parameter set, and
    the current-hour window, so the next hour misses and re-fetches.

    External-API resilience (NFR-R-1): On network failure / non-2xx / malformed
    JSON / non-auth WebServiceError the tool raises
    ``AirNowUpstreamError(retryable=True)``; an HTTP 401/403 or an
    authentication WebServiceError raises ``AirNowAuthError`` (credential card);
    no key on any path raises ``AirNowMissingKeyError`` (credential card).

    Source-tier: FR-HEP-2 Tier 1 (EPA AirNow is the authoritative U.S.
    air-quality observation network).
    """
    # bbox validation (None is an input error -- AirNow has no global mode).
    vbbox = _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(vbbox)

    # parameters normalization.
    param_norm = _validate_parameters(parameters)

    # monitor_type coercion (defensive against LLM-invented strings).
    try:
        mt = int(monitor_type)
    except (TypeError, ValueError):
        mt = 0

    # Key resolution -- raises AirNowMissingKeyError PRE-NETWORK if none.
    api_key_resolved = _resolve_api_key(api_key=api_key, secret_ref=secret_ref)

    # Current-hour window (drives both the query AND the cache key vintage).
    start_date, end_date = _current_hour_window()

    # Cache-key params. The API key is INTENTIONALLY excluded (the underlying
    # observations do not vary by caller/key). The window strings ARE included
    # so each hour gets a distinct key (belt-and-suspenders with the
    # dynamic-1h TTL bucket vintage).
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "parameters": param_norm,
        "monitor_type": mt,
        "start": start_date,
        "end": end_date,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_airnow_bytes(
            q_bbox,
            param_norm,
            api_key_resolved,
            start_date=start_date,
            end_date=end_date,
            monitor_type=mt,
        ),
    )
    assert result.uri is not None, (
        "fetch_airnow_air_quality is cacheable; uri must be set by read_through"
    )

    param_label = ", ".join(_PARAMETER_LONG_NAME.get(p, p) for p in param_norm)
    param_id = "-".join(p.lower() for p in param_norm)
    name = (
        f"EPA AirNow air quality ({param_label}) — bbox "
        f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
    )
    layer_id = (
        f"airnow-aq-{param_id}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
    )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="airnow_air_quality",
        role="primary",
        units="AQI",
    )
