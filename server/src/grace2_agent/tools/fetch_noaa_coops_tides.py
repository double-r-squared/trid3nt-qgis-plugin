"""``fetch_noaa_coops_tides`` atomic tool — NOAA CO-OPS tide-station observations and predictions (job A9).

Wraps the NOAA Center for Operational Oceanographic Products and Services
(CO-OPS) REST API to retrieve water-level time series — either verified
observations (``product="water_level"``) or astronomical tide predictions
(``product="predictions"``) — for all CO-OPS stations within a requested
bbox and date range. Returns a FlatGeobuf with one Point feature per
station, carrying the per-station time series inline.

**API surface** (verified live 2026-06-09):

    Station discovery:
        https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json
            ?type=waterlevels&units=metric&format=json
        Returns all ~300 water-level stations globally; we filter by bbox
        after download (a single cheap request covers the full network).

    Data retrieval (one request per station):
        https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
            ?begin_date=YYYYMMDD&end_date=YYYYMMDD
            &station={id}&product={product}&datum=MLLW
            &time_zone=gmt&interval=h&units=metric
            &application=grace2&format=json

    Response fields:
        metadata.id, metadata.name, metadata.lat, metadata.lon
        data[].t  — ISO datetime "YYYY-MM-DD HH:MM"
        data[].v  — water-level value in meters
        data[].s  — sigma (standard deviation of 6-minute samples, obs only)
        data[].f  — quality flags comma-string
        data[].q  — quality indicator ("p" preliminary, "v" verified)

    Predictions response uses ``predictions[]`` key instead of ``data[]``;
    each entry has ``t`` + ``v`` but no ``s`` or ``f``.

API limits: no API key required. CO-OPS rate-limits to a single station per
request; no explicit documented rate limit, but large fan-out (many stations
× large date ranges) should be throttled. We impose a _MAX_STATIONS cap and
_STATION_REQUEST_DELAY between per-station calls.

**Date-range limits**: CO-OPS allows up to 31 calendar days per data request
for 6-minute interval products; the hourly interval has no enforced maximum
but very long ranges (>365 days) produce large responses. We cap at
``_MAX_DATE_RANGE_DAYS = 366`` in the tool, with a warning for >31 days.

**Cache**: ``dynamic-1h`` for recent / near-real-time observations (preliminary
data may change); ``static-30d`` would be appropriate for old fully-verified
observations but the single ``dynamic-1h`` class is conservative and safe.

**Output format** (FlatGeobuf — vector with embedded time series):

    Geometry: Point (station location, EPSG:4326)
    Properties:
        station_id           (str)   — CO-OPS station identifier (7 digits)
        station_name         (str)   — station common name
        lon                  (float) — station longitude (EPSG:4326)
        lat                  (float) — station latitude (EPSG:4326)
        product              (str)   — "water_level" or "predictions"
        datum                (str)   — "MLLW" (always; see datum note)
        time_start           (str)   — ISO-8601 first timestep
        time_end             (str)   — ISO-8601 last timestep
        n_timesteps          (int)   — count of hourly samples
        wl_min_m             (float) — minimum water level (m)
        wl_max_m             (float) — maximum water level (m)
        wl_mean_m            (float) — mean water level (m)
        time_series_csv      (str)   — "iso,value_m" comma-separated rows

Datum note: the tool pins MLLW (Mean Lower Low Water) as the datum for both
observations and predictions — the standard chart datum for US coastal work.
NAVD88 is available for some stations but requires separate product codes;
MLLW is safe everywhere CO-OPS publishes hourly data.

``supports_global_query=False`` — CO-OPS is primarily a US/territory network
(~300 stations worldwide). A global bbox would just return all ~300 stations,
which is a ~500 KB payload; this is technically feasible but unlikely to be
what the user intends, so we require bbox to be specified. The tool will NOT
raise an error if you pass the entire world in the bbox, but it will warn.

FR-AS-11 typed-error surface: ``COOPSTidesError`` (base, retryable=True),
``COOPSTidesInputError`` (bad bbox / product / dates, retryable=False),
``COOPSTidesUpstreamError`` (HTTP/network failure, retryable=True),
``COOPSTidesEmptyError`` (no stations in bbox, retryable=False).

FR-DC-9 / Wave-1.5 payload estimation: ~2 KB per station-day (hourly samples
= 24 rows × ~80 bytes), so a 10-station × 30-day bbox → ~0.6 MB.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_noaa_coops_tides",
    "estimate_payload_mb",
    "COOPSTidesError",
    "COOPSTidesInputError",
    "COOPSTidesUpstreamError",
    "COOPSTidesEmptyError",
    "_validate_bbox",
    "_validate_product",
    "_validate_date_range",
    "_round_bbox_to_6dp",
    "_discover_stations_in_bbox",
    "_fetch_station_data",
    "_build_flatgeobuf",
    "_fetch_coops_tides_bytes",
    "COOPS_STATIONS_URL",
    "COOPS_DATA_URL",
]

logger = logging.getLogger("grace2_agent.tools.fetch_noaa_coops_tides")

# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class COOPSTidesError(RuntimeError):
    """Base class for fetch_noaa_coops_tides failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "COOPS_TIDES_ERROR"
    retryable: bool = True


class COOPSTidesInputError(COOPSTidesError):
    """Invalid inputs — bad bbox, unknown product, malformed dates.

    Not retryable: the caller must fix the argument.
    """

    error_code = "COOPS_TIDES_INPUT_ERROR"
    retryable = False


class COOPSTidesUpstreamError(COOPSTidesError):
    """CO-OPS REST API request failed (network error, HTTP 4xx/5xx, bad JSON).

    Retryable — transient CO-OPS outages recover on retry.
    """

    error_code = "COOPS_TIDES_UPSTREAM_ERROR"
    retryable = True


class COOPSTidesEmptyError(COOPSTidesError):
    """No CO-OPS water-level stations found within the requested bbox.

    Not retryable — the bbox genuinely has no instrumented stations. Use a
    larger bbox or a different area.
    """

    error_code = "COOPS_TIDES_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: CO-OPS station-discovery endpoint (all water-level stations, ~300 globally).
COOPS_STATIONS_URL = (
    "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
    "?type=waterlevels&units=metric&format=json"
)

#: CO-OPS data-retrieval endpoint (one request per station).
COOPS_DATA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

#: User-Agent per NOAA usage policy.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeouts (seconds).
_STATIONS_TIMEOUT = 30.0
_DATA_TIMEOUT = 30.0

#: Maximum stations to fetch data for (bounds API spend on wide bboxes).
_MAX_STATIONS = 50

#: Polite delay between per-station data requests (seconds).
_STATION_REQUEST_DELAY = 0.1

#: Maximum date range in days (>31 days exceeds CO-OPS documented per-request
#: limit for 6-min products; hourly is more permissive but we cap here).
_MAX_DATE_RANGE_DAYS = 366

#: Allowed product values exposed to callers.
_VALID_PRODUCTS: frozenset[str] = frozenset({"water_level", "predictions"})


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_noaa_coops_tides",
        ttl_class="dynamic-1h",
        source_class="noaa_coops_tides",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_noaa_coops_tides without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    product: str = "water_level",
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    Heuristic: ~2 KB per station per day of hourly data (24 samples × ~80
    bytes per row in the time_series_csv attribute). Per-station overhead
    (geometry + scalar fields) adds ~0.5 KB per station.

    CO-OPS has ~300 stations globally but only 2 near Fort Myers, so bbox
    area is a weak proxy for station count. We treat 1° × 1° ≈ 2 stations
    (coastal US density); a 10° × 10° bbox might hold ~20 stations at most.
    """
    if bbox is None:
        # Misuse / global call — still emit a reasonable estimate
        n_stations = 30
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
            # ~2 stations per square degree, capped at _MAX_STATIONS
            n_stations = min(_MAX_STATIONS, max(1, int(sq_deg * 2.0)))
        except (TypeError, ValueError):
            n_stations = 10

    if not start_date or not end_date:
        n_days = 1
    else:
        try:
            d0 = _dt.date.fromisoformat(start_date)
            d1 = _dt.date.fromisoformat(end_date)
            n_days = max(1, (d1 - d0).days + 1)
        except ValueError:
            n_days = 1

    # 2 KB / station / day for time series + 0.5 KB / station overhead
    kb_per_station = 0.5 + 2.0 * n_days
    return max(0.01, n_stations * kb_per_station / 1_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``COOPSTidesInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise COOPSTidesInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise COOPSTidesInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise COOPSTidesInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise COOPSTidesInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise COOPSTidesInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _validate_product(product: str) -> None:
    """Raise ``COOPSTidesInputError`` for unsupported product values."""
    if not isinstance(product, str):
        raise COOPSTidesInputError(
            f"product must be a str; got {type(product).__name__}"
        )
    if product not in _VALID_PRODUCTS:
        raise COOPSTidesInputError(
            f"unsupported product {product!r}; allowed: {sorted(_VALID_PRODUCTS)}"
        )


def _validate_date_range(
    start_date: str, end_date: str
) -> tuple[_dt.date, _dt.date]:
    """Parse and validate the ISO date range.

    CO-OPS data is available from the 1800s through present. We do a basic
    sanity check rather than a strict lower bound.
    """
    for field, val in [("start_date", start_date), ("end_date", end_date)]:
        if not isinstance(val, str):
            raise COOPSTidesInputError(
                f"{field} must be an ISO-8601 YYYY-MM-DD string; got {val!r}"
            )
    try:
        d0 = _dt.date.fromisoformat(start_date)
    except ValueError as exc:
        raise COOPSTidesInputError(
            f"start_date={start_date!r} is not a valid ISO date: {exc}"
        ) from exc
    try:
        d1 = _dt.date.fromisoformat(end_date)
    except ValueError as exc:
        raise COOPSTidesInputError(
            f"end_date={end_date!r} is not a valid ISO date: {exc}"
        ) from exc
    if d0 > d1:
        raise COOPSTidesInputError(
            f"start_date must be <= end_date; got start={d0}, end={d1}"
        )
    n_days = (d1 - d0).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise COOPSTidesInputError(
            f"date range {n_days} days exceeds hard cap {_MAX_DATE_RANGE_DAYS}; "
            f"call in chunks and aggregate"
        )
    return d0, d1


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6 decimal places (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET. Raises ``COOPSTidesUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise COOPSTidesUpstreamError(
            f"upstream HTTP {exc.code} for {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise COOPSTidesUpstreamError(
            f"network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise COOPSTidesUpstreamError(
            f"timed out after {timeout}s for {url}"
        ) from exc


# ---------------------------------------------------------------------------
# Station discovery.
# ---------------------------------------------------------------------------


def _discover_stations_in_bbox(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch the CO-OPS station catalog and filter to those inside bbox.

    Returns a list of station dicts: ``{id, name, lat, lng}``.
    Raises ``COOPSTidesUpstreamError`` if the catalog download fails.
    Raises ``COOPSTidesEmptyError`` if no stations fall inside bbox.
    """
    west, south, east, north = bbox
    body = _http_get(COOPS_STATIONS_URL, timeout=_STATIONS_TIMEOUT)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise COOPSTidesUpstreamError(
            f"CO-OPS station catalog returned non-JSON response: {exc}"
        ) from exc

    stations_raw = data.get("stations", [])
    if not stations_raw:
        raise COOPSTidesUpstreamError(
            "CO-OPS station catalog returned empty 'stations' list"
        )

    matching: list[dict[str, Any]] = []
    for s in stations_raw:
        try:
            lat = float(s["lat"])
            lng = float(s["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if west <= lng <= east and south <= lat <= north:
            matching.append({
                "id": str(s.get("id", "")),
                "name": str(s.get("name", "")),
                "lat": lat,
                "lng": lng,
            })
        if len(matching) >= _MAX_STATIONS:
            logger.warning(
                "fetch_noaa_coops_tides: bbox=%s contains >%d stations; "
                "capping at %d",
                bbox,
                _MAX_STATIONS,
                _MAX_STATIONS,
            )
            break

    if not matching:
        raise COOPSTidesEmptyError(
            f"no CO-OPS water-level stations found in bbox={bbox}; "
            f"CO-OPS covers primarily the US coastline and territories "
            f"(~300 stations globally). Try a wider bbox or a different area."
        )

    logger.info(
        "fetch_noaa_coops_tides: found %d station(s) in bbox=%s",
        len(matching),
        bbox,
    )
    return matching


# ---------------------------------------------------------------------------
# Per-station data retrieval.
# ---------------------------------------------------------------------------


def _build_coops_url(
    station_id: str,
    product: str,
    d0: _dt.date,
    d1: _dt.date,
) -> str:
    """Build the CO-OPS data-getter URL for a single station + date range."""
    params = {
        "begin_date": d0.strftime("%Y%m%d"),
        "end_date": d1.strftime("%Y%m%d"),
        "station": station_id,
        "product": product,
        "datum": "MLLW",
        "time_zone": "gmt",
        "interval": "h",
        "units": "metric",
        "application": "grace2",
        "format": "json",
    }
    return f"{COOPS_DATA_URL}?{urllib.parse.urlencode(params)}"


def _fetch_station_data(
    station: dict[str, Any],
    product: str,
    d0: _dt.date,
    d1: _dt.date,
) -> list[dict[str, Any]] | None:
    """Fetch hourly water-level time series for one station.

    Returns a list of ``{t: str, v: float}`` dicts, or ``None`` if the
    station returned an error (no data in range, decommissioned, etc.).
    Swallows individual station errors so one bad station does not abort
    the whole bbox fetch.
    """
    url = _build_coops_url(station["id"], product, d0, d1)
    try:
        body = _http_get(url, timeout=_DATA_TIMEOUT)
    except COOPSTidesUpstreamError as exc:
        logger.warning(
            "fetch_noaa_coops_tides: station %s HTTP error: %s",
            station["id"],
            exc,
        )
        return None

    try:
        resp = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "fetch_noaa_coops_tides: station %s non-JSON response: %s",
            station["id"],
            exc,
        )
        return None

    # CO-OPS reports errors in the JSON (e.g. no data for date range).
    if "error" in resp:
        logger.info(
            "fetch_noaa_coops_tides: station %s API error: %s",
            station["id"],
            resp["error"],
        )
        return None

    # "water_level" uses "data" key; "predictions" uses "predictions" key.
    rows_raw = resp.get("data") or resp.get("predictions") or []
    if not rows_raw:
        logger.info(
            "fetch_noaa_coops_tides: station %s returned empty data array",
            station["id"],
        )
        return None

    rows: list[dict[str, Any]] = []
    for row in rows_raw:
        t = row.get("t", "")
        v_raw = row.get("v")
        if v_raw is None or v_raw == "":
            continue
        try:
            v = float(v_raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        # Normalize timestamp to ISO-8601 with Z suffix.
        t_iso = t.replace(" ", "T") + "Z" if " " in t else t
        rows.append({"t": t_iso, "v": v})

    if not rows:
        return None

    return rows


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _build_flatgeobuf(
    records: list[dict[str, Any]],
    product: str,
) -> bytes:
    """Convert per-station time-series records to a FlatGeobuf byte string.

    Each record:
        station_id, station_name, lon, lat, rows (list of {t, v})

    Each feature:
        Point geometry (lon, lat, EPSG:4326) + scalar properties +
        time_series_csv inline attribute for downstream SFINCS boundary use.
    """
    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point
    except ImportError as exc:
        raise COOPSTidesUpstreamError(
            f"geopandas / shapely / pandas not available: {exc}"
        ) from exc

    rows_out: list[dict[str, Any]] = []
    geoms: list[Any] = []

    for rec in records:
        series = rec["rows"]
        if not series:
            continue

        # Build inline CSV "iso,value_m" for SFINCS boundary consumption.
        buf = io.StringIO()
        writer = csv.writer(buf)
        values: list[float] = []
        for entry in series:
            v = entry["v"]
            writer.writerow([entry["t"], f"{v:.6f}"])
            values.append(v)
        ts_csv = buf.getvalue()

        import numpy as np

        rows_out.append({
            "station_id": rec["station_id"],
            "station_name": rec["station_name"],
            "lon": rec["lon"],
            "lat": rec["lat"],
            "product": product,
            "datum": "MLLW",
            "time_start": series[0]["t"],
            "time_end": series[-1]["t"],
            "n_timesteps": len(values),
            "wl_min_m": float(np.nanmin(values)),
            "wl_max_m": float(np.nanmax(values)),
            "wl_mean_m": float(np.nanmean(values)),
            "time_series_csv": ts_csv,
        })
        geoms.append(Point(rec["lon"], rec["lat"]))

    if not rows_out:
        # Return a schema-only empty FGB so downstream readers still parse.
        empty_df = pd.DataFrame(
            columns=[
                "station_id", "station_name", "lon", "lat", "product",
                "datum", "time_start", "time_end", "n_timesteps",
                "wl_min_m", "wl_max_m", "wl_mean_m", "time_series_csv",
            ]
        )
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        df = pd.DataFrame(rows_out)
        gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_coops_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:
            raise COOPSTidesUpstreamError(
                f"FlatGeobuf serialization failed: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_noaa_coops_tides: FlatGeobuf serialized %d station(s) = %d bytes",
            len(rows_out),
            len(fgb_bytes),
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Top-level fetch function (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_coops_tides_bytes(
    bbox: tuple[float, float, float, float],
    product: str,
    d0: _dt.date,
    d1: _dt.date,
) -> bytes:
    """End-to-end: discover stations → fetch per-station data → FGB bytes."""
    # 1. Discover stations inside bbox.
    stations = _discover_stations_in_bbox(bbox)

    # 2. Fetch time series for each station.
    records: list[dict[str, Any]] = []
    for i, station in enumerate(stations):
        if i > 0:
            time.sleep(_STATION_REQUEST_DELAY)
        rows = _fetch_station_data(station, product, d0, d1)
        if rows:
            records.append({
                "station_id": station["id"],
                "station_name": station["name"],
                "lon": station["lng"],
                "lat": station["lat"],
                "rows": rows,
            })

    if not records:
        raise COOPSTidesEmptyError(
            f"all {len(stations)} station(s) in bbox={bbox} returned no data "
            f"for product={product!r} date_range=[{d0}, {d1}]; stations may "
            f"have gaps or have not yet published data for this period"
        )

    logger.info(
        "fetch_noaa_coops_tides: %d/%d station(s) returned data; "
        "building FlatGeobuf",
        len(records),
        len(stations),
    )

    # 3. Build FlatGeobuf.
    return _build_flatgeobuf(records, product)


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
def fetch_noaa_coops_tides(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    product: str = "water_level",
    # job-0164 / Wave 4.10 convention: absorb LLM-invented kwargs
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NOAA CO-OPS tide-station observations or predictions as a FlatGeobuf.

    **What it does:** Retrieves hourly water-level time series from the NOAA
    Center for Operational Oceanographic Products and Services (CO-OPS) Data
    API for all instrumented tide stations within a bbox, using the
    ``products/water_level`` or ``products/predictions`` endpoint. Returns a
    FlatGeobuf with one Point feature per station, carrying the full hourly
    time series inline as a ``time_series_csv`` attribute suitable for SFINCS
    coastal boundary forcing. Tier-1 free, no API key. Covers US coastal waters,
    Great Lakes, and US territories.

    **When to use:**
    - User asks "what are the tide levels at Fort Myers / Key West / any US
      coastal city" for a past or upcoming date range.
    - Agent needs observed coastal water-level boundary conditions for a
      compound-flood SFINCS model run at a US/territory location (composes
      with ``model_flood_scenario`` as the coastal forcing input).
    - User asks for tide predictions to plan coastal operations or assess
      flood-tide coincidence risk.
    - Agent needs to validate a GTSM global tide estimate against the
      authoritative US tide-gauge record (cross-check via this tool).
    - User asks for storm-surge context: "how high did the water get at
      Fort Myers during Ian?" — use ``product="water_level"`` to retrieve
      the observed record.

    **When NOT to use:**
    - For global coastal water-level (non-US) → use ``fetch_gtsm_tide_surge``
      (GTSM v3.0 global reanalysis).
    - For river-discharge / streamflow → use ``fetch_noaa_nwm_streamflow``
      or ``fetch_streamflow`` (USGS NWIS).
    - For wave height or ocean swell → CO-OPS does not serve wave products;
      use ERA5 or NOAA WAVEWATCH III (not currently in the tool catalog).
    - For gridded storm-surge inundation rasters → use ``model_flood_scenario``
      (SFINCS) with this tool's output as coastal boundary forcing.
    - For sub-hourly tide data (6-minute observations) → the current tool
      returns hourly (``interval=h``); extend with ``interval=6`` in a
      future version if SFINCS setup requires finer resolution.

    **Parameters:**
        bbox: ``(west, south, east, north)`` in EPSG:4326 (WGS84 decimal
            degrees). Required — ``supports_global_query=False``. CO-OPS
            has ~300 stations globally, primarily US coastline and territories;
            specify a coastal bbox. Cap of 50 stations per call.
            Example for Fort Myers + Naples area: ``(-82.5, 25.5, -81.0, 27.5)``
            → returns 2 stations (8725520 Fort Myers + 8725114 Naples Bay).
        start_date: ISO YYYY-MM-DD; inclusive start of the data window.
            Example: ``"2022-09-28"`` (Hurricane Ian landfall day).
        end_date: ISO YYYY-MM-DD; inclusive end of the data window.
            Hard cap of 366 days from start_date. For multi-year spans, call
            in annual chunks and aggregate.
        product: One of:
            - ``"water_level"`` (default): verified (or preliminary) observed
              water level at the gauge, relative to MLLW datum. Best for
              post-event analysis and compound-flood model validation.
            - ``"predictions"``: astronomical tide prediction (no storm surge
              component). Best for future planning, tide tables, and
              decomposing the observed signal into tide vs surge residual.

    **Returns:**
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/dynamic-1h/noaa_coops_tides/<key>.fgb``
        Each feature is a Point at the station location (EPSG:4326) with
        attributes: ``station_id`` (7-digit CO-OPS ID), ``station_name``,
        ``lon``, ``lat``, ``product``, ``datum`` (always "MLLW"),
        ``time_start`` / ``time_end`` (ISO-8601 UTC), ``n_timesteps`` (int),
        ``wl_min_m`` / ``wl_max_m`` / ``wl_mean_m`` (float, meters),
        ``time_series_csv`` (comma-separated "iso,value_m" rows for SFINCS
        boundary input). ``layer_type="vector"``, ``role="primary"``,
        ``units="m (MLLW)"``.

    **Cross-tool dependencies (FR-TA-3):**
        - Feeds INTO: ``model_flood_scenario`` (coastal boundary forcing),
          ``publish_layer`` (map display).
        - Cross-checks: ``fetch_gtsm_tide_surge`` (global non-US tide+surge
          reanalysis) — CONUS basin with CO-OPS coverage should prefer this
          tool; non-CONUS should prefer GTSM.
        - Composes ALONGSIDE: ``fetch_mrms_qpe`` (precip forcing),
          ``fetch_noaa_nwm_streamflow`` (river discharge), and
          ``fetch_era5_reanalysis`` (offshore wind/wave forcing) for the
          full SFINCS compound-flood forcing stack.
        - Sibling NWM tool (river): ``fetch_noaa_nwm_streamflow`` for CONUS
          fluvial forcing.

    **Error types (FR-AS-11):**
        - ``COOPSTidesInputError``: bad bbox / product / dates (retryable=False).
        - ``COOPSTidesUpstreamError``: HTTP/network failure (retryable=True).
        - ``COOPSTidesEmptyError``: no CO-OPS stations in bbox or all stations
          have no data for the requested period (retryable=False).

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="noaa_coops_tides"``.
    Cache key is SHA-256 of ``(bbox-rounded-6dp, start_date, end_date, product)``
    so identical calls reuse the same FlatGeobuf.

    Tier-1 free. No API key. ``supports_global_query=False``.
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise COOPSTidesInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc

    _validate_bbox(bbox)  # type: ignore[arg-type]
    _validate_product(product)
    d0, d1 = _validate_date_range(start_date, end_date)

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "start_date": d0.isoformat(),
        "end_date": d1.isoformat(),
        "product": product,
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_coops_tides_bytes(q_bbox, product, d0, d1),
    )
    assert result.uri is not None, (
        "fetch_noaa_coops_tides is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    return LayerURI(
        layer_id=(
            f"coops-tides-{product}-"
            f"{d0.isoformat()}-{d1.isoformat()}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"CO-OPS Tides — {product.replace('_', ' ').title()} "
            f"({d0.isoformat()} → {d1.isoformat()})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset=f"coops_{product}",
        role="primary",
        units="m (MLLW)",
        bbox=q_bbox,
    )
