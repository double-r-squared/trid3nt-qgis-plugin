"""``fetch_snotel_snow`` atomic tool — real NRCS SNOTEL / SCAN snow stations.

Fetches **real, observed** mountain-snowpack measurements from the USDA NRCS
Air-Water Database (AWDB) REST API — the modern REST surface behind the National
Water and Climate Center (``wcc.sc.egov.usda.gov/awdbRestApi/``), NOT the
deprecated SOAP service. One Point feature per SNOTEL/SCAN station within a
bbox, carrying the station triplet/name plus the latest Snow Water Equivalent
(SWE, inches) and snow depth (inches).

This is the mountain water-supply gap: snowpack is the dominant western US water
reservoir, and the SNOTEL network is the federal automated record for it. The
agent had no way to answer "show me the SNOTEL snow stations in the Colorado
Rockies / Sierra Nevada and their current snow water equivalent" — this is the
canonical observed snowpack source.

**API surface** (NRCS AWDB REST, free, NO API key required):

    STATIONS metadata (locations + elevation, used for the bbox filter):
        https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/stations
            ?networkCds=SNTL,SCAN&activeOnly=true
        Returns one JSON object per station with ``stationTriplet``, ``name``,
        ``stateCode``, ``networkCode``, ``latitude``, ``longitude``,
        ``elevation`` (ft). The endpoint has NO bbox parameter, so we fetch the
        active SNTL+SCAN catalog (a few thousand stations, cached upstream) and
        filter to the requested bbox client-side. The loose ``networkCds``
        filter can leak SNOW/USGS/COOP/BOR neighbours, so we additionally pin to
        ``networkCode in {SNTL, SCAN}`` (the NRCS automated networks).

    DATA (the SWE + snow-depth readings):
        https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data
            ?stationTriplets=<csv>&elements=WTEQ,SNWD&duration=DAILY
            &beginDate=<iso>&endDate=<iso>&periodRef=END
        ``WTEQ`` = snow water equivalent (in); ``SNWD`` = snow depth (in). Each
        station block carries one ``data[]`` entry per element, each with a
        ``values[]`` array of ``{date, value}``. We take the LATEST non-null
        sample per element. To resolve "latest" we request a short trailing
        window (default last 10 days ending today) and keep the final reading;
        a no-data sentinel (null ``value``) is skipped, and an off-season zero
        (e.g. midsummer SWE = 0.0) is reported HONESTLY as 0.0, not dropped.

**Spatial selector**: a single required ``bbox=(west, south, east, north)`` in
EPSG:4326. SNOTEL is a US-mountains network (Western US + Alaska + a few eastern
SCAN sites), so ``supports_global_query=False``. There is no service-side bbox
area cap (we filter the full catalog client-side), but a global-scale bbox is
pointless — the network is spatially sparse and mountain-bound.

**Fallback norm** (primary -> honest typed error): the STATIONS metadata fetch
is the spatial primary; the DATA fetch attaches readings. If zero SNTL/SCAN
stations fall inside the bbox, we raise a typed ``SnotelNoStationsError`` (the
area has no SNOTEL coverage — most of the US lowlands). If stations exist but
the DATA service is unreachable, we still emit the station LOCATIONS with null
readings (locations are useful on their own) rather than failing the whole
layer. We never return an empty success-shaped layer and never fabricate a
reading.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
point FeatureCollection (one point per station), serialized as FlatGeobuf.
``style_preset="snotel_snow"``; ``LayerURI.bbox`` is the stations' extent so the
camera auto-zooms.

Tier-1, no auth, ``supports_global_query=False`` (US mountain networks only).

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

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_snotel_snow",
    "estimate_payload_mb",
    "SnotelError",
    "SnotelInputError",
    "SnotelUpstreamError",
    "SnotelNoStationsError",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_build_stations_url",
    "_build_data_url",
    "_parse_stations_json",
    "_filter_stations_to_bbox",
    "_parse_data_json",
    "_merge_readings",
    "_build_flatgeobuf",
    "_records_bbox",
    "_fetch_snotel_snow_bytes",
    "STATIONS_URL",
    "DATA_URL",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.soil.fetch_snotel_snow")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class SnotelError(RuntimeError):
    """Base class for fetch_snotel_snow failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "SNOTEL_ERROR"
    retryable: bool = True


class SnotelInputError(SnotelError):
    """Invalid inputs — bad/missing bbox. Not retryable: caller must fix it."""

    error_code = "SNOTEL_INPUT_ERROR"
    retryable = False


class SnotelUpstreamError(SnotelError):
    """NRCS AWDB request failed (network error, HTTP 5xx, bad body). Retryable."""

    error_code = "SNOTEL_UPSTREAM_ERROR"
    retryable = True


class SnotelNoStationsError(SnotelError):
    """No SNOTEL/SCAN stations fall inside the requested bbox.

    Not retryable — the area genuinely has no NRCS automated snow stations
    (most of the US outside the western mountains). Pick a mountain region
    (Rockies, Sierra Nevada, Cascades, Wasatch, Alaska) or widen the bbox.
    """

    error_code = "SNOTEL_NO_STATIONS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NRCS AWDB REST base (the modern REST, not the deprecated SOAP service).
_AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

#: Stations metadata endpoint (locations + elevation).
STATIONS_URL = _AWDB_BASE + "/stations"

#: Time-series data endpoint (SWE + snow depth readings).
DATA_URL = _AWDB_BASE + "/data"

#: NRCS automated snow networks: SNTL = SNOTEL (snow telemetry, the western
#: mountain snowpack network); SCAN = Soil Climate Analysis Network (carries
#: SWE/depth at some sites). The loose ``networkCds`` API filter leaks other
#: networks (SNOW manual courses, USGS, COOP, BOR), so we re-pin client-side.
_SNOW_NETWORKS: frozenset[str] = frozenset({"SNTL", "SCAN"})

#: AWDB element codes: WTEQ = snow water equivalent (in); SNWD = snow depth (in).
_ELEM_SWE = "WTEQ"
_ELEM_DEPTH = "SNWD"
_ELEMENTS = f"{_ELEM_SWE},{_ELEM_DEPTH}"

#: Trailing window (days) used to resolve the LATEST daily reading per station.
#: We request the last N days and keep the final non-null sample per element.
_LATEST_WINDOW_DAYS = 10

#: Descriptive User-Agent (NRCS, like most federal services, prefers one).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds). The stations catalog is a few thousand objects.
_HTTP_TIMEOUT = 120.0

#: USGS no-data style sentinel guard (AWDB uses null, but guard finite anyway).
_NODATA_FLOOR = -999990.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_snotel_snow",
        ttl_class="dynamic-1h",
        source_class="snotel_snow",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_snotel_snow without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    Each station is one Point feature with a handful of small scalar properties
    (~180 bytes serialized). SNOTEL density in the mountain West is sparse:

    - 1 deg x 1 deg mountain bbox -> ~5-30 stations -> ~5 KB
    - a multi-state mountain region -> up to ~hundreds -> ~50 KB

    The estimate is conservative; SNOTEL layers are always tiny.
    """
    n_stations = 30  # default guess
    if bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # ~10 SNOTEL stations per 1 deg square in the snowy mountain West.
            n_stations = max(1, int(sq_deg * 10))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_stations * 180 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``SnotelInputError`` if the bbox is malformed or out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise SnotelInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise SnotelInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise SnotelInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise SnotelInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise SnotelInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``SnotelUpstreamError`` on failure.

    A 404 from AWDB means "no match" — we surface it as an empty body so the
    caller's honest-empty handling engages instead of aborting.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("fetch_snotel_snow: AWDB returned 404 (no match) for %s", url)
            return b""
        raise SnotelUpstreamError(
            f"NRCS AWDB returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SnotelUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise SnotelUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def _build_stations_url() -> str:
    """Build the stations-metadata URL for the active SNTL+SCAN catalog."""
    params = [
        ("networkCds", ",".join(sorted(_SNOW_NETWORKS))),
        ("activeOnly", "true"),
    ]
    return STATIONS_URL + "?" + urllib.parse.urlencode(params)


def _build_data_url(
    triplets: list[str],
    *,
    begin_date: str,
    end_date: str,
) -> str:
    """Build the time-series DATA URL for a set of station triplets.

    Requests DAILY WTEQ + SNWD over ``[begin_date, end_date]`` with
    ``periodRef=END`` so the values align to the end of each daily period.
    """
    params = [
        ("stationTriplets", ",".join(triplets)),
        ("elements", _ELEMENTS),
        ("duration", "DAILY"),
        ("beginDate", begin_date),
        ("endDate", end_date),
        ("periodRef", "END"),
        ("returnFlags", "false"),
        ("returnOriginalValues", "false"),
        ("returnSuspectData", "false"),
    ]
    return DATA_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Parsers.
# ---------------------------------------------------------------------------


def _parse_stations_json(raw: bytes) -> list[dict[str, Any]]:
    """Parse the stations-metadata body -> list of station dicts.

    Keeps only stations with a finite lat/lon in an NRCS snow network
    (``networkCode in {SNTL, SCAN}``). Returns ``[]`` for an empty body.
    """
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SnotelUpstreamError(
            f"NRCS AWDB stations response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(obj, list):
        # AWDB returns a top-level list; anything else is unexpected.
        return []

    out: list[dict[str, Any]] = []
    for s in obj:
        if not isinstance(s, dict):
            continue
        net = str(s.get("networkCode") or "").strip().upper()
        if net not in _SNOW_NETWORKS:
            continue
        trip = str(s.get("stationTriplet") or "").strip()
        if not trip:
            continue
        try:
            lat = float(s.get("latitude"))
            lon = float(s.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        elev: float | None
        try:
            elev = float(s.get("elevation"))
            if not math.isfinite(elev):
                elev = None
        except (TypeError, ValueError):
            elev = None
        out.append(
            {
                "triplet": trip,
                "name": str(s.get("name") or "").strip(),
                "state": str(s.get("stateCode") or "").strip() or None,
                "network": net,
                "elevation_ft": elev,
                "lon": lon,
                "lat": lat,
            }
        )
    return out


def _filter_stations_to_bbox(
    stations: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Return only the stations whose point falls inside the bbox (inclusive)."""
    west, south, east, north = bbox
    return [
        s
        for s in stations
        if west <= s["lon"] <= east and south <= s["lat"] <= north
    ]


def _parse_data_json(raw: bytes) -> dict[str, dict[str, Any]]:
    """Parse the time-series DATA body -> {triplet: {swe_in, snow_depth_in, date}}.

    Takes the LATEST non-null sample per element. SWE = WTEQ; depth = SNWD. An
    off-season zero (value == 0.0) is a VALID reading and is preserved; only a
    null / no-data value is skipped. ``date`` is the latest sample date seen for
    the station (preferring the SWE sample date). Returns ``{}`` for empty body.
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SnotelUpstreamError(
            f"NRCS AWDB data response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(obj, list):
        return {}

    readings: dict[str, dict[str, Any]] = {}
    for block in obj:
        if not isinstance(block, dict):
            continue
        trip = str(block.get("stationTriplet") or "").strip()
        if not trip:
            continue
        rec = readings.setdefault(
            trip, {"swe_in": None, "snow_depth_in": None, "date": None}
        )
        for el in block.get("data") or []:
            se = el.get("stationElement") or {}
            code = str(se.get("elementCode") or "").strip().upper()
            if code not in (_ELEM_SWE, _ELEM_DEPTH):
                continue
            # Latest non-null sample.
            latest_val: float | None = None
            latest_dt: str | None = None
            for sample in el.get("values") or []:
                v = sample.get("value")
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(fv) or fv <= _NODATA_FLOOR:
                    continue
                # values[] is date-ordered; keep walking so the final wins.
                latest_val = fv
                latest_dt = str(sample.get("date") or "").strip() or latest_dt
            if latest_val is None:
                continue
            if code == _ELEM_SWE:
                rec["swe_in"] = latest_val
                if latest_dt:
                    rec["date"] = latest_dt
            else:  # SNWD
                rec["snow_depth_in"] = latest_val
                if rec["date"] is None and latest_dt:
                    rec["date"] = latest_dt
    return readings


def _merge_readings(
    stations: list[dict[str, Any]],
    readings: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach the parsed readings onto each station record by triplet.

    Stations with no reading keep ``swe_in``/``snow_depth_in``/``date`` = None
    (the DATA-unreachable / no-recent-sample case). The station LOCATION still
    survives so the overlay shows it.
    """
    out: list[dict[str, Any]] = []
    for s in stations:
        r = readings.get(s["triplet"], {})
        out.append(
            {
                **s,
                "swe_in": r.get("swe_in"),
                "snow_depth_in": r.get("snow_depth_in"),
                "date": r.get("date"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize station records -> FlatGeobuf bytes (Point geometry, EPSG:4326).

    One Point feature per station carrying ``triplet``, ``name``, ``state``,
    ``network``, ``elevation_ft``, ``swe_in``, ``snow_depth_in``, ``date``.

    Raises ``SnotelUpstreamError`` if geopandas/shapely are unavailable or the
    write fails. ``records`` must be non-empty (the caller enforces the
    no-stations honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SnotelUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "triplet": [str(r["triplet"]) for r in records],
        "name": [str(r.get("name") or "") for r in records],
        "state": [str(r.get("state") or "") for r in records],
        "network": [str(r.get("network") or "") for r in records],
        "elevation_ft": [r.get("elevation_ft") for r in records],
        "swe_in": [r.get("swe_in") for r in records],
        "snow_depth_in": [r.get("snow_depth_in") for r in records],
        "date": [r.get("date") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_snotel_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise SnotelUpstreamError(
            f"FlatGeobuf write failed for {len(records)} SNOTEL stations: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the station points.

    Pads a degenerate single-point extent by ~0.05 deg so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.05
        east += 0.05
    if south == north:
        south -= 0.05
        north += 0.05
    return (west, south, east, north)


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through). Returns (fgb_bytes, extent_bbox).
# ---------------------------------------------------------------------------


def _fetch_snotel_snow_bytes(
    *,
    bbox: tuple[float, float, float, float],
    now: _dt.date | None = None,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: stations metadata -> bbox filter -> readings -> FGB.

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``SnotelNoStationsError`` when
    zero SNTL/SCAN stations fall inside the bbox. If stations exist but the DATA
    service is unreachable, emits station LOCATIONS with null readings (the
    locations are still useful) rather than failing the layer.
    """
    # 1. STATIONS metadata (the spatial primary).
    stations_url = _build_stations_url()
    logger.info("fetch_snotel_snow: stations GET %s", stations_url)
    stations_raw = _http_get(stations_url)
    all_stations = _parse_stations_json(stations_raw)
    in_bbox = _filter_stations_to_bbox(all_stations, bbox)

    if not in_bbox:
        raise SnotelNoStationsError(
            f"No active NRCS SNOTEL/SCAN snow stations found inside bbox={bbox!r}. "
            f"SNOTEL is a western-US mountain network (Rockies, Sierra Nevada, "
            f"Cascades, Wasatch, Alaska, plus scattered eastern SCAN sites); the "
            f"requested area has no automated snow stations. Pick a mountain "
            f"region or widen the bbox."
        )

    logger.info(
        "fetch_snotel_snow: %d SNTL/SCAN station(s) in bbox (of %d active)",
        len(in_bbox),
        len(all_stations),
    )

    # 2. DATA: latest SWE + snow depth over a short trailing window.
    today = now or _dt.date.today()
    begin = (today - _dt.timedelta(days=_LATEST_WINDOW_DAYS)).isoformat()
    end = today.isoformat()
    triplets = [s["triplet"] for s in in_bbox]
    readings: dict[str, dict[str, Any]] = {}
    try:
        data_url = _build_data_url(triplets, begin_date=begin, end_date=end)
        logger.info("fetch_snotel_snow: data GET %s", data_url)
        data_raw = _http_get(data_url)
        readings = _parse_data_json(data_raw)
        n_with = sum(
            1
            for r in readings.values()
            if r.get("swe_in") is not None or r.get("snow_depth_in") is not None
        )
        logger.info(
            "fetch_snotel_snow: %d/%d station(s) carry a recent reading",
            n_with,
            len(in_bbox),
        )
    except SnotelUpstreamError as exc:
        # DATA unreachable but we have locations -> degrade to locations only.
        logger.warning(
            "fetch_snotel_snow: DATA service failed (%s); emitting station "
            "locations with null readings",
            exc,
        )
        readings = {}

    records = _merge_readings(in_bbox, readings)
    extent = _records_bbox(records)
    assert extent is not None  # records is non-empty here
    return _build_flatgeobuf(records), extent


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
def fetch_snotel_snow(
    bbox: tuple[float, float, float, float] | None = None,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL, OBSERVED NRCS SNOTEL/SCAN snow stations as a point FlatGeobuf.

    Retrieves active USDA NRCS SNOTEL (snow telemetry) and SCAN snow stations
    inside a bbox plus their LATEST Snow Water Equivalent (SWE, inches) and snow
    depth (inches), from the NRCS Air-Water Database (AWDB) REST API behind the
    National Water and Climate Center. Returns one Point feature per station at
    the station's coordinates, carrying the latest reading. This is the
    canonical **observed** mountain-snowpack source for western US water supply.

    When to use:
        - The user asks for SNOTEL stations, snow water equivalent / SWE, snow
          depth, snowpack, or "snow telemetry" sites in a region
          (e.g. "show me the SNOTEL snow stations in the Colorado Rockies and
          their current snow water equivalent", "where are the snowpack gauges
          in the Sierra Nevada", "map SWE at the snow stations near Tahoe").
        - You need actual measured SWE/depth at instrumented sites for a
          mountain water-supply or snowmelt-runoff question.

    When NOT to use:
        - Gridded snow-cover imagery / fractional snow cover -> a raster product
          (e.g. MODIS/VIIRS snow), NOT this point-station tool.
        - Stream discharge / gage height -> ``fetch_usgs_nwis_gauges`` (observed)
          or ``fetch_noaa_nwm_streamflow`` (modeled).
        - Precipitation / design-storm forcing -> ``fetch_mrms_qpe`` or
          ``lookup_precip_return_period``.
        - Global / non-mountain-US snow -> not covered (SNOTEL is a western-US +
          Alaska + scattered eastern SCAN network; supports_global_query=False).

    Spatial selector:
        bbox: Required ``(west, south, east, north)`` in EPSG:4326. The tool
            fetches the active SNTL+SCAN catalog and filters to this bbox
            client-side (there is no service-side bbox parameter). Choose a
            mountain region; lowland bboxes have no SNOTEL coverage and raise
            ``SnotelNoStationsError``.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/dynamic-1h/snotel_snow/<key>.fgb``
        - ``layer_type="vector"``, ``role="primary"``,
          ``style_preset="snotel_snow"``, ``units="in (SWE / snow depth)"``.
        - Geometry: Point at each station's coordinates, EPSG:4326.
        - ``bbox`` is set to the stations' extent so the client camera
          auto-zooms (the layer renders via the inline-GeoJSON vector path).
        - Properties per station: ``triplet`` (NRCS station triplet, e.g.
          ``"335:CO:SNTL"``), ``name``, ``state`` (2-letter), ``network``
          (``SNTL`` or ``SCAN``), ``elevation_ft``, ``swe_in`` (latest WTEQ snow
          water equivalent, inches; null if not reported; 0.0 in the off-season
          is a HONEST reading, not a gap), ``snow_depth_in`` (latest SNWD snow
          depth, inches; null if not reported), ``date`` (ISO date of the latest
          reading; null when only locations are available).

    Fallback behaviour (data-source fallback norm -> honest typed error): the
    stations metadata fetch is the spatial primary. If zero SNTL/SCAN stations
    fall inside the bbox, ``SnotelNoStationsError`` is raised — never an empty
    success-shaped layer. If stations exist but the DATA service is unreachable,
    the tool degrades to station LOCATIONS with null readings (locations are
    still useful) rather than failing the whole layer. An off-season SWE/depth
    of 0.0 is reported honestly; a no-data sample is reported as null.

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="snotel_snow"``. Cache key
    is SHA-256 of the bbox-rounded-6dp, so identical-scope calls within the hour
    reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox from a mountain place name BEFORE this call),
          ``fetch_administrative_boundaries`` (state/county framing).
        - Cross-checks: ``fetch_usgs_nwis_gauges`` (downstream snowmelt
          discharge), ``fetch_dem`` / terrain tools (snowpack-by-elevation).
        - Upstream data source: NRCS AWDB REST
          (wcc.sc.egov.usda.gov/awdbRestApi/services/v1/stations + /data).

    Errors (FR-AS-11 typed-error surface):
        - ``SnotelInputError``: no bbox / bad bbox (retryable=False).
        - ``SnotelUpstreamError``: NRCS network failure / HTTP 5xx / bad body
          (retryable=True).
        - ``SnotelNoStationsError``: no SNOTEL/SCAN stations in the bbox
          (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (USDA NRCS federal snow network). Claims from
    SNOTEL readings should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=False`` (US mountains).
    """
    # 1. Resolve + validate the bbox selector.
    if bbox is None:
        raise SnotelInputError(
            "fetch_snotel_snow requires bbox=(west, south, east, north) in "
            "EPSG:4326 over a mountain region (e.g. the Colorado Rockies or the "
            "Sierra Nevada)."
        )
    if not isinstance(bbox, (tuple, list)):
        raise SnotelInputError(
            f"bbox must be a 4-tuple/list; got {type(bbox).__name__}"
        )
    try:
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
    except (TypeError, ValueError) as exc:
        raise SnotelInputError(f"bbox values must be numeric; got {bbox!r}") from exc
    _validate_bbox(bbox_t)
    resolved_bbox = _round_bbox_to_6dp(bbox_t)

    # 2. Cache-key params.
    params: dict[str, Any] = {"bbox": list(resolved_bbox)}

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_snotel_snow_bytes(bbox=resolved_bbox)
        captured["extent"] = extent
        return fgb

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_snotel_snow is cacheable; uri must be set by read_through"
    )

    # 4. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty — fall back to the requested bbox.
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 5. Build a descriptive layer name + stable id.
    scope_tag = (
        f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
        f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
    )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"snotel-snow-{seed}",
        name=f"SNOTEL snow stations — {scope_tag}",
        layer_type="vector",
        uri=result.uri,
        style_preset="snotel_snow",
        role="primary",
        units="in (SWE / snow depth)",
        bbox=extent_bbox,
    )
