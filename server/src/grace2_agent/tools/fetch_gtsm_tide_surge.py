"""``fetch_gtsm_tide_surge`` atomic tool — Global Tide and Surge Model v3.0 Tier-2 fetcher (job-0132).

Wraps the Copernicus Climate Data Store (CDS) ``cdsapi`` client to retrieve
GTSM (Global Tide and Surge Model, Deltares) water-level time series — the
coastal forcing that combines astronomical tide + meteorologically-driven
surge — for a bbox + date range, converts the returned NetCDF into a
CSV-tabular time-series output (one row per gauge × hour), and routes it
through the FR-DC cache shim.

Research-validated as the **compound-flood coastal boundary** (Eilander
et al., 2023; Muis et al., 2020 / 2023) — GTSM provides the canonical
coastal water-level boundary condition for global SFINCS / compound-flood
literature outside of agency-instrumented basins (CO-OPS in the US). Sprint-
12-mega Wave 2 lands this as a Tier-2 substrate fetcher; downstream
composers (``model_compound_flood_global`` etc.) consume the LayerURI.

Supported outputs (``output`` param):

    "water_level"  — total water level (tide + surge); the canonical SFINCS
                     coastal boundary input. Default.
    "surge_only"   — meteorological surge component only (water_level minus
                     pure-tide); useful for surge attribution and forecast
                     skill vs tide gauge baselines.

API surface (verified 2026-06-08):

    cdsapi.Client(url, key).retrieve(
        "sis-water-level-change-timeseries-cmip6",
        {
            "experiment":      ["reanalysis"],
            "variable":        ["total_water_level"]  | ["storm_surge_residual"],
            "year":            ["YYYY", ...],
            "month":           ["MM",   ...],
            "temporal_aggregation": ["10_min"]  | ["hourly"],
            "format":          "zip",
        },
        out_path,
    )

The retrieve call is **blocking** from the caller's perspective but **async
on the CDS side**: the server queues the request, runs it, then streams the
ZIP archive containing NetCDF files. The cdsapi client transparently polls
until the job completes. We wrap retrieve in a ``concurrent.futures``
watchdog with a 5-minute wall-clock budget per the kickoff so a stuck
queue surfaces as ``GTSMUpstreamError`` instead of hanging the agent.

API-key resolution (Tier-2 secret handling per kickoff):

1. Explicit ``api_key`` kwarg (live test path, dev override).
2. ``secret_ref`` (a ``SecretRecord`` per ``grace2_contracts.secrets``)
   → ``Persistence.get_secret_value()`` (the production per-Case path
   landed by Wave 2 sibling job-0124).
3. ``GRACE2_COPERNICUS_CDS_API_KEY`` env var (local dev convenience).
4. ``~/.cdsapirc`` if present — the cdsapi library's own default lookup;
   this is what the live test on a developer machine uses.

If none of the four resolve a key, the tool raises ``GTSMMissingKeyError``
(retryable=False) and the agent surface routes a "needs a key" message
to the user via the secrets panel (sprint-12 Case-UX).

FR-TA-2 atomic tool. FR-CE-8 / FR-DC-3/4: routed through ``read_through``
with ``ttl_class="static-30d"`` — GTSM reanalysis time series are
historical and stable. A 30-day cache class is conservative; in practice
once a (bbox, date-range, output) request lands it is byte-stable for
years.

Cache key composition (per audit.md): the cache shim hashes
``(bbox-6dp, start_date, end_date, output)``. The cache key intentionally
does NOT include the api_key — the underlying GTSM grid is the same for
every caller (FR-DC-4 dedup).

Output format (FlatGeobuf — vector with embedded time series):

    Geometry: Point (one feature per GTSM gauge inside the bbox)
    Properties:
        gauge_id                (str)   — GTSM station identifier
        lon                     (float) — gauge longitude (EPSG:4326)
        lat                     (float) — gauge latitude (EPSG:4326)
        time_start              (str)   — ISO-8601 first timestep
        time_end                (str)   — ISO-8601 last timestep
        n_timesteps             (int)   — number of hourly samples
        wl_min_m                (float) — minimum water level (m)
        wl_max_m                (float) — maximum water level (m)
        wl_mean_m               (float) — mean water level (m)
        output                  (str)   — "water_level" or "surge_only"
        time_series_csv         (str)   — comma-separated "iso,value" pairs

We use FlatGeobuf carrying the time series inline so a single LayerURI
can serve both the point geometry (for map display of stations in bbox)
AND the per-station hydrograph (for SFINCS boundary forcing). Downstream
composers parse ``time_series_csv`` to populate ``bnd.bzs`` SFINCS boundary
inputs.

CRS: EPSG:4326 (GTSM coordinates are WGS84 decimal degrees).

Geographic-correctness gate (job-0086 codified lesson): we hard-filter
emitted features so every gauge lies within the requested bbox. GTSM
ships ~43k stations globally; an unfiltered NetCDF carries the global
network even when the user asks for a small coastal window.

``supports_global_query=False`` — GTSM is global, but the full-globe
time series for a year is ~GB-scale; bbox is required to keep the
pipeline tractable. A global call here is a misuse pattern (the kickoff
explicitly calls this out).

Payload estimation (per audit.md):
    ~0.1 MB per day per coastal bbox (gauges × hourly samples × ~12 B/row).
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import math
import os
import tempfile
import zipfile
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["fetch_gtsm_tide_surge"]

logger = logging.getLogger("grace2_agent.tools.fetch_gtsm_tide_surge")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class GTSMError(RuntimeError):
    """Base class for fetch_gtsm_tide_surge failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "GTSM_ERROR"
    retryable: bool = True


class GTSMInputError(GTSMError):
    """Bad inputs (unknown output, malformed bbox / dates)."""

    error_code = "GTSM_INPUT_ERROR"
    retryable = False


class GTSMUpstreamError(GTSMError):
    """CDS API returned an error or the retrieve timed out / network failed."""

    error_code = "GTSM_UPSTREAM_ERROR"
    retryable = True


class GTSMMissingKeyError(GTSMError):
    """No API key resolved via any of the four lookup paths.

    Raised BEFORE any network call. The agent surface uses this to prompt
    the user to add a Copernicus CDS key via the secrets panel.
    """

    error_code = "GTSM_MISSING_KEY"
    retryable = False


class GTSMAuthError(GTSMError):
    """CDS API rejected the key (invalid / revoked / not licensed)."""

    error_code = "GTSM_AUTH_ERROR"
    retryable = False


class GTSMEmptyError(GTSMError):
    """The retrieved NetCDF contained no gauges in the requested bbox."""

    error_code = "GTSM_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# CDS dataset id for the GTSM v3.0 water-level reanalysis time series.
_CDS_DATASET = "sis-water-level-change-timeseries-cmip6"

# Default CDS API endpoint URL. cdsapi >= 0.7 routes to the new CDS-Beta
# (cds-beta.climate.copernicus.eu) but the legacy URL still works.
_DEFAULT_CDS_URL = "https://cds.climate.copernicus.eu/api"

# Wall-clock budget for the CDS retrieve call (queue + run + stream).
# Per audit.md: poll up to 5 min for completion.
_RETRIEVE_TIMEOUT_S = 300

# Allowed output values exposed to callers.
_ALLOWED_OUTPUTS: frozenset[str] = frozenset({"water_level", "surge_only"})

# Map caller-facing output → CDS variable name.
_OUTPUT_TO_CDS_VARIABLE: dict[str, str] = {
    "water_level": "total_water_level",
    "surge_only": "storm_surge_residual",
}

# Sanity cap on date range. GTSM hourly time series at every station are
# big — a 1-year window already crosses the gauge × hours × bytes back-
# of-the-envelope into the hundreds of MB for a regional bbox.
_MAX_DATE_RANGE_DAYS = 366


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_gtsm_tide_surge",
    ttl_class="static-30d",
    source_class="gtsm",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    output: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB for a given call.

    Per audit.md: ~0.1 MB per day per coastal bbox. We treat ``bbox=None``
    as a (mis-)global call (360° × 180°) — the tool declares
    ``supports_global_query=False`` so this should not happen in practice,
    but the estimator stays defensive.

    Used by the tool-payload-warning envelope. Wrong answers are cheap
    (a chat warning instead of a hard block); we err on the high side so
    the user sees the warning rather than a surprise download.
    """
    if bbox is None:
        # Misuse case (supports_global_query=False); still emit a high
        # number so the chat warning fires.
        coastal_factor = 1.0
    else:
        try:
            west, south, east, north = bbox
            # Roughly scale by area, capped at the coastal-fraction of the
            # planet (~10%) since GTSM only resolves coastline gauges.
            sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
            coastal_factor = max(0.1, min(1.0, sq_deg / 90.0))
        except (TypeError, ValueError):
            coastal_factor = 1.0

    if not start_date or not end_date:
        n_days = 1
    else:
        try:
            d0 = _dt.date.fromisoformat(start_date)
            d1 = _dt.date.fromisoformat(end_date)
            n_days = max(1, (d1 - d0).days + 1)
        except ValueError:
            n_days = 1

    # 0.1 MB / day / coastal bbox per audit.md.
    return 0.1 * float(n_days) * coastal_factor


# ---------------------------------------------------------------------------
# bbox / output / date helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``GTSMInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise GTSMInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise GTSMInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise GTSMInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise GTSMInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if west >= east or south >= north:
        raise GTSMInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _validate_output(output: str) -> None:
    """Raise ``GTSMInputError`` for unsupported output values."""
    if not isinstance(output, str):
        raise GTSMInputError(
            f"output must be a str; got {type(output).__name__}"
        )
    if output not in _ALLOWED_OUTPUTS:
        raise GTSMInputError(
            f"unsupported GTSM output {output!r}; allowed: {sorted(_ALLOWED_OUTPUTS)}"
        )


def _parse_iso_date(s: str, *, field: str) -> _dt.date:
    if not isinstance(s, str):
        raise GTSMInputError(f"{field} must be ISO-8601 YYYY-MM-DD; got {s!r}")
    try:
        return _dt.date.fromisoformat(s)
    except ValueError as exc:
        raise GTSMInputError(
            f"{field}={s!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc


def _validate_date_range(start_date: str, end_date: str) -> tuple[_dt.date, _dt.date]:
    """Validate ISO dates + ordering + reasonable window.

    GTSM v3.0 reanalysis covers 1950 → ~2024 in the CDS catalogue at the
    time of writing; the SRS-side validation here pins the window to a
    sane span and re-uses the CDS server-side error for out-of-coverage
    edge cases (which surface as ``GTSMUpstreamError``).
    """
    d0 = _parse_iso_date(start_date, field="start_date")
    d1 = _parse_iso_date(end_date, field="end_date")
    if d0 > d1:
        raise GTSMInputError(
            f"start_date must be <= end_date; got start={d0}, end={d1}"
        )
    if d0.year < 1950 or d1.year > _dt.date.today().year + 1:
        raise GTSMInputError(
            f"date range [{d0}, {d1}] outside GTSM coverage (1950 → present)"
        )
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise GTSMInputError(
            f"date range {n_days} days exceeds hard cap {_MAX_DATE_RANGE_DAYS}; "
            f"call in chunks and aggregate"
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

    Priority (per audit.md, mirrors the sibling fetch_era5_reanalysis pattern):

    1. Explicit ``api_key`` kwarg.
    2. ``secret_ref`` (a ``SecretRecord``) → ``Persistence.get_secret_value``
       (the per-Case path landed by Wave 2 sibling job-0124).
    3. ``GRACE2_COPERNICUS_CDS_API_KEY`` env var.
    4. ``None`` — cdsapi falls back to ``~/.cdsapirc`` on instantiation.

    A return value of ``None`` means "let cdsapi find its own key via the
    library's default discovery path (``~/.cdsapirc``)". We do NOT raise
    ``GTSMMissingKeyError`` for the None case because the live developer
    workflow stores credentials in ``~/.cdsapirc``; the cdsapi Client
    constructor will raise its own diagnostic if neither the explicit key
    nor the rc file is present. We catch that and re-raise as
    ``GTSMMissingKeyError`` from the call site.
    """
    # 1. Explicit kwarg.
    if api_key:
        return api_key

    # 2. secret_ref via Persistence (lazy import to avoid MCP startup cost).
    if secret_ref is not None:
        try:
            return _materialize_secret(secret_ref)
        except Exception as exc:  # noqa: BLE001
            raise GTSMMissingKeyError(
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

    Mirrors the fetch_era5_reanalysis / fetch_ebird_observations pattern:
    lazy import of Persistence, sync-bridge for async coroutine, and a
    test-mock shortcut for plain strings.
    """
    if isinstance(secret_ref, str):
        return secret_ref

    persistence = _get_persistence_for_secrets()
    if persistence is None:
        raise GTSMMissingKeyError(
            "Persistence not bound; cannot resolve secret_ref. "
            "Pass api_key=... explicitly in this context."
        )

    coro = persistence.get_secret_value(secret_ref)
    return _run_coro_sync(coro)


_PERSISTENCE_FOR_SECRETS: Any | None = None


def set_persistence_for_secrets(persistence: Any | None) -> None:
    """Bind the agent-service ``Persistence`` for secret materialization.

    Mirrors the sibling Tier-2 binding (``fetch_era5_reanalysis`` job-0131,
    ``fetch_ebird_observations`` job-0128). Called once at startup by the
    agent service; tests inject a mock.
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
# CDS retrieve → ZIP/NetCDF (with timeout watchdog).
# ---------------------------------------------------------------------------


def _build_cds_request(
    output: str,
    d0: _dt.date,
    d1: _dt.date,
) -> dict[str, Any]:
    """Construct the CDS retrieve-request dict for a (output, date-range).

    The GTSM CDS dataset (``sis-water-level-change-timeseries-cmip6``)
    accepts:

    - ``experiment``: "reanalysis" for the historical GTSM v3.0 series.
    - ``variable``:   "total_water_level" or "storm_surge_residual".
    - ``year``/``month``: lists of zero-padded strings.
    - ``temporal_aggregation``: "hourly" (we pin hourly for SFINCS boundary
      forcing; "10_min" is finer than the SFINCS step typically needs).
    - ``format``: "zip" — the dataset packages monthly NetCDF files into a
      ZIP archive.

    GTSM is global per file (no bbox in the request); we filter to the
    requested bbox locally after download. This is the documented usage
    pattern in the GTSM CDS docs (the gauge network is irregular and the
    server-side spatial subsetter is not exposed).
    """
    # Days spanned, expressed as (year, month) explicit lists.
    years: set[str] = set()
    months: set[str] = set()
    cur = d0
    one = _dt.timedelta(days=1)
    while cur <= d1:
        years.add(f"{cur.year:04d}")
        months.add(f"{cur.month:02d}")
        cur += one

    return {
        "experiment": ["reanalysis"],
        "variable": [_OUTPUT_TO_CDS_VARIABLE[output]],
        "year": sorted(years),
        "month": sorted(months),
        "temporal_aggregation": ["hourly"],
        "format": "zip",
    }


def _cds_retrieve_with_timeout(
    api_url: str,
    api_key: str | None,
    request: dict[str, Any],
    out_path: str,
    timeout_s: int = _RETRIEVE_TIMEOUT_S,
) -> None:
    """Call cdsapi.Client.retrieve under a wall-clock timeout watchdog.

    Mirrors the sibling fetch_era5_reanalysis implementation: cdsapi has
    no native ``timeout`` parameter, so we spawn the retrieve in a worker
    thread and join with a deadline; on timeout we raise
    ``GTSMUpstreamError`` (retryable).

    Note: a timed-out request leaves an orphan CDS job server-side; the
    client cannot cancel it. The user will see it in their CDS dashboard
    queue history. Documented in the docstring; surfaced as
    ``OQ-0132-CDS-ORPHAN-JOB``.
    """
    import threading

    try:
        import cdsapi  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GTSMUpstreamError(
            f"cdsapi package not available: {exc}"
        ) from exc

    err_box: dict[str, BaseException] = {}

    def _do_retrieve() -> None:
        try:
            client_kwargs: dict[str, Any] = {"quiet": True}
            if api_url:
                client_kwargs["url"] = api_url
            if api_key:
                client_kwargs["key"] = api_key
            # If api_key is None, cdsapi falls back to ~/.cdsapirc.
            client = cdsapi.Client(**client_kwargs)
            client.retrieve(_CDS_DATASET, request, out_path)
        except BaseException as exc:  # noqa: BLE001
            err_box["err"] = exc

    t = threading.Thread(target=_do_retrieve, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise GTSMUpstreamError(
            f"CDS retrieve exceeded {timeout_s}s wall-clock budget; "
            f"the CDS job may still be queued server-side."
        )
    if "err" in err_box:
        exc = err_box["err"]
        msg = str(exc)
        low = msg.lower()
        if any(tok in low for tok in ("401", "403", "authentication", "unauthorized")):
            raise GTSMAuthError(
                f"CDS API rejected the key: {msg[:200]}"
            ) from exc
        if "no api key" in low or ("missing" in low and "key" in low):
            raise GTSMMissingKeyError(
                f"CDS API key not available: {msg[:200]}"
            ) from exc
        raise GTSMUpstreamError(
            f"CDS retrieve failed: {msg[:200]}"
        ) from exc


# ---------------------------------------------------------------------------
# ZIP/NetCDF → FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _extract_netcdfs_from_zip(zip_path: str) -> list[str]:
    """Extract every ``.nc`` file from the CDS-returned ZIP to a temp dir.

    Returns absolute paths to the extracted NetCDF files. Caller is
    responsible for cleanup (we keep them in a temp dir which OS-level
    rotation eventually clears, but the cache layer also unlinks).
    """
    tmpdir = tempfile.mkdtemp(prefix="grace2_gtsm_zip_")
    extracted: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if not member.lower().endswith(".nc"):
                    continue
                # Defensive: collapse any directory traversal.
                safe_name = os.path.basename(member)
                if not safe_name:
                    continue
                target = os.path.join(tmpdir, safe_name)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                extracted.append(target)
    except zipfile.BadZipFile as exc:
        raise GTSMUpstreamError(
            f"CDS returned a malformed ZIP archive: {exc}"
        ) from exc

    if not extracted:
        raise GTSMUpstreamError(
            f"CDS ZIP archive carried no .nc files (members={zf.namelist()})"
        )
    return extracted


def _netcdf_to_gauge_records(
    nc_paths: list[str],
    bbox: tuple[float, float, float, float],
    output: str,
) -> list[dict[str, Any]]:
    """Open the GTSM NetCDF(s), subset to bbox, return one record per gauge.

    The GTSM NetCDF schema has stations along one dimension and time along
    another, with separate coordinate arrays for ``station_x_coordinate``
    (longitude) and ``station_y_coordinate`` (latitude) plus a data
    variable carrying the water-level time series. Variable naming has
    drifted across versions (``waterlevel``, ``total_water_level``,
    ``surge``, ``storm_surge_residual``); we discover the data variable
    by exclusion.

    Returns a list of dicts (one per in-bbox gauge) carrying:
        gauge_id, lon, lat, times (list[str]), values (list[float])

    Raises:
        ``GTSMUpstreamError``: NetCDF open / xarray read failure.
        ``GTSMEmptyError``: no gauges fall in the requested bbox.
    """
    try:
        import numpy as np
        import xarray as xr
    except ImportError as exc:
        raise GTSMUpstreamError(
            f"xarray / numpy not available: {exc}"
        ) from exc

    west, south, east, north = bbox

    # Merge multi-file NetCDFs by concatenating along the time dimension.
    # Each monthly file in the ZIP carries the same station network so we
    # can simply concat along time.
    try:
        if len(nc_paths) == 1:
            ds = xr.open_dataset(nc_paths[0], chunks=None)
        else:
            ds = xr.open_mfdataset(
                sorted(nc_paths),
                combine="by_coords",
                chunks=None,
                parallel=False,
            )
    except Exception as exc:  # noqa: BLE001
        raise GTSMUpstreamError(
            f"xarray could not open GTSM NetCDF(s) {nc_paths!r}: {exc}"
        ) from exc

    try:
        # Discover the station coords. GTSM uses ``station_x_coordinate`` /
        # ``station_y_coordinate`` in the canonical CDS schema; we also
        # tolerate ``lon`` / ``lat`` / ``longitude`` / ``latitude``.
        lon_name = _pick_coord(ds, ("station_x_coordinate", "lon", "longitude", "x"))
        lat_name = _pick_coord(ds, ("station_y_coordinate", "lat", "latitude", "y"))
        if lon_name is None or lat_name is None:
            raise GTSMUpstreamError(
                f"GTSM NetCDF lacks station lon/lat coordinates; "
                f"variables={list(ds.variables)}"
            )

        lons = np.asarray(ds[lon_name].values, dtype=np.float64)
        lats = np.asarray(ds[lat_name].values, dtype=np.float64)

        # Normalize longitudes to -180..180 (some GTSM files use 0..360).
        lons_norm = np.where(lons > 180.0, lons - 360.0, lons)

        # bbox mask (geographic-correctness gate).
        mask = (
            (lons_norm >= west)
            & (lons_norm <= east)
            & (lats >= south)
            & (lats <= north)
        )
        in_bbox_idx = np.flatnonzero(mask)
        if in_bbox_idx.size == 0:
            raise GTSMEmptyError(
                f"bbox={bbox} contains no GTSM gauges "
                f"(network has {lons.size} stations globally)"
            )

        # Discover the data variable. CDS variable name in the request
        # maps to the in-NetCDF name; fall back to discovery by exclusion.
        target_cds_var = _OUTPUT_TO_CDS_VARIABLE[output]
        coord_names = set(ds.coords)
        candidate_names = [target_cds_var, "waterlevel", "water_level", "surge"]
        data_var = None
        for name in candidate_names:
            if name in ds.data_vars:
                data_var = name
                break
        if data_var is None:
            data_vars = [
                v for v in ds.data_vars
                if v not in coord_names and v not in {lon_name, lat_name}
            ]
            if not data_vars:
                raise GTSMUpstreamError(
                    f"GTSM NetCDF lacks a recognizable water-level data variable; "
                    f"variables={list(ds.variables)}"
                )
            data_var = data_vars[0]

        da = ds[data_var]

        # Time axis discovery.
        time_name = _pick_coord(ds, ("time", "datetime"))
        if time_name is None or time_name not in da.dims:
            raise GTSMUpstreamError(
                f"GTSM NetCDF data var {data_var!r} lacks a time dim; "
                f"dims={list(da.dims)}"
            )

        # Station dim is whatever's left after time.
        station_dim_candidates = [d for d in da.dims if d != time_name]
        if len(station_dim_candidates) != 1:
            raise GTSMUpstreamError(
                f"could not identify GTSM station dim for var {data_var!r}; "
                f"dims={list(da.dims)}"
            )
        station_dim = station_dim_candidates[0]

        # Build per-gauge records.
        time_values = ds[time_name].values
        # Cast times to numpy datetime64 → ISO strings.
        time_strs = [_np_datetime_to_iso(t) for t in time_values]

        # Pull each gauge's time series.
        records: list[dict[str, Any]] = []
        for raw_idx in in_bbox_idx:
            i = int(raw_idx)
            try:
                series = np.asarray(
                    da.isel({station_dim: i}).values, dtype=np.float64
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fetch_gtsm_tide_surge: failed to isel station %d: %s", i, exc
                )
                continue

            # Skip all-NaN stations (offline / out-of-coverage).
            if not np.isfinite(series).any():
                continue

            # Gauge identifier: try a station-id variable; else fall back
            # to the integer index, which GTSM uses as the implicit id.
            gauge_id = _pick_gauge_id(ds, station_dim, i)

            records.append({
                "gauge_id": gauge_id,
                "lon": float(lons_norm[i]),
                "lat": float(lats[i]),
                "times": time_strs,
                "values": [float(v) for v in series],
            })

        if not records:
            raise GTSMEmptyError(
                f"bbox={bbox} matched {in_bbox_idx.size} gauge(s) but all "
                f"carried all-NaN time series"
            )

        logger.info(
            "fetch_gtsm_tide_surge: extracted %d gauge(s) inside bbox=%s "
            "(global network=%d stations); %d timesteps each",
            len(records),
            bbox,
            int(lons.size),
            len(time_strs),
        )
        return records
    finally:
        try:
            ds.close()
        except Exception:  # noqa: BLE001
            pass


def _pick_coord(ds: Any, candidates: tuple[str, ...]) -> str | None:
    """Return the first coord/variable name in ``ds`` matching ``candidates``."""
    for name in candidates:
        if name in ds.variables or name in ds.coords:
            return name
    return None


def _pick_gauge_id(ds: Any, station_dim: str, index: int) -> str:
    """Pick a gauge identifier from the dataset for station-index ``index``.

    GTSM ships a ``station_id`` or ``stations`` variable in newer releases;
    older ones rely on the integer station index. We probe a small set of
    candidate variable names and fall back to ``"GTSM-{index:06d}"``.
    """
    candidates = ("station_id", "stations", "id", "station_name", "name")
    for name in candidates:
        if name in ds.variables:
            try:
                raw = ds[name].isel({station_dim: index}).values
                if hasattr(raw, "item"):
                    raw = raw.item()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                s = str(raw).strip()
                if s:
                    return s
            except Exception:  # noqa: BLE001
                continue
    return f"GTSM-{index:06d}"


def _np_datetime_to_iso(t: Any) -> str:
    """Convert a numpy datetime64 / Python datetime / pandas Timestamp to ISO."""
    try:
        import numpy as np
        import pandas as pd  # type: ignore[import-not-found]
    except ImportError:
        # Fallback: best-effort str cast.
        return str(t)

    try:
        if isinstance(t, np.datetime64):
            ts = pd.Timestamp(t)
        else:
            ts = pd.Timestamp(t)
        # Force UTC tz, then ISO-8601 with Z.
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return str(t)


def _records_to_flatgeobuf_bytes(
    records: list[dict[str, Any]],
    output: str,
) -> bytes:
    """Convert per-gauge records to a FlatGeobuf carrying inline time series.

    Each feature is a Point geometry (the gauge location, EPSG:4326) with
    attributes carrying the gauge metadata + a CSV-string of the time
    series so a downstream composer can parse it into a SFINCS boundary.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import numpy as np
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GTSMUpstreamError(
            f"geopandas / shapely / pandas not available: {exc}"
        ) from exc

    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    for rec in records:
        times = rec["times"]
        values = rec["values"]
        # Build CSV "iso,value" lines; skip non-finite entries so the
        # boundary parser doesn't choke.
        buf = io.StringIO()
        writer = csv.writer(buf)
        finite_values: list[float] = []
        for ts, val in zip(times, values, strict=False):
            if val is None:
                continue
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(v):
                continue
            writer.writerow([ts, f"{v:.6f}"])
            finite_values.append(v)
        ts_csv = buf.getvalue()

        if not finite_values:
            # All-NaN gauge; skip.
            continue

        rows.append({
            "gauge_id": rec["gauge_id"],
            "lon": rec["lon"],
            "lat": rec["lat"],
            "time_start": times[0] if times else "",
            "time_end": times[-1] if times else "",
            "n_timesteps": len(finite_values),
            "wl_min_m": float(np.nanmin(finite_values)),
            "wl_max_m": float(np.nanmax(finite_values)),
            "wl_mean_m": float(np.nanmean(finite_values)),
            "output": output,
            "time_series_csv": ts_csv,
        })
        geoms.append(Point(rec["lon"], rec["lat"]))

    if not rows:
        # Schema-only empty FGB so downstream readers still parse.
        empty_df = pd.DataFrame(
            columns=[
                "gauge_id",
                "lon",
                "lat",
                "time_start",
                "time_end",
                "n_timesteps",
                "wl_min_m",
                "wl_max_m",
                "wl_mean_m",
                "output",
                "time_series_csv",
            ]
        )
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        df = pd.DataFrame(rows)
        gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_gtsm_"
        ) as fgb_f:
            tmp_fgb = fgb_f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise GTSMUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_gtsm_tide_surge: FlatGeobuf serialized %d gauge(s) = %d bytes",
            len(rows),
            len(fgb_bytes),
        )
        return fgb_bytes
    finally:
        if tmp_fgb:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_gtsm_bytes(
    bbox: tuple[float, float, float, float],
    d0: _dt.date,
    d1: _dt.date,
    output: str,
    api_key: str | None,
) -> bytes:
    """End-to-end: CDS retrieve → ZIP → NetCDF(s) → bbox-subset → FlatGeobuf."""
    request = _build_cds_request(output, d0, d1)

    zip_fd, zip_path = tempfile.mkstemp(
        suffix=".zip", prefix="grace2_gtsm_cds_"
    )
    os.close(zip_fd)
    nc_paths: list[str] = []
    try:
        _cds_retrieve_with_timeout(
            api_url=_DEFAULT_CDS_URL,
            api_key=api_key,
            request=request,
            out_path=zip_path,
        )
        nc_paths = _extract_netcdfs_from_zip(zip_path)
        records = _netcdf_to_gauge_records(nc_paths, bbox, output)
        return _records_to_flatgeobuf_bytes(records, output)
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
        for p in nc_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        # Best-effort: remove the temp dir parent. The OS cleans it
        # eventually if rmdir fails because of stray files.
        for p in nc_paths:
            d = os.path.dirname(p)
            try:
                os.rmdir(d)
            except OSError:
                pass
            break


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_gtsm_tide_surge(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    output: str = "water_level",
    api_key: str | None = None,
    secret_ref: Any | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Global Tide and Surge Model v3.0 Tier-2 coastal water-level fetcher.

    **What it does:** Retrieves hourly tide + storm-surge time series from
    the Deltares GTSM v3.0 reanalysis via the Copernicus Climate Data Store
    (CDS) ``sis-water-level-change-timeseries-cmip6`` dataset. Downloads the
    CDS-returned ZIP archive containing monthly NetCDF files, subsets to the
    requested bbox and date range, and serialises one Point feature per gauge
    to a FlatGeobuf with the per-station time series embedded inline as a
    ``time_series_csv`` attribute. Requires a free Copernicus CDS API key.

    **When to use:**

    - SFINCS compound-flood coastal boundary forcing for a non-CONUS basin
      where NOAA CO-OPS has no tide gauge (e.g. Bay of Bengal, West Africa,
      Caribbean island arcs). Example: bbox ``(-70.0, 10.0, -60.0, 20.0)``
      for a Lesser Antilles hurricane scenario, ``start_date="2017-09-05"``,
      ``end_date="2017-09-11"``.
    - Post-event surge attribution along arbitrary coastlines to separate
      tide and meteorological surge components (use ``output="surge_only"``).
    - Globally-consistent storm-surge climatology for multi-hazard compound
      flood studies (Eilander et al. 2023; Muis et al. 2020/2023).

    **When NOT to use:**

    - CONUS with operating CO-OPS tide gauges — use ``fetch_noaa_coops_tides``
      for real observational records; GTSM is a model, not a gauge measurement.
    - Forecasted tide/surge — GTSM v3.0 reanalysis is historical only (1950 to
      ~2024); use ECMWF/NHC tools for future surge forecasts.
    - Sub-hourly boundary timesteps — output is pinned to hourly; the GTSM CDS
      dataset supports 10-min aggregation but SFINCS rarely needs finer steps.
    - Gridded water-level fields — output is a sparse gauge network; composers
      interpolate to the SFINCS grid via ``bnd.bzs`` boundary handler.

    **Parameters:**

    - ``bbox``: ``(west, south, east, north)`` in EPSG:4326. Required;
      ``supports_global_query=False`` — global time-series is GB-scale.
    - ``start_date``: ISO YYYY-MM-DD inclusive. GTSM coverage: 1950 → ~2024.
    - ``end_date``: ISO YYYY-MM-DD inclusive. Hard cap 366 days from start.
    - ``output``: ``"water_level"`` (default, tide + surge; canonical SFINCS
      input) or ``"surge_only"`` (meteorological residual only).
    - ``api_key``: optional explicit CDS API key (overrides all other paths).
    - ``secret_ref``: optional ``SecretRecord`` resolved via
      ``Persistence.get_secret_value`` (per-Case production path).

    **Returns:**

    ``LayerURI`` pointing at ``s3://trid3nt-cache/cache/static-30d/gtsm/<key>.fgb``.
    FlatGeobuf, Point geometry (EPSG:4326), one feature per GTSM gauge.
    Feature attributes: ``gauge_id``, ``lon``, ``lat``, ``time_start``,
    ``time_end``, ``n_timesteps``, ``wl_min_m`` / ``wl_max_m`` / ``wl_mean_m``
    (m), ``output``, ``time_series_csv`` (``"iso,value"`` CSV lines).
    ``layer_type="vector"``, ``role="primary"``, ``units="m"``.

    Raises: ``GTSMMissingKeyError`` (no key), ``GTSMAuthError`` (bad key),
    ``GTSMInputError`` (bad params), ``GTSMEmptyError`` (no gauges in bbox),
    ``GTSMUpstreamError`` (CDS queue timeout / network failure, retryable).

    **Cross-tool dependencies:**

    - Consumed by: ``build_sfincs_model`` (parses ``time_series_csv`` to
      populate ``bnd.bzs`` coastal boundary); ``model_compound_flood_global``
      (compound-flood composer, Wave 2+).
    - Pair with: ``fetch_era5_reanalysis`` (atmospheric + wave forcing for
      the same event window); ``fetch_cama_flood_discharge`` (fluvial inflow
      boundary); ``fetch_mrms_qpe`` / ERA5 precip (rainfall forcing).
    - Auth path shares ``Persistence.get_secret_value`` with
      ``fetch_era5_reanalysis`` (both use the Copernicus CDS key).

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key =
    SHA-256 of ``(bbox-6dp, start_date, end_date, output)`` — api_key
    excluded (FR-DC-4 dedup).
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    _validate_output(output)
    d0, d1 = _validate_date_range(start_date, end_date)

    # ---- API-key resolution (pre-network; cheap fail) ----
    resolved_key = _resolve_api_key(api_key=api_key, secret_ref=secret_ref)
    # NOTE: resolved_key may be None — that is intentional. cdsapi falls
    # back to ~/.cdsapirc; the auth error surfaces from the call site.

    # ---- Cache-key params (key omits api_key by design) ----
    q_bbox = _round_bbox_to_6dp(bbox)
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "start_date": d0.isoformat(),
        "end_date": d1.isoformat(),
        "output": output,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_gtsm_bytes(
            bbox=q_bbox,
            d0=d0,
            d1=d1,
            output=output,
            api_key=resolved_key,
        ),
    )
    assert result.uri is not None, (
        "fetch_gtsm_tide_surge is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"gtsm-{output}-"
            f"{d0.isoformat()}-{d1.isoformat()}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"GTSM Tide + Surge — {output.replace('_', ' ').title()} "
            f"({d0.isoformat()} → {d1.isoformat()})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset=f"gtsm_{output}",
        role="primary",
        units="m",
    )
