"""``fetch_asos_metar`` atomic tool — Iowa State IEM ASOS/METAR station observations (job-A7).

Fetches hourly ASOS/METAR surface weather observations from the Iowa State
University Iowa Environmental Mesonet (IEM) CGI service. Observations include
temperature, dewpoint, wind speed/direction, altimeter, MSLP, visibility,
sky coverage, and present-weather codes — the primary surface-met forcing
layer for hazard-event context and boundary-layer weather overlays.

API surface (IEM ASOS CGI, free, no API key required):

    base: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
    station discovery: https://mesonet.agron.iastate.edu/geojson/network/{state}_ASOS.geojson

All ASOS stations in a bbox are discovered by fetching the per-state ASOS
network GeoJSON (one request per state that overlaps the bbox), filtering by
coordinates, then bulk-requesting all matching stations in a single CGI call.

Output: a FlatGeobuf point layer (one point per observation, at the station
coordinates) containing all standard ASOS surface-met fields. EPSG:4326.
Cache: ``dynamic-1h`` for recent windows; ``static-30d`` for historical.

FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_asos_metar",
    "ASASMETARError",
    "ASASMETARInputError",
    "ASASMETARUpstreamError",
    "ASASMETAREmptyError",
    "estimate_payload_mb",
    "_discover_stations_in_bbox",
    "_fetch_asos_csv_bytes",
    "_parse_csv_to_fgb",
]

logger = logging.getLogger("grace2_agent.tools.fetch_asos_metar")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ASASMETARError(RuntimeError):
    """Base class for fetch_asos_metar failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "ASOS_METAR_ERROR"
    retryable: bool = True


class ASASMETARInputError(ASASMETARError):
    """Invalid inputs — bad bbox, out-of-range dates, unknown data field.

    Not retryable: the caller must fix the argument.
    """

    error_code = "ASOS_METAR_INPUT_ERROR"
    retryable = False


class ASASMETARUpstreamError(ASASMETARError):
    """IEM ASOS CGI request failed (network error, HTTP 5xx, malformed CSV).

    Retryable — transient IEM outages recover on retry.
    """

    error_code = "ASOS_METAR_UPSTREAM_ERROR"
    retryable = True


class ASASMETAREmptyError(ASASMETARError):
    """No ASOS stations found in the bbox, or all observations are missing.

    Not retryable — the bbox contains no IEM-archived ASOS stations for
    the requested period. Either widen the bbox or check station coverage.
    """

    error_code = "ASOS_METAR_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_IEM_ASOS_CGI = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_IEM_NETWORK_GEOJSON = (
    "https://mesonet.agron.iastate.edu/geojson/network/{state}_ASOS.geojson"
)

# User-Agent required by IEM (best practice; IEM is generally tolerant but
# a descriptive UA is recommended for their rate-limiting logic).
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

# HTTP timeouts (seconds).
_NETWORK_TIMEOUT = 30.0
_DATA_TIMEOUT = 60.0

# Maximum stations allowed in a single bulk request to IEM.
# IEM supports up to several hundred but we cap to avoid oversized URLs.
_MAX_STATIONS = 100

# CONUS + territories state codes recognized by IEM ASOS network.
# Used to decide which network GeoJSONs to fetch when discovering stations
# in a bbox that spans state lines.
_IEM_ASOS_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU",
)

# Rough bounding boxes for each US state (lon_min, lat_min, lon_max, lat_max)
# used to skip states that obviously don't overlap the requested bbox, saving
# unnecessary network fetches. Values are intentionally generous (padded ~0.5°).
_STATE_BBOX: dict[str, tuple[float, float, float, float]] = {
    "AL": (-88.6, 30.1, -84.8, 35.0),
    "AK": (-180.0, 51.2, -129.9, 71.4),
    "AZ": (-114.9, 31.3, -109.0, 37.0),
    "AR": (-94.7, 33.0, -89.6, 36.5),
    "CA": (-124.5, 32.5, -114.1, 42.0),
    "CO": (-109.1, 36.9, -102.0, 41.0),
    "CT": (-73.7, 40.9, -71.7, 42.1),
    "DE": (-75.8, 38.4, -75.0, 39.9),
    "FL": (-87.7, 24.4, -79.9, 31.0),
    "GA": (-85.6, 30.4, -80.8, 35.0),
    "HI": (-160.3, 18.9, -154.8, 22.2),
    "ID": (-117.3, 41.9, -111.0, 49.0),
    "IL": (-91.5, 36.9, -87.0, 42.5),
    "IN": (-88.1, 37.7, -84.7, 41.8),
    "IA": (-96.7, 40.4, -90.1, 43.5),
    "KS": (-102.1, 36.9, -94.6, 40.0),
    "KY": (-89.6, 36.5, -81.9, 39.1),
    "LA": (-94.1, 28.9, -88.8, 33.0),
    "ME": (-71.1, 43.0, -66.9, 47.5),
    "MD": (-79.5, 37.9, -75.0, 39.7),
    "MA": (-73.5, 41.2, -69.9, 42.9),
    "MI": (-90.5, 41.7, -82.4, 48.3),
    "MN": (-97.2, 43.5, -89.5, 49.4),
    "MS": (-91.7, 30.2, -88.1, 35.0),
    "MO": (-95.8, 35.9, -89.1, 40.6),
    "MT": (-116.1, 44.4, -104.0, 49.0),
    "NE": (-104.1, 40.0, -95.3, 43.0),
    "NV": (-120.0, 35.0, -114.0, 42.0),
    "NH": (-72.6, 42.7, -70.6, 45.3),
    "NJ": (-75.6, 38.9, -73.9, 41.4),
    "NM": (-109.1, 31.3, -103.0, 37.0),
    "NY": (-79.8, 40.5, -71.8, 45.0),
    "NC": (-84.3, 33.8, -75.4, 36.6),
    "ND": (-104.1, 45.9, -96.6, 49.0),
    "OH": (-84.8, 38.4, -80.5, 42.3),
    "OK": (-103.0, 33.6, -94.4, 37.0),
    "OR": (-124.7, 41.9, -116.5, 46.3),
    "PA": (-80.5, 39.7, -74.7, 42.3),
    "RI": (-71.9, 41.1, -71.1, 42.0),
    "SC": (-83.4, 32.0, -78.5, 35.2),
    "SD": (-104.1, 42.5, -96.4, 45.9),
    "TN": (-90.3, 34.9, -81.6, 36.7),
    "TX": (-106.6, 25.8, -93.5, 36.5),
    "UT": (-114.1, 37.0, -109.0, 42.0),
    "VT": (-73.4, 42.7, -71.5, 45.0),
    "VA": (-83.7, 36.5, -75.3, 39.5),
    "WA": (-124.8, 45.5, -116.9, 49.0),
    "WV": (-82.7, 37.2, -77.7, 40.6),
    "WI": (-92.9, 42.5, -86.8, 47.1),
    "WY": (-111.1, 40.9, -104.0, 45.0),
    "DC": (-77.2, 38.8, -76.9, 39.0),
    "PR": (-67.3, 17.9, -65.2, 18.6),
    "VI": (-65.1, 17.7, -64.6, 18.4),
    "GU": (144.6, 13.2, 145.0, 13.7),
}

# ASOS data fields available from IEM CGI.
# Full list: tmpf (temp °F), dwpf (dewpoint °F), relh (RH %), sknt (wind kt),
# drct (wind dir °), gust (gust kt), alti (altimeter inHg), mslp (MSLP hPa),
# vsby (visibility miles), wxcodes (present weather), skyc1/2/3/4 (sky cover),
# skyl1/2/3/4 (sky layer height ft), feel (heat index/wind chill °F),
# ice_accretion_1hr/3hr/6hr (in), peak_wind_gust, peak_wind_drct, peak_wind_time
_DEFAULT_DATA_FIELDS = (
    "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp",
    "vsby", "wxcodes", "skyc1", "skyl1",
)

# Properties preserved from CSV → FlatGeobuf.
_FGB_COLUMNS = (
    "station", "valid", "lon", "lat", "elevation",
    "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp",
    "vsby", "wxcodes", "skyc1", "skyl1",
)

# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Build AtomicToolMetadata defensively to handle schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_asos_metar",
        ttl_class="dynamic-1h",
        source_class="asos_metar",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not yet support supports_global_query; "
            "registering fetch_asos_metar without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    ASOS observations are small (~200 bytes per row per station per hour).
    Typical estimates:
    - 1 station × 1 day ≈ 24 obs × 200 B ≈ 5 KB
    - 10 stations × 24 hours ≈ 240 obs × 200 B ≈ 48 KB
    - 50 stations × 7 days ≈ 8,400 obs × 200 B ≈ 1.7 MB

    The estimate is conservative (uses bbox area to guess station count).
    """
    n_stations = 5  # default guess
    n_hours = 24     # default guess
    if bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # Rough density: ~1-2 ASOS stations per 1° square in CONUS.
            n_stations = max(1, min(_MAX_STATIONS, int(sq_deg * 1.5)))
        except (TypeError, ValueError):
            pass
    if start_time is not None and end_time is not None:
        try:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%d"
            dt_start = _dt.strptime(start_time[:10], fmt)
            dt_end = _dt.strptime(end_time[:10], fmt)
            n_hours = max(1, int((dt_end - dt_start).total_seconds() / 3600))
        except (ValueError, TypeError):
            pass
    obs = n_stations * n_hours
    return max(0.001, obs * 200 / 1_000_000)


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _NETWORK_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``ASASMETARUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise ASASMETARUpstreamError(
            f"IEM returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ASASMETARUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise ASASMETARUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# Station discovery — find all ASOS stations within a bbox.
# ---------------------------------------------------------------------------


def _bbox_overlaps_state(
    bbox: tuple[float, float, float, float],
    state_bbox: tuple[float, float, float, float],
) -> bool:
    """Return True if ``bbox`` overlaps ``state_bbox``."""
    w1, s1, e1, n1 = bbox
    w2, s2, e2, n2 = state_bbox
    return not (e1 < w2 or w1 > e2 or n1 < s2 or s1 > n2)


def _discover_stations_in_bbox(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Find all IEM-archived ASOS stations whose coordinates fall inside ``bbox``.

    Fetches the per-state ASOS network GeoJSON for each state that overlaps the
    bbox. Returns a list of station dicts with keys: ``sid``, ``lon``, ``lat``,
    ``sname``, ``state``.

    Raises:
        ``ASASMETARUpstreamError`` — network failure fetching station metadata.
        ``ASASMETAREmptyError`` — no ASOS stations found in the bbox.
    """
    west, south, east, north = bbox
    stations: list[dict[str, Any]] = []
    seen: set[str] = set()

    for state in _IEM_ASOS_STATES:
        state_box = _STATE_BBOX.get(state)
        if state_box is None:
            continue
        if not _bbox_overlaps_state(bbox, state_box):
            continue

        url = _IEM_NETWORK_GEOJSON.format(state=state)
        try:
            raw = _http_get(url, timeout=_NETWORK_TIMEOUT)
        except ASASMETARUpstreamError as exc:
            logger.warning(
                "fetch_asos_metar: failed to fetch %s station list: %s", state, exc
            )
            continue

        try:
            geojson = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "fetch_asos_metar: malformed GeoJSON for %s network: %s", state, exc
            )
            continue

        for feat in geojson.get("features", []):
            coords = (feat.get("geometry") or {}).get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (west <= lon <= east and south <= lat <= north):
                continue
            sid = feat.get("id") or (feat.get("properties") or {}).get("sid")
            if not sid or sid in seen:
                continue
            props = feat.get("properties") or {}
            seen.add(sid)
            stations.append({
                "sid": str(sid),
                "lon": lon,
                "lat": lat,
                "sname": props.get("sname", ""),
                "state": state,
            })

        if len(stations) >= _MAX_STATIONS:
            logger.info(
                "fetch_asos_metar: station cap (%d) reached; truncating discovery",
                _MAX_STATIONS,
            )
            stations = stations[:_MAX_STATIONS]
            break

    return stations


# ---------------------------------------------------------------------------
# IEM CGI data fetch.
# ---------------------------------------------------------------------------


def _build_asos_url(
    station_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    data_fields: tuple[str, ...],
) -> str:
    """Build the IEM ASOS CGI URL for the given stations and time range.

    Uses ``format=onlycomma`` (no HTML, CSV only) with ``latlon=yes`` and
    ``elev=yes`` so coordinates are embedded per-row. ``report_type=3``
    returns only the routine hourly observations (excludes specials/SPECIs).
    ``missing=null`` encodes missing values as the literal string 'null'
    (parseable without ambiguity).
    """
    params: list[tuple[str, str]] = []
    for sid in station_ids:
        params.append(("station", sid))
    for field in data_fields:
        params.append(("data", field))
    params.extend([
        ("year1", str(start_dt.year)),
        ("month1", str(start_dt.month)),
        ("day1", str(start_dt.day)),
        ("hour1", str(start_dt.hour)),
        ("minute1", str(start_dt.minute)),
        ("year2", str(end_dt.year)),
        ("month2", str(end_dt.month)),
        ("day2", str(end_dt.day)),
        ("hour2", str(end_dt.hour)),
        ("minute2", str(end_dt.minute)),
        ("tz", "UTC"),
        ("format", "onlycomma"),
        ("latlon", "yes"),
        ("elev", "yes"),
        ("missing", "null"),
        ("trace", "T"),
        ("direct", "no"),
        ("report_type", "3"),
    ])
    return _IEM_ASOS_CGI + "?" + urllib.parse.urlencode(params)


def _fetch_asos_csv_bytes(
    station_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    data_fields: tuple[str, ...],
) -> bytes:
    """Download the IEM ASOS CSV for ``station_ids`` over ``[start_dt, end_dt]``.

    Returns the raw CSV bytes. Raises ``ASASMETARUpstreamError`` on failure.
    """
    url = _build_asos_url(station_ids, start_dt, end_dt, data_fields)
    logger.info(
        "fetch_asos_metar: GET %d station(s) from %s to %s",
        len(station_ids),
        start_dt.isoformat(),
        end_dt.isoformat(),
    )
    return _http_get(url, timeout=_DATA_TIMEOUT)


# ---------------------------------------------------------------------------
# CSV → FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _parse_csv_to_fgb(
    csv_bytes: bytes,
    station_meta: dict[str, dict[str, Any]],
) -> bytes:
    """Parse IEM ASOS CSV + serialize to FlatGeobuf.

    Each row becomes a Point feature at the station's lon/lat (taken from the
    CSV's per-row ``lon``/``lat`` columns — IEM populates these when
    ``latlon=yes`` is set). Falls back to ``station_meta`` coordinates when
    the CSV row carries null coordinates (rare for ASOS).

    Raises:
        ``ASASMETARUpstreamError`` — geopandas/pandas/shapely not available,
          or CSV is corrupt.
        ``ASASMETAREmptyError`` — all rows have missing coordinates or the
          CSV has zero data rows after header stripping.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ASASMETARUpstreamError(
            f"geopandas / pandas / shapely not available: {exc}"
        ) from exc

    # IEM returns a comment block at the top before the CSV header.
    # Strip lines starting with '#'.
    text = csv_bytes.decode("utf-8", errors="replace")
    clean_lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    clean_text = "\n".join(clean_lines)

    try:
        df = pd.read_csv(
            io.StringIO(clean_text),
            dtype=str,
            low_memory=False,
            keep_default_na=False,
            na_values=["null", "M", ""],
        )
    except pd.errors.ParserError as exc:
        raise ASASMETARUpstreamError(
            f"ASOS CSV parse failed: {exc}"
        ) from exc

    if df.empty:
        raise ASASMETAREmptyError(
            "IEM ASOS returned zero data rows for the requested stations/period"
        )

    # IEM always includes 'station', 'valid', 'lon', 'lat' when latlon=yes.
    required = {"station", "valid"}
    missing_cols = required - set(df.columns)
    if missing_cols:
        raise ASASMETARUpstreamError(
            f"ASOS CSV missing required columns: {sorted(missing_cols)}; "
            f"got {sorted(df.columns)[:12]}..."
        )

    # Coerce lon/lat.
    if "lon" in df.columns and "lat" in df.columns:
        df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
        df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    else:
        # Fallback to station metadata coordinates.
        df["lon"] = df["station"].map(
            lambda s: station_meta.get(s, {}).get("lon")
        )
        df["lat"] = df["station"].map(
            lambda s: station_meta.get(s, {}).get("lat")
        )
        df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
        df["lat"] = pd.to_numeric(df["lat"], errors="coerce")

    df = df.dropna(subset=["lon", "lat"]).copy()
    if df.empty:
        raise ASASMETAREmptyError(
            "All ASOS observation rows lack valid coordinates"
        )

    # WGS84 sanity clip.
    df = df[
        (df["lon"].between(-180.0, 180.0))
        & (df["lat"].between(-90.0, 90.0))
    ].copy()
    if df.empty:
        raise ASASMETAREmptyError(
            "All ASOS observation rows have out-of-range coordinates"
        )

    # Numeric coercions for key fields.
    for col in ("elevation", "tmpf", "dwpf", "sknt", "drct", "gust",
                "alti", "mslp", "vsby", "skyl1"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Keep only the columns we expose downstream; fill missing with None.
    keep = [c for c in _FGB_COLUMNS if c in df.columns]
    df_out = df[keep].copy()

    # Build GeoDataFrame with Point geometry.
    geom = [Point(lon, lat) for lon, lat in zip(df["lon"], df["lat"])]
    gdf = gpd.GeoDataFrame(df_out, geometry=geom, crs="EPSG:4326")

    logger.info(
        "fetch_asos_metar: %d observations for %d station(s)",
        len(gdf),
        gdf["station"].nunique() if "station" in gdf.columns else -1,
    )

    # Serialize to FlatGeobuf.
    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_asos_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:
            raise ASASMETARUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} observations: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _parse_datetime(s: str, field_name: str) -> datetime:
    """Parse an ISO-8601 date or datetime string to a UTC-aware datetime.

    Accepts: ``"YYYY-MM-DD"``, ``"YYYY-MM-DDTHH:MM:SSZ"``,
    ``"YYYY-MM-DD HH:MM"``. Returns UTC datetime.
    """
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    raise ASASMETARInputError(
        f"{field_name}={s!r} is not a parseable date/datetime string; "
        "use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"
    )


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ASASMETARInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    w, s, e, n = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ASASMETARInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0):
        raise ASASMETARInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
        raise ASASMETARInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if w >= e or s >= n:
        raise ASASMETARInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


# ---------------------------------------------------------------------------
# Top-level fetch bytes (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_asos_metar_bytes(
    bbox: tuple[float, float, float, float],
    start_dt: datetime,
    end_dt: datetime,
    data_fields: tuple[str, ...],
) -> bytes:
    """End-to-end: discover stations → download CSV → convert to FlatGeobuf bytes."""
    stations = _discover_stations_in_bbox(bbox)
    if not stations:
        raise ASASMETAREmptyError(
            f"No IEM ASOS stations found inside bbox={bbox}; "
            "either no ASOS stations cover this area or all are currently offline"
        )

    station_ids = [s["sid"] for s in stations]
    station_meta = {s["sid"]: s for s in stations}
    logger.info(
        "fetch_asos_metar: discovered %d station(s): %s",
        len(station_ids),
        station_ids[:10],
    )

    csv_bytes = _fetch_asos_csv_bytes(station_ids, start_dt, end_dt, data_fields)
    return _parse_csv_to_fgb(csv_bytes, station_meta)


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
def fetch_asos_metar(
    bbox: tuple[float, float, float, float],
    start_time: str | None = None,
    end_time: str | None = None,
    # job-A7: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch ASOS/METAR surface weather observations as a point FlatGeobuf.

    Retrieves hourly surface weather observations from all Automated Surface
    Observing System (ASOS) stations within the requested bbox, sourced from
    the Iowa State University Iowa Environmental Mesonet (IEM) CGI archive.
    Observations include temperature, dewpoint, wind speed/direction/gust,
    altimeter setting, MSLP, visibility, sky cover, and present-weather codes.

    When to use:
      - User asks for current or historical surface weather at an airport or
        weather station (e.g., "what was the wind at Fort Myers yesterday?",
        "show me temperature readings near Naples, FL").
      - Hazard-event context: surface met conditions before/during a flood,
        hurricane, or wildfire event ("what were wind speeds when Ian made
        landfall?").
      - Multi-station spatial overlay of surface weather for a region.
      - Providing boundary-layer meteorology input for hazard models that need
        observed wind, humidity, or temperature fields.

    When NOT to use:
      - Forecasts or future weather — ASOS is an observational archive; use
        ``fetch_nws_event`` or ``fetch_nws_alerts_conus`` for current NWS
        products, or ``fetch_hrrr_forecast`` for model forecasts.
      - Gridded analysis or reanalysis over large areas — use ``fetch_era5_reanalysis``
        (global, hourly ERA5) or ``fetch_mrms_qpe`` (radar QPE for precipitation).
      - Fire-weather station observations outside IEM ASOS coverage — use
        ``fetch_raws_weather`` for RAWS (remote automated weather stations
        used by fire agencies, often in non-airport locations).
      - Tide gauge / coastal water-level observations — use
        ``fetch_noaa_coops_tides`` for NOAA CO-OPS.
      - Non-US regions — IEM ASOS archive covers US + territories only.

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            All IEM-archived ASOS stations whose coordinates fall inside this
            bbox are queried. Maximum ``_MAX_STATIONS`` (100) stations.
            Example for Fort Myers / Naples area:
            ``(-82.5, 25.8, -81.0, 27.5)``.
        start_time: Start of observation window as ISO-8601 date or datetime
            (e.g. ``"2024-09-26"`` or ``"2024-09-26T00:00:00Z"``).
            Defaults to 24 hours before ``end_time`` (or 24 hours before now
            when both are omitted). IEM archive extends back to the 1920s for
            many CONUS stations; typical recent data available with ~1-hour lag.
        end_time: End of observation window as ISO-8601 date or datetime
            (e.g. ``"2024-09-28"``). Defaults to current UTC time when omitted.
            Wide windows (> 30 days × many stations) may produce large payloads
            (> 25 MB); the payload-warning system will surface a confirmation
            prompt before fetching.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``gs://grace-2-hazard-prod-cache/cache/dynamic-1h/asos_metar/<key>.fgb``
        - ``layer_type="vector"``, ``role="context"``, ``units="mixed"``
          (temperature in °F, wind in knots, pressure in inHg/hPa,
          visibility in miles — standard ASOS/METAR units).
        - Geometry: Point at each ASOS station's coordinates, EPSG:4326.
        - Properties per observation: ``station`` (ICAO/FAA id),
          ``valid`` (UTC observation time ISO-8601), ``lon``, ``lat``,
          ``elevation`` (ft), ``tmpf`` (°F), ``dwpf`` (°F), ``sknt``
          (wind kt), ``drct`` (wind dir °), ``gust`` (gust kt), ``alti``
          (altimeter inHg), ``mslp`` (MSLP hPa), ``vsby`` (visibility mi),
          ``wxcodes`` (present weather), ``skyc1`` (sky cover), ``skyl1``
          (sky layer height ft). Missing values are ``null``.

    Cache: ``dynamic-1h`` — identical ``(bbox-4dp, start_time, end_time)`` calls
    within the same UTC hour reuse the cached FlatGeobuf.

    Cross-tool dependencies:
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (bbox derivation from place name), ``aggregate_claims_across_sources``
          (wind/pressure claims from ASOS for FR-HEP consensus).
        - Complements: ``fetch_era5_reanalysis`` (gridded global reanalysis),
          ``fetch_mrms_qpe`` (radar precipitation), ``fetch_goes_satellite``
          (satellite imagery), ``fetch_raws_weather`` (fire-weather stations).
        - Superseded for current NWS alerts: ``fetch_nws_alerts_conus``,
          ``fetch_nws_event``.
        - Upstream: IEM ASOS CGI + per-state ASOS network GeoJSON
          (mesonet.agron.iastate.edu).

    Errors (FR-AS-11 typed-error surface):
        - ``ASASMETARInputError``: invalid bbox or date strings (retryable=False).
        - ``ASASMETARUpstreamError``: IEM network failure or malformed CSV
          (retryable=True).
        - ``ASASMETAREmptyError``: no ASOS stations in bbox or all observations
          missing coordinates (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (federal-network stations archived by IEM;
    ASOS is operated by FAA/NWS). Claims from ASOS observations should be
    marked ``source_authority_tier=1`` in ``ClaimSet`` aggregation.

    supports_global_query=False — IEM ASOS archive covers US + territories only.
    """
    # 1. Validate and normalize inputs.
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise ASASMETARInputError(
            f"bbox must be a 4-element tuple (min_lon, min_lat, max_lon, max_lat); "
            f"got {bbox!r}"
        )
    bbox = tuple(float(v) for v in bbox)  # type: ignore[assignment]
    _validate_bbox(bbox)  # type: ignore[arg-type]

    # Normalize time window.
    now_utc = datetime.now(timezone.utc)
    if end_time is None:
        end_dt = now_utc
    else:
        end_dt = _parse_datetime(end_time, "end_time")

    if start_time is None:
        start_dt = end_dt - timedelta(hours=24)
    else:
        start_dt = _parse_datetime(start_time, "start_time")

    # IEM does not serve data more than a few years into the future; cap at now.
    if start_dt > now_utc:
        raise ASASMETARInputError(
            f"start_time={start_dt.isoformat()} is in the future; "
            "ASOS is an observational archive (no forecasts)"
        )

    if start_dt >= end_dt:
        raise ASASMETARInputError(
            f"start_time={start_dt.isoformat()} must be before "
            f"end_time={end_dt.isoformat()}"
        )

    # Round bbox to 4 dp for stable cache keying.
    bbox_r = tuple(round(v, 4) for v in bbox)  # type: ignore[assignment]

    # Normalize date strings to ISO-8601 for cache keying.
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 2. Build cache params.
    params: dict[str, Any] = {
        "bbox": list(bbox_r),
        "start_time": start_iso,
        "end_time": end_iso,
    }

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_asos_metar_bytes(
            bbox_r,  # type: ignore[arg-type]
            start_dt,
            end_dt,
            _DEFAULT_DATA_FIELDS,
        ),
    )
    assert result.uri is not None, (
        "fetch_asos_metar is cacheable; uri must be set by read_through"
    )

    # 4. Build descriptive layer name.
    bbox_tag = (
        f"{bbox_r[0]:.2f},{bbox_r[1]:.2f}→{bbox_r[2]:.2f},{bbox_r[3]:.2f}"
    )
    date_tag = start_iso[:10]
    if start_iso[:10] != end_iso[:10]:
        date_tag += f"–{end_iso[:10]}"

    # Stable layer_id seed.
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"asos-metar-{seed}",
        name=f"ASOS/METAR observations — {bbox_tag} ({date_tag})",
        layer_type="vector",
        uri=result.uri,
        style_preset="asos_metar",
        role="context",
        units="mixed",
        bbox=bbox_r,
    )
