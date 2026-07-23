"""``fetch_raws_weather`` atomic tool — Iowa Mesonet RAWS fire-weather stations (job-A12).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_raws_weather",
    "RAWSWeatherError",
    "RAWSWeatherInputError",
    "RAWSWeatherUpstreamError",
    "RAWSWeatherEmptyError",
    "estimate_payload_mb",
    "_discover_raws_stations_in_bbox",
    "_fetch_raws_obs_for_station_date",
    "_build_raws_fgb",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.weather.fetch_raws_weather")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class RAWSWeatherError(RuntimeError):
    """Base class for fetch_raws_weather failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "RAWS_WEATHER_ERROR"
    retryable: bool = True


class RAWSWeatherInputError(RAWSWeatherError):
    """Invalid inputs — bad bbox, out-of-range dates.

    Not retryable: the caller must fix the argument.
    """

    error_code = "RAWS_WEATHER_INPUT_ERROR"
    retryable = False


class RAWSWeatherUpstreamError(RAWSWeatherError):
    """IEM network request failed (HTTP error, connection reset, malformed JSON).

    Retryable — transient IEM outages recover on retry.
    """

    error_code = "RAWS_WEATHER_UPSTREAM_ERROR"
    retryable = True


class RAWSWeatherEmptyError(RAWSWeatherError):
    """No RAWS stations found in bbox, or all retrieved observations are empty.

    Not retryable — the bbox contains no IEM-archived RAWS stations for the
    requested period. Widen the bbox or choose a different date window.
    """

    error_code = "RAWS_WEATHER_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_IEM_NETWORK_GEOJSON = (
    "https://mesonet.agron.iastate.edu/geojson/network/{state}_DCP.geojson"
)
_IEM_OBHISTORY_URL = (
    "https://mesonet.agron.iastate.edu/api/1/obhistory.json"
    "?station={station}&network={state}_DCP&date={date}"
)

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# HTTP timeouts (seconds).
_NETWORK_TIMEOUT = 30.0
_DATA_TIMEOUT = 60.0

# Inter-request delay for per-station/per-date calls to avoid IEM rate limits.
_STATION_REQUEST_DELAY = 0.1  # seconds

# Max RAWS stations to query per call (prevents oversized fan-out).
_MAX_STATIONS = 50

# Max date range for a single call.
_MAX_DATE_RANGE_DAYS = 14

# RAWS stations in IEM DCP networks are identified by this substring in sname.
_RAWS_NAME_MARKER = "RAWS"

# CONUS + territories state codes that have IEM _DCP networks.
# Used to select which network GeoJSONs to fetch when the bbox spans states.
_IEM_DCP_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR",
)

# Rough state bounding boxes (lon_min, lat_min, lon_max, lat_max) — intentionally
# generous (~0.5° pad) so no RAWS-dense border area is silently missed.
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
}

# FlatGeobuf output columns (subset of IEM obhistory fields that are most
# useful for fire-weather analysis and hazard-model context).
_FGB_COLUMNS = (
    "station", "station_name", "state", "utc_valid",
    "lon", "lat", "elevation",
    "tmpf",       # air temperature °F
    "dwpf",       # dewpoint °F
    "relh",       # relative humidity % (from URHRGZZ SHEF code)
    "sknt",       # wind speed knots
    "drct",       # wind direction degrees
    "gust",       # wind gust knots (VBIRGZZ max)
    "solar_rad",  # solar radiation W/m² (from XRIRGZZ SHEF code)
    "precip_in",  # precipitation inches (from PCIRGZZ SHEF code)
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Build AtomicToolMetadata defensively to handle schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_raws_weather",
        ttl_class="dynamic-1h",
        source_class="raws_weather",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not yet support supports_global_query; "
            "registering fetch_raws_weather without it"
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

    RAWS observations are small (~150-200 bytes per row per station per hour).
    RAWS typically report at 10-60 min intervals; ~24 obs per station per day.
    Typical estimates:
    - 5 stations × 1 day ≈ 120 obs × 175 B ≈ 21 KB
    - 20 stations × 7 days ≈ 3,360 obs × 175 B ≈ 0.59 MB
    - 50 stations × 14 days ≈ 16,800 obs × 175 B ≈ 2.9 MB

    The estimate is conservative (uses bbox area to guess station count).
    """
    n_stations = 5  # default
    n_obs = 24      # default obs per station per day
    n_days = 1

    if bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # RAWS density in fire-prone CONUS: ~0.5-2 per 1° square.
            n_stations = max(1, min(_MAX_STATIONS, int(sq_deg * 1.0)))
        except (TypeError, ValueError):
            pass

    if start_time is not None and end_time is not None:
        try:
            from datetime import datetime as _dt
            fmt = "%Y-%m-%d"
            dt_start = _dt.strptime(start_time[:10], fmt)
            dt_end = _dt.strptime(end_time[:10], fmt)
            n_days = max(1, (dt_end - dt_start).days + 1)
        except (ValueError, TypeError):
            pass

    total_obs = n_stations * n_obs * n_days
    return max(0.001, total_obs * 175 / 1_000_000)


# ---------------------------------------------------------------------------
# HTTP helpers.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _NETWORK_TIMEOUT) -> bytes:
    """HTTP GET. Raises ``RAWSWeatherUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RAWSWeatherUpstreamError(
            f"IEM returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RAWSWeatherUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise RAWSWeatherUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# Station discovery.
# ---------------------------------------------------------------------------


def _bbox_overlaps_state(
    bbox: tuple[float, float, float, float],
    state_bbox: tuple[float, float, float, float],
) -> bool:
    """Return True if ``bbox`` overlaps ``state_bbox``."""
    w1, s1, e1, n1 = bbox
    w2, s2, e2, n2 = state_bbox
    return not (e1 < w2 or w1 > e2 or n1 < s2 or s1 > n2)


def _discover_raws_stations_in_bbox(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Find all IEM-archived RAWS stations whose coordinates fall inside ``bbox``.

    Fetches the per-state DCP network GeoJSON for each state that overlaps the
    bbox, then filters features to only those with "RAWS" in the station name.

    Returns:
        List of station dicts with keys: ``sid``, ``lon``, ``lat``, ``sname``,
        ``state``, ``elevation``, ``network``.

    Raises:
        ``RAWSWeatherUpstreamError`` — network failure fetching station metadata.
        ``RAWSWeatherEmptyError`` — no RAWS stations found in bbox.
    """
    west, south, east, north = bbox
    stations: list[dict[str, Any]] = []
    seen: set[str] = set()

    for state in _IEM_DCP_STATES:
        state_box = _STATE_BBOX.get(state)
        if state_box is None:
            continue
        if not _bbox_overlaps_state(bbox, state_box):
            continue

        url = _IEM_NETWORK_GEOJSON.format(state=state)
        try:
            raw = _http_get(url, timeout=_NETWORK_TIMEOUT)
        except RAWSWeatherUpstreamError as exc:
            logger.warning(
                "fetch_raws_weather: failed to fetch %s DCP network: %s", state, exc
            )
            continue

        try:
            geojson = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "fetch_raws_weather: malformed GeoJSON for %s_DCP: %s", state, exc
            )
            continue

        for feat in geojson.get("features", []):
            coords = (feat.get("geometry") or {}).get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (west <= lon <= east and south <= lat <= north):
                continue

            props = feat.get("properties") or {}
            sname = props.get("sname", "")
            # Only keep RAWS stations (identified by "RAWS" in station name).
            if _RAWS_NAME_MARKER not in sname.upper():
                continue

            sid = feat.get("id") or props.get("sid")
            if not sid or sid in seen:
                continue
            seen.add(sid)

            stations.append({
                "sid": str(sid),
                "lon": lon,
                "lat": lat,
                "sname": sname,
                "state": state,
                "elevation": props.get("elevation"),
                "network": f"{state}_DCP",
            })

        if len(stations) >= _MAX_STATIONS:
            logger.info(
                "fetch_raws_weather: station cap (%d) reached during discovery",
                _MAX_STATIONS,
            )
            stations = stations[:_MAX_STATIONS]
            break

    return stations


# ---------------------------------------------------------------------------
# IEM obhistory observation fetch.
# ---------------------------------------------------------------------------


def _fetch_raws_obs_for_station_date(
    station_id: str,
    network: str,
    obs_date: _date,
) -> list[dict[str, Any]]:
    """Fetch one day of RAWS observations from IEM obhistory API.

    Returns a list of observation dicts (may be empty for offline stations).
    Raises ``RAWSWeatherUpstreamError`` on network/parse failure.
    """
    date_str = obs_date.strftime("%Y-%m-%d")
    url = _IEM_OBHISTORY_URL.format(
        station=station_id, state=network.replace("_DCP", ""), date=date_str
    )
    logger.debug(
        "fetch_raws_weather: GET %s / %s @ %s", station_id, network, date_str
    )
    try:
        raw = _http_get(url, timeout=_DATA_TIMEOUT)
    except RAWSWeatherUpstreamError:
        raise

    try:
        resp_json = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RAWSWeatherUpstreamError(
            f"Malformed JSON from IEM obhistory for {station_id}: {exc}"
        ) from exc

    return resp_json.get("data", [])


# ---------------------------------------------------------------------------
# Build FlatGeobuf from collected observations.
# ---------------------------------------------------------------------------


def _build_raws_fgb(
    rows: list[dict[str, Any]],
) -> bytes:
    """Serialize a list of observation dicts to FlatGeobuf bytes.

    Each row must already have ``lon``, ``lat``, ``station``, and the
    standard RAWS fields mapped from IEM obhistory JSON.

    Raises:
        ``RAWSWeatherUpstreamError`` — geopandas / shapely not available or
          FlatGeobuf write failed.
        ``RAWSWeatherEmptyError`` — no rows with valid coordinates.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RAWSWeatherUpstreamError(
            f"geopandas / pandas / shapely not available: {exc}"
        ) from exc

    if not rows:
        raise RAWSWeatherEmptyError(
            "No RAWS observations collected for any station in the bbox/window"
        )

    df = pd.DataFrame(rows)

    # Drop rows missing coordinates.
    df = df.dropna(subset=["lon", "lat"]).copy()
    if df.empty:
        raise RAWSWeatherEmptyError(
            "All RAWS observation rows lack valid coordinates"
        )

    # WGS84 sanity clip.
    df = df[
        (df["lon"].between(-180.0, 180.0))
        & (df["lat"].between(-90.0, 90.0))
    ].copy()
    if df.empty:
        raise RAWSWeatherEmptyError(
            "All RAWS observation rows have out-of-range coordinates"
        )

    # Keep only the columns we expose downstream; add any missing with None.
    for col in _FGB_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df_out = df[[c for c in _FGB_COLUMNS if c in df.columns]].copy()

    # Numeric coercions.
    for col in ("tmpf", "dwpf", "relh", "sknt", "drct", "gust",
                "solar_rad", "precip_in", "elevation"):
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors="coerce")

    # Build GeoDataFrame.
    geom = [Point(lon, lat) for lon, lat in zip(df["lon"], df["lat"])]
    gdf = gpd.GeoDataFrame(df_out, geometry=geom, crs="EPSG:4326")

    logger.info(
        "fetch_raws_weather: %d observations from %d station(s)",
        len(gdf),
        gdf["station"].nunique() if "station" in gdf.columns else -1,
    )

    # Serialize to FlatGeobuf.
    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_raws_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:
            raise RAWSWeatherUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} RAWS observations: {exc}"
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
# Input validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise RAWSWeatherInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    w, s, e, n = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise RAWSWeatherInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= w <= 180.0 and -180.0 <= e <= 180.0):
        raise RAWSWeatherInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= s <= 90.0 and -90.0 <= n <= 90.0):
        raise RAWSWeatherInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if w >= e or s >= n:
        raise RAWSWeatherInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _parse_date(s: str, field_name: str) -> _date:
    """Parse ISO-8601 date string to a ``datetime.date``. Accepts ``YYYY-MM-DD``
    or ``YYYY-MM-DDTHH:MM:SSZ`` (truncates to date)."""
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
    raise RAWSWeatherInputError(
        f"{field_name}={s!r} is not a parseable date; use YYYY-MM-DD"
    )


# ---------------------------------------------------------------------------
# Core bytes fetch (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_raws_bytes(
    bbox: tuple[float, float, float, float],
    start_date: _date,
    end_date: _date,
) -> bytes:
    """End-to-end: discover RAWS → collect observations → FlatGeobuf."""
    stations = _discover_raws_stations_in_bbox(bbox)
    if not stations:
        raise RAWSWeatherEmptyError(
            f"No IEM-archived RAWS stations found inside bbox={bbox}; "
            "RAWS coverage is heaviest in the western US fire belt"
        )

    logger.info(
        "fetch_raws_weather: discovered %d RAWS station(s): %s",
        len(stations),
        [s["sid"] for s in stations[:10]],
    )

    # Enumerate dates in [start_date, end_date].
    n_days = (end_date - start_date).days + 1
    all_dates = [start_date + timedelta(days=i) for i in range(n_days)]

    # Build a lookup for station metadata keyed by sid.
    station_meta = {s["sid"]: s for s in stations}

    rows: list[dict[str, Any]] = []
    for station in stations:
        sid = station["sid"]
        network = station["network"]
        for obs_date in all_dates:
            try:
                obs_list = _fetch_raws_obs_for_station_date(sid, network, obs_date)
            except RAWSWeatherUpstreamError as exc:
                logger.warning(
                    "fetch_raws_weather: skipping %s on %s: %s",
                    sid, obs_date, exc,
                )
                continue

            for obs in obs_list:
                # Map IEM SHEF codes to fire-weather field names.
                row: dict[str, Any] = {
                    "station": sid,
                    "station_name": station.get("sname", ""),
                    "state": station.get("state", ""),
                    "utc_valid": obs.get("utc_valid"),
                    "lon": station["lon"],
                    "lat": station["lat"],
                    "elevation": station.get("elevation"),
                    "tmpf": obs.get("tmpf"),
                    "dwpf": obs.get("dwpf"),
                    # URHRGZZ = instantaneous relative humidity %
                    "relh": obs.get("URHRGZZ"),
                    "sknt": obs.get("sknt"),
                    "drct": obs.get("drct"),
                    # VBIRGZZ = wind resultant magnitude (proxy for gust)
                    "gust": obs.get("VBIRGZZ"),
                    # XRIRGZZ = solar radiation W/m²
                    "solar_rad": obs.get("XRIRGZZ"),
                    # PCIRGZZ = precipitation (inches, cumulative)
                    "precip_in": obs.get("PCIRGZZ"),
                }
                rows.append(row)

            time.sleep(_STATION_REQUEST_DELAY)

    return _build_raws_fgb(rows)


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
def fetch_raws_weather(
    bbox: tuple[float, float, float, float],
    start_time: str | None = None,
    end_time: str | None = None,
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch RAWS fire-weather station observations as a point FlatGeobuf.

    **What it does:** Retrieves sub-hourly observations from Remote Automated
    Weather Stations (RAWS) — fire-weather monitoring stations operated by the
    US Forest Service, BLM, NPS, BIA, and state forestry agencies — sourced
    from the Iowa State University Iowa Environmental Mesonet (IEM) DCP network
    archive. RAWS are sited specifically at fire-prone ridges, canyons, and
    forest margins and report the fire-weather parameters most relevant to
    fire behavior: temperature, relative humidity, wind speed/direction, solar
    radiation, and precipitation. Returns a FlatGeobuf point layer with one
    feature per observation per station. Tier-1 free, no API key.

    **When to use:**
      - User asks about fire-weather conditions at or near a wildfire, fire
        perimeter, or fire-prone area (e.g., "what were the wind and RH
        conditions near the Caldor Fire on Aug 18?", "show me fire-weather
        stations in the Angeles National Forest").
      - Providing fire-weather forcing (wind, RH, temp) for fire-behavior
        model inputs (FARSITE, BEHAVE+, WindNinja wind fields).
      - Wildfire risk context: overlaying current or historical RAWS
        observations on active fire perimeters or MTBS burn severity maps.
      - Computing fire-weather indices (FFMC, DMC, DC, ISI, FWI, BUI) from
        observed temp, RH, wind, and rain inputs.
      - User asks for fire-weather station data, RAWS observations, or
        NFDRS/NWS fire-weather products at a specific location.

    **When NOT to use:**
      - Surface weather at airports — use ``fetch_asos_metar`` (ASOS/METAR
        network, broader coverage, available for non-fire-weather use cases).
      - Gridded weather analysis or reanalysis — use ``fetch_era5_reanalysis``
        (global) or ``fetch_gridmet`` (CONUS daily 4 km; includes fire-weather
        variables like ERC and BI derived from NFDRS).
      - Active fire detections / fire radiative power — use
        ``fetch_firms_active_fire`` (NASA VIIRS/MODIS thermal anomalies).
      - Fire perimeter polygons — use ``fetch_nifc_fire_perimeters`` (NIFC
        current perimeters) or ``fetch_mtbs_burn_severity`` (historical).
      - Locations outside the western/southern US fire belt — RAWS coverage
        is sparse east of the Mississippi and non-existent outside the US.
        Use ``fetch_asos_metar`` or ``fetch_era5_reanalysis`` instead.

    **Parameters:**

        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            All IEM-archived RAWS stations within this bbox are queried.
            Maximum ``_MAX_STATIONS`` (50) stations; tool warns and truncates
            if the bbox contains more.
            Example for the Caldor Fire area (Sierra Nevada, CA):
            ``(-121.0, 38.5, -119.5, 39.5)``.
        start_time: Start date as ISO-8601 string (``"YYYY-MM-DD"`` or
            ``"YYYY-MM-DDTHH:MM:SSZ"`` — time component is ignored; the tool
            requests full calendar days). Defaults to 1 day before ``end_time``
            (or 1 day before today when both are omitted). RAWS data at IEM
            extends back to the early 2000s for most western US stations.
        end_time: End date as ISO-8601 string. Defaults to today (UTC).
            Maximum window is ``_MAX_DATE_RANGE_DAYS`` (14 days) per call.
            Wider historical windows should be requested in separate calls
            to avoid large per-station fan-out.

    **Returns:**

        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/dynamic-1h/raws_weather/<key>.fgb``
        - ``layer_type="vector"``, ``role="context"``, ``units="mixed"``
          (temperature in °F, wind in knots, RH in %, solar in W/m²,
          precip in inches — standard RAWS/NFDRS units).
        - Geometry: Point at each RAWS station's coordinates, EPSG:4326.
        - Properties per observation: ``station`` (NWSLI/IEM ID),
          ``station_name``, ``state``, ``utc_valid`` (ISO-8601),
          ``lon``, ``lat``, ``elevation`` (m), ``tmpf`` (°F), ``dwpf`` (°F),
          ``relh`` (RH %), ``sknt`` (wind speed kt), ``drct`` (wind dir °),
          ``gust`` (wind resultant kt), ``solar_rad`` (W/m²),
          ``precip_in`` (in). Missing values are ``null``.

    Cache: ``dynamic-1h`` — identical ``(bbox-4dp, start_date, end_date)``
    calls within the same UTC hour reuse the cached FlatGeobuf.

    **Cross-tool dependencies:**

        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (bbox from place name), ``fetch_nifc_fire_perimeters``
          (co-overlay fire perimeter + weather), ``fetch_firms_active_fire``
          (co-overlay thermal anomalies + RAWS obs).
        - Feeds INTO: ``aggregate_claims_across_sources`` (wind/RH claims for
          FR-HEP wildfire event consensus), fire-weather index computation
          (NFDRS/FWI pipeline — future tool).
        - Complements: ``fetch_asos_metar`` (airport weather, no fire-weather
          specialization), ``fetch_gridmet`` (gridded daily fire-weather),
          ``fetch_landfire_fuels`` (fuel moisture context).
        - Upstream: IEM DCP network GeoJSON + obhistory API
          (mesonet.agron.iastate.edu).

    Errors:
        - ``RAWSWeatherInputError``: bad bbox or dates (retryable=False).
        - ``RAWSWeatherUpstreamError``: IEM network or parse failure
          (retryable=True).
        - ``RAWSWeatherEmptyError``: no RAWS stations in bbox or no
          observations for the period (retryable=False).


    supports_global_query=False — IEM RAWS archive covers US + territories
    only; coverage is concentrated in the western US fire belt.
    """
    # 1. Validate and normalize inputs.
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise RAWSWeatherInputError(
            f"bbox must be a 4-element tuple (min_lon, min_lat, max_lon, max_lat); "
            f"got {bbox!r}"
        )
    bbox = tuple(float(v) for v in bbox)  # type: ignore[assignment]
    _validate_bbox(bbox)  # type: ignore[arg-type]

    today_utc = datetime.now(timezone.utc).date()

    if end_time is None:
        end_date = today_utc
    else:
        end_date = _parse_date(end_time, "end_time")

    if start_time is None:
        start_date = end_date - timedelta(days=1)
    else:
        start_date = _parse_date(start_time, "start_time")

    if start_date > today_utc:
        raise RAWSWeatherInputError(
            f"start_time={start_date} is in the future; "
            "RAWS is an observational archive (no forecasts)"
        )

    if start_date > end_date:
        raise RAWSWeatherInputError(
            f"start_time={start_date} must be on or before end_time={end_date}"
        )

    n_days = (end_date - start_date).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise RAWSWeatherInputError(
            f"Date range of {n_days} days exceeds maximum ({_MAX_DATE_RANGE_DAYS}); "
            "split into smaller windows or reduce the date range"
        )

    # Round bbox to 4 dp for stable cache keying.
    bbox_r = tuple(round(v, 4) for v in bbox)  # type: ignore[assignment]

    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    # 2. Build cache params.
    params: dict[str, Any] = {
        "bbox": list(bbox_r),
        "start_date": start_iso,
        "end_date": end_iso,
    }

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_raws_bytes(
            bbox_r,  # type: ignore[arg-type]
            start_date,
            end_date,
        ),
    )
    assert result.uri is not None, (
        "fetch_raws_weather is cacheable; uri must be set by read_through"
    )

    # 4. Build descriptive layer name.
    bbox_tag = (
        f"{bbox_r[0]:.2f},{bbox_r[1]:.2f}→{bbox_r[2]:.2f},{bbox_r[3]:.2f}"
    )
    date_tag = start_iso
    if start_iso != end_iso:
        date_tag += f"–{end_iso}"

    # Stable layer_id seed.
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"raws-weather-{seed}",
        name=f"RAWS fire-weather — {bbox_tag} ({date_tag})",
        layer_type="vector",
        uri=result.uri,
        style_preset="raws_weather",
        role="context",
        units="mixed",
        bbox=bbox_r,
    )
