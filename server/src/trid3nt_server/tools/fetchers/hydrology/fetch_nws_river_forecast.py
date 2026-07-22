"""``fetch_nws_river_forecast`` atomic tool - NWS river forecasts + flood categories.

Fetches **real** NWS river / streamgauge forecast points (AHPS / NWPS) within a
bounding box from the National Water Prediction Service API (``api.water.noaa.gov``)
and emits one Point feature per gauge carrying the OBSERVED and FORECAST river
stage plus the NWS **flood category** (``no_flood`` / ``action`` / ``minor`` /
``moderate`` / ``major``). This is the flood-warning surface the agent was
missing: USGS NWIS (``fetch_usgs_nwis_gauges``) gives the raw instrument record
and the NWM (``fetch_noaa_nwm_streamflow``) gives modeled reach flow, but neither
carries the NWS forecast stage or the operational flood-category threshold a
flood-warning question actually needs ("which river gauges are forecast to reach
flood stage in this area").

**API surface** (NWPS v1, free, NO API key required):

    PRIMARY - Gauges-by-bbox list:
        https://api.water.noaa.gov/nwps/v1/gauges
            ?bbox.xmin=<west>&bbox.ymin=<south>
            &bbox.xmax=<east>&bbox.ymax=<north>
            &srid=EPSG_4326
        Returns every gauge whose location falls inside the bbox, each carrying
        a ``status`` block with ``observed`` and ``forecast`` sub-objects
        (``primary`` stage, ``primaryUnit``, ``secondary`` flow, ``floodCategory``,
        ``validTime``). The ``srid=EPSG_4326`` is REQUIRED - without it the bbox
        filter is silently dropped and the API returns its entire gauge set.

    ENRICHMENT (optional, ``include_thresholds=True``) - per-gauge detail:
        https://api.water.noaa.gov/nwps/v1/gauges/<lid>
        Carries the ``flood.categories`` threshold stages (action/minor/moderate/
        major, in ft). The list response does NOT carry thresholds, so when the
        caller asks for them we fetch up to ``_MAX_THRESHOLD_GAUGES`` gauges
        individually and join the threshold stages onto the feature props. This
        is OFF by default to keep the tool a single HTTP call.

**Flood-category normalization**: the NWPS API uses ``no_flooding`` where the
domain vocabulary is ``no_flood``; we normalize that one token. Operational
non-flood states pass through verbatim (``not_defined``, ``obs_not_current``,
``out_of_service``) so the caller can see why a gauge has no category rather than
silently dropping it.

    SERIES + CREST (optional, ``include_series=True``) - per-gauge stageflow:
        https://api.water.noaa.gov/nwps/v1/gauges/<lid>/stageflow
        Carries the OBSERVED (~30 days hourly) and FORECAST (~28 6-hourly
        points) stage/flow series. We derive the forecast CREST (max forecast
        stage + its valid time) and embed the series inline as compact JSON
        attributes for charting, bounded to ``_MAX_SERIES_GAUGES`` gauges.

    SINGLE GAUGE (optional, ``gauge_id=<lid>``) - the detail endpoint above is
    used directly (the detail body carries the same identity + status shape as
    one gauges-list entry, plus the thresholds for free).

Output schema:
    Driver: FlatGeobuf, EPSG:4326, Point geometry (one feature per gauge).
    Props: lid, usgs_id, name, flood_category, fcst_flood_category,
           obs_stage_ft, obs_flow_kcfs, obs_valid_time,
           fcst_stage_ft, fcst_flow_kcfs, fcst_valid_time,
           rfc, wfo, state,
           action_stage_ft, minor_stage_ft, moderate_stage_ft, major_stage_ft
           (the *_stage_ft thresholds are populated only with include_thresholds
           or gauge_id mode),
           fcst_crest_stage_ft, fcst_crest_time, obs_series_json,
           fcst_series_json (populated only with include_series).

Tier-1, no auth, ``supports_global_query=False`` (US + territories only - NWS
river forecasts cover the US gauge network).
"""

from __future__ import annotations

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
    "fetch_nws_river_forecast",
    "estimate_payload_mb",
    "NwsRiverForecastError",
    "NwsRiverForecastInputError",
    "NwsRiverForecastBboxTooLargeError",
    "NwsRiverForecastUpstreamError",
    "NwsRiverForecastNoGaugesError",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_build_gauges_url",
    "_build_gauge_detail_url",
    "_parse_gauges_json",
    "_parse_gauge_thresholds",
    "_parse_stageflow",
    "_normalize_flood_category",
    "_build_flatgeobuf",
    "_build_gauge_stageflow_url",
    "_fetch_nws_river_forecast_bytes",
    "GAUGES_URL",
    "GAUGE_DETAIL_URL",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_nws_river_forecast")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NwsRiverForecastError(RuntimeError):
    """Base class for fetch_nws_river_forecast failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NWS_RIVER_FORECAST_ERROR"
    retryable: bool = True


class NwsRiverForecastInputError(NwsRiverForecastError):
    """Invalid inputs - bad bbox shape / out-of-range coordinates.

    Not retryable: the caller must fix the argument.
    """

    error_code = "NWS_RIVER_FORECAST_INPUT_ERROR"
    retryable = False


class NwsRiverForecastBboxTooLargeError(NwsRiverForecastInputError):
    """The requested bbox is implausibly large (a whole-hemisphere ask).

    Not retryable as-is. The NWPS gauge set is large; an unbounded bbox would
    pull thousands of points. The caller should re-issue with a tighter bbox
    (a basin / metro / state-sized area).
    """

    error_code = "NWS_RIVER_FORECAST_BBOX_TOO_LARGE"
    retryable = False


class NwsRiverForecastUpstreamError(NwsRiverForecastError):
    """NWPS request failed (network error, HTTP 5xx, bad body).

    Retryable - transient NWPS outages recover on retry.
    """

    error_code = "NWS_RIVER_FORECAST_UPSTREAM_ERROR"
    retryable = True


class NwsRiverForecastNoGaugesError(NwsRiverForecastError):
    """No NWS river/forecast gauges found inside the bbox.

    Not retryable - the area genuinely has no AHPS/NWPS forecast points. Either
    widen the bbox or pick an area on a forecast river reach.
    """

    error_code = "NWS_RIVER_FORECAST_NO_GAUGES"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NWPS gauges-by-bbox list endpoint.
GAUGES_URL = "https://api.water.noaa.gov/nwps/v1/gauges"

#: NWPS single-gauge detail endpoint (carries flood.categories thresholds).
GAUGE_DETAIL_URL = "https://api.water.noaa.gov/nwps/v1/gauges/"

#: User-Agent per NOAA usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds).
_HTTP_TIMEOUT = 60.0

#: Implausible-bbox gate. The NWPS gauge set spans the US; a request larger than
#: ~CONUS makes no sense and would pull an unbounded number of points. CONUS is
#: ~60 deg lon x ~25 deg lat = ~1500 deg^2; we cap a little above that.
_MAX_BBOX_SQ_DEG = 2000.0

#: Cap on per-gauge threshold-enrichment calls. Each enrichment is one extra
#: HTTP round-trip; we bound it so include_thresholds on a dense bbox cannot
#: fan out to hundreds of requests.
_MAX_THRESHOLD_GAUGES = 60

#: Cap on per-gauge stageflow-series enrichment calls (include_series). The
#: observed series alone is ~2800 hourly points per gauge, so the fan-out is
#: bounded much tighter than the threshold enrichment.
_MAX_SERIES_GAUGES = 12

#: Most-recent observed points kept when embedding the series inline (the
#: NWPS observed window is ~30 days hourly; ~4 days is what a chart needs).
_MAX_OBS_SERIES_POINTS = 96

#: NWPS no-data / missing sentinel for stage & flow scalars.
_NWPS_MISSING = -999.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata - registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_nws_river_forecast",
        ttl_class="dynamic-1h",
        source_class="nws_nwps_river_forecast",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_nws_river_forecast without it"
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

    Each gauge is one Point feature with ~20 small scalar/string props
    (~400 bytes serialized). NWPS forecast-gauge density across CONUS rivers is
    roughly ~15 gauges per 1 deg square in populated basins. The estimate is
    conservative; river-gauge layers are always small.
    """
    n_gauges = 30  # default guess
    if bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            n_gauges = max(1, int(sq_deg * 15))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_gauges * 400 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NwsRiverForecastInputError`` if the bbox is malformed / out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise NwsRiverForecastInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NwsRiverForecastInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise NwsRiverForecastInputError(
            f"bbox lon values out of [-180, 180]: {bbox!r}"
        )
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise NwsRiverForecastInputError(
            f"bbox lat values out of [-90, 90]: {bbox!r}"
        )
    if west >= east or south >= north:
        raise NwsRiverForecastInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _bbox_area_sq_deg(bbox: tuple[float, float, float, float]) -> float:
    west, south, east, north = bbox
    return max(0.0, east - west) * max(0.0, north - south)


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Flood-category normalization.
# ---------------------------------------------------------------------------

#: NWPS -> domain vocabulary. Only "no_flooding" -> "no_flood" is remapped;
#: action/minor/moderate/major already match. Operational non-flood states
#: (not_defined / obs_not_current / out_of_service / "") pass through verbatim
#: so the caller can SEE why a gauge has no category.
_FLOOD_CATEGORY_MAP = {"no_flooding": "no_flood"}


def _normalize_flood_category(raw: Any) -> str:
    """Normalize an NWPS floodCategory token to the domain vocabulary."""
    if raw is None:
        return ""
    s = str(raw).strip()
    return _FLOOD_CATEGORY_MAP.get(s, s)


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``NwsRiverForecastUpstreamError`` on failure.

    A 404 on the gauge-detail endpoint means "no such gauge"; we surface it as
    an empty body so threshold enrichment skips that gauge instead of aborting.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("fetch_nws_river_forecast: NWPS returned 404 for %s", url)
            return b""
        raise NwsRiverForecastUpstreamError(
            f"NWPS API returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise NwsRiverForecastUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise NwsRiverForecastUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def _build_gauges_url(bbox: tuple[float, float, float, float]) -> str:
    """Build the NWPS gauges-by-bbox list URL.

    ``srid=EPSG_4326`` is REQUIRED - without it the NWPS API silently drops the
    bbox filter and returns its whole gauge set.
    """
    west, south, east, north = bbox
    params: list[tuple[str, str]] = [
        ("bbox.xmin", f"{west}"),
        ("bbox.ymin", f"{south}"),
        ("bbox.xmax", f"{east}"),
        ("bbox.ymax", f"{north}"),
        ("srid", "EPSG_4326"),
    ]
    return GAUGES_URL + "?" + urllib.parse.urlencode(params)


def _build_gauge_detail_url(lid: str) -> str:
    """Build the NWPS single-gauge detail URL (carries flood.categories)."""
    return GAUGE_DETAIL_URL + urllib.parse.quote(str(lid).strip(), safe="")


def _build_gauge_stageflow_url(lid: str) -> str:
    """Build the NWPS per-gauge stageflow URL (observed + forecast series)."""
    return _build_gauge_detail_url(lid) + "/stageflow"


# ---------------------------------------------------------------------------
# Parsers.
# ---------------------------------------------------------------------------


def _coerce_float(v: Any) -> float | None:
    """Coerce a value to float, mapping the NWPS missing sentinel to None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    # NWPS encodes missing thresholds/flows as -9999 / -999.
    if f <= -999.0:
        return None
    return f


def _parse_gauges_json(raw: bytes) -> list[dict[str, Any]]:
    """Parse the NWPS gauges-list JSON body -> one record per gauge.

    Each gauge carries ``status.observed`` and ``status.forecast`` sub-objects
    (primary stage, primaryUnit, secondary flow, floodCategory, validTime). We
    flatten those plus the gauge identity (lid/usgsId/name/rfc/wfo/state) into a
    flat record. Records with no parseable coordinate are dropped. Returns ``[]``
    when the body is empty or carries zero gauges.
    """
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NwsRiverForecastUpstreamError(
            f"NWPS gauges response is not valid JSON: {exc}"
        ) from exc

    gauges = obj.get("gauges") or []
    records: list[dict[str, Any]] = []

    for g in gauges:
        lat = _coerce_float(g.get("latitude"))
        lon = _coerce_float(g.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue

        status = g.get("status") or {}
        obs = status.get("observed") or {}
        fc = status.get("forecast") or {}

        rec: dict[str, Any] = {
            "lid": str(g.get("lid") or "").strip(),
            "usgs_id": str(g.get("usgsId") or "").strip(),
            "name": str(g.get("name") or "").strip(),
            "lon": lon,
            "lat": lat,
            "rfc": str((g.get("rfc") or {}).get("abbreviation") or "").strip(),
            "wfo": str((g.get("wfo") or {}).get("abbreviation") or "").strip(),
            "state": str((g.get("state") or {}).get("abbreviation") or "").strip(),
            "flood_category": _normalize_flood_category(obs.get("floodCategory")),
            "obs_stage_ft": _coerce_float(obs.get("primary")),
            "obs_flow_kcfs": _coerce_float(obs.get("secondary")),
            "obs_valid_time": str(obs.get("validTime") or "").strip() or None,
            "fcst_flood_category": _normalize_flood_category(fc.get("floodCategory")),
            "fcst_stage_ft": _coerce_float(fc.get("primary")),
            "fcst_flow_kcfs": _coerce_float(fc.get("secondary")),
            "fcst_valid_time": str(fc.get("validTime") or "").strip() or None,
            # Threshold stages - populated only by include_thresholds enrichment.
            "action_stage_ft": None,
            "minor_stage_ft": None,
            "moderate_stage_ft": None,
            "major_stage_ft": None,
            # Forecast crest + inline series - populated only by the
            # include_series stageflow enrichment.
            "fcst_crest_stage_ft": None,
            "fcst_crest_time": None,
            "obs_series_json": None,
            "fcst_series_json": None,
        }
        if not rec["lid"]:
            continue
        records.append(rec)

    return records


def _parse_gauge_thresholds(raw: bytes) -> dict[str, float | None]:
    """Parse a single-gauge detail body -> the flood-category threshold stages.

    Returns ``{action_stage_ft, minor_stage_ft, moderate_stage_ft,
    major_stage_ft}`` with None for any missing/sentinel threshold. Returns all
    None for an empty body (404 / no detail).
    """
    out: dict[str, float | None] = {
        "action_stage_ft": None,
        "minor_stage_ft": None,
        "moderate_stage_ft": None,
        "major_stage_ft": None,
    }
    if not raw:
        return out
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return out
    cats = ((obj.get("flood") or {}).get("categories")) or {}
    for cat in ("action", "minor", "moderate", "major"):
        stage = _coerce_float((cats.get(cat) or {}).get("stage"))
        out[f"{cat}_stage_ft"] = stage
    return out


def _parse_stageflow(raw: bytes) -> dict[str, Any]:
    """Parse a per-gauge ``/stageflow`` body -> observed + forecast series.

    The NWPS stageflow body is ``{"observed": {"data": [{"validTime",
    "primary", "secondary"}, ...], "primaryUnits": "ft", ...}, "forecast":
    {...}}`` (live-verified 2026-07-07; observed is ~30 days hourly, forecast
    ~28 6-hourly points). Returns::

        {"observed": [(iso_time, stage_ft, flow_kcfs), ...],   # full
         "forecast": [(iso_time, stage_ft, flow_kcfs), ...],   # full
         "fcst_crest_stage_ft": float | None,   # max forecast stage
         "fcst_crest_time": str | None}         # its validTime

    Empty body (404) or a malformed body -> empty series + None crest; the
    per-gauge enrichment is best-effort by design.
    """
    empty: dict[str, Any] = {
        "observed": [],
        "forecast": [],
        "fcst_crest_stage_ft": None,
        "fcst_crest_time": None,
    }
    if not raw:
        return empty
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return empty
    if not isinstance(obj, dict):
        return empty

    def _series(key: str) -> list[tuple[str, float | None, float | None]]:
        block = obj.get(key) or {}
        pts: list[tuple[str, float | None, float | None]] = []
        for p in block.get("data") or []:
            if not isinstance(p, dict):
                continue
            t = str(p.get("validTime") or "").strip()
            if not t:
                continue
            pts.append(
                (t, _coerce_float(p.get("primary")), _coerce_float(p.get("secondary")))
            )
        return pts

    observed = _series("observed")
    forecast = _series("forecast")
    crest_stage: float | None = None
    crest_time: str | None = None
    for t, stage, _flow in forecast:
        if stage is not None and (crest_stage is None or stage > crest_stage):
            crest_stage = stage
            crest_time = t
    return {
        "observed": observed,
        "forecast": forecast,
        "fcst_crest_stage_ft": crest_stage,
        "fcst_crest_time": crest_time,
    }


def _series_to_json(
    points: list[tuple[str, float | None, float | None]],
) -> str:
    """Compact column-oriented JSON for inline embedding in an FGB attribute."""
    return json.dumps(
        {
            "t": [p[0] for p in points],
            "stage_ft": [p[1] for p in points],
            "flow_kcfs": [p[2] for p in points],
        },
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------

#: The ordered FGB column set. Kept explicit so the schema is stable regardless
#: of which records carry which optional fields.
_FGB_FLOAT_COLS = [
    "obs_stage_ft",
    "obs_flow_kcfs",
    "fcst_stage_ft",
    "fcst_flow_kcfs",
    "action_stage_ft",
    "minor_stage_ft",
    "moderate_stage_ft",
    "major_stage_ft",
    "fcst_crest_stage_ft",
]
_FGB_STR_COLS = [
    "lid",
    "usgs_id",
    "name",
    "rfc",
    "wfo",
    "state",
    "flood_category",
    "fcst_flood_category",
    "obs_valid_time",
    "fcst_valid_time",
    "fcst_crest_time",
    "obs_series_json",
    "fcst_series_json",
]


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize gauge records -> FlatGeobuf bytes (Point geometry, EPSG:4326).

    Raises ``NwsRiverForecastUpstreamError`` if geopandas/shapely are
    unavailable or the write fails. ``records`` must be non-empty (the caller
    enforces the no-gauges honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NwsRiverForecastUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data: dict[str, list[Any]] = {}
    for col in _FGB_STR_COLS:
        data[col] = [str(r.get(col) or "") for r in records]
    for col in _FGB_FLOAT_COLS:
        # Keep real None (not the -999 sentinel) so pyogrio writes a true null
        # rather than a magic number that would corrupt downstream stats.
        data[col] = [
            (None if r.get(col) is None else float(r[col])) for r in records
        ]

    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nws_river_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise NwsRiverForecastUpstreamError(
            f"FlatGeobuf write failed for {len(records)} river gauges: {exc}"
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
    """Compute the (west, south, east, north) extent of the gauge points.

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


def _enrich_thresholds(records: list[dict[str, Any]]) -> None:
    """In-place join of flood-category threshold stages onto gauge records.

    Fetches the single-gauge detail for up to ``_MAX_THRESHOLD_GAUGES`` gauges
    and writes the action/minor/moderate/major threshold stages onto each. A
    per-gauge failure (network / 404) is non-fatal: that gauge keeps None
    thresholds and the rest proceed. Beyond the cap, gauges keep None.
    """
    for rec in records[:_MAX_THRESHOLD_GAUGES]:
        lid = rec.get("lid")
        if not lid:
            continue
        try:
            raw = _http_get(_build_gauge_detail_url(lid))
        except NwsRiverForecastUpstreamError as exc:
            logger.info(
                "fetch_nws_river_forecast: threshold enrich skipped for %s: %s",
                lid,
                exc,
            )
            continue
        thresholds = _parse_gauge_thresholds(raw)
        rec.update(thresholds)


def _enrich_series(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """In-place join of the stageflow series + forecast crest onto records.

    Fetches ``/gauges/<lid>/stageflow`` for up to ``_MAX_SERIES_GAUGES`` gauges
    and writes onto each record:

      - ``fcst_crest_stage_ft`` / ``fcst_crest_time`` - the maximum forecast
        stage and its valid time (the forecast CREST),
      - ``obs_series_json`` - the most-recent ``_MAX_OBS_SERIES_POINTS``
        observed points as compact column JSON ``{"t": [...],
        "stage_ft": [...], "flow_kcfs": [...]}``,
      - ``fcst_series_json`` - the full forecast series, same shape.

    A per-gauge failure is non-fatal (that gauge keeps None). Returns the
    per-lid raw series dict ``{lid: {"observed": [...], "forecast": [...],
    "fcst_crest_stage_ft": ..., "fcst_crest_time": ...}}`` for callers that
    want the untrimmed series (charting).
    """
    out: dict[str, dict[str, Any]] = {}
    for rec in records[:_MAX_SERIES_GAUGES]:
        lid = rec.get("lid")
        if not lid:
            continue
        try:
            raw = _http_get(_build_gauge_stageflow_url(lid))
        except NwsRiverForecastUpstreamError as exc:
            logger.info(
                "fetch_nws_river_forecast: series enrich skipped for %s: %s",
                lid,
                exc,
            )
            continue
        series = _parse_stageflow(raw)
        out[str(lid)] = series
        rec["fcst_crest_stage_ft"] = series["fcst_crest_stage_ft"]
        rec["fcst_crest_time"] = series["fcst_crest_time"]
        rec["obs_series_json"] = _series_to_json(
            series["observed"][-_MAX_OBS_SERIES_POINTS:]
        )
        rec["fcst_series_json"] = _series_to_json(series["forecast"])
    return out


def _fetch_single_gauge_records(gauge_id: str) -> list[dict[str, Any]]:
    """Fetch one gauge by NWS lid via the detail endpoint -> gauge records.

    The detail body carries the same identity + ``status`` shape as one entry
    of the gauges list (live-verified 2026-07-07), so it is parsed by wrapping
    it as a single-element gauges list. The flood-category thresholds ride
    along for free (the detail body already carries ``flood.categories``).
    Returns ``[]`` for a 404 / unknown lid.
    """
    raw = _http_get(_build_gauge_detail_url(gauge_id))
    if not raw:
        return []
    try:
        detail = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NwsRiverForecastUpstreamError(
            f"NWPS gauge detail for {gauge_id!r} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(detail, dict):
        return []
    records = _parse_gauges_json(
        json.dumps({"gauges": [detail]}).encode("utf-8")
    )
    # Thresholds come free with the detail body.
    for rec in records:
        rec.update(_parse_gauge_thresholds(raw))
    return records


def _fetch_nws_river_forecast_bytes(
    *,
    bbox: tuple[float, float, float, float] | None,
    include_thresholds: bool,
    gauge_id: str | None = None,
    include_series: bool = False,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: gauges (bbox list OR single lid) -> enrich -> FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``NwsRiverForecastNoGaugesError``
    when the scope resolves to zero gauges.
    """
    if gauge_id is not None:
        logger.info(
            "fetch_nws_river_forecast: DETAIL GET %s",
            _build_gauge_detail_url(gauge_id),
        )
        records = _fetch_single_gauge_records(gauge_id)
        if not records:
            raise NwsRiverForecastNoGaugesError(
                f"NWPS has no river-forecast gauge with lid={gauge_id!r}. "
                f"Gauge ids are NWS location ids (e.g. 'CIDI4'), not USGS site "
                f"numbers; find one via a bbox query first."
            )
    else:
        assert bbox is not None  # the tool body enforces bbox-or-gauge_id
        url = _build_gauges_url(bbox)
        logger.info("fetch_nws_river_forecast: GAUGES GET %s", url)
        raw = _http_get(url)
        records = _parse_gauges_json(raw)

        if not records:
            raise NwsRiverForecastNoGaugesError(
                f"No NWS river/forecast gauges (AHPS/NWPS) found inside bbox={bbox!r}. "
                f"The NWPS gauges-by-bbox service returned zero forecast points. "
                f"Either the area has no forecast river reach or the bbox misses the "
                f"river; try a larger bbox or an area on a known forecast river."
            )

        if include_thresholds:
            _enrich_thresholds(records)

    logger.info(
        "fetch_nws_river_forecast: NWPS returned %d river gauge(s) for %s",
        len(records),
        f"lid={gauge_id}" if gauge_id is not None else "bbox",
    )

    if include_series:
        _enrich_series(records)

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
def fetch_nws_river_forecast(
    bbox: tuple[float, float, float, float] | None = None,
    include_thresholds: bool = False,
    gauge_id: str | None = None,
    include_series: bool = False,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NWS river-forecast gauges + flood categories as a Point FlatGeobuf.

    Retrieves the NWS / National Water Prediction Service (AHPS/NWPS) river
    forecast points inside ``bbox`` and returns one Point feature per gauge,
    each carrying:

      - the OBSERVED river stage (``obs_stage_ft``) and flow (``obs_flow_kcfs``),
      - the FORECAST river stage (``fcst_stage_ft``) and flow (``fcst_flow_kcfs``),
      - the NWS **flood category** for both observed (``flood_category``) and
        forecast (``fcst_flood_category``): one of ``no_flood`` / ``action`` /
        ``minor`` / ``moderate`` / ``major`` (or an operational state such as
        ``not_defined`` / ``out_of_service`` when the gauge has no category),
      - the gauge identity (``lid``, ``usgs_id``, ``name``, ``rfc``, ``wfo``,
        ``state``) and the observed/forecast valid times.

    This is the flood-warning surface: USGS NWIS (``fetch_usgs_nwis_gauges``) is
    the raw instrument record and NWM (``fetch_noaa_nwm_streamflow``) is modeled
    reach flow; this tool is the NWS forecast + flood-category source.

    Args:
        bbox: ``(west, south, east, north)`` in EPSG:4326. REQUIRED unless
            ``gauge_id`` is given.
        include_thresholds: when True, fetch each gauge's flood-category
            threshold stages (``action_stage_ft`` / ``minor_stage_ft`` /
            ``moderate_stage_ft`` / ``major_stage_ft``, ft) via a per-gauge
            detail call (bounded to the first ~60 gauges). OFF by default to
            keep the tool a single HTTP request. Always ON in ``gauge_id``
            mode (the detail body carries the thresholds for free).
        gauge_id: Optional NWS location id (lid, e.g. ``"CIDI4"`` - NOT a
            USGS site number) selecting a SINGLE gauge via the NWPS detail
            endpoint; ``bbox`` is then optional/ignored. Use a bbox query
            first to discover lids.
        include_series: when True, fetch each gauge's ``/stageflow`` observed
            + forecast time series (bounded to the first ~12 gauges) and add:
            ``fcst_crest_stage_ft`` / ``fcst_crest_time`` (the forecast CREST
            - the max forecast stage and when it occurs) plus
            ``obs_series_json`` (most-recent ~96 observed points) and
            ``fcst_series_json`` (full forecast series) as compact
            column-oriented JSON strings ``{"t": [...], "stage_ft": [...],
            "flow_kcfs": [...]}`` for charting. OFF by default.

    Returns:
        A vector ``LayerURI`` (FlatGeobuf, Point, EPSG:4326).

    Raises:
        NwsRiverForecastInputError: bbox and gauge_id both missing / malformed.
        NwsRiverForecastBboxTooLargeError: bbox implausibly large.
        NwsRiverForecastNoGaugesError: zero forecast gauges in the bbox, or
            an unknown gauge_id.
        NwsRiverForecastUpstreamError: NWPS API / network failure.

    Tier-1 free. No API key. ``supports_global_query=False`` (US + territories).
    """
    # 1. Resolve + validate the spatial selector (bbox OR gauge_id).
    gauge_lid: str | None = None
    if gauge_id is not None:
        gauge_lid = str(gauge_id).strip().upper()
        if not gauge_lid or not gauge_lid.isalnum():
            raise NwsRiverForecastInputError(
                f"gauge_id must be an alphanumeric NWS lid (e.g. 'CIDI4'); "
                f"got {gauge_id!r}"
            )
    resolved_bbox: tuple[float, float, float, float] | None = None
    if gauge_lid is None:
        if bbox is None:
            raise NwsRiverForecastInputError(
                "fetch_nws_river_forecast requires bbox=(west, south, east, "
                "north) in EPSG:4326 (or a gauge_id lid for a single gauge)."
            )
        if not isinstance(bbox, (tuple, list)):
            raise NwsRiverForecastInputError(
                f"bbox must be a 4-tuple/list; got {type(bbox).__name__}"
            )
        try:
            bbox_t: tuple[float, float, float, float] = tuple(
                float(v) for v in bbox
            )  # type: ignore[assignment]
        except (TypeError, ValueError) as exc:
            raise NwsRiverForecastInputError(
                f"bbox values must be numeric; got {bbox!r}: {exc}"
            ) from exc
        _validate_bbox(bbox_t)
        area = _bbox_area_sq_deg(bbox_t)
        if area > _MAX_BBOX_SQ_DEG:
            raise NwsRiverForecastBboxTooLargeError(
                f"bbox area {area:.0f} deg^2 exceeds the {_MAX_BBOX_SQ_DEG:.0f} deg^2 "
                f"limit; the NWS river-gauge set spans the US, so an unbounded bbox "
                f"would pull thousands of points. Re-issue with a basin / metro / "
                f"state-sized bbox."
            )
        resolved_bbox = _round_bbox_to_6dp(bbox_t)
    inc_thresh = bool(include_thresholds)
    inc_series = bool(include_series)

    # 2. Cache-key params.
    params: dict[str, Any] = {
        "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
        "include_thresholds": inc_thresh,
    }
    # Keep pre-extension cache keys byte-identical for the default call shape:
    # the new params join the key only when they deviate from the default.
    if gauge_lid is not None:
        params["gauge_id"] = gauge_lid
    if inc_series:
        params["include_series"] = True

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_nws_river_forecast_bytes(
            bbox=resolved_bbox,
            include_thresholds=inc_thresh,
            gauge_id=gauge_lid,
            include_series=inc_series,
        )
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
        "fetch_nws_river_forecast is cacheable; uri must be set by read_through"
    )

    # 4. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty - fall back to the requested bbox.
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 5. Build a descriptive layer name + stable id.
    if gauge_lid is not None:
        scope_tag = f"lid {gauge_lid}"
    else:
        assert resolved_bbox is not None
        scope_tag = (
            f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
            f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
        )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    return LayerURI(
        layer_id=f"nws-river-forecast-{seed}",
        name=f"NWS river forecast gauges - {scope_tag}",
        layer_type="vector",
        uri=result.uri,
        style_preset="nws_river_gauges",
        role="primary",
        units="ft (river stage) + flood category",
        bbox=extent_bbox,
    )
