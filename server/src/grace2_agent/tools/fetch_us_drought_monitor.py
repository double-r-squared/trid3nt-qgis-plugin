"""``fetch_us_drought_monitor`` atomic tool — US Drought Monitor (USDM)
weekly drought-category polygons (D0-D4) clipped to a bbox.

Wraps the Esri Living Atlas ``US_Drought_Intensity_v1`` ArcGIS REST
FeatureServer, the authoritative re-publication of the National Drought
Mitigation Center (NDMC) US Drought Monitor. The USDM is released every
Thursday (valid as of the prior Tuesday) and classifies drought into five
intensity categories:

    ``dm=0`` -> D0  Abnormally Dry
    ``dm=1`` -> D1  Moderate Drought
    ``dm=2`` -> D2  Severe Drought
    ``dm=3`` -> D3  Extreme Drought
    ``dm=4`` -> D4  Exceptional Drought

**What it does:**
Fetches the dissolved drought-category polygons intersecting a user bbox for
either the current week (default) or a specified past USDM release date.
Returns a FlatGeobuf vector layer with one (Multi)Polygon per drought class
intersecting the bbox, annotated with ``dm`` (0-4 integer class), ``label``
(human-readable category name), ``period`` (the USDM release period as
``YYYYMMDD``), and ``valid_date`` (ISO date) attributes suitable for map
display, narration, and downstream fire / agriculture overlays.

**When to use:**
- User asks for the US Drought Monitor map, current drought conditions, "how
  bad is the drought in [region]?", or "show me drought categories near X".
- Agent needs drought-intensity polygons as a wildfire-risk or
  agricultural-stress context layer (intersect with ``fetch_field_boundaries``
  / FTW ag fields, ``fetch_firms_active_fire``, or fuel/vegetation layers).
- User wants a past drought snapshot (e.g. the 2021 Southwest megadrought) via
  the optional ``date`` parameter.
- User wants to count exposed assets/population inside a drought footprint
  (feed into ``compute_zonal_statistics``).

**When NOT to use:**
- For soil-moisture / SPI / SPEI raster indices -> the USDM is a categorical
  expert-synthesis product, not a continuous index; use a dedicated
  reanalysis/index tool.
- For precipitation deficit, streamflow drought, or reservoir levels -> use
  ``fetch_usgs_nwis_gauges`` (streamflow) or a precip reanalysis tool.
- For areas outside the United States -> the USDM covers the 50 states,
  Puerto Rico, and the US-affiliated Pacific/Caribbean islands ONLY; a bbox
  over the open ocean or a foreign country returns an honest empty layer.
- For active-fire detections -> use ``fetch_firms_active_fire`` /
  ``fetch_goes_active_fire``; drought is the antecedent dryness context, not
  the fire itself.

**Parameters:**
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 (WGS84 decimal
        degrees). Required -- ``supports_global_query=False`` (US-only polygon
        source; a global query would cover the entire CONUS dissolved set).
        The USDM polygons are dissolved per category at national scale, so an
        envelope-intersect query returns the FULL category polygon clipped at
        intersection (geometry may extend beyond the bbox). Example for the
        drought-prone US Southwest (Arizona): ``(-114.0, 31.3, -109.0, 37.0)``.
    date: Optional USDM release date selecting a past weekly snapshot. Accepts
        ``"YYYY-MM-DD"`` or ``"YYYYMMDD"``. The USDM releases weekly on a
        Tuesday valid-date; the value is matched against the archive's
        ``period`` field, so it should fall on a valid USDM Tuesday (the tool
        does NOT snap to the nearest release -- an off-cadence date yields an
        honest empty layer). Defaults to ``None`` -> the current/latest week
        from the live "current conditions" layer.

**Returns:**
    ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
    ``s3://<cache-bucket>/cache/semi-static-7d/us_drought_monitor/<key>.fgb``
    Each feature is a (Multi)Polygon in EPSG:4326. Properties: ``dm`` (int 0-4,
    drought class), ``label`` (str, e.g. "D2 Severe Drought"), ``period`` (str
    ``YYYYMMDD`` USDM release), ``valid_date`` (str ISO date or empty).
    ``layer_type="vector"``, ``role="primary"``,
    ``style_preset="us_drought_monitor"``, ``units="dm_class"``.

**Cross-tool dependencies:**
    - Feeds INTO ``compute_zonal_statistics`` ("how many ag acres are in D3+
      extreme drought?") and ``clip_vector_to_polygon`` (admin/watershed-scoped
      drought report via ``fetch_administrative_boundaries``).
    - Pairs WITH ``fetch_field_boundaries`` (FTW/fiboa ag fields) for
      drought-on-agriculture demos and ``fetch_firms_active_fire`` /
      ``fetch_goes_active_fire`` for drought-as-fire-antecedent context.

**Cache:** ``semi-static-7d`` (FR-DC-2). The USDM publishes a new product once
per week, so a 7-day stale window matches the source's release cadence exactly.

**FR-AS-11 typed-error surface:** ``US_DROUGHT_MONITORError`` (base,
retryable=True), ``US_DROUGHT_MONITORInputError`` (non-retryable bbox/date
validation), ``US_DROUGHT_MONITORUpstreamError`` (retryable ArcGIS REST
network / HTTP / parse failure), ``US_DROUGHT_MONITOREmptyError`` (no drought
in bbox -- NOT raised by default; we serialize an empty FGB so the layer still
appears with a zero-feature notice, e.g. a bbox over a non-drought area).

**FR-DC-9 payload estimation:** USDM category polygons are heavily dissolved;
geometry size is driven far more by how many category boundaries the bbox
crosses than by bbox area. We estimate ~0.6 MB per square degree, clamped to
[0.05, 60] MB.

``supports_global_query=False`` -- US-only polygon source.

Endpoint pattern (verified live 2026-06-27):
    Current week (layer 3, "US_Drought_Current"):
        https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/
        US_Drought_Intensity_v1/FeatureServer/3/query
    Archive 2000-present (layer 2, "US_Drought", filter by ``period``):
        .../US_Drought_Intensity_v1/FeatureServer/2/query

    Query parameters::
        where=1=1               (or period='YYYYMMDD' for the archive)
        geometry={xmin,ymin,xmax,ymax}
        geometryType=esriGeometryEnvelope
        inSR=4326
        spatialRel=esriSpatialRelIntersects
        outFields=OBJECTID,dm,period,ddate
        outSR=4326
        f=geojson
        resultRecordCount=2000

    Response: GeoJSON FeatureCollection of (Multi)Polygons with a ``dm``
    integer (0-4) and ``period`` (``YYYYMMDD``) per feature.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_us_drought_monitor",
    "estimate_payload_mb",
    "US_DROUGHT_MONITORError",
    "US_DROUGHT_MONITORInputError",
    "US_DROUGHT_MONITORUpstreamError",
    "US_DROUGHT_MONITOREmptyError",
    "_validate_bbox",
    "_normalize_date",
    "_round_bbox_to_6dp",
    "_build_usdm_url",
    "_fetch_usdm_features",
    "_features_to_flatgeobuf",
    "_fetch_usdm_bytes",
    "USDM_CURRENT_URL",
    "USDM_ARCHIVE_URL",
    "DM_LABELS",
]

logger = logging.getLogger("grace2_agent.tools.fetch_us_drought_monitor")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class US_DROUGHT_MONITORError(RuntimeError):
    """Base class for fetch_us_drought_monitor failures."""

    error_code: str = "US_DROUGHT_MONITOR_ERROR"
    retryable: bool = True


class US_DROUGHT_MONITORInputError(US_DROUGHT_MONITORError):
    """Caller passed an invalid bbox or date value."""

    error_code = "US_DROUGHT_MONITOR_INPUT_INVALID"
    retryable = False


class US_DROUGHT_MONITORUpstreamError(US_DROUGHT_MONITORError):
    """USDM ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "US_DROUGHT_MONITOR_UPSTREAM_ERROR"
    retryable = True


class US_DROUGHT_MONITOREmptyError(US_DROUGHT_MONITORError):
    """No drought-category features found in bbox.

    NOT raised by default (we serialize an empty FGB instead -- a bbox over an
    area with no drought, the open ocean, or a foreign country legitimately
    has no USDM footprint), but available for future strict-mode opt-in.
    """

    error_code = "US_DROUGHT_MONITOR_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Esri Living Atlas USDM current-conditions layer (latest weekly release).
USDM_CURRENT_URL = (
    "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
    "US_Drought_Intensity_v1/FeatureServer/3/query"
)

#: Esri Living Atlas USDM archive layer (2000-present; filter by ``period``).
USDM_ARCHIVE_URL = (
    "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/"
    "US_Drought_Intensity_v1/FeatureServer/2/query"
)

#: Human-readable label per USDM drought-monitor class (dm field).
DM_LABELS: dict[int, str] = {
    0: "D0 Abnormally Dry",
    1: "D1 Moderate Drought",
    2: "D2 Severe Drought",
    3: "D3 Extreme Drought",
    4: "D4 Exceptional Drought",
}

#: Fields requested from the ArcGIS REST endpoint.
_OUT_FIELDS = "OBJECTID,dm,period,ddate"

#: Per-page record cap. USDM is dissolved to <=5 features per release nationally,
#: so a bbox query returns at most a handful; 2000 is the server max and a safe
#: ceiling.
_PAGE_SIZE = 2000

#: HTTP request timeout (seconds). USDM polygons are large (dissolved national
#: geometry), so allow a comfortable margin.
_HTTP_TIMEOUT_S = 60.0

#: User-Agent string.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Payload estimation heuristic: MB per square degree of bbox.
_PAYLOAD_MB_PER_SQ_DEG = 0.6
_PAYLOAD_MIN_MB = 0.05
_PAYLOAD_MAX_MB = 60.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_us_drought_monitor",
        ttl_class="semi-static-7d",
        source_class="us_drought_monitor",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(
            **common,
            supports_global_query=False,
            payload_mb_estimator_name="estimate_payload_mb",
        )  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support all Wave-1.5 flags; "
            "registering fetch_us_drought_monitor without them"
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
    """Estimate the FlatGeobuf payload size for a USDM fetch.

    Heuristic: ~0.6 MB per square degree of bbox. USDM category polygons are
    heavily dissolved; the dominant size driver is how many category
    boundaries the bbox crosses, so this is a coarse area-scaled bound.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
    """
    if bbox is None:
        area_sq_deg = 9.0
    else:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
            area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
        except (TypeError, ValueError):
            area_sq_deg = 9.0

    est = area_sq_deg * _PAYLOAD_MB_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``US_DROUGHT_MONITORInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise US_DROUGHT_MONITORInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise US_DROUGHT_MONITORInputError(
            f"bbox contains non-finite values: {bbox!r}"
        )
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise US_DROUGHT_MONITORInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise US_DROUGHT_MONITORInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise US_DROUGHT_MONITORInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _normalize_date(date: str | None) -> str | None:
    """Normalize an optional USDM release date to the ``YYYYMMDD`` archive form.

    Accepts ``"YYYY-MM-DD"`` or ``"YYYYMMDD"`` (8 digits). Returns ``None`` for
    ``None`` (current-week mode). Raises ``US_DROUGHT_MONITORInputError`` for
    any other shape or an impossible calendar date.

    Note: the value is NOT snapped to the nearest USDM Tuesday -- the caller is
    responsible for passing a valid release date. An off-cadence date that does
    not match any archived ``period`` legitimately returns an empty layer.
    """
    if date is None:
        return None
    if not isinstance(date, str):
        raise US_DROUGHT_MONITORInputError(
            f"date must be a string 'YYYY-MM-DD' or 'YYYYMMDD'; "
            f"got {type(date).__name__}"
        )
    raw = date.strip()
    compact = raw.replace("-", "")
    if not re.fullmatch(r"\d{8}", compact):
        raise US_DROUGHT_MONITORInputError(
            f"date must be 'YYYY-MM-DD' or 'YYYYMMDD' (8 digits); got {date!r}"
        )
    # Validate it is a real calendar date.
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise US_DROUGHT_MONITORInputError(
            f"date {date!r} is not a valid calendar date: {exc}"
        ) from exc
    return compact


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_usdm_url(
    bbox: tuple[float, float, float, float],
    period: str | None,
) -> tuple[str, dict[str, str]]:
    """Build the USDM ArcGIS REST query URL + params dict.

    When ``period`` is ``None`` the current-conditions layer (3) is queried with
    ``where=1=1``. When a ``period`` (``YYYYMMDD``) is supplied the archive
    layer (2) is queried with ``where=period='<period>'``.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        period: USDM release period ``YYYYMMDD`` or ``None`` for current week.

    Returns:
        ``(url, params)`` tuple for an httpx GET.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    if period is None:
        url = USDM_CURRENT_URL
        where = "1=1"
    else:
        url = USDM_ARCHIVE_URL
        where = f"period='{period}'"
    params: dict[str, str] = {
        "where": where,
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": _OUT_FIELDS,
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
    }
    return url, params


# ---------------------------------------------------------------------------
# HTTP fetch.
# ---------------------------------------------------------------------------


def _fetch_usdm_features(
    bbox: tuple[float, float, float, float],
    period: str | None,
) -> list[dict[str, Any]]:
    """Fetch all USDM drought-category polygon features intersecting bbox.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        period: USDM release period ``YYYYMMDD`` or ``None`` for current week.

    Returns:
        List of GeoJSON Feature dicts. Empty list if no drought intersects the
        bbox for the requested week (non-drought area, ocean, foreign land, or
        an off-cadence archive date with no matching ``period``).

    Raises:
        ``US_DROUGHT_MONITORUpstreamError``: on network / HTTP / parse failures.
    """
    url, params = _build_usdm_url(bbox, period)
    logger.info(
        "fetch_us_drought_monitor: GET %s (period=%s, bbox=%s)",
        url,
        period or "current",
        bbox,
    )

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise US_DROUGHT_MONITORUpstreamError(
            f"USDM request failed url={url} period={period}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise US_DROUGHT_MONITORUpstreamError(
            f"USDM returned HTTP {resp.status_code} url={url} "
            f"period={period}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise US_DROUGHT_MONITORUpstreamError(
            f"USDM returned non-JSON url={url} period={period}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise US_DROUGHT_MONITORUpstreamError(
            f"USDM response is not a JSON object url={url}: "
            f"type={type(body).__name__!r}"
        )

    # ArcGIS REST may surface errors inside a 200 envelope.
    if "error" in body:
        raise US_DROUGHT_MONITORUpstreamError(
            f"USDM query returned error envelope url={url} "
            f"period={period}: {body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise US_DROUGHT_MONITORUpstreamError(
            f"USDM response is not a GeoJSON FeatureCollection url={url}: "
            f"type={body.get('type')!r}"
        )

    features = body.get("features", []) or []
    logger.info(
        "fetch_us_drought_monitor: period=%s -> %d feature(s)",
        period or "current",
        len(features),
    )
    return features


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _ddate_to_iso(ddate: Any) -> str:
    """Convert an ArcGIS epoch-ms ``ddate`` to an ISO date string, or ``""``."""
    if isinstance(ddate, (int, float)):
        try:
            return datetime.fromtimestamp(
                ddate / 1000.0, tz=timezone.utc
            ).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert USDM drought-category features to FlatGeobuf bytes.

    Produces a single GeoDataFrame with ``dm`` (int class), ``label`` (category
    name), ``period`` (``YYYYMMDD``), and ``valid_date`` (ISO) columns. Always
    emits valid FlatGeobuf bytes -- an empty list yields an empty-schema FGB so
    the cache shim has something concrete to persist and the layer still
    appears with a zero-feature notice.

    Args:
        features: list of GeoJSON Feature dicts from the USDM endpoint.

    Returns:
        FlatGeobuf bytes in EPSG:4326.

    Raises:
        ``US_DROUGHT_MONITORUpstreamError``: if geopandas is unavailable or
        FlatGeobuf serialization fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise US_DROUGHT_MONITORUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        props = feat.get("properties") or {}
        try:
            dm = int(props.get("dm", 0))
        except (TypeError, ValueError):
            continue
        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "dm": dm,
                "label": DM_LABELS.get(dm, f"D{dm}"),
                "period": str(props.get("period", "")),
                "valid_date": _ddate_to_iso(props.get("ddate")),
            },
        })

    if not cleaned:
        import pandas as pd
        empty_df = pd.DataFrame(columns=["dm", "label", "period", "valid_date"])
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_usdm_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise US_DROUGHT_MONITORUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} USDM features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_us_drought_monitor: FlatGeobuf = %d bytes (%d feature(s))",
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
# End-to-end fetcher (passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_usdm_bytes(
    bbox: tuple[float, float, float, float],
    period: str | None,
) -> bytes:
    """Fetch USDM drought-category features for bbox/period -> FlatGeobuf bytes."""
    features = _fetch_usdm_features(bbox, period)
    return _features_to_flatgeobuf(features)


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
def fetch_us_drought_monitor(
    bbox: tuple[float, float, float, float],
    date: str | None = None,
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """US Drought Monitor weekly drought-category polygons as a FlatGeobuf layer.

    Fetches the dissolved USDM drought-category (Multi)Polygons intersecting the
    bbox for the current week (default) or a specified past release ``date``.
    Returns a FlatGeobuf with one feature per drought class (D0-D4) intersecting
    the bbox, annotated with ``dm`` (0-4 class), ``label`` (category name),
    ``period`` (USDM release ``YYYYMMDD``), and ``valid_date`` (ISO date).

    **When to use:**
    - User asks for the US Drought Monitor map, current drought conditions, or
      "how bad is the drought in [region]?".
    - Agent needs drought-intensity polygons as a wildfire-risk or
      agricultural-stress context layer (intersect with ag fields, active fire,
      or fuel/vegetation layers).
    - User wants a past drought snapshot via the optional ``date`` parameter.

    **When NOT to use:**
    - For continuous soil-moisture / SPI / SPEI indices -> the USDM is a
      categorical expert-synthesis product, not a numeric index.
    - For streamflow drought / reservoir levels -> use ``fetch_usgs_nwis_gauges``.
    - For areas outside the United States and its territories -> the USDM is
      US-only; an out-of-coverage bbox returns an honest empty layer.
    - For active-fire detections -> use ``fetch_firms_active_fire`` /
      ``fetch_goes_active_fire`` (drought is the antecedent dryness, not fire).

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            ``supports_global_query=False`` -- US-only polygon source. USDM
            polygons are dissolved per category nationally, so an
            envelope-intersect query returns the full category polygon clipped
            at intersection. Example for the US Southwest (Arizona):
            ``(-114.0, 31.3, -109.0, 37.0)``.
        date: Optional USDM release date for a past weekly snapshot. Accepts
            ``"YYYY-MM-DD"`` or ``"YYYYMMDD"``; should fall on a valid USDM
            Tuesday release date. Defaults to ``None`` -> current/latest week.

    **Returns:**
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Each feature
        is a (Multi)Polygon in EPSG:4326. Properties: ``dm`` (int 0-4 drought
        class), ``label`` (str, e.g. "D2 Severe Drought"), ``period`` (str
        ``YYYYMMDD``), ``valid_date`` (str ISO date or empty). ``layer_type``
        is ``"vector"``, ``role`` is ``"primary"``, ``style_preset`` is
        ``"us_drought_monitor"``, ``units`` is ``"dm_class"``.

    **Cross-tool dependencies (FR-TA-3):**
        - Feeds INTO: ``compute_zonal_statistics`` (assets/acres inside a
          drought footprint), ``clip_vector_to_polygon`` (admin/watershed-scoped
          drought report with ``fetch_administrative_boundaries``).
        - Pairs WITH: ``fetch_field_boundaries`` (FTW/fiboa ag fields) for
          drought-on-agriculture demos, ``fetch_firms_active_fire`` /
          ``fetch_goes_active_fire`` for drought-as-fire-antecedent context.

    **Error types (FR-AS-11):**
        - ``US_DROUGHT_MONITORInputError``: bad bbox or date (retryable=False).
        - ``US_DROUGHT_MONITORUpstreamError``: HTTP/network failure, ArcGIS
          error envelope, or FlatGeobuf serialization failure (retryable=True).
        - ``US_DROUGHT_MONITOREmptyError``: no drought in bbox (retryable=False;
          not raised by default -- an empty FGB is returned instead).

    Cache: ``ttl_class="semi-static-7d"``, ``source_class="us_drought_monitor"``.
    Cache key is SHA-256 of ``(bbox-rounded-6dp, period-or-"current")``.

    ``supports_global_query=False``. No API key required.
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise US_DROUGHT_MONITORInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc

    _validate_bbox(bbox)  # type: ignore[arg-type]
    period = _normalize_date(date)

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "period": period or "current",
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_usdm_bytes(q_bbox, period),
    )
    assert result.uri is not None, (
        "fetch_us_drought_monitor is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    period_tag = period or "current"
    return LayerURI(
        layer_id=(
            f"us-drought-monitor-{period_tag}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"US Drought Monitor [{period_tag}] -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="us_drought_monitor",
        role="primary",
        units="dm_class",
        bbox=q_bbox,
    )
