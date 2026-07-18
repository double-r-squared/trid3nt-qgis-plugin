"""``fetch_usgs_earthquakes`` atomic tool — real USGS seismic events as points.

Queries the USGS FDSN Event Web Service (the same machine API behind the USGS
"Latest Earthquakes" map) for recorded earthquakes inside a bbox and time
window, above a minimum magnitude. Returns one Point feature per event at the
epicenter, carrying ``mag``, ``depth_km``, ``time``, ``place`` and the USGS
event-page ``url``. This is the canonical OBSERVED seismic-event source — the
instrument/network record of what actually happened, NOT a probabilistic hazard
model.

**API surface** (USGS FDSN Event, free, NO API key required):

    https://earthquake.usgs.gov/fdsnws/event/1/query
        ?format=geojson
        &minlongitude=...&minlatitude=...&maxlongitude=...&maxlatitude=...
        &starttime=YYYY-MM-DDTHH:MM:SS&endtime=YYYY-MM-DDTHH:MM:SS
        &minmagnitude=...
        &orderby=time&limit=20000

The response is a GeoJSON ``FeatureCollection``. Each feature is a Point whose
geometry ``coordinates`` are ``[longitude, latitude, depth_km]`` (the THIRD
coordinate is event depth in kilometers). Per-feature ``properties`` carry
``mag`` (magnitude), ``magType`` (e.g. ``"md"``, ``"ml"``, ``"mw"``),
``place`` (a human "5 km SW of Ridgemark, CA" string), ``time`` and ``updated``
(epoch MILLISECONDS UTC), ``url`` (the USGS event page), ``type`` (usually
``"earthquake"``; can be ``"quarry blast"`` etc.), ``status``, ``tsunami``,
``felt``, ``sig`` and ``net``.

**Window semantics**: when ``start_date`` / ``end_date`` are omitted the tool
defaults to the most-recent ~30 days (the same window the USGS "Significant
Earthquakes, Past 30 Days" feed uses). A window is capped at 366 days so a
single call stays a bounded payload; a longer span should be chunked.

**Result-cap handling**: the FDSN service caps a single response at 20000
events. We request ``limit=20000`` and inspect ``metadata.count``; if the cap is
hit we raise a typed ``EarthquakesResultTooLargeError`` telling the caller to
narrow the bbox, shorten the window, or raise the minimum magnitude — we never
silently truncate.

**Honest-empty path** (data-source fallback norm — primary -> honest typed
error): the FDSN service returns an HTTP 200 ``FeatureCollection`` with ZERO
features for a quiescent area/window. That is a legitimate "no earthquakes"
answer, not an error condition, so we raise a typed
``EarthquakesNoEventsError`` (retryable=False) carrying the scope — never an
empty success-shaped layer.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
point FeatureCollection (one point per event), serialized as FlatGeobuf and
rendered via the inline vector path. ``style_preset="earthquakes"`` (the client
sizes the marker by ``mag`` and colors by ``depth_km``); ``LayerURI.bbox`` is
set to the events' extent so the camera auto-zooms.

Tier-1, no auth, ``supports_global_query=True`` (the FDSN service is global; a
bbox-less call covers the whole planet, bounded by the magnitude floor + window
+ the 20000 result cap).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_usgs_earthquakes",
    "estimate_payload_mb",
    "EarthquakesError",
    "EarthquakesInputError",
    "EarthquakesResultTooLargeError",
    "EarthquakesUpstreamError",
    "EarthquakesNoEventsError",
    "_validate_bbox",
    "_resolve_window",
    "_round_bbox_to_6dp",
    "_build_query_url",
    "_parse_event_geojson",
    "_events_bbox",
    "_build_flatgeobuf",
    "_fetch_usgs_earthquakes_bytes",
    "FDSN_EVENT_URL",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "FDSN_RESULT_LIMIT",
]

logger = logging.getLogger("grace2_agent.tools.fetch_usgs_earthquakes")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class EarthquakesError(RuntimeError):
    """Base class for fetch_usgs_earthquakes failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "USGS_EARTHQUAKES_ERROR"
    retryable: bool = True


class EarthquakesInputError(EarthquakesError):
    """Invalid inputs — bad bbox shape, bad date, reversed window, bad magnitude.

    Not retryable: the caller must fix the argument.
    """

    error_code = "USGS_EARTHQUAKES_INPUT_ERROR"
    retryable = False


class EarthquakesResultTooLargeError(EarthquakesError):
    """The query would return more than the FDSN 20000-event result cap.

    Not retryable as-is. The caller should narrow the bbox, shorten the time
    window, or raise the minimum magnitude.
    """

    error_code = "USGS_EARTHQUAKES_RESULT_TOO_LARGE"
    retryable = False


class EarthquakesUpstreamError(EarthquakesError):
    """USGS FDSN request failed (network error, HTTP 5xx, bad body).

    Retryable — transient USGS outages recover on retry.
    """

    error_code = "USGS_EARTHQUAKES_UPSTREAM_ERROR"
    retryable = True


class EarthquakesNoEventsError(EarthquakesError):
    """No earthquakes matched the bbox / window / magnitude floor.

    Not retryable — the area was seismically quiet over that window above that
    magnitude. Either widen the window, lower the minimum magnitude, or pick a
    more seismically active area. We never return an empty success-shaped layer.
    """

    error_code = "USGS_EARTHQUAKES_NO_EVENTS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: USGS FDSN Event Web Service query endpoint.
FDSN_EVENT_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

#: Default look-back window (days) when no explicit start/end is supplied.
DEFAULT_WINDOW_DAYS = 30

#: Maximum window (days). A single FDSN call should stay a bounded payload;
#: a wider span should be chunked by the caller.
MAX_WINDOW_DAYS = 366

#: FDSN single-response result cap. The service returns HTTP 400 if a query
#: would exceed this; we request it explicitly and detect the cap via
#: ``metadata.count`` so we can raise a precise typed error.
FDSN_RESULT_LIMIT = 20000

#: User-Agent per USGS usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: HTTP timeout (seconds). FDSN is generally fast; a wide window can be slow.
_HTTP_TIMEOUT = 90.0

#: USGS encodes a missing magnitude / depth in some catalogs; guard for None.
#: (No sentinel like -999 is used by FDSN GeoJSON; values are simply absent.)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_usgs_earthquakes",
        ttl_class="dynamic-1h",
        source_class="usgs_earthquakes",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(  # type: ignore[call-arg]
            **common,
            supports_global_query=True,
            payload_mb_estimator_name="estimate_payload_mb",
        )
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support all Wave-1.5 flags; "
            "registering fetch_usgs_earthquakes without them"
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
    min_magnitude: float | None = None,
    **_kw: Any,
) -> float:
    """Estimate the output FlatGeobuf size in MB.

    Each event is one Point feature with a handful of small scalar properties
    (~200 bytes serialized). Event density scales with area, window length, and
    inversely with the magnitude floor. The estimate is intentionally
    conservative; earthquake point layers are small (the FDSN result cap of
    20000 events bounds a single response at roughly ~4 MB).
    """
    # Window length (days).
    try:
        window = _resolve_window(start_date, end_date)
        d0 = _dt.datetime.fromisoformat(window[0].replace("Z", "+00:00"))
        d1 = _dt.datetime.fromisoformat(window[1].replace("Z", "+00:00"))
        n_days = max(1.0, (d1 - d0).total_seconds() / 86400.0)
    except Exception:
        n_days = float(DEFAULT_WINDOW_DAYS)

    # Area (sq deg). A bbox-less (global) query covers the whole sphere.
    if bbox is None:
        area_sq_deg = 64800.0  # 360 * 180
    else:
        try:
            west, south, east, north = (float(v) for v in bbox)
            area_sq_deg = max(0.0, east - west) * max(0.0, north - south)
        except (TypeError, ValueError):
            area_sq_deg = 100.0

    # Magnitude floor: each +1 magnitude is ~10x fewer events (Gutenberg-Richter).
    mmag = 2.5 if min_magnitude is None else float(min_magnitude)
    mag_factor = 10.0 ** max(0.0, (2.5 - mmag))

    # ~0.002 events per sq-deg per day at mag>=2.5 in a seismically average region.
    n_events = area_sq_deg * n_days * 0.002 * mag_factor
    n_events = min(float(FDSN_RESULT_LIMIT), max(0.0, n_events))
    return max(0.001, n_events * 200 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation + window resolution.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``EarthquakesInputError`` if the bbox is malformed or out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise EarthquakesInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise EarthquakesInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise EarthquakesInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise EarthquakesInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise EarthquakesInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _resolve_window(
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    """Resolve the (starttime, endtime) FDSN window as ISO-8601 UTC strings.

    Both omitted -> the most-recent ``DEFAULT_WINDOW_DAYS`` (~30) days ending
    now. Accepts ISO ``YYYY-MM-DD`` (date) or full ``YYYY-MM-DDTHH:MM:SS``
    forms. Raises ``EarthquakesInputError`` on a malformed date, a reversed
    range, or a span beyond ``MAX_WINDOW_DAYS``.

    Returns ``(starttime_iso, endtime_iso)`` — second-precision UTC strings the
    FDSN service accepts verbatim.
    """
    now = _dt.datetime.now(_dt.timezone.utc)

    def _parse(s: str, *, is_end: bool) -> _dt.datetime:
        raw = str(s).strip()
        try:
            dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            # Bare date (YYYY-MM-DD) -> midnight (start) or end-of-day (end).
            try:
                d = _dt.date.fromisoformat(raw)
            except ValueError:
                raise EarthquakesInputError(
                    f"{'end_date' if is_end else 'start_date'}={s!r} is not a "
                    f"valid ISO date/datetime (YYYY-MM-DD or "
                    f"YYYY-MM-DDTHH:MM:SS): {exc}"
                ) from exc
            t = _dt.time(23, 59, 59) if is_end else _dt.time(0, 0, 0)
            dt = _dt.datetime.combine(d, t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc)

    if start_date is None and end_date is None:
        end = now
        start = now - _dt.timedelta(days=DEFAULT_WINDOW_DAYS)
    elif start_date is not None and end_date is None:
        start = _parse(start_date, is_end=False)
        end = now
    elif start_date is None and end_date is not None:
        end = _parse(end_date, is_end=True)
        start = end - _dt.timedelta(days=DEFAULT_WINDOW_DAYS)
    else:
        start = _parse(start_date, is_end=False)  # type: ignore[arg-type]
        end = _parse(end_date, is_end=True)  # type: ignore[arg-type]

    if start > end:
        raise EarthquakesInputError(
            f"start_date must be <= end_date; got start={start.isoformat()}, "
            f"end={end.isoformat()}"
        )
    span_days = (end - start).total_seconds() / 86400.0
    if span_days > MAX_WINDOW_DAYS:
        raise EarthquakesInputError(
            f"time window {span_days:.0f} days exceeds the {MAX_WINDOW_DAYS}-day "
            f"cap; request a shorter window or call in chunks"
        )

    fmt = "%Y-%m-%dT%H:%M:%S"
    return (start.strftime(fmt), end.strftime(fmt))


def _validate_min_magnitude(min_magnitude: float | None) -> float | None:
    """Validate the magnitude floor. ``None`` -> no floor (FDSN default)."""
    if min_magnitude is None:
        return None
    try:
        m = float(min_magnitude)
    except (TypeError, ValueError) as exc:
        raise EarthquakesInputError(
            f"min_magnitude must be numeric; got {min_magnitude!r}"
        ) from exc
    if not math.isfinite(m):
        raise EarthquakesInputError(
            f"min_magnitude must be finite; got {min_magnitude!r}"
        )
    # USGS magnitudes range roughly -1 .. 10; clamp to that physical envelope.
    if not (-2.0 <= m <= 12.0):
        raise EarthquakesInputError(
            f"min_magnitude={m} is outside the physical range [-2, 12]"
        )
    return m


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> tuple[bytes, int]:
    """Plain HTTP GET. Returns ``(body, status)``.

    Raises ``EarthquakesUpstreamError`` on network failure / HTTP 5xx. A 400
    (FDSN "bad request" — typically a too-large result set or an out-of-range
    parameter) and a 204 (FDSN "no content" — no events) are returned to the
    caller as ``(body, status)`` so the parse layer can map them to the right
    typed error instead of aborting.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), int(resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code in (204, 400, 404):
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            return body, int(exc.code)
        raise EarthquakesUpstreamError(
            f"USGS FDSN returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise EarthquakesUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise EarthquakesUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# URL builder.
# ---------------------------------------------------------------------------


def _build_query_url(
    *,
    bbox: tuple[float, float, float, float] | None,
    starttime: str,
    endtime: str,
    min_magnitude: float | None,
) -> str:
    """Build the FDSN Event query URL for a bbox + window + magnitude floor."""
    params: list[tuple[str, str]] = [
        ("format", "geojson"),
        ("starttime", starttime),
        ("endtime", endtime),
        ("orderby", "time"),
        ("limit", str(FDSN_RESULT_LIMIT)),
    ]
    if bbox is not None:
        west, south, east, north = bbox
        params.append(("minlongitude", repr(west)))
        params.append(("minlatitude", repr(south)))
        params.append(("maxlongitude", repr(east)))
        params.append(("maxlatitude", repr(north)))
    if min_magnitude is not None:
        params.append(("minmagnitude", repr(min_magnitude)))
    return FDSN_EVENT_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# GeoJSON parser.
# ---------------------------------------------------------------------------


def _epoch_ms_to_iso(ms: Any) -> str | None:
    """Convert FDSN epoch-milliseconds to an ISO-8601 UTC string, or None."""
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    try:
        dt = _dt.datetime.fromtimestamp(v / 1000.0, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_event_geojson(raw: bytes) -> tuple[list[dict[str, Any]], int | None]:
    """Parse the FDSN GeoJSON body -> (event records, metadata.count).

    Each ``features[]`` is one event. Geometry is a Point whose ``coordinates``
    are ``[lon, lat, depth_km]`` (the third coordinate is depth in km). We emit
    one record per event:

        {id, lon, lat, depth_km, mag, mag_type, place, time, updated, url,
         event_type, status, tsunami, felt, sig, net}

    Events with no parseable lon/lat are dropped. Returns
    ``([], count)`` for an empty FeatureCollection. ``count`` is
    ``metadata.count`` (the FDSN-reported event count) when present, else None.
    """
    if not raw:
        return [], None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EarthquakesUpstreamError(
            f"USGS FDSN response is not valid JSON: {exc}"
        ) from exc

    if not isinstance(obj, dict):
        raise EarthquakesUpstreamError(
            f"USGS FDSN response is not a JSON object: type={type(obj).__name__}"
        )
    if obj.get("type") != "FeatureCollection":
        raise EarthquakesUpstreamError(
            f"USGS FDSN response is not a GeoJSON FeatureCollection: "
            f"type={obj.get('type')!r}"
        )

    metadata = obj.get("metadata") or {}
    count = metadata.get("count")
    try:
        count_int: int | None = int(count) if count is not None else None
    except (TypeError, ValueError):
        count_int = None

    features = obj.get("features") or []
    records: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        depth_km: float | None = None
        if len(coords) >= 3:
            try:
                d = float(coords[2])
                if math.isfinite(d):
                    depth_km = d
            except (TypeError, ValueError):
                depth_km = None

        props = feat.get("properties") or {}
        mag: float | None = None
        try:
            mv = props.get("mag")
            if mv is not None:
                fm = float(mv)
                if math.isfinite(fm):
                    mag = fm
        except (TypeError, ValueError):
            mag = None

        records.append(
            {
                "id": str(feat.get("id") or "").strip(),
                "lon": lon,
                "lat": lat,
                "depth_km": depth_km,
                "mag": mag,
                "mag_type": str(props.get("magType") or "").strip() or None,
                "place": str(props.get("place") or "").strip() or None,
                "time": _epoch_ms_to_iso(props.get("time")),
                "updated": _epoch_ms_to_iso(props.get("updated")),
                "url": str(props.get("url") or "").strip() or None,
                "event_type": str(props.get("type") or "").strip() or None,
                "status": str(props.get("status") or "").strip() or None,
                "tsunami": int(props.get("tsunami") or 0),
                "felt": (
                    int(props["felt"])
                    if props.get("felt") not in (None, "")
                    else None
                ),
                "sig": (
                    int(props["sig"])
                    if props.get("sig") not in (None, "")
                    else None
                ),
                "net": str(props.get("net") or "").strip() or None,
            }
        )

    return records, count_int


# ---------------------------------------------------------------------------
# Extent + FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _events_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the event points.

    Pads a degenerate single-point extent by ~0.1 deg so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.1
        east += 0.1
    if south == north:
        south -= 0.1
        north += 0.1
    return (west, south, east, north)


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize event records -> FlatGeobuf bytes (Point geometry, EPSG:4326).

    One Point feature per event carrying ``id``, ``mag``, ``depth_km``,
    ``mag_type``, ``place``, ``time``, ``updated``, ``url``, ``event_type``,
    ``status``, ``tsunami``, ``felt``, ``sig``, ``net``.

    Raises ``EarthquakesUpstreamError`` if geopandas/shapely are unavailable or
    the write fails. ``records`` must be non-empty (the caller enforces the
    no-events honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EarthquakesUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "id": [str(r.get("id") or "") for r in records],
        "mag": [r.get("mag") for r in records],
        "depth_km": [r.get("depth_km") for r in records],
        "mag_type": [r.get("mag_type") for r in records],
        "place": [r.get("place") for r in records],
        "time": [r.get("time") for r in records],
        "updated": [r.get("updated") for r in records],
        "url": [r.get("url") for r in records],
        "event_type": [r.get("event_type") for r in records],
        "status": [r.get("status") for r in records],
        "tsunami": [int(r.get("tsunami") or 0) for r in records],
        "felt": [r.get("felt") for r in records],
        "sig": [r.get("sig") for r in records],
        "net": [r.get("net") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_eq_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise EarthquakesUpstreamError(
            f"FlatGeobuf write failed for {len(records)} earthquake events: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through). Returns (fgb_bytes, extent_bbox).
# ---------------------------------------------------------------------------


def _fetch_usgs_earthquakes_bytes(
    *,
    bbox: tuple[float, float, float, float] | None,
    starttime: str,
    endtime: str,
    min_magnitude: float | None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: FDSN GeoJSON -> records -> FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises:
      - ``EarthquakesResultTooLargeError`` when the FDSN result cap is hit.
      - ``EarthquakesNoEventsError`` when zero events match.
    """
    url = _build_query_url(
        bbox=bbox,
        starttime=starttime,
        endtime=endtime,
        min_magnitude=min_magnitude,
    )
    logger.info("fetch_usgs_earthquakes: GET %s", url)
    raw, status = _http_get(url)

    scope = (
        f"bbox={bbox!r}" if bbox is not None else "global (no bbox)"
    )
    window_str = f"{starttime}..{endtime}"
    mag_str = (
        f"M>={min_magnitude}" if min_magnitude is not None else "all magnitudes"
    )

    # HTTP 400 from FDSN: usually a too-large result set, or an out-of-range
    # parameter. The body carries a plaintext usage message.
    if status == 400:
        msg = raw.decode("utf-8", errors="replace")[:300] if raw else ""
        if "exceeds" in msg.lower() or "limit" in msg.lower() or not msg:
            raise EarthquakesResultTooLargeError(
                f"USGS FDSN refused the query ({scope}, {window_str}, {mag_str}) "
                f"as too large or out of range. Narrow the bbox, shorten the "
                f"time window, or raise min_magnitude. FDSN said: {msg!r}"
            )
        raise EarthquakesInputError(
            f"USGS FDSN rejected the query ({scope}, {window_str}, {mag_str}): "
            f"{msg!r}"
        )

    # HTTP 204 = no content = no events.
    if status == 204:
        raise EarthquakesNoEventsError(
            f"No earthquakes matched {scope} over {window_str} ({mag_str}). The "
            f"USGS FDSN service returned no events. Widen the window, lower "
            f"min_magnitude, or pick a more seismically active area."
        )

    records, count = _parse_event_geojson(raw)

    # FDSN caps a single response at FDSN_RESULT_LIMIT events. If we hit the cap
    # the result is truncated -> raise instead of silently dropping events.
    if (count is not None and count > FDSN_RESULT_LIMIT) or (
        len(records) >= FDSN_RESULT_LIMIT
    ):
        raise EarthquakesResultTooLargeError(
            f"USGS FDSN matched {count if count is not None else '>='}"
            f"{len(records)} events for {scope} over {window_str} ({mag_str}), "
            f"exceeding the {FDSN_RESULT_LIMIT}-event response cap. Narrow the "
            f"bbox, shorten the window, or raise min_magnitude."
        )

    if not records:
        raise EarthquakesNoEventsError(
            f"No earthquakes matched {scope} over {window_str} ({mag_str}). The "
            f"USGS FDSN service returned zero events for that scope. Widen the "
            f"window, lower min_magnitude, or pick a more seismically active area."
        )

    logger.info(
        "fetch_usgs_earthquakes: %d event(s) for %s over %s (%s)",
        len(records),
        scope,
        window_str,
        mag_str,
    )
    extent = _events_bbox(records)
    assert extent is not None  # records is non-empty here
    return _build_flatgeobuf(records), extent


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_usgs_earthquakes(
    bbox: tuple[float, float, float, float] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_magnitude: float | None = 2.5,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL, RECORDED USGS earthquakes as a point FlatGeobuf.

    Retrieves recorded seismic events from the USGS FDSN Event Web Service (the
    machine API behind the USGS "Latest Earthquakes" map) inside a bbox and time
    window, above a minimum magnitude. Returns one Point feature per event at
    the epicenter, carrying the magnitude, depth, origin time, place
    description, and the USGS event-page URL. This is the canonical OBSERVED
    seismic-event source — the network/instrument record of what actually
    happened — NOT a probabilistic seismic-hazard model.

    When to use:
        - The user asks for recent / historical earthquakes, "seismic events",
          "quakes", "tremors", or "where did earthquakes happen"
          (e.g. "show me the earthquakes in California this month", "recent
          quakes near Anchorage", "magnitude 5+ earthquakes in the last year",
          "did anything shake near Ridgecrest last week").
        - You need actual recorded epicenters, magnitudes, and depths — the real
          event catalog — to map, count, or annotate.
        - Providing seismic context for a damage / shaking / infrastructure
          discussion ("what earthquakes preceded this?").

    When NOT to use:
        - PROBABILISTIC seismic HAZARD (PGA / shaking with a return period, the
          USGS National Seismic Hazard Model) — that is a hazard surface, not an
          event catalog. (If a probabilistic-hazard tool exists, use it; this
          tool returns the OBSERVED event record only.)
        - ShakeMap / ground-motion FOOTPRINT rasters for a single event — this
          tool returns epicenter POINTS, not the modeled shaking field.
        - Volcano / landslide / tsunami inundation — use the dedicated hazard
          tools; ``tsunami`` here is only a per-event boolean flag from USGS.
        - Faults / fault traces as polylines — this tool returns events, not the
          fault geometry.

    Parameters:
        bbox: Optional ``(west, south, east, north)`` in EPSG:4326 to restrict
            to an area of interest (a region, state, or metro). When omitted the
            query is GLOBAL (``supports_global_query=True``) — bounded by the
            magnitude floor, the time window, and the 20000-event response cap.
            Derive a bbox from a place name with ``geocode_location`` or
            ``fetch_administrative_boundaries`` first for area-scoped asks.
        start_date / end_date: Optional ISO ``YYYY-MM-DD`` (or full
            ``YYYY-MM-DDTHH:MM:SS``) window bounds, UTC. When BOTH are omitted
            the tool defaults to the most-recent ~30 days. When only one is
            given the other is derived (a 30-day span anchored to the supplied
            bound). The window is capped at 366 days; a longer span should be
            chunked. Example: ``start_date="2019-07-04"``,
            ``end_date="2019-07-07"`` for the Ridgecrest sequence.
        min_magnitude: Minimum magnitude floor (default ``2.5`` — the threshold
            below which events are mostly micro-quakes / noise). Pass a lower
            value (e.g. ``0`` or ``1``) for a dense local micro-seismicity map,
            or a higher value (e.g. ``4.5`` or ``6``) for only the significant
            events. Pass ``None`` for no floor (all catalogued events).

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Geometry:
        Point at each epicenter, EPSG:4326. ``layer_type="vector"``,
        ``role="primary"``, ``style_preset="earthquakes"`` (the client sizes the
        marker by ``mag`` and colors by ``depth_km``), ``units="magnitude / km"``.
        ``bbox`` is set to the events' extent so the camera auto-zooms.
        Properties per event:
            - ``id`` (USGS event id, e.g. ``"nc75382006"``),
            - ``mag`` (magnitude; null if not assigned),
            - ``depth_km`` (focal depth, km; null if absent),
            - ``mag_type`` (magnitude type: ``"md"``, ``"ml"``, ``"mw"``, ...),
            - ``place`` (human description, e.g. "5 km SW of Ridgemark, CA"),
            - ``time`` (origin time, ISO-8601 UTC),
            - ``updated`` (last-update time, ISO-8601 UTC),
            - ``url`` (USGS event page),
            - ``event_type`` (usually "earthquake"; can be "quarry blast", ...),
            - ``status`` ("automatic" | "reviewed"),
            - ``tsunami`` (0/1 USGS tsunami flag),
            - ``felt`` (number of "Did You Feel It?" reports; null if none),
            - ``sig`` (USGS significance score),
            - ``net`` (contributing network code, e.g. "nc", "ci", "us").

    Honest-empty path (data-source fallback norm — primary -> honest typed
    error): the FDSN service returns an HTTP 200 FeatureCollection with ZERO
    features for a seismically quiet area/window. That is a legitimate "no
    earthquakes" answer, not a success — so ``EarthquakesNoEventsError`` is
    raised (never an empty success-shaped layer).

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="usgs_earthquakes"``.
    Cache key is SHA-256 of ``(bbox-rounded-6dp, starttime, endtime,
    min_magnitude)``, so identical-scope calls within the hour reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox from a place name BEFORE this call),
          ``fetch_administrative_boundaries`` (state/county framing),
          ``compute_zonal_statistics`` (count events inside a polygon).
        - Upstream data source: USGS FDSN Event Web Service
          (earthquake.usgs.gov/fdsnws/event/1/query).

    Errors (FR-AS-11 typed-error surface):
        - ``EarthquakesInputError``: bad bbox / bad date / reversed window /
          out-of-range magnitude (retryable=False).
        - ``EarthquakesResultTooLargeError``: the query exceeds the FDSN
          20000-event response cap — narrow bbox / shorten window / raise
          min_magnitude (retryable=False).
        - ``EarthquakesUpstreamError``: USGS network failure / HTTP 5xx / bad
          body (retryable=True).
        - ``EarthquakesNoEventsError``: no events matched the scope
          (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (USGS federal seismic network). Claims from
    these event records should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=True``.
    """
    # 1. Resolve + validate the spatial selector (optional bbox).
    resolved_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        if not isinstance(bbox, (tuple, list)):
            raise EarthquakesInputError(
                f"bbox must be a 4-tuple/list or omitted; got {type(bbox).__name__}"
            )
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        resolved_bbox = _round_bbox_to_6dp(bbox_t)

    # 2. Resolve the temporal window + magnitude floor.
    starttime, endtime = _resolve_window(start_date, end_date)
    resolved_min_mag = _validate_min_magnitude(min_magnitude)

    # 3. Cache-key params.
    params: dict[str, Any] = {
        "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
        "starttime": starttime,
        "endtime": endtime,
        "min_magnitude": resolved_min_mag,
    }

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_usgs_earthquakes_bytes(
            bbox=resolved_bbox,
            starttime=starttime,
            endtime=endtime,
            min_magnitude=resolved_min_mag,
        )
        captured["extent"] = extent
        return fgb

    # 4. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_usgs_earthquakes is cacheable; uri must be set by read_through"
    )

    # 5. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty — fall back to the requested bbox (a global query
    # has no requested bbox, so leave it None: the inline vector path fits the
    # map to the rendered features).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 6. Build a descriptive layer name + stable id.
    if resolved_bbox is not None:
        scope_tag = (
            f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
            f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
        )
    else:
        scope_tag = "global"
    mag_tag = (
        f"M>={resolved_min_mag:g}" if resolved_min_mag is not None else "all M"
    )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    name = f"USGS earthquakes — {scope_tag} ({mag_tag}, {starttime[:10]}..{endtime[:10]})"
    layer_id = f"usgs-earthquakes-{seed}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="earthquakes",
        role="primary",
        units="magnitude / km",
        bbox=extent_bbox,
    )
