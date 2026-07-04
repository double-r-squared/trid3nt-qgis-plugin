"""``fetch_era5_reanalysis`` atomic tool — Copernicus ERA5 reanalysis Tier-2 fetcher (job-0131).

Wraps the Copernicus Climate Data Store (CDS) ``cdsapi`` client to retrieve
the ERA5 hourly reanalysis (``reanalysis-era5-single-levels``) for a single
variable over a bbox and date range, converts the returned NetCDF into a
CRS-tagged COG (one band per hourly timestep, mean across the window), and
routes it through the FR-DC cache shim.

Research-validated as the **compound-flood global substrate** (Bates et al.,
NHESS 2023) — ERA5 winds + precip + significant wave height + storm surge
forcing are how the global SFINCS / GeoFLOOD compound-flood literature builds
boundary conditions outside of agency-instrumented basins. Sprint-12-mega
Wave 2 lands this as a Tier-2 substrate fetcher; downstream composers
(``model_compound_flood_global`` etc.) consume the LayerURI.

Supported variables (single-level, hourly; ERA5 single-levels CDS dataset):
    "10m_wind_speed"                 m s-1     DERIVED wind SPEED magnitude
                                               sqrt(u^2 + v^2) @ 10m — the answer
                                               to a generic "wind" request
    "10m_u_component_of_wind"        m s-1     wind east-component @ 10m
    "10m_v_component_of_wind"        m s-1     wind north-component @ 10m
    "2m_temperature"                 K         air temperature @ 2m
    "total_precipitation"            m         hourly total precip (cumulative
                                               per hour, native units METRES)
    "runoff"                         m         surface runoff (native METRES)
    "significant_height_of_combined_wind_waves_and_swell"
                                     m         significant wave height

``"10m_wind_speed"`` is a DERIVED raster variable: the tool issues TWO CDS
retrievals (``10m_u_component_of_wind`` + ``10m_v_component_of_wind``) for the
same bbox / date range, then writes the elementwise magnitude
``sqrt(u^2 + v^2)`` (m s-1) as the single-band time-mean COG (NaN nodata
preserved), stamping ``style_preset='wind_speed'`` so the publish registry
renders it 0–25 m s-1 viridis. The signed U/V components remain available for
direction-sensitive work.

API surface (verified 2026-06-08):

    cdsapi.Client(url, key).retrieve(
        "reanalysis-era5-single-levels",
        {
            "product_type": "reanalysis",
            "variable": "<variable>",
            "year":  ["YYYY", ...],
            "month": ["MM",   ...],
            "day":   ["DD",   ...],
            "time":  ["HH:00", ...],
            "area":  [N, W, S, E],    # CDS bbox convention!
            "format": "netcdf",
        },
        out_path,
    )

The retrieve call is **blocking** from the caller's perspective but **async
on the CDS side**: the server queues the request, runs it, then streams the
result back. The cdsapi client transparently polls until the job completes
(default poll interval ~1s, no caller-side timeout). We wrap retrieve in a
``concurrent.futures`` watchdog with a 5-minute wall-clock budget per the
kickoff so a stuck queue surfaces as ``ERA5UpstreamError`` instead of
hanging the agent process.

API-key resolution (Tier-2 secret handling per kickoff):

1. Explicit ``api_key`` kwarg (live test path, dev override).
2. ``secret_ref`` (a ``SecretRecord`` per ``grace2_contracts.secrets``)
   → ``Persistence.get_secret_value()`` (the production per-Case path
   landed by Wave 2 sibling job-0124).
3. ``GRACE2_COPERNICUS_CDS_API_KEY`` env var (local dev convenience).
4. ``~/.cdsapirc`` if present — the cdsapi library's own default lookup;
   this is what the live test on a developer machine uses.

If none of the four resolve a key, the tool raises ``ERA5MissingKeyError``
(retryable=False) and the agent surface routes a "needs a key" message to
the user via the secrets panel (sprint-12 Case-UX).

FR-TA-2 atomic tool. FR-CE-8 / FR-DC-3/4: routed through ``read_through``
with ``ttl_class="static-30d"`` — ERA5 reanalysis is historical and stable
(month-old data is finalised; ERA5T preliminary data also locks in after
~3 months). A 30-day cache class is conservative; in practice once a
(variable, bbox, date-range) request lands it is byte-stable for years.

Cache key composition (per audit.md): the cache shim hashes
``(variable, bbox-6dp, start_date, end_date)``. The cache key intentionally
does NOT include the api_key — the underlying ERA5 grid is the same for
every caller (FR-DC-4 dedup).

Output COG schema:
    Driver: COG, EPSG:4326 (ERA5 native projection)
    Bands:  1 (window-mean across the requested date range)
    Dtype:  float32
    Nodata: NaN
    Tags:
        units, source="ERA5_reanalysis-era5-single-levels",
        variable, start_date, end_date, tool="fetch_era5_reanalysis"

CRS: EPSG:4326. ERA5 native resolution is 0.25° (≈27 km at the equator).

Geographic-correctness gate (job-0086 codified lesson): we tag the output
COG with the exact pixel-aligned bounds derived from the requested bbox so
``rasterio.open(uri).bounds`` returns a window inside the requested area.

Payload estimation (per audit.md):
    ~0.5 MB per variable per day per 1° square at 0.25° native res.

``supports_global_query=True`` — ERA5 is global, so passing
``bbox=(-180,-90,180,90)`` is a legitimate (if expensive) call.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import tempfile
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["fetch_era5_reanalysis"]

logger = logging.getLogger("grace2_agent.tools.fetch_era5_reanalysis")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ERA5Error(RuntimeError):
    """Base class for fetch_era5_reanalysis failures."""

    error_code: str = "ERA5_ERROR"
    retryable: bool = True


class ERA5InputError(ERA5Error):
    """Bad inputs (unknown variable, malformed bbox / dates)."""

    error_code = "ERA5_INPUT_ERROR"
    retryable = False


class ERA5UpstreamError(ERA5Error):
    """CDS API returned an error or the retrieve timed out / network failed."""

    error_code = "ERA5_UPSTREAM_ERROR"
    retryable = True


class ERA5MissingKeyError(ERA5Error):
    """No API key resolved via any of the four lookup paths.

    Raised BEFORE any network call. The agent surface uses this to prompt
    the user to add a Copernicus CDS key via the secrets panel.
    """

    error_code = "ERA5_MISSING_KEY"
    retryable = False


class ERA5AuthError(ERA5Error):
    """CDS API rejected the key (invalid / revoked / not licensed)."""

    error_code = "ERA5_AUTH_ERROR"
    retryable = False


class ERA5EmptyError(ERA5Error):
    """The retrieved NetCDF contained no finite pixels in the requested bbox."""

    error_code = "ERA5_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# CDS dataset id (single-level hourly reanalysis).
_CDS_DATASET = "reanalysis-era5-single-levels"

# Default CDS API endpoint URL. cdsapi >= 0.7 routes to the new CDS-Beta
# (cds-beta.climate.copernicus.eu) but the legacy URL still works.
_DEFAULT_CDS_URL = "https://cds.climate.copernicus.eu/api"

# Wall-clock budget for the CDS retrieve call (queue + run + stream).
# Per audit.md: poll up to 5 min for completion.
_RETRIEVE_TIMEOUT_S = 300

# Narrow, specific phrases that mark a cdsapi failure as "no credentials
# configured at all" (the missing-``~/.cdsapirc`` / no-key family) — distinct
# from a present-but-rejected key (AUTH) or a transient queue/network failure
# (UPSTREAM). When the cdsapi Client constructor cannot find any key it raises
# ``Exception("Missing/incomplete configuration file: <path>/.cdsapirc")``;
# these phrases catch that and the close variants WITHOUT over-matching a
# generic upstream error (LIVE BUG NATE 2026-06-18 — the missing-config
# message previously fell through to ERA5UpstreamError and no credential card
# fired). Matched case-insensitively against the lower-cased message.
_MISSING_KEY_CDS_PHRASES: tuple[str, ...] = (
    ".cdsapirc",
    "missing/incomplete configuration",
    "missing or incomplete configuration",
    "incomplete configuration file",
    "no api key configured",
    "no api key found",
    "credentials not configured",
    "no credentials found",
)

# Real CDS single-level variable names (each maps to one CDS retrieve).
_CDS_VARIABLES: frozenset[str] = frozenset(
    {
        "10m_u_component_of_wind",
        "10m_v_component_of_wind",
        "2m_temperature",
        "total_precipitation",
        "runoff",
        "significant_height_of_combined_wind_waves_and_swell",
    }
)

# Derived variable: wind SPEED magnitude sqrt(u^2 + v^2) over the 10 m
# components. Not a CDS variable — the tool retrieves both components and
# combines them into one raster band. The components it depends on, its units,
# and its style preset are named here.
_DERIVED_WIND_SPEED = "10m_wind_speed"
_WIND_SPEED_COMPONENTS = ("10m_u_component_of_wind", "10m_v_component_of_wind")
_WIND_SPEED_UNITS = "m s-1"
_WIND_SPEED_STYLE_PRESET = "wind_speed"

# Every variable the tool accepts (CDS-native + derived).
_ALLOWED_VARIABLES: frozenset[str] = _CDS_VARIABLES | {_DERIVED_WIND_SPEED}

# Native units per variable (used to tag the output COG and surface in
# narration). ERA5 single-levels documentation.
_VARIABLE_UNITS: dict[str, str] = {
    "10m_wind_speed": _WIND_SPEED_UNITS,
    "10m_u_component_of_wind": "m s-1",
    "10m_v_component_of_wind": "m s-1",
    "2m_temperature": "K",
    "total_precipitation": "m",
    "runoff": "m",
    "significant_height_of_combined_wind_waves_and_swell": "m",
}

# Sanity cap on date range — refuse multi-year ad-hoc retrievals that would
# blow through the CDS quota. A 1-year window already produces ~365 hourly
# timesteps * variable * grid. Composers wanting a multi-year climatology
# should call this tool in a loop and aggregate.
_MAX_DATE_RANGE_DAYS = 366


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_era5_reanalysis",
    ttl_class="static-30d",
    source_class="era5",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    variable: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output COG size in MB for a given call (Wave 1.5 surface).

    Per audit.md: ~0.5 MB per variable per day per 1° square at 0.25° native
    res. We treat ``bbox=None`` as global (360° × 180°).

    Used by the tool-payload-warning envelope (see
    ``AtomicToolMetadata.payload_mb_estimator_name``). Wrong answers are
    cheap (a chat warning instead of a hard block); we err on the high
    side so the user sees the warning rather than a surprise download.
    """
    if bbox is None:
        sq_deg = 360.0 * 180.0
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
        except (TypeError, ValueError):
            sq_deg = 1.0

    if not start_date or not end_date:
        n_days = 1
    else:
        try:
            d0 = _dt.date.fromisoformat(start_date)
            d1 = _dt.date.fromisoformat(end_date)
            n_days = max(1, (d1 - d0).days + 1)
        except ValueError:
            n_days = 1

    # 0.5 MB / variable / day / 1° square per audit.md.
    return 0.5 * float(n_days) * float(max(1.0, sq_deg))


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``ERA5InputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise ERA5InputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ERA5InputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise ERA5InputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise ERA5InputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise ERA5InputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_variable(variable: str) -> None:
    """Raise ``ERA5InputError`` for unsupported variable names."""
    if not isinstance(variable, str):
        raise ERA5InputError(
            f"variable must be a str; got {type(variable).__name__}"
        )
    if variable not in _ALLOWED_VARIABLES:
        raise ERA5InputError(
            f"unsupported ERA5 variable {variable!r}; allowed: "
            f"{sorted(_ALLOWED_VARIABLES)}"
        )


def _parse_iso_date(s: str, *, field: str) -> _dt.date:
    if not isinstance(s, str):
        raise ERA5InputError(f"{field} must be ISO-8601 YYYY-MM-DD; got {s!r}")
    try:
        return _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise ERA5InputError(
            f"{field}={s!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc


def _validate_date_range(start_date: str, end_date: str) -> tuple[_dt.date, _dt.date]:
    """Validate ISO dates + ordering + reasonable window."""
    d0 = _parse_iso_date(start_date, field="start_date")
    d1 = _parse_iso_date(end_date, field="end_date")
    if d0 > d1:
        raise ERA5InputError(
            f"start_date must be <= end_date; got start={d0}, end={d1}"
        )
    # ERA5 covers 1940-01-01 onward. Reject obvious typos.
    if d0.year < 1940 or d1.year > _dt.date.today().year + 1:
        raise ERA5InputError(
            f"date range [{d0}, {d1}] outside ERA5 coverage (1940 → present)"
        )
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise ERA5InputError(
            f"date range {n_days} days exceeds hard cap "
            f"{_MAX_DATE_RANGE_DAYS}; call in chunks and aggregate"
        )
    return d0, d1


# ---------------------------------------------------------------------------
# API-key resolution (FR-AS-11 + §F.3 per-Case secret path).
# ---------------------------------------------------------------------------


def _resolve_api_key(
    api_key: str | None,
    secret_ref: Any | None,
) -> str | None:
    """Return the live CDS API key from one of the four lookup paths.

    Priority (per audit.md):

    1. Explicit ``api_key`` kwarg.
    2. ``secret_ref`` (a ``SecretRecord``) → ``Persistence.get_secret_value``
       (the per-Case path landed by Wave 2 sibling job-0124).
    3. ``GRACE2_COPERNICUS_CDS_API_KEY`` env var.
    4. ``None`` — cdsapi falls back to ``~/.cdsapirc`` on instantiation.

    A return value of ``None`` means "let cdsapi find its own key via the
    library's default discovery path (``~/.cdsapirc``)". We do NOT raise
    ``ERA5MissingKeyError`` for the None case because the live developer
    workflow stores credentials in ``~/.cdsapirc``; the cdsapi Client
    constructor will raise its own diagnostic if neither the explicit key
    nor the rc file is present. We catch that and re-raise as
    ``ERA5MissingKeyError`` from the call site.
    """
    # 1. Explicit kwarg.
    if api_key:
        return api_key

    # 2. secret_ref via Persistence (lazy import to avoid MCP startup cost).
    if secret_ref is not None:
        try:
            return _materialize_secret(secret_ref)
        except Exception as exc:  # noqa: BLE001
            raise ERA5MissingKeyError(
                f"secret_ref lookup failed: {exc}"
            ) from exc

    # 3. Env var fallback.
    env_key = os.environ.get("GRACE2_COPERNICUS_CDS_API_KEY")
    if env_key:
        return env_key

    # 4. None → cdsapi finds ~/.cdsapirc itself.
    return None


def _materialize_secret(secret_ref: Any) -> str:
    """Bridge ``Persistence.get_secret_value`` (async) into a sync caller.

    Mirrors the fetch_ebird_observations pattern: lazy import of Persistence,
    sync-bridge for async coroutine, and a test-mock shortcut for plain
    strings.
    """
    if isinstance(secret_ref, str):
        return secret_ref

    persistence = _get_persistence_for_secrets()
    if persistence is None:
        raise ERA5MissingKeyError(
            "Persistence not bound; cannot resolve secret_ref. "
            "Pass api_key=... explicitly in this context."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for secret materialization.

    Mirrors the eBird Tier-2 binding (``fetch_ebird_observations`` job-0128).
    Called once at startup by the agent service; tests inject a mock.
    """
    global _PERSISTENCE_FOR_SECRETS
    _PERSISTENCE_FOR_SECRETS = persistence


def _get_persistence_for_secrets() -> Any | None:
    return _PERSISTENCE_FOR_SECRETS


def _run_coro_sync(coro: Any) -> Any:
    """Run an ``asyncio`` coroutine and return its result from sync context."""
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


# ---------------------------------------------------------------------------
# CDS retrieve → NetCDF (with timeout watchdog).
# ---------------------------------------------------------------------------


def _build_cds_request(
    variable: str,
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
) -> dict[str, Any]:
    """Construct the CDS retrieve-request dict for a (variable, bbox, range).

    CDS expects:
    - ``area = [N, W, S, E]`` (NOT west/south/east/north!).
    - ``year`` / ``month`` / ``day`` as zero-padded string lists. The CDS
      docs allow either every-day-explicit OR a year-month-all-days form;
      we pin to the explicit per-day list so the request shape is
      deterministic across date ranges.
    - ``time = ["HH:00"]`` hourly slots, zero-padded.
    """
    west, south, east, north = bbox

    # Days spanned, expressed as a (year, month, day) explicit list.
    years: set[str] = set()
    months: set[str] = set()
    days: set[str] = set()
    cur = d0
    one = _dt.timedelta(days=1)
    while cur <= d1:
        years.add(f"{cur.year:04d}")
        months.add(f"{cur.month:02d}")
        days.add(f"{cur.day:02d}")
        cur += one

    # All 24 hourly slots.
    hours = [f"{h:02d}:00" for h in range(24)]

    return {
        "product_type": "reanalysis",
        "variable": variable,
        "year": sorted(years),
        "month": sorted(months),
        "day": sorted(days),
        "time": hours,
        "area": [north, west, south, east],  # N, W, S, E — CDS convention
        "format": "netcdf",
    }


def _cds_retrieve_with_timeout(
    api_url: str,
    api_key: str | None,
    request: dict[str, Any],
    out_path: str,
    timeout_s: int = _RETRIEVE_TIMEOUT_S,
) -> None:
    """Call cdsapi.Client.retrieve under a wall-clock timeout watchdog.

    cdsapi has no native ``timeout`` parameter — the library polls the CDS
    queue every ~1s until the job completes (or fails) and only then
    streams the file. We spawn the retrieve in a worker thread and join
    with a deadline; on timeout we raise ``ERA5UpstreamError`` (retryable).

    Note: a timed-out request leaves an orphan CDS job server-side; the
    client cannot cancel it. The user will see it in their CDS dashboard
    queue history. Documented in the docstring; surfaced as
    ``OQ-0131-CDS-ORPHAN-JOB``.
    """
    import threading

    try:
        import cdsapi  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ERA5UpstreamError(
            f"cdsapi package not available: {exc}"
        ) from exc

    err_box: dict[str, BaseException] = {}

    def _do_retrieve() -> None:
        try:
            # cdsapi.Client kwargs:
            #   url, key, verify, timeout, quiet, debug, full_stack,
            #   delete, retry_max, sleep_max, wait_until_complete
            client_kwargs: dict[str, Any] = {"quiet": True}
            if api_url:
                client_kwargs["url"] = api_url
            if api_key:
                client_kwargs["key"] = api_key
            # If api_key is None, cdsapi falls back to ~/.cdsapirc on its own.
            client = cdsapi.Client(**client_kwargs)
            client.retrieve(_CDS_DATASET, request, out_path)
        except BaseException as exc:  # noqa: BLE001
            err_box["err"] = exc

    t = threading.Thread(target=_do_retrieve, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise ERA5UpstreamError(
            f"CDS retrieve exceeded {timeout_s}s wall-clock budget; "
            f"the CDS job may still be queued server-side."
        )
    if "err" in err_box:
        exc = err_box["err"]
        msg = str(exc)
        low = msg.lower()
        # Classify in priority order: MISSING-KEY (no credentials configured at
        # all) → AUTH (a key is present but rejected) → generic UPSTREAM.
        #
        # The NO-KEY case is what fires when none of the four key-resolution
        # paths produced a key AND there is no ``~/.cdsapirc``: cdsapi's own
        # Client constructor raises ``Exception("Missing/incomplete
        # configuration file: <path>/.cdsapirc")`` (LIVE BUG NATE 2026-06-18:
        # a Mexico Beach run hit exactly this and got NO secret-entry card
        # because the message matched neither the auth nor the old missing-key
        # heuristic, so it fell through to ``ERA5UpstreamError`` and the
        # credential pipeline never fired). We now classify it as
        # ``ERA5MissingKeyError`` (error_code ERA5_MISSING_KEY) so the server's
        # ``is_credential_error`` → ``credential-request`` path surfaces the
        # registered ``ecmwf_cds`` card. ``_MISSING_KEY_CDS_PHRASES`` is kept
        # narrow + specific so a genuine transient/queue/timeout upstream
        # failure is NOT misclassified as a missing key.
        if any(phrase in low for phrase in _MISSING_KEY_CDS_PHRASES):
            raise ERA5MissingKeyError(
                f"No Copernicus CDS API key is configured "
                f"(cdsapi: {msg[:200]})"
            ) from exc
        # AUTH: a key is present but the CDS server rejected it (invalid /
        # revoked / not licensed). cdsapi surfaces these with "401" / "403" /
        # "Authentication" / "User not authenticated" / "unauthorized".
        if any(tok in low for tok in ("401", "403", "authentication", "unauthorized")):
            raise ERA5AuthError(
                f"CDS API rejected the key: {msg[:200]}"
            ) from exc
        # Backstop missing-key heuristic ("no api key" / a message that names
        # both "missing" and "key") — kept for upstreams whose phrasing differs
        # from cdsapi's own constructor text.
        if "no api key" in low or ("missing" in low and "key" in low):
            raise ERA5MissingKeyError(
                f"CDS API key not available: {msg[:200]}"
            ) from exc
        # Everything else is a genuine transient/queue/network/upstream
        # failure → retryable ERA5UpstreamError (do NOT misclassify as a
        # missing key, or we'd prompt for a key the user already has).
        raise ERA5UpstreamError(
            f"CDS retrieve failed: {msg[:200]}"
        ) from exc


# ---------------------------------------------------------------------------
# NetCDF → COG conversion.
# ---------------------------------------------------------------------------


def _netcdf_to_da(
    nc_path: str,
    cds_variable: str,
    bbox: tuple[float, float, float, float],
) -> Any:
    """Open one CDS NetCDF, time-mean to a single 2D band, clip to bbox.

    Returns a 2D ``xarray.DataArray`` (EPSG:4326, in-memory values) for one
    CDS-native variable. Factored out of ``_netcdf_to_cog_bytes`` so the derived
    ``10m_wind_speed`` can build both component DataArrays on the same grid and
    combine them before writing a single COG.

    Raises:
        ``ERA5UpstreamError``: NetCDF open / xarray read failure.
        ``ERA5EmptyError``: bbox falls outside the variable's coverage.
    """
    import numpy as np
    import rioxarray  # noqa: F401 — registers .rio accessor on DataArrays
    import xarray as xr

    try:
        ds = xr.open_dataset(nc_path, engine="netcdf4", chunks=None)
    except Exception as exc:  # noqa: BLE001
        # netcdf4 may not be available; try the default engine.
        try:
            ds = xr.open_dataset(nc_path, chunks=None)
        except Exception as exc2:  # noqa: BLE001
            raise ERA5UpstreamError(
                f"xarray could not open CDS NetCDF {nc_path}: {exc2} "
                f"(netcdf4-engine error: {exc})"
            ) from exc2

    try:
        # CDS variable short names differ from the long-name request. The
        # mapping is documented in ERA5 single-levels parameter db; we
        # discover the data variable by exclusion (drop coordinate vars).
        data_vars = [v for v in ds.data_vars if v not in ds.coords]
        if not data_vars:
            raise ERA5UpstreamError(
                f"CDS NetCDF carried no data variables; got {list(ds.variables)}"
            )

        # Prefer the variable whose long_name attribute mentions the
        # requested ERA5 variable name; fall back to the first data_var.
        chosen = data_vars[0]
        target_token = cds_variable.replace("_", " ").lower()
        for v in data_vars:
            ln = ds[v].attrs.get("long_name", "").lower()
            if target_token in ln:
                chosen = v
                break

        da = ds[chosen]

        # Average across all non-spatial dims (time, expver, etc.) so we
        # emit a single 2D band. ERA5T data often ships a second "expver"
        # dim (1 = ERA5, 5 = ERA5T preliminary); a simple mean across this
        # axis is documented as the "merge" path in the ERA5 user guide.
        keep_dims = {"latitude", "longitude", "lat", "lon", "y", "x"}
        reduce_dims = [d for d in da.dims if d not in keep_dims]
        if reduce_dims:
            da = da.mean(dim=reduce_dims, skipna=True, keep_attrs=True)

        # Standardize coord names to latitude/longitude if shipped as lat/lon.
        rename_map: dict[str, str] = {}
        if "lat" in da.dims and "latitude" not in da.dims:
            rename_map["lat"] = "latitude"
        if "lon" in da.dims and "longitude" not in da.dims:
            rename_map["lon"] = "longitude"
        if rename_map:
            da = da.rename(rename_map)

        # Set CRS via rioxarray. ERA5 ships on EPSG:4326.
        da = da.rio.write_crs("EPSG:4326")
        # ERA5 latitudes are typically descending (90 → -90); rioxarray's
        # clip_box wants standard orientation. Sort latitude ascending if
        # required.
        if "latitude" in da.dims and len(da["latitude"]) > 1:
            lat_vals = da["latitude"].values
            if lat_vals[0] > lat_vals[-1]:
                da = da.sortby("latitude")

        # ERA5 longitudes may be 0..360. Convert to -180..180 if needed so
        # the bbox clip works for both (-bbox) and (+bbox) requests.
        if "longitude" in da.dims:
            lon_vals = da["longitude"].values
            if lon_vals.max() > 180.0:
                da = da.assign_coords(
                    longitude=(((da["longitude"] + 180) % 360) - 180)
                )
                da = da.sortby("longitude")

        # Clip to requested bbox (geographic-correctness gate).
        west, south, east, north = bbox
        try:
            da = da.rio.clip_box(
                minx=west, miny=south, maxx=east, maxy=north, crs="EPSG:4326"
            )
        except Exception as exc:  # noqa: BLE001
            raise ERA5UpstreamError(
                f"rioxarray clip_box to bbox={bbox} failed: {exc}"
            ) from exc

        if da.size == 0:
            raise ERA5EmptyError(
                f"bbox={bbox} produced an empty ERA5 window after clip"
            )

        arr = np.asarray(da.values, dtype=np.float32)
        if not np.isfinite(arr).any():
            raise ERA5EmptyError(
                f"bbox={bbox} produced no finite ERA5 pixels (all-NaN window)"
            )

        # Materialize values while the dataset is open so the caller can close
        # ``ds`` and still operate on the returned DataArray.
        return da.compute()
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _da_to_cog_bytes(da: Any, variable: str) -> bytes:
    """Stamp metadata on ``da`` and write a float32 EPSG:4326 COG; return bytes.

    Per audit.md, the kickoff returns "GeoTIFF" — we write COG which is a
    GeoTIFF profile (and the canonical raster output across the rest of the
    GRACE-2 atomic-tool set: HRSL, MTBS, LANDFIRE, NLCD).
    """
    import numpy as np

    arr = np.asarray(da.values, dtype=np.float32)

    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="grace2_era5_")
    os.close(out_fd)
    try:
        da_out = da.astype("float32")
        # Tag metadata.
        da_out.attrs["units"] = _VARIABLE_UNITS.get(
            variable, da.attrs.get("units", "")
        )
        da_out.attrs["source"] = "ERA5_reanalysis-era5-single-levels"
        da_out.attrs["variable"] = variable
        da_out.attrs["tool"] = "fetch_era5_reanalysis"
        try:
            da_out.rio.to_raster(
                out_path,
                driver="COG",
                dtype="float32",
                compress="DEFLATE",
                nodata=float("nan"),
            )
        except Exception:  # noqa: BLE001 — fall back to GTiff if COG fails
            da_out.rio.to_raster(
                out_path,
                driver="GTiff",
                dtype="float32",
                compress="DEFLATE",
                nodata=float("nan"),
            )

        with open(out_path, "rb") as f:
            cog_bytes = f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    logger.info(
        "fetch_era5_reanalysis: wrote %d-byte COG (variable=%s, "
        "min=%.4f, max=%.4f, mean=%.4f)",
        len(cog_bytes),
        variable,
        float(np.nanmin(arr)),
        float(np.nanmax(arr)),
        float(np.nanmean(arr)),
    )
    return cog_bytes


def _netcdf_to_cog_bytes(
    nc_path: str,
    variable: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Open the CDS-returned NetCDF, time-mean across timesteps, write a COG.

    Returns COG bytes (float32, EPSG:4326). The output has one band carrying
    the time-mean of the requested CDS-native variable across the date range.
    (The derived ``10m_wind_speed`` is handled upstream in ``_fetch_era5_bytes``
    via ``_combine_wind_components_to_cog_bytes`` since it spans two NetCDFs.)

    Geographic-correctness gate (job-0086): we clip the output to the
    requested bbox after reprojection (ERA5 ships on a 0.25° grid with
    longitudes 0..360 OR -180..180 depending on the variable family; we
    normalize to -180..180 with rioxarray before clipping).

    Raises:
        ``ERA5UpstreamError``: NetCDF open / xarray read / COG write failure.
        ``ERA5EmptyError``: bbox falls outside the variable's coverage.
    """
    try:
        import numpy as np  # noqa: F401 — used by helpers
        import rioxarray  # noqa: F401 — registers .rio accessor on DataArrays
        import xarray as xr  # noqa: F401
    except ImportError as exc:
        raise ERA5UpstreamError(
            f"xarray / rioxarray / numpy not available: {exc}"
        ) from exc

    da = _netcdf_to_da(nc_path, variable, bbox)
    return _da_to_cog_bytes(da, variable)


def _combine_wind_components_to_cog_bytes(
    u_nc_path: str,
    v_nc_path: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Combine the U + V wind-component NetCDFs into a wind-SPEED COG.

    Reads both 10 m component NetCDFs, takes each one's time-mean clipped to
    bbox, and writes the elementwise magnitude ``sqrt(u^2 + v^2)`` (m s-1) as a
    single-band float32 COG with NaN nodata preserved.

    Raises:
        ``ERA5UpstreamError``: NetCDF open / read / COG write failure.
        ``ERA5EmptyError``: bbox falls outside coverage (all-NaN window).
    """
    try:
        import numpy as np
        import rioxarray  # noqa: F401 — registers .rio accessor on DataArrays
        import xarray as xr
    except ImportError as exc:
        raise ERA5UpstreamError(
            f"xarray / rioxarray / numpy not available: {exc}"
        ) from exc

    u_var, v_var = _WIND_SPEED_COMPONENTS
    da_u = _netcdf_to_da(u_nc_path, u_var, bbox)
    da_v = _netcdf_to_da(v_nc_path, v_var, bbox)

    # Magnitude = sqrt(u^2 + v^2). NaN in either component propagates as NaN
    # (np.hypot preserves NaN), preserving the nodata mask. xarray aligns the
    # two DataArrays on their (latitude, longitude) coords — they ride the
    # identical ERA5 0.25° grid so alignment is a no-op.
    speed = xr.apply_ufunc(np.hypot, da_u, da_v, keep_attrs=False)
    speed = speed.astype("float32")
    # apply_ufunc drops the rio CRS accessor state on some xarray versions;
    # re-stamp CRS so to_raster writes a georeferenced COG.
    speed = speed.rio.write_crs("EPSG:4326")

    arr = np.asarray(speed.values, dtype=np.float32)
    if not np.isfinite(arr).any():
        raise ERA5EmptyError(
            f"bbox={bbox} produced no finite ERA5 wind-speed pixels (all-NaN)"
        )

    return _da_to_cog_bytes(speed, _DERIVED_WIND_SPEED)


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _retrieve_cds_variable_to_netcdf(
    cds_variable: str,
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
    api_key: str | None,
    out_path: str,
) -> None:
    """Build + run the CDS retrieve for one CDS-native variable into ``out_path``."""
    request = _build_cds_request(cds_variable, bbox, d0, d1)
    _cds_retrieve_with_timeout(
        api_url=_DEFAULT_CDS_URL,
        api_key=api_key,
        request=request,
        out_path=out_path,
    )


def _fetch_era5_bytes(
    variable: str,
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
    api_key: str | None,
) -> bytes:
    """End-to-end: CDS retrieve(s) → NetCDF(s) → COG bytes.

    For a CDS-native variable this is one retrieve → one COG. For the derived
    ``10m_wind_speed`` it issues TWO retrieves (the U + V 10 m components) and
    writes the elementwise ``sqrt(u^2 + v^2)`` magnitude as one band.
    """
    if variable == _DERIVED_WIND_SPEED:
        u_var, v_var = _WIND_SPEED_COMPONENTS
        u_fd, u_path = tempfile.mkstemp(suffix=".nc", prefix="grace2_era5_cds_u_")
        os.close(u_fd)
        v_fd, v_path = tempfile.mkstemp(suffix=".nc", prefix="grace2_era5_cds_v_")
        os.close(v_fd)
        try:
            _retrieve_cds_variable_to_netcdf(u_var, bbox, d0, d1, api_key, u_path)
            _retrieve_cds_variable_to_netcdf(v_var, bbox, d0, d1, api_key, v_path)
            return _combine_wind_components_to_cog_bytes(u_path, v_path, bbox)
        finally:
            for p in (u_path, v_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    nc_fd, nc_path = tempfile.mkstemp(
        suffix=".nc", prefix="grace2_era5_cds_"
    )
    os.close(nc_fd)
    try:
        _retrieve_cds_variable_to_netcdf(variable, bbox, d0, d1, api_key, nc_path)
        return _netcdf_to_cog_bytes(nc_path, variable, bbox)
    finally:
        try:
            os.unlink(nc_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls Copernicus CDS external API for ERA5 reanalysis),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_era5_reanalysis(
    bbox: tuple[float, float, float, float],
    variable: str,
    start_date: str,
    end_date: str,
    api_key: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Copernicus ERA5 global reanalysis Tier-2 fetcher (single-level hourly).

    **What it does:** Retrieves an ERA5 hourly reanalysis variable for a bbox
    and date range via the Copernicus Climate Data Store (CDS) ``cdsapi``
    client, converts the returned NetCDF into a CRS-tagged COG (time-mean
    over the requested window), and routes the result through the 30-day cache.
    Supported variables: 10 m U/V wind components, 2 m temperature, total
    precipitation, surface runoff, and significant wave height.

    **When to use:**
    - A GENERIC historical wind request ("how windy was it", "wind speed over
      the Gulf during the storm", "show me the wind") → use
      ``variable="10m_wind_speed"``. This returns a SINGLE positive wind-speed
      magnitude field (``sqrt(u^2 + v^2)``, m s-1) — the natural answer to "how
      windy". Only reach for the lone signed ``10m_u_component_of_wind`` /
      ``10m_v_component_of_wind`` when the user explicitly needs wind DIRECTION
      or a vector component.
    - Building global or non-CONUS compound-flood forcing: ERA5 winds, precip,
      or wave height as SFINCS storm-surge / pluvial boundary conditions where
      agency instrumentation (CO-OPS, MRMS, ATCF) does not reach.
    - Any post-event analysis that needs a globally consistent atmospheric
      reanalysis over a historical period (1940 to ~3 months ago).
    - User asks for historical wind fields, precipitation totals, or sea-state
      data outside the CONUS gauge network.
    - Compound-flood substrate following Bates et al. (NHESS 2023): ERA5 is
      the research-validated global atmospheric forcing for that workflow.

    **When NOT to use:**
    - DO NOT use for real-time / forecast data — ERA5 lags by 5 days (ERA5T
      preliminary) or 3 months (finalised); use ``fetch_hrrr_forecast`` or
      ``fetch_goes_satellite`` for live/near-real-time queries.
    - DO NOT use for CONUS precipitation when MRMS is available — MRMS QPE is
      1 km gauge-corrected vs ERA5's 27 km; use ``fetch_mrms_qpe`` instead.
    - DO NOT use for sub-hourly timesteps; ERA5 is hourly minimum.
    - Requires a Copernicus CDS API key (free registration at
      https://cds.climate.copernicus.eu/user/register); raises
      ``ERA5MissingKeyError`` without one.

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(west, south, east,
      north)`` in EPSG:4326. ``supports_global_query=True`` — global bbox
      ``(-180, -90, 180, 90)`` is valid but expensive. Example for Gulf
      Coast: ``(-100.0, 20.0, -80.0, 35.0)``.
    - ``variable`` (str): one of ``"10m_wind_speed"`` (DERIVED wind-speed
      magnitude ``sqrt(u^2 + v^2)``, the answer to a generic "wind" request),
      ``"10m_u_component_of_wind"``, ``"10m_v_component_of_wind"``,
      ``"2m_temperature"``, ``"total_precipitation"``, ``"runoff"``,
      ``"significant_height_of_combined_wind_waves_and_swell"``. For any
      non-directional wind question pass ``"10m_wind_speed"``.
    - ``start_date`` (str): ISO YYYY-MM-DD inclusive. ERA5 coverage: 1940-01-01
      to ~3 months ago (finalised) or 5 days ago (ERA5T preliminary).
    - ``end_date`` (str): ISO YYYY-MM-DD inclusive. Hard cap 366 days from start.
    - ``api_key`` (str | None): explicit CDS key; overrides all other resolution
      paths. Resolved priority: kwarg → ``secret_ref`` → env var
      ``GRACE2_COPERNICUS_CDS_API_KEY`` → ``~/.cdsapirc``.
    - ``secret_ref`` (Any | None): per-Case ``SecretRecord`` for production.

    **Returns:** A ``LayerURI`` pointing at a float32 COG in the cache bucket
    (``gs://grace-2-hazard-prod-cache/cache/static-30d/era5/<key>.tif``)
    carrying the time-mean of the requested variable, clipped to bbox.
    ``layer_type="raster"``, ``role="primary"``. Units: ``"m s-1"`` for
    wind, ``"K"`` for temperature, ``"m"`` for precipitation / runoff /
    wave height. EPSG:4326, 0.25° native (~27 km).

    **Cross-tool dependencies:**
    - Upstream: ``geocode_location`` supplies bbox from a place name.
    - Downstream: ``build_sfincs_model`` consumes wind/precip COGs as forcing;
      ``fetch_gtsm_tide_surge`` pairs for the coastal boundary condition.
    - Alternative: ``fetch_mrms_qpe`` (CONUS, 1 km, gauge-corrected, no key),
      ``fetch_hrrr_forecast`` (CONUS, 3 km, forecast, no key).

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key
    is ``(variable, bbox-rounded-6dp, start_date, end_date)`` — api_key
    excluded so the same ERA5 grid is shared across callers (FR-DC-4 dedup).
    CDS jobs are queued server-side; wrapped in a 5-minute wall-clock timeout.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    _validate_variable(variable)
    d0, d1 = _validate_date_range(start_date, end_date)

    # ---- API-key resolution (pre-network; cheap fail) ----
    resolved_key = _resolve_api_key(api_key=api_key, secret_ref=secret_ref)
    # NOTE: resolved_key may be None — that is intentional. cdsapi falls
    # back to ~/.cdsapirc; the auth error surfaces from the call site.

    # ---- Cache-key params (key omits api_key by design) ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "variable": variable,
        "bbox": list(q_bbox),
        "start_date": d0.isoformat(),
        "end_date": d1.isoformat(),
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_era5_bytes(
            variable=variable,
            bbox=q_bbox,
            d0=d0,
            d1=d1,
            api_key=resolved_key,
        ),
    )
    assert result.uri is not None, (
        "fetch_era5_reanalysis is cacheable; uri must be set by read_through"
    )

    # The derived wind-speed variable stamps the shared ``wind_speed`` preset
    # (0–25 m s-1 viridis in the publish registry); CDS-native variables keep
    # their per-variable preset name.
    style_preset = (
        _WIND_SPEED_STYLE_PRESET
        if variable == _DERIVED_WIND_SPEED
        else f"era5_{variable}"
    )

    return LayerURI(
        layer_id=(
            f"era5-{variable.replace('_', '-')}-"
            f"{d0.isoformat()}-{d1.isoformat()}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"ERA5 Reanalysis — {variable.replace('_', ' ').title()} "
            f"({d0.isoformat()} → {d1.isoformat()})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset=style_preset,
        role="primary",
        units=_VARIABLE_UNITS.get(variable),
    )
