"""``fetch_openaq_measurements`` atomic tool — OpenAQ global air-quality fetcher.

Wraps the OpenAQ API v3 (``https://api.openaq.org/v3/``) to return the most
recent ground-station air-quality measurements (PM2.5, PM10, NO2, O3, SO2, CO,
and others) clipped to a bbox as a FlatGeobuf POINT layer. This is the GLOBAL
complement to the US-only AirNow / EPA AQS surface: OpenAQ aggregates reference
monitors and low-cost sensors from national networks worldwide.

API surface (verified 2026-06-27):

    Stations in a bbox (carries the sensor->parameter->units map):
        GET https://api.openaq.org/v3/locations
            ?bbox={min_lon},{min_lat},{max_lon},{max_lat}&limit={n}&page={p}
    Latest value per sensor at a station:
        GET https://api.openaq.org/v3/locations/{locations_id}/latest
    Header on EVERY request:
        X-API-Key: <api_key>

The ``/v3/locations`` response gives each station's ``coordinates``
(latitude/longitude), ``country``, ``datetimeLast``, and a ``sensors`` array
where each sensor carries ``parameter{name,units,displayName}``. The
``/v3/locations/{id}/latest`` response gives, per sensor, the latest ``value``,
``datetime{utc,local}``, and ``sensorsId`` — but NOT the parameter/units. We
cross-reference ``sensorsId`` against the station's ``sensors`` array to attach
``parameter`` / ``units`` to every latest value. Each output FlatGeobuf POINT is
thus ONE (station, parameter) latest measurement.

bbox format (verified): OpenAQ v3 uses a comma-delimited
``min_lon,min_lat,max_lon,max_lat`` WGS84 envelope — IDENTICAL to GRACE-2's
``(west, south, east, north)`` convention, so no axis flip is needed. An
out-of-range bbox returns HTTP 422; an unauthenticated request returns HTTP
401 with ``{"message":"Unauthorized. A valid API key must be provided in the
X-API-Key header."}`` (both verified live 2026-06-27).

SECRET gate (mirrors fetch_ebird_observations / fetch_usace_dams): OpenAQ v3
requires a free API key (registration at ``https://explore.openaq.org/`` ->
account -> API keys). The agent resolves the key in this order:

1. Explicit ``api_key`` kwarg (live-test path, dev override).
2. ``secret_ref`` ``SecretRecord`` -> ``Persistence.get_secret_value()`` (the
   production per-Case secret-vault path).
3. ``GRACE2_OPENAQ_API_KEY`` env var (local dev convenience).

If NONE of the three resolve a key, the tool raises ``OpenAQMissingKeyError``
(``error_code="OPENAQ_KEY_REQUIRED"``, ``retryable=False``) BEFORE any network
call — the agent surface routes a NAME-ONLY credential-request card to the user
(per FR-AS-11). We NEVER fabricate a layer when the key is missing; OpenAQ has
no public unauthenticated mirror, so the honest degrade IS the typed error
(unlike fetch_usace_dams, which falls back to a public ESRI mirror). A key that
resolves but is REJECTED by the API (401/403) raises ``OpenAQAuthError`` so the
agent surfaces a re-enter-the-key card.

FR-DC-3/4: routed through ``read_through`` with ``ttl_class="dynamic-1h"`` —
OpenAQ latest values update as new station readings arrive; an hourly window
balances freshness against the API rate budget. The cache key intentionally
does NOT include the api_key (the underlying measurements don't vary by caller).

Output FlatGeobuf schema (one POINT feature per (station, parameter)):
    Geometry: Point (EPSG:4326)
    Properties:
        location_id   (int)   — OpenAQ ``locationsId``
        location_name (str)   — station name
        country       (str)   — ISO country code ("US", "IN", ...)
        parameter     (str)   — measured parameter ("pm25", "no2", "o3", ...)
        display_name  (str)   — human label ("PM2.5", "NO2 mass", ...)
        value         (float) — latest measured value
        unit          (str)   — measurement unit ("ug/m3", "ppm", ...)
        datetime_utc  (str)   — measurement timestamp (ISO-8601 UTC)
        datetime_local(str)   — measurement timestamp (station-local)
        sensor_id     (int)   — OpenAQ ``sensorsId``

FR-TA-2 atomic tool. FR-AS-11: ``OpenAQError`` / sub-classes carry
``error_code`` + ``retryable`` for the agent's retry/clarify/fallback surface.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_openaq_measurements",
    "OpenAQError",
    "OpenAQInputError",
    "OpenAQUpstreamError",
    "OpenAQMissingKeyError",
    "OpenAQAuthError",
    "estimate_payload_mb",
    "set_persistence_for_secrets",
    "_resolve_api_key",
    "_validate_bbox",
    "_validate_parameters",
    "_round_bbox_to_6dp",
    "_fetch_locations_page",
    "_fetch_all_locations",
    "_fetch_location_latest",
    "_assemble_measurement_rows",
    "_rows_to_flatgeobuf_bytes",
    "_fetch_openaq_bytes",
    "PRESERVED_PROPERTIES",
    "DEFAULT_PARAMETERS",
]

logger = logging.getLogger("grace2_agent.tools.fetch_openaq_measurements")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class OpenAQError(RuntimeError):
    """Base class for fetch_openaq_measurements failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "OPENAQ_ERROR"
    retryable: bool = True


class OpenAQInputError(OpenAQError):
    """Caller passed an invalid bbox or unsupported parameter filter."""

    error_code = "OPENAQ_INPUT_INVALID"
    retryable = False


class OpenAQUpstreamError(OpenAQError):
    """OpenAQ API returned 5xx / 422 / non-JSON / the network call failed."""

    error_code = "OPENAQ_UPSTREAM_ERROR"
    retryable = True


class OpenAQMissingKeyError(OpenAQError):
    """No OpenAQ API key resolved via any of the three lookup paths.

    Raised BEFORE any network call. OpenAQ v3 has NO public unauthenticated
    mirror, so unlike fetch_usace_dams we cannot degrade to a key-less source
    — the HONEST degrade IS this typed error. The ``error_code`` is the
    kickoff-specified ``OPENAQ_KEY_REQUIRED`` sentinel; the
    ``OpenAQMissingKeyError`` class name + the ``_KEY_REQUIRED`` /
    ``_AUTH_ERROR`` suffix family are recognised by the agent's
    provider-agnostic credential pipeline, which surfaces a NAME-ONLY
    credential-request card prompting the user to add an OpenAQ key via the
    per-Case secrets panel. ``retryable=False`` — retrying without a key is
    futile; the agent waits for the user to supply one.
    """

    error_code = "OPENAQ_KEY_REQUIRED"
    retryable = False


class OpenAQAuthError(OpenAQError):
    """OpenAQ API returned 401/403 — the supplied key is invalid/revoked.

    Distinct from ``OpenAQMissingKeyError`` (which fires pre-network when no
    key resolves at all). This fires when a key resolved (kwarg / secret_ref /
    env) but the API rejected it. The ``_AUTH_ERROR`` error-code suffix and the
    class name are recognised by the agent's credential pipeline, which
    surfaces a re-enter-the-key card. ``retryable=False`` because retrying the
    same bad key is futile.
    """

    error_code = "OPENAQ_AUTH_ERROR"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# OpenAQ API v3 endpoints. The locations endpoint carries the
# sensor->parameter->units map + station coordinates; the per-location latest
# endpoint carries the most-recent value + datetime per sensor.
_OPENAQ_BASE = "https://api.openaq.org/v3"
_LOCATIONS_URL = f"{_OPENAQ_BASE}/locations"
_LOCATION_LATEST_URL_TMPL = f"{_OPENAQ_BASE}/locations/{{locations_id}}/latest"

# Env-var fallback name for the API key (UPPER_SNAKE of the credential scope,
# matching the credential-pipeline naming convention used by eBird/ERA5).
_OPENAQ_KEY_ENV = "GRACE2_OPENAQ_API_KEY"

# Canonical OpenAQ pollutant parameter names (v3 controlled vocabulary). Used
# to validate the optional ``parameters`` filter. Not exhaustive of every
# OpenAQ parameter (it tracks meteo + many pollutants), but covers the core
# air-quality pollutants the kickoff named plus common extras.
VALID_PARAMETERS: frozenset[str] = frozenset(
    {
        "pm25",
        "pm10",
        "pm1",
        "no2",
        "no",
        "nox",
        "o3",
        "so2",
        "co",
        "co2",
        "bc",  # black carbon
        "ch4",
        "nh3",
    }
)

# Default parameter set when the caller does not filter: the six core
# air-quality pollutants the kickoff named.
DEFAULT_PARAMETERS: tuple[str, ...] = ("pm25", "pm10", "no2", "o3", "so2", "co")

# Properties preserved per emitted (station, parameter) point feature. Explicit
# allow-list keeps the FlatGeobuf column set stable across OpenAQ schema growth.
PRESERVED_PROPERTIES: tuple[str, ...] = (
    "location_id",
    "location_name",
    "country",
    "parameter",
    "display_name",
    "value",
    "unit",
    "datetime_utc",
    "datetime_local",
    "sensor_id",
)

# Per-request timeout. OpenAQ usually responds <1s for a city-sized bbox; pad
# generously for slow networks / large pages.
_TIMEOUT_S = 30.0

# OpenAQ v3 locations page size. The API caps ``limit`` at 1000; we request 200
# per page (a city/metro bbox rarely has more, and smaller pages keep each
# request snappy + bound memory).
_LOCATIONS_PAGE_SIZE = 200

# Hard caps so a runaway whole-continent bbox can't burn the API budget. We
# page locations up to this many stations, then stop (the bbox is too big —
# the caller should narrow it).
_MAX_LOCATIONS = 2000

# User-Agent per OpenAQ usage guidelines.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)


# ---------------------------------------------------------------------------
# Payload estimator hook (Wave 1.5 / FR-DC-9).
# ---------------------------------------------------------------------------

# Empirical sizing: each (station, parameter) point serializes to ~0.3 KB of
# FlatGeobuf (point geometry + ~10 scalar attributes). A city bbox typically
# yields 5-50 stations x ~3 parameters (~5-50 KB); a country-sized bbox can
# yield hundreds of stations. The estimator scales by bbox area against a
# nominal global station density.
_BYTES_PER_FEATURE_ESTIMATE = 320

# Rough global density: OpenAQ aggregates ~30k active locations worldwide; the
# inhabited land surface is ~1.5e8 sq km. We express density in features per
# sq-degree (1 sq deg ~ 12300 sq km at the equator) and scale conservatively —
# the estimator is advisory, so over-estimating slightly is the safe direction.
_FEATURES_PER_SQ_DEG_ESTIMATE = 4.0


def estimate_payload_mb(**args: Any) -> float:
    """FR-DC-9 / Wave-1.5 payload estimator (called by the chat-warning gate).

    Scales by bbox area. A None / missing / malformed bbox returns a modest
    default (the tool requires a bbox, so this path is advisory only). The
    signature accepts ``**args`` to match the estimator convention (the gate
    passes the tool kwargs unchanged).
    """
    bbox = args.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return 0.05
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return 0.05
    area = max(0.0, (max_lon - min_lon)) * max(0.0, (max_lat - min_lat))
    est_stations = min(_MAX_LOCATIONS, max(1, int(area * _FEATURES_PER_SQ_DEG_ESTIMATE)))
    # ~3 parameters per station on average.
    est_features = est_stations * 3
    est_bytes = est_features * _BYTES_PER_FEATURE_ESTIMATE
    return float(est_bytes) / (1024 * 1024)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# ``supports_global_query=False`` — a bbox is required; OpenAQ's global station
# population is not a meaningful single-layer sweep (and the per-station latest
# fan-out would be enormous). The agent narrows by bbox.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_openaq_measurements",
    ttl_class="dynamic-1h",
    source_class="openaq",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``OpenAQInputError`` if bbox is invalid.

    bbox is ``(west, south, east, north)`` = ``(min_lon, min_lat, max_lon,
    max_lat)`` in EPSG:4326 — the SAME order OpenAQ v3 expects, so no flip.
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise OpenAQInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise OpenAQInputError(f"bbox contains non-finite/non-numeric values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise OpenAQInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise OpenAQInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise OpenAQInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(float(v), 6) for v in bbox)  # type: ignore[return-value]


def _validate_parameters(
    parameters: str | list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Normalize the optional ``parameters`` filter to canonical OpenAQ names.

    Accepts a single value or a list/tuple, case-insensitive. Each must be one
    of the OpenAQ controlled vocabulary (``VALID_PARAMETERS``). Returns the
    normalized lowercase list. ``None`` -> the ``DEFAULT_PARAMETERS`` set (the
    six core pollutants the kickoff named).

    Raises ``OpenAQInputError`` on an unknown parameter name.
    """
    if parameters is None:
        return list(DEFAULT_PARAMETERS)
    if isinstance(parameters, str):
        raw = [parameters]
    elif isinstance(parameters, (list, tuple)):
        raw = list(parameters)
    else:
        raise OpenAQInputError(
            f"parameters must be a str or list of str; got "
            f"{type(parameters).__name__}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise OpenAQInputError(f"parameters entries must be str; got {item!r}")
        key = item.strip().lower()
        if not key:
            raise OpenAQInputError("parameters entries must be non-empty")
        if key not in VALID_PARAMETERS:
            raise OpenAQInputError(
                f"parameter {item!r} is not a recognised OpenAQ pollutant; "
                f"expected one of {sorted(VALID_PARAMETERS)}"
            )
        if key not in out:
            out.append(key)
    if not out:
        return list(DEFAULT_PARAMETERS)
    return out


# ---------------------------------------------------------------------------
# API-key resolution (canonical 3-path secret loader — mirrors
# fetch_ebird_observations / fetch_usace_dams).
# ---------------------------------------------------------------------------

# Module-level Persistence binding. The agent service sets this at startup via
# ``set_persistence_for_secrets`` so this fetcher can resolve a ``secret_ref``
# without importing the MCP client. Tests inject a mock via the same setter.
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
    (agent-runtime path). Mirrors the eBird fetcher's bridge so the
    secret-loader semantics are identical.
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
        raise OpenAQMissingKeyError(
            "Persistence not bound; cannot resolve secret_ref for OpenAQ. "
            "Pass api_key=... explicitly in this context, or add an OpenAQ "
            "key via the per-Case secrets panel."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


def _resolve_api_key(
    api_key: str | None,
    secret_ref: Any | None,
) -> str:
    """Return the live OpenAQ API key from one of three lookup paths.

    Priority (canonical 3-path secret loader):

    1. Explicit ``api_key`` kwarg (live-test / dev override).
    2. ``secret_ref`` (a ``SecretRecord``) -> ``Persistence.get_secret_value``
       (the per-Case production path).
    3. ``GRACE2_OPENAQ_API_KEY`` env var (dev convenience).

    Raises ``OpenAQMissingKeyError`` (``OPENAQ_KEY_REQUIRED``) if NONE of the
    three paths produce a key — OpenAQ has no public mirror, so the honest
    degrade IS this typed error.
    """
    if api_key:
        return api_key
    if secret_ref is not None:
        try:
            resolved = _materialize_secret(secret_ref)
        except OpenAQMissingKeyError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as missing-key
            raise OpenAQMissingKeyError(
                f"OpenAQ secret_ref lookup failed: {exc}"
            ) from exc
        if resolved:
            return resolved
    env_key = os.environ.get(_OPENAQ_KEY_ENV)
    if env_key:
        return env_key
    raise OpenAQMissingKeyError(
        "no OpenAQ API key available: pass api_key=..., secret_ref=..., or set "
        "the GRACE2_OPENAQ_API_KEY env var. Register a free key at "
        "https://explore.openaq.org/ (account -> API keys)."
    )


# ---------------------------------------------------------------------------
# OpenAQ HTTP fetch — locations (with bbox) + per-location latest.
# ---------------------------------------------------------------------------


def _check_response(resp: httpx.Response, *, context: str) -> dict[str, Any]:
    """Validate an OpenAQ HTTP response, returning the parsed JSON body.

    Raises:
        ``OpenAQAuthError``: 401/403 (bad/revoked key).
        ``OpenAQUpstreamError``: 422 (bad bbox -> we surface upstream as the
            tool pre-validates), 5xx, non-JSON, or non-object body.
    """
    if resp.status_code in (401, 403):
        raise OpenAQAuthError(
            f"OpenAQ rejected the API key ({context}, HTTP {resp.status_code}): "
            f"{resp.text[:200]!r}"
        )
    if resp.status_code == 422:
        raise OpenAQUpstreamError(
            f"OpenAQ rejected the request ({context}, HTTP 422 — likely an "
            f"out-of-range bbox): {resp.text[:200]!r}"
        )
    if resp.status_code >= 400:
        raise OpenAQUpstreamError(
            f"OpenAQ returned HTTP {resp.status_code} ({context}): "
            f"{resp.text[:300]!r}"
        )
    try:
        body = resp.json()
    except (ValueError, httpx.DecodingError) as exc:
        raise OpenAQUpstreamError(
            f"OpenAQ returned non-JSON ({context}): {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise OpenAQUpstreamError(
            f"OpenAQ response is not a JSON object ({context}): "
            f"type={type(body).__name__}"
        )
    return body


def _fetch_locations_page(
    bbox: tuple[float, float, float, float],
    api_key: str,
    page: int,
    *,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    """Fetch one page of stations in the bbox. Returns the ``results`` list."""
    west, south, east, north = bbox
    params = {
        "bbox": f"{west},{south},{east},{north}",
        "limit": str(_LOCATIONS_PAGE_SIZE),
        "page": str(page),
    }
    headers = {
        "X-API-Key": api_key,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    try:
        resp = client.get(_LOCATIONS_URL, params=params, headers=headers)
    except httpx.RequestError as exc:
        raise OpenAQUpstreamError(
            f"OpenAQ network failure (locations page={page}): {exc}"
        ) from exc
    body = _check_response(resp, context=f"locations page={page}")
    results = body.get("results")
    if results is None:
        return []
    if not isinstance(results, list):
        raise OpenAQUpstreamError(
            f"OpenAQ locations 'results' is not a list (page={page}): "
            f"type={type(results).__name__}"
        )
    return results


def _fetch_all_locations(
    bbox: tuple[float, float, float, float],
    api_key: str,
    *,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    """Page through every station in the bbox up to ``_MAX_LOCATIONS``."""
    accumulated: list[dict[str, Any]] = []
    page = 1
    while True:
        results = _fetch_locations_page(bbox, api_key, page, client=client)
        accumulated.extend(results)
        logger.info(
            "fetch_openaq_measurements: locations page=%d returned=%d total=%d",
            page,
            len(results),
            len(accumulated),
        )
        if len(results) < _LOCATIONS_PAGE_SIZE:
            break
        if len(accumulated) >= _MAX_LOCATIONS:
            logger.warning(
                "fetch_openaq_measurements: hit _MAX_LOCATIONS=%d cap; "
                "truncating sweep (narrow the bbox)",
                _MAX_LOCATIONS,
            )
            accumulated = accumulated[:_MAX_LOCATIONS]
            break
        page += 1
    return accumulated


def _fetch_location_latest(
    locations_id: int,
    api_key: str,
    *,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    """Fetch the latest value per sensor at one station. Returns ``results``."""
    url = _LOCATION_LATEST_URL_TMPL.format(locations_id=locations_id)
    headers = {
        "X-API-Key": api_key,
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    try:
        resp = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise OpenAQUpstreamError(
            f"OpenAQ network failure (latest location_id={locations_id}): {exc}"
        ) from exc
    body = _check_response(resp, context=f"latest location_id={locations_id}")
    results = body.get("results")
    if results is None:
        return []
    if not isinstance(results, list):
        raise OpenAQUpstreamError(
            f"OpenAQ latest 'results' is not a list (location_id="
            f"{locations_id}): type={type(results).__name__}"
        )
    return results


# ---------------------------------------------------------------------------
# Assembly: join stations + latest values + sensor->parameter map.
# ---------------------------------------------------------------------------


def _build_sensor_param_map(
    station: dict[str, Any],
) -> dict[int, dict[str, str]]:
    """Map ``sensorsId -> {parameter, display_name, unit}`` for one station.

    Reads the station's ``sensors`` array (from the /v3/locations response).
    The /latest endpoint carries ``sensorsId`` + ``value`` but NOT the
    parameter/units, so this map is the cross-reference.
    """
    out: dict[int, dict[str, str]] = {}
    sensors = station.get("sensors") or []
    if not isinstance(sensors, list):
        return out
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        sid = sensor.get("id")
        if not isinstance(sid, int):
            continue
        param = sensor.get("parameter") or {}
        if not isinstance(param, dict):
            param = {}
        out[sid] = {
            "parameter": str(param.get("name") or ""),
            "display_name": str(param.get("displayName") or param.get("name") or ""),
            "unit": str(param.get("units") or ""),
        }
    return out


def _assemble_measurement_rows(
    stations: list[dict[str, Any]],
    latest_by_location: dict[int, list[dict[str, Any]]],
    *,
    bbox: tuple[float, float, float, float],
    parameters: list[str],
) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
    """Join stations + latest values into (rows, point coords) for the FGB.

    For each station, for each latest sensor reading, cross-reference the
    sensor's parameter/units via the station's sensor map, filter to the
    requested ``parameters`` set, hard-filter the point to the requested bbox
    (geographic-correctness gate — job-0086 codified lesson), and emit one row.

    Returns ``(rows, geoms)`` parallel lists; ``geoms`` are ``(lon, lat)``.
    """
    west, south, east, north = bbox
    param_set = set(parameters)
    rows: list[dict[str, Any]] = []
    geoms: list[tuple[float, float]] = []
    skipped_no_coord = 0
    skipped_outside = 0
    skipped_unknown_param = 0
    skipped_param_filter = 0

    for station in stations:
        if not isinstance(station, dict):
            continue
        loc_id = station.get("id")
        loc_name = str(station.get("name") or "")
        country_obj = station.get("country") or {}
        country = (
            str(country_obj.get("code") or country_obj.get("name") or "")
            if isinstance(country_obj, dict)
            else ""
        )
        # Station-level fallback coordinates (used when a latest record has
        # null coords — common for stationary monitors).
        st_coords = station.get("coordinates") or {}
        st_lon = st_coords.get("longitude") if isinstance(st_coords, dict) else None
        st_lat = st_coords.get("latitude") if isinstance(st_coords, dict) else None

        sensor_map = _build_sensor_param_map(station)

        latest_records = latest_by_location.get(loc_id, []) if isinstance(loc_id, int) else []
        for rec in latest_records:
            if not isinstance(rec, dict):
                continue
            sensor_id = rec.get("sensorsId")
            meta = sensor_map.get(sensor_id) if isinstance(sensor_id, int) else None
            if meta is None or not meta.get("parameter"):
                skipped_unknown_param += 1
                continue
            param_name = meta["parameter"].lower()
            if param_name not in param_set:
                skipped_param_filter += 1
                continue

            # Coordinates: prefer the latest record's own coords, fall back to
            # the station coords.
            rec_coords = rec.get("coordinates") or {}
            lon = (
                rec_coords.get("longitude")
                if isinstance(rec_coords, dict) and rec_coords.get("longitude") is not None
                else st_lon
            )
            lat = (
                rec_coords.get("latitude")
                if isinstance(rec_coords, dict) and rec_coords.get("latitude") is not None
                else st_lat
            )
            if lon is None or lat is None:
                skipped_no_coord += 1
                continue
            try:
                lon_f = float(lon)
                lat_f = float(lat)
            except (TypeError, ValueError):
                skipped_no_coord += 1
                continue
            if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
                skipped_no_coord += 1
                continue
            if not (west <= lon_f <= east and south <= lat_f <= north):
                skipped_outside += 1
                continue

            value_raw = rec.get("value")
            try:
                value_f = float(value_raw) if value_raw is not None else None
            except (TypeError, ValueError):
                value_f = None

            dt = rec.get("datetime") or {}
            dt_utc = str(dt.get("utc") or "") if isinstance(dt, dict) else ""
            dt_local = str(dt.get("local") or "") if isinstance(dt, dict) else ""

            rows.append(
                {
                    "location_id": int(loc_id) if isinstance(loc_id, int) else -1,
                    "location_name": loc_name,
                    "country": country,
                    "parameter": param_name,
                    "display_name": meta["display_name"],
                    "value": value_f,
                    "unit": meta["unit"],
                    "datetime_utc": dt_utc,
                    "datetime_local": dt_local,
                    "sensor_id": int(sensor_id) if isinstance(sensor_id, int) else -1,
                }
            )
            geoms.append((lon_f, lat_f))

    if skipped_no_coord:
        logger.info(
            "fetch_openaq_measurements: skipped %d records with missing coords",
            skipped_no_coord,
        )
    if skipped_outside:
        logger.info(
            "fetch_openaq_measurements: filtered %d records outside bbox %s",
            skipped_outside,
            bbox,
        )
    if skipped_unknown_param:
        logger.info(
            "fetch_openaq_measurements: skipped %d latest records with no "
            "sensor->parameter mapping",
            skipped_unknown_param,
        )
    if skipped_param_filter:
        logger.info(
            "fetch_openaq_measurements: filtered %d records outside the "
            "requested parameter set",
            skipped_param_filter,
        )
    return rows, geoms


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _rows_to_flatgeobuf_bytes(
    rows: list[dict[str, Any]],
    geoms: list[tuple[float, float]],
) -> bytes:
    """Convert assembled (rows, geoms) to FlatGeobuf bytes.

    Always emits a valid FlatGeobuf — an empty input yields a header-only FGB
    with the documented column schema (honest-empty, never a fabricated layer).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OpenAQUpstreamError(
            f"geopandas / shapely not available for FlatGeobuf conversion: {exc}"
        ) from exc

    if not rows:
        empty_df = pd.DataFrame(columns=list(PRESERVED_PROPERTIES))
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        df = pd.DataFrame(rows, columns=list(PRESERVED_PROPERTIES))
        point_geoms = [Point(lon, lat) for (lon, lat) in geoms]
        gdf = gpd.GeoDataFrame(df, geometry=point_geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_openaq_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise OpenAQUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} feature(s): {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_openaq_measurements: FlatGeobuf = %d bytes (%d feature(s))",
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
# End-to-end fetcher (locations -> per-location latest -> assemble -> FGB).
# ---------------------------------------------------------------------------


def _fetch_openaq_bytes(
    bbox: tuple[float, float, float, float],
    api_key: str,
    parameters: list[str],
) -> bytes:
    """Pipeline: bbox stations -> per-station latest -> join -> bbox-filter -> FGB."""
    with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=True) as client:
        stations = _fetch_all_locations(bbox, api_key, client=client)
        latest_by_location: dict[int, list[dict[str, Any]]] = {}
        for station in stations:
            loc_id = station.get("id") if isinstance(station, dict) else None
            if not isinstance(loc_id, int):
                continue
            latest_by_location[loc_id] = _fetch_location_latest(
                loc_id, api_key, client=client
            )

    rows, geoms = _assemble_measurement_rows(
        stations,
        latest_by_location,
        bbox=bbox,
        parameters=parameters,
    )
    logger.info(
        "fetch_openaq_measurements: assembled %d (station,parameter) point(s) "
        "from %d station(s)",
        len(rows),
        len(stations),
    )
    return _rows_to_flatgeobuf_bytes(rows, geoms)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls the external OpenAQ API), destructiveHint=False,
    # idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_openaq_measurements(
    bbox: tuple[float, float, float, float],
    parameters: str | list[str] | None = None,
    api_key: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """OpenAQ global air-quality latest measurements as a FlatGeobuf point layer.

    What it does:
        Fetches the most recent ground-station air-quality measurements
        (PM2.5, PM10, NO2, O3, SO2, CO, and other pollutants) from the OpenAQ
        v3 API for every monitoring station inside ``bbox``, returning one
        FlatGeobuf POINT feature per (station, parameter) latest reading with
        ``parameter`` / ``value`` / ``unit`` / ``datetime`` properties. OpenAQ
        aggregates reference monitors and low-cost sensors from national
        networks WORLDWIDE — this is the GLOBAL complement to the US-only
        AirNow / EPA AQS surface.

    When to use:
        - User asks about air quality / pollution anywhere outside the US, or
          wants a global-coverage source ("what's the PM2.5 in Delhi right
          now?", "show NO2 monitoring stations around London").
        - Overlaying current pollutant concentrations on a wildfire-smoke,
          dust-storm, or industrial-hazard footprint.
        - Pairing observed ground-station air quality with modeled smoke
          (HRRR-Smoke) or satellite aerosol layers.

    When NOT to use:
        - DO NOT use for US-only regulatory AQI when a US source is preferred —
          AirNow / EPA AQS give the official US AQI; OpenAQ ingests US data too
          but is not the US regulatory authority.
        - DO NOT use for HISTORICAL time-series — this returns LATEST values
          only; use the OpenAQ ``/sensors/{id}/measurements|hours|days``
          aggregate endpoints (a future tool) for time-series.
        - DO NOT use for modeled / forecast air quality — OpenAQ is
          ground-truth observations; use HRRR-Smoke / CAMS for forecasts.
        - DO NOT use for satellite column densities (NO2/aerosol from
          TROPOMI/Sentinel-5P) — those are a different (raster) source.

    Parameters:
        bbox: REQUIRED ``(min_lon, min_lat, max_lon, max_lat)`` envelope in
            EPSG:4326 (= ``(west, south, east, north)``). This is the SAME
            axis order OpenAQ v3 expects, so no flip. Example:
            ``(76.8, 28.4, 77.4, 28.9)`` for the Delhi NCR returns the city's
            monitoring stations. Narrow the bbox to a city/metro: a
            country-sized sweep is capped at 2000 stations.
        parameters: Optional pollutant filter — a single name or a list,
            case-insensitive, each one of ``pm25`` / ``pm10`` / ``pm1`` /
            ``no2`` / ``no`` / ``nox`` / ``o3`` / ``so2`` / ``co`` / ``co2`` /
            ``bc`` / ``ch4`` / ``nh3``. Defaults to the six core pollutants
            (``pm25, pm10, no2, o3, so2, co``). Applied client-side after the
            sensor->parameter join.
        api_key: Optional explicit OpenAQ API key (highest-priority resolution
            path). When set, used directly.
        secret_ref: Optional ``SecretRecord`` (from the per-Case secrets panel)
            -> resolved to the key via ``Persistence.get_secret_value`` at
            invocation time (the production path).

    Returns:
        A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
        ``s3://<cache>/cache/dynamic-1h/openaq/<key>.fgb`` containing point
        geometries (``Point`` in EPSG:4326) and the schema ``location_id``,
        ``location_name``, ``country``, ``parameter``, ``display_name``,
        ``value``, ``unit``, ``datetime_utc``, ``datetime_local``,
        ``sensor_id``. ``layer_type="vector"``, ``role="primary"``,
        ``units=None`` (the per-feature ``unit`` column carries the pollutant
        unit, which varies by parameter).

    Raises:
        ``OpenAQMissingKeyError`` (``OPENAQ_KEY_REQUIRED``): no API key resolved
            from any of the three paths — raised BEFORE any network call. The
            agent surfaces a credential-request card (OpenAQ has no public
            mirror, so this honest typed error IS the degrade).
        ``OpenAQAuthError`` (``OPENAQ_AUTH_ERROR``): the API rejected the key
            (401/403; revoked / invalid) — the agent surfaces a re-enter card.
        ``OpenAQInputError``: bad bbox or unknown parameter name.
        ``OpenAQUpstreamError``: OpenAQ 5xx / 422 / non-JSON / network failure
            (retryable).

    Cross-tool dependencies:
        Consumes optional bbox from ``fetch_administrative_boundaries`` /
        ``geocode_location`` (geocode "Delhi" -> derive bbox -> call this tool).
        Pairs with ``fetch_hrrr_smoke`` / satellite aerosol layers for
        observed-vs-modeled air-quality comparison, and feeds
        ``compute_zonal_statistics`` for population-weighted exposure.

    Cache: ``dynamic-1h`` (OpenAQ latest values refresh as new readings land;
    an hourly window balances freshness against the API rate budget). Cache
    key: SHA-256 of (bbox rounded-6dp, parameters) + the top-of-hour vintage.
    The key intentionally omits the api_key — the underlying measurements do
    not vary by caller.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    param_norm = _validate_parameters(parameters)

    # ---- API-key resolution (pre-network; cheap fail with honest typed error) ----
    resolved_key = _resolve_api_key(api_key=api_key, secret_ref=secret_ref)

    # ---- Cache-key params (quantized; key omits api_key by design) ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "parameters": sorted(param_norm),
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_openaq_bytes(
            bbox=q_bbox,
            api_key=resolved_key,
            parameters=param_norm,
        ),
    )
    assert result.uri is not None, (
        "fetch_openaq_measurements is cacheable; uri must be set by read_through"
    )

    param_label = ", ".join(p.upper() for p in param_norm)
    return LayerURI(
        layer_id=f"openaq-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"OpenAQ Air Quality (latest) — {param_label}",
        layer_type="vector",
        uri=result.uri,
        style_preset="openaq_measurements",
        role="primary",
        units=None,
        bbox=q_bbox,
    )
