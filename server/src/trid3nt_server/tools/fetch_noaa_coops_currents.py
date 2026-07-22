"""``fetch_noaa_coops_currents`` atomic tool — NOAA CO-OPS tidal-current stations.

Wraps the NOAA Center for Operational Oceanographic Products and Services
(CO-OPS) REST API to retrieve the latest observed (or predicted) tidal-current
speed and direction for all CO-OPS *current* stations within a requested bbox.
Returns a FlatGeobuf with one Point feature per station, each carrying the
station's latest current ``speed_kn`` (knots) + ``direction_deg`` (degrees
true) + the observation/prediction ``datetime``.

This is the *currents* sibling of ``fetch_noaa_coops_tides`` (water level). The
two complement each other: tides give you the rising/falling water surface,
currents give you the horizontal flood/ebb flow at tidal inlets and channels.

**API surface** (verified live 2026-06-27):

    Station discovery (currents network, distinct from water-level network):
        https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json
            ?type=currents&units=metric&format=json
        Returns the ~88 realtime current stations globally; we filter by bbox
        after download (a single cheap request covers the full network).
        Current station ids are alphanumeric (e.g. "s08010"), unlike the
        7-digit water-level ids.

    Observed currents (product="currents", one request per station):
        https://api.tidesandcurrents.noaa.gov/api/prod/datagetter
            ?begin_date=YYYYMMDD&end_date=YYYYMMDD
            &station={id}&product=currents&time_zone=gmt
            &units=english&application=trid3nt&format=json
        Response data[] rows:
            t — ISO datetime "YYYY-MM-DD HH:MM"
            s — current speed (knots when units=english; cm/s when metric)
            d — current direction (degrees true, 0-359)
            b — measurement bin index
        We use ``units=english`` so ``s`` is in KNOTS — the standard unit
        for reporting tidal currents.

    Predicted currents (product="currents_predictions", one per station):
        ...&product=currents_predictions&interval=MAX_SLACK
            &units=english&time_zone=gmt&...
        Response shape:
            current_predictions.cp[] rows:
                Time           — "YYYY-MM-DD HH:MM"
                Type           — "flood" | "slack" | "ebb"
                Velocity_Major — signed speed along the major axis (knots);
                                 positive = flood, negative = ebb
                meanFloodDir   — mean flood direction (deg true)
                meanEbbDir     — mean ebb direction (deg true)
                Bin / Depth    — measurement geometry
        We map the prediction nearest to ``now`` to ``speed_kn`` =
        abs(Velocity_Major) and ``direction_deg`` = meanFloodDir when flooding
        / meanEbbDir when ebbing (0 speed at slack -> direction held from the
        adjacent flood/ebb where available).

API limits: no API key required. CO-OPS serves one station per data request;
we impose ``_MAX_STATIONS`` and ``_STATION_REQUEST_DELAY`` to bound fan-out.

**Snapshot semantics**: this tool returns the *single latest* observed value
(or the prediction nearest ``now``) per station — a current-conditions
snapshot, not a full time series. Callers wanting a time series of water
level should use ``fetch_noaa_coops_tides`` (which embeds the full hourly
series inline).

**Cache**: ``dynamic-1h`` — observed currents update every ~6 minutes and
predictions are recomputed; a 1-hour TTL keeps the snapshot fresh without
hammering the upstream.

**Output format** (FlatGeobuf — POINT vector):

    Geometry: Point (station location, EPSG:4326)
    Properties:
        station_id     (str)   — CO-OPS current-station id (e.g. "s08010")
        station_name   (str)   — station common name
        lon            (float) — station longitude (EPSG:4326)
        lat            (float) — station latitude (EPSG:4326)
        product        (str)   — "currents" or "currents_predictions"
        speed_kn       (float) — latest/predicted current speed (knots)
        direction_deg  (float) — current set direction (degrees true 0-359)
        datetime       (str)   — ISO-8601 UTC of the observation/prediction
        bin            (int)   — measurement bin index (observed; -1 if N/A)
        flow_state     (str)   — "flood"/"ebb"/"slack" (predictions) or "" (obs)

``supports_global_query=False`` — CO-OPS currents is a US/territory network
(~88 realtime stations). A bbox is required.

FR-AS-11 typed-error surface: ``COOPSCurrentsError`` (base, retryable=True),
``COOPSCurrentsInputError`` (bad bbox / product, retryable=False),
``COOPSCurrentsUpstreamError`` (HTTP/network failure, retryable=True),
``COOPSCurrentsEmptyError`` (no current stations in bbox / no data,
retryable=False).
"""

from __future__ import annotations

import datetime as _dt
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

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_noaa_coops_currents",
    "estimate_payload_mb",
    "COOPSCurrentsError",
    "COOPSCurrentsInputError",
    "COOPSCurrentsUpstreamError",
    "COOPSCurrentsEmptyError",
    "_validate_bbox",
    "_validate_product",
    "_round_bbox_to_6dp",
    "_discover_stations_in_bbox",
    "_fetch_station_currents",
    "_build_flatgeobuf",
    "_fetch_coops_currents_bytes",
    "COOPS_STATIONS_URL",
    "COOPS_DATA_URL",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_noaa_coops_currents")

# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class COOPSCurrentsError(RuntimeError):
    """Base class for fetch_noaa_coops_currents failures."""

    error_code: str = "COOPS_CURRENTS_ERROR"
    retryable: bool = True


class COOPSCurrentsInputError(COOPSCurrentsError):
    """Invalid inputs — bad bbox or unknown product. Not retryable."""

    error_code = "COOPS_CURRENTS_INPUT_ERROR"
    retryable = False


class COOPSCurrentsUpstreamError(COOPSCurrentsError):
    """CO-OPS REST API request failed (network, HTTP 4xx/5xx, bad JSON)."""

    error_code = "COOPS_CURRENTS_UPSTREAM_ERROR"
    retryable = True


class COOPSCurrentsEmptyError(COOPSCurrentsError):
    """No CO-OPS current stations in bbox (or none returned data). Not retryable."""

    error_code = "COOPS_CURRENTS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: CO-OPS current-station discovery endpoint (~88 realtime stations).
COOPS_STATIONS_URL = (
    "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
    "?type=currents&units=metric&format=json"
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

#: Observed-currents lookback window (days). The latest sample within this
#: window is the snapshot; 2 days tolerates station gaps / maintenance.
_OBSERVED_LOOKBACK_DAYS = 2

#: Predictions look-ahead window (days) for picking the prediction nearest now.
_PREDICTION_WINDOW_DAYS = 2

#: Allowed product values exposed to callers.
_VALID_PRODUCTS: frozenset[str] = frozenset({"currents", "currents_predictions"})


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_noaa_coops_currents",
        ttl_class="dynamic-1h",
        source_class="noaa_coops_currents",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_noaa_coops_currents without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    product: str = "currents",
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    Snapshot semantics: one Point feature per station with a handful of scalar
    attributes (~0.4 KB/station serialized). CO-OPS currents is a sparse
    network (~88 stations globally), so even a large bbox yields a tiny
    payload. Area is a weak proxy for station count.
    """
    if bbox is None:
        n_stations = 30
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
            # currents are sparser than tide gauges: ~1 station per sq degree
            n_stations = min(_MAX_STATIONS, max(1, int(sq_deg * 1.0)))
        except (TypeError, ValueError):
            n_stations = 10

    kb_per_station = 0.4
    return max(0.01, n_stations * kb_per_station / 1_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``COOPSCurrentsInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise COOPSCurrentsInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise COOPSCurrentsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise COOPSCurrentsInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise COOPSCurrentsInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise COOPSCurrentsInputError(
            f"bbox is degenerate (west < east, south < north required): {bbox!r}"
        )


def _validate_product(product: str) -> None:
    """Raise ``COOPSCurrentsInputError`` for unsupported product values."""
    if not isinstance(product, str):
        raise COOPSCurrentsInputError(
            f"product must be a str; got {type(product).__name__}"
        )
    if product not in _VALID_PRODUCTS:
        raise COOPSCurrentsInputError(
            f"unsupported product {product!r}; allowed: {sorted(_VALID_PRODUCTS)}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coords to 6 decimal places for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET. Raises ``COOPSCurrentsUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise COOPSCurrentsUpstreamError(
            f"upstream HTTP {exc.code} for {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise COOPSCurrentsUpstreamError(
            f"network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise COOPSCurrentsUpstreamError(
            f"timed out after {timeout}s for {url}"
        ) from exc


# ---------------------------------------------------------------------------
# Station discovery.
# ---------------------------------------------------------------------------


def _discover_stations_in_bbox(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch the CO-OPS current-station catalog and filter to those in bbox.

    Returns a list of ``{id, name, lat, lng}`` dicts.
    Raises ``COOPSCurrentsUpstreamError`` if the catalog download fails.
    Raises ``COOPSCurrentsEmptyError`` if no stations fall inside bbox.
    """
    west, south, east, north = bbox
    body = _http_get(COOPS_STATIONS_URL, timeout=_STATIONS_TIMEOUT)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise COOPSCurrentsUpstreamError(
            f"CO-OPS current-station catalog returned non-JSON response: {exc}"
        ) from exc

    stations_raw = data.get("stations", [])
    if not stations_raw:
        raise COOPSCurrentsUpstreamError(
            "CO-OPS current-station catalog returned empty 'stations' list"
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
                "fetch_noaa_coops_currents: bbox=%s contains >%d stations; "
                "capping at %d",
                bbox,
                _MAX_STATIONS,
                _MAX_STATIONS,
            )
            break

    if not matching:
        raise COOPSCurrentsEmptyError(
            f"no CO-OPS tidal-current stations found in bbox={bbox}; "
            f"CO-OPS currents covers primarily US tidal inlets and channels "
            f"(~88 realtime stations globally). Try a wider bbox or a known "
            f"tidal-inlet area (e.g. San Francisco Bay, New York Harbor)."
        )

    logger.info(
        "fetch_noaa_coops_currents: found %d station(s) in bbox=%s",
        len(matching),
        bbox,
    )
    return matching


# ---------------------------------------------------------------------------
# Per-station data retrieval.
# ---------------------------------------------------------------------------


def _build_currents_url(
    station_id: str,
    product: str,
    d0: _dt.date,
    d1: _dt.date,
) -> str:
    """Build the CO-OPS data-getter URL for a single current station."""
    params: dict[str, str] = {
        "begin_date": d0.strftime("%Y%m%d"),
        "end_date": d1.strftime("%Y%m%d"),
        "station": station_id,
        "product": product,
        "time_zone": "gmt",
        "units": "english",  # english -> speed in KNOTS
        "application": "trid3nt",
        "format": "json",
    }
    if product == "currents_predictions":
        # MAX_SLACK returns the flood/slack/ebb sequence with directions.
        params["interval"] = "MAX_SLACK"
    return f"{COOPS_DATA_URL}?{urllib.parse.urlencode(params)}"


def _parse_iso(t: str) -> str:
    """Normalize a CO-OPS "YYYY-MM-DD HH:MM" string to ISO-8601 UTC."""
    return t.replace(" ", "T") + "Z" if " " in t else t


def _to_dt(t: str) -> _dt.datetime | None:
    """Parse a CO-OPS "YYYY-MM-DD HH:MM" string to a UTC datetime, or None."""
    try:
        return _dt.datetime.strptime(t, "%Y-%m-%d %H:%M").replace(
            tzinfo=_dt.timezone.utc
        )
    except (ValueError, TypeError):
        return None


def _fetch_station_currents(
    station: dict[str, Any],
    product: str,
    d0: _dt.date,
    d1: _dt.date,
    now: _dt.datetime,
) -> dict[str, Any] | None:
    """Fetch the latest observed / nearest predicted current for one station.

    Returns a snapshot dict ``{speed_kn, direction_deg, datetime, bin,
    flow_state}`` or ``None`` if the station has no usable data. Individual
    station errors are swallowed so one bad station does not abort the bbox.
    """
    url = _build_currents_url(station["id"], product, d0, d1)
    try:
        body = _http_get(url, timeout=_DATA_TIMEOUT)
    except COOPSCurrentsUpstreamError as exc:
        logger.warning(
            "fetch_noaa_coops_currents: station %s HTTP error: %s",
            station["id"],
            exc,
        )
        return None

    try:
        resp = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning(
            "fetch_noaa_coops_currents: station %s non-JSON response: %s",
            station["id"],
            exc,
        )
        return None

    if "error" in resp:
        logger.info(
            "fetch_noaa_coops_currents: station %s API error: %s",
            station["id"],
            resp["error"],
        )
        return None

    if product == "currents":
        return _parse_observed(resp)
    return _parse_predictions(resp, now)


def _parse_observed(resp: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the most-recent valid observed-current row."""
    rows = resp.get("data") or []
    best: dict[str, Any] | None = None
    best_dt: _dt.datetime | None = None
    for row in rows:
        t = row.get("t", "")
        s_raw = row.get("s")
        d_raw = row.get("d")
        if s_raw in (None, "") or d_raw in (None, ""):
            continue
        try:
            speed = float(s_raw)
            direction = float(d_raw)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(speed) and math.isfinite(direction)):
            continue
        rdt = _to_dt(t)
        if rdt is None:
            continue
        if best_dt is None or rdt > best_dt:
            best_dt = rdt
            try:
                bin_idx = int(row.get("b"))
            except (TypeError, ValueError):
                bin_idx = -1
            best = {
                "speed_kn": speed,
                "direction_deg": direction % 360.0,
                "datetime": _parse_iso(t),
                "bin": bin_idx,
                "flow_state": "",
            }
    return best


def _parse_predictions(
    resp: dict[str, Any], now: _dt.datetime
) -> dict[str, Any] | None:
    """Pick the predicted current nearest ``now`` (flood/slack/ebb sequence)."""
    cp = resp.get("current_predictions") or {}
    rows = cp.get("cp") if isinstance(cp, dict) else None
    if not rows:
        return None

    best: dict[str, Any] | None = None
    best_gap: float | None = None
    for row in rows:
        t = row.get("Time", "")
        rdt = _to_dt(t)
        if rdt is None:
            continue
        try:
            vmaj = float(row.get("Velocity_Major"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(vmaj):
            continue
        flow = str(row.get("Type", "")).lower()
        # Direction: flood uses meanFloodDir, ebb uses meanEbbDir. Velocity_Major
        # sign disambiguates (positive = flood). Slack holds 0 speed.
        try:
            flood_dir = float(row.get("meanFloodDir"))
        except (TypeError, ValueError):
            flood_dir = float("nan")
        try:
            ebb_dir = float(row.get("meanEbbDir"))
        except (TypeError, ValueError):
            ebb_dir = float("nan")
        if vmaj > 0:
            direction = flood_dir
        elif vmaj < 0:
            direction = ebb_dir
        else:  # slack
            direction = flood_dir if math.isfinite(flood_dir) else ebb_dir
        if not math.isfinite(direction):
            direction = 0.0

        gap = abs((rdt - now).total_seconds())
        if best_gap is None or gap < best_gap:
            best_gap = gap
            try:
                bin_idx = int(row.get("Bin"))
            except (TypeError, ValueError):
                bin_idx = -1
            best = {
                "speed_kn": abs(vmaj),
                "direction_deg": direction % 360.0,
                "datetime": _parse_iso(t),
                "bin": bin_idx,
                "flow_state": flow,
            }
    return best


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _build_flatgeobuf(
    records: list[dict[str, Any]],
    product: str,
) -> bytes:
    """Convert per-station snapshot records to a FlatGeobuf byte string.

    Each record carries: station_id, station_name, lon, lat, speed_kn,
    direction_deg, datetime, bin, flow_state.
    """
    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point
    except ImportError as exc:
        raise COOPSCurrentsUpstreamError(
            f"geopandas / shapely / pandas not available: {exc}"
        ) from exc

    _COLS = [
        "station_id", "station_name", "lon", "lat", "product",
        "speed_kn", "direction_deg", "datetime", "bin", "flow_state",
    ]

    rows_out: list[dict[str, Any]] = []
    geoms: list[Any] = []

    for rec in records:
        rows_out.append({
            "station_id": rec["station_id"],
            "station_name": rec["station_name"],
            "lon": float(rec["lon"]),
            "lat": float(rec["lat"]),
            "product": product,
            "speed_kn": float(rec["speed_kn"]),
            "direction_deg": float(rec["direction_deg"]),
            "datetime": rec["datetime"],
            "bin": int(rec["bin"]),
            "flow_state": rec["flow_state"],
        })
        geoms.append(Point(float(rec["lon"]), float(rec["lat"])))

    if not rows_out:
        empty_df = pd.DataFrame(columns=_COLS)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        df = pd.DataFrame(rows_out)
        gdf = gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_coops_currents_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:
            raise COOPSCurrentsUpstreamError(
                f"FlatGeobuf serialization failed: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_noaa_coops_currents: FlatGeobuf serialized %d station(s) "
            "= %d bytes",
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


def _fetch_coops_currents_bytes(
    bbox: tuple[float, float, float, float],
    product: str,
    now: _dt.datetime | None = None,
) -> bytes:
    """End-to-end: discover stations -> fetch latest current -> FGB bytes."""
    now = now or _dt.datetime.now(_dt.timezone.utc)

    if product == "currents":
        d0 = (now - _dt.timedelta(days=_OBSERVED_LOOKBACK_DAYS)).date()
        d1 = now.date()
    else:  # currents_predictions -> look ahead from today
        d0 = now.date()
        d1 = (now + _dt.timedelta(days=_PREDICTION_WINDOW_DAYS)).date()

    # 1. Discover stations inside bbox.
    stations = _discover_stations_in_bbox(bbox)

    # 2. Fetch the latest/predicted current for each station.
    records: list[dict[str, Any]] = []
    for i, station in enumerate(stations):
        if i > 0:
            time.sleep(_STATION_REQUEST_DELAY)
        snap = _fetch_station_currents(station, product, d0, d1, now)
        if snap:
            records.append({
                "station_id": station["id"],
                "station_name": station["name"],
                "lon": station["lng"],
                "lat": station["lat"],
                **snap,
            })

    if not records:
        raise COOPSCurrentsEmptyError(
            f"all {len(stations)} current station(s) in bbox={bbox} returned "
            f"no data for product={product!r}; stations may be offline, in "
            f"maintenance, or have no published data for this window"
        )

    logger.info(
        "fetch_noaa_coops_currents: %d/%d station(s) returned data; "
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
    # Annotations: readOnlyHint=True, openWorldHint=True (external public API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_noaa_coops_currents(
    bbox: tuple[float, float, float, float],
    product: str = "currents",
    # job-0164 / Wave 4.10 convention: absorb LLM-invented kwargs
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NOAA CO-OPS tidal-current stations + latest current as a FlatGeobuf.

    **What it does:** Retrieves the latest observed (or predicted) tidal-current
    speed + direction from the NOAA CO-OPS Data API for all current stations
    within a bbox. Returns a POINT FlatGeobuf with one feature per station,
    carrying ``speed_kn`` (knots), ``direction_deg`` (degrees true), and the
    observation ``datetime``. A current-conditions snapshot — complements the
    water-level time series from ``fetch_noaa_coops_tides``. Tier-1 free, no API
    key. Covers US tidal inlets, harbors, and channels (~88 realtime stations).

    **When to use:**
    - User asks "what's the tidal current at the Golden Gate / San Francisco
      Bay / New York Harbor right now" (speed + flood/ebb direction).
    - Agent needs ebb/flood current context at a tidal inlet for navigation,
      a spill-drift estimate, or a sediment-transport narrative.
    - User asks for predicted slack/flood/ebb timing -> use
      ``product="currents_predictions"``.

    **When NOT to use:**
    - For water LEVEL / tide height time series -> use ``fetch_noaa_coops_tides``.
    - For global (non-US) currents -> CO-OPS does not cover; no global tool
      currently in the catalog.
    - For deep-ocean / large-scale circulation currents -> CO-OPS is tidal
      (inlet/channel) only.
    - For wave height / swell -> CO-OPS does not serve wave products.

    **Parameters:**
        bbox: ``(west, south, east, north)`` in EPSG:4326. Required
            (``supports_global_query=False``). Example San Francisco Bay:
            ``(-123.0, 37.4, -122.0, 38.2)`` -> 4 stations (Martinez,
            Southampton Shoal, Oakland Outer/Inner Harbor). Cap of 50 stations.
        product: One of:
            - ``"currents"`` (default): latest observed current speed +
              direction at the station's primary bin.
            - ``"currents_predictions"``: predicted current nearest now from
              the flood/slack/ebb sequence (carries ``flow_state``).

    **Returns:**
        ``LayerURI`` to a FlatGeobuf in the cache bucket. Each feature is a
        Point (EPSG:4326) with attributes ``station_id``, ``station_name``,
        ``lon``, ``lat``, ``product``, ``speed_kn`` (float, knots),
        ``direction_deg`` (float, degrees true 0-359), ``datetime`` (ISO-8601
        UTC), ``bin`` (int, -1 if N/A), ``flow_state`` ("flood"/"ebb"/"slack"
        for predictions, "" for observed). ``layer_type="vector"``,
        ``role="primary"``, ``units="kn"``.

    **Cross-tool dependencies (FR-TA-3):**
        - Complements: ``fetch_noaa_coops_tides`` (water level at the same
          inlets), ``fetch_gtsm_tide_surge`` (global tide+surge).
        - Feeds INTO: ``publish_layer`` (map display of current vectors).

    **Error types (FR-AS-11):**
        - ``COOPSCurrentsInputError``: bad bbox / product (retryable=False).
        - ``COOPSCurrentsUpstreamError``: HTTP/network failure (retryable=True).
        - ``COOPSCurrentsEmptyError``: no current stations in bbox or none
          returned data (retryable=False).

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="noaa_coops_currents"``.
    Tier-1 free. No API key. ``supports_global_query=False``.
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise COOPSCurrentsInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc

    _validate_bbox(bbox)  # type: ignore[arg-type]
    _validate_product(product)

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "product": product,
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_coops_currents_bytes(q_bbox, product),
    )
    assert result.uri is not None, (
        "fetch_noaa_coops_currents is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    return LayerURI(
        layer_id=(
            f"coops-currents-{product}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"CO-OPS Tidal Currents — {product.replace('_', ' ').title()}"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset=f"coops_{product}",
        role="primary",
        units="kn",
        bbox=q_bbox,
    )
