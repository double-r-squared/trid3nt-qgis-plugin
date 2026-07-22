"""``fetch_hifld_transmission_lines`` atomic tool — HIFLD electric power
transmission LINES within a bbox.

Wraps the national Homeland Infrastructure Foundation-Level Data (HIFLD)
``Electric Power Transmission Lines`` polyline inventory, served from a public,
unauthenticated ArcGIS REST FeatureService. Returns a FlatGeobuf
``LineString`` / ``MultiLineString`` layer of every transmission line segment
intersecting a user-supplied bbox, together with the canonical HIFLD attribute
payload (line ID, line type, operational status, owner/operator, nominal
``VOLTAGE`` in kV, ``VOLT_CLASS`` band, and the two connected substation names).

This is the LINE (power-grid backbone) complement to the POINT lifeline
inventory exposed by ``fetch_hifld_critical_infrastructure`` (hospitals /
schools / fire stations / police / power plants). Together they cover the
electric-power lifeline-infrastructure exposure surface: power plants as the
generation points, transmission lines as the bulk-transport network.

**Source / endpoint (verified live 2026-06-27):**
    The HIFLD national transmission-line layer is published on a stable public
    ArcGIS Online org (HIFLD Open / geoplatform-published, no token) at::

        https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/
            Electric_Power_Transmission_Lines/FeatureServer/0/query

    It is an ``esriGeometryPolyline`` Feature Layer with ``maxRecordCount=2000``
    and ``supportsPagination=True`` (52,244 features nationally). It is queried
    by ``esriGeometryEnvelope`` + ``inSR=4326`` + ``f=geojson`` exactly like the
    ``fetch_hifld_critical_infrastructure`` / ``fetch_noaa_slr_scenarios`` ESRI
    REST pattern, but the geometries are polylines rather than points.

    NOTE: this layer is on a DIFFERENT public ArcGIS org
    (``Hp6G80Pky0om7QvQ``) than the HIFLD point mirror
    (``C8EMgrsFcRFL6LrL`` used by ``fetch_hifld_critical_infrastructure``) —
    the point mirror does not host the transmission-line polyline service.
    Both are keyless.

**What it does:**
    Fetches all transmission-line segments intersecting the bbox, paginating if
    the bbox holds more than one server page, and serializes them to a
    FlatGeobuf vector layer in EPSG:4326 with the HIFLD attribute columns
    preserved. A ``infra_type`` / ``infra_label`` column is added so the layer
    self-describes for narration and downstream overlay.

**When to use:**
    - User asks "where are the power transmission lines near [place]?",
      "show the electric grid / power lines in [bbox]", or "what high-voltage
      lines cross this area?".
    - A hazard / exposure workflow needs the electric-power transmission network
      inside a flood, fire, surge, or earthquake footprint (intersect with
      ``compute_zonal_statistics`` or ``clip_vector_to_polygon`` to find lines
      at risk).
    - Lifeline-infrastructure resilience analysis: which transmission corridors
      and at what voltage class are exposed to a modeled hazard.
    - Pairs with ``fetch_hifld_critical_infrastructure(facility_type="power_plants")``
      to show generation points plus the lines that move that power.

**When NOT to use:**
    - For power PLANTS / hospitals / schools / fire / police POINTS ->
      ``fetch_hifld_critical_infrastructure``.
    - For dams -> ``fetch_usace_dams``. For the building stock -> ``fetch_usace_nsi``.
    - For non-US areas — HIFLD coverage is US (50 states + DC + territories).
    - For real-time grid load / outage status — HIFLD is a static inventory.
      (Live outage data is a separate, event-driven source.)
    - For local distribution lines / service drops — HIFLD covers the
      bulk-power TRANSMISSION network, not last-mile distribution.

**Parameters:**
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required —
        ``supports_global_query=False`` (a national sweep is 52k+ polylines).
        Example for Houston metro: ``(-95.8, 29.5, -95.0, 30.1)`` (569 lines).
    min_voltage_kv: Optional nominal-voltage floor in kV. When provided, only
        segments with ``VOLTAGE >= min_voltage_kv`` are returned (server-side
        ``where`` filter). Useful to isolate high-voltage backbone (e.g.
        ``230`` or ``345``). Default ``None`` returns all voltage classes.

**Returns:**
    ``LayerURI`` pointing at a FlatGeobuf of ``LineString`` / ``MultiLineString``
    features in EPSG:4326. Properties include the HIFLD source columns (ID, TYPE,
    STATUS, OWNER, VOLTAGE, VOLT_CLASS, SUB_1, SUB_2, ...) plus ``infra_type``
    (str, always ``"transmission_line"``) and ``infra_label`` (str). ``layer_type``
    is ``"vector"``, ``role="primary"``, ``style_preset="hifld_transmission_lines"``,
    ``units="kV"``.

**Cache:** ``static-30d`` — HIFLD inventories update infrequently (annual-ish).

**FR-AS-11 typed-error surface:** ``HIFLDTransmissionError`` (base,
retryable=True), ``HIFLDTransmissionInputError`` (bad bbox / min_voltage_kv,
non-retryable), ``HIFLDTransmissionUpstreamError`` (ArcGIS REST network / HTTP /
parse failure, retryable), ``HIFLDTransmissionEmptyError`` (no segments — NOT
raised by default; an empty FGB is serialized so the layer still appears with a
zero-feature notice).

``supports_global_query=False``. No API key required.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_hifld_transmission_lines",
    "estimate_payload_mb",
    "HIFLDTransmissionError",
    "HIFLDTransmissionInputError",
    "HIFLDTransmissionUpstreamError",
    "HIFLDTransmissionEmptyError",
    "_validate_bbox",
    "_validate_min_voltage",
    "_round_bbox_to_6dp",
    "_build_query_url",
    "_fetch_features_paginated",
    "_features_to_flatgeobuf",
    "_fetch_transmission_bytes",
    "TRANSMISSION_SERVICE_URL",
    "INFRA_LABEL",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hazard.fetch_hifld_transmission_lines")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class HIFLDTransmissionError(RuntimeError):
    """Base class for fetch_hifld_transmission_lines failures."""

    error_code: str = "HIFLD_TRANSMISSION_ERROR"
    retryable: bool = True


class HIFLDTransmissionInputError(HIFLDTransmissionError):
    """Caller passed an invalid bbox or min_voltage_kv."""

    error_code = "HIFLD_TRANSMISSION_INPUT_INVALID"
    retryable = False


class HIFLDTransmissionUpstreamError(HIFLDTransmissionError):
    """HIFLD ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "HIFLD_TRANSMISSION_UPSTREAM_ERROR"
    retryable = True


class HIFLDTransmissionEmptyError(HIFLDTransmissionError):
    """No transmission lines found in bbox.

    NOT raised by default (a bbox over open water / remote interior
    legitimately has no transmission lines); an empty FGB is serialized
    instead. Kept for strict-mode opt-in.
    """

    error_code = "HIFLD_TRANSMISSION_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Public, unauthenticated ArcGIS Online org hosting the national HIFLD
#: ``Electric Power Transmission Lines`` polyline layer (HIFLD Open /
#: geoplatform-published). Verified 2026-06-27: ``esriGeometryPolyline``,
#: ``maxRecordCount=2000``, ``supportsPagination=True``, 52,244 features.
#: This is a DIFFERENT org than the HIFLD point mirror (``C8EMgrsFcRFL6LrL``):
#: the point mirror does not host this polyline service.
TRANSMISSION_SERVICE_URL = (
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
    "Electric_Power_Transmission_Lines/FeatureServer/0/query"
)

#: Human label for the layer (self-describing column + LayerURI name).
INFRA_LABEL = "Electric Power Transmission Line"

#: Self-describing infra_type column value.
INFRA_TYPE = "transmission_line"

# User-Agent — identify this client clearly to the ESRI cluster.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Server-enforced max page size on this FeatureService.
_PAGE_SIZE = 2000

# HTTP request timeout (seconds).
_HTTP_TIMEOUT_S = 45.0

# Hard cap on features paginated in one call — keeps FGB + S3 write tractable.
# A large state-sized bbox can run several thousand line segments. Callers
# wanting more should narrow the bbox.
_MAX_TOTAL_FEATURES = 30_000

# Payload sizing: each polyline feature serializes to ~0.7 KB of FlatGeobuf
# on average (variable vertex count + ~18 scalar HIFLD attributes). The
# Houston metro prototype was 382 KB for 569 features (~0.67 KB/feature).
_BYTES_PER_FEATURE = 700
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 50.0

# Rough national line-segment density per square degree, used only by the
# advisory payload estimator. Houston metro (~0.48 sq-deg) returned 569
# segments -> ~1200/sq-deg; pad to a conservative round figure.
_DENSITY_PER_SQ_DEG = 1200


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_hifld_transmission_lines",
        ttl_class="static-30d",
        source_class="hifld_transmission_lines",
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
            "registering fetch_hifld_transmission_lines without them"
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
    """Estimate the FlatGeobuf payload size for a transmission-line fetch.

    Scales by bbox area and national line-segment density. Advisory only.
    """
    if bbox is None:
        area_sq_deg = 1.0
    else:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
            area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
        except (TypeError, ValueError):
            area_sq_deg = 1.0

    est_features = min(_MAX_TOTAL_FEATURES, max(1, int(area_sq_deg * _DENSITY_PER_SQ_DEG)))
    est_mb = (est_features * _BYTES_PER_FEATURE) / (1024 * 1024)
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est_mb))


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``HIFLDTransmissionInputError`` if bbox is invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise HIFLDTransmissionInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise HIFLDTransmissionInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise HIFLDTransmissionInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise HIFLDTransmissionInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HIFLDTransmissionInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _validate_min_voltage(min_voltage_kv: Any) -> float | None:
    """Normalize and validate the optional voltage floor.

    Returns ``None`` (no filter) or a non-negative float. Raises
    ``HIFLDTransmissionInputError`` for non-numeric / negative values.
    """
    if min_voltage_kv is None:
        return None
    if isinstance(min_voltage_kv, bool) or not isinstance(min_voltage_kv, (int, float)):
        raise HIFLDTransmissionInputError(
            f"min_voltage_kv must be a number or None; got "
            f"{type(min_voltage_kv).__name__}: {min_voltage_kv!r}"
        )
    if not math.isfinite(min_voltage_kv) or min_voltage_kv < 0:
        raise HIFLDTransmissionInputError(
            f"min_voltage_kv must be a finite non-negative number; got {min_voltage_kv!r}"
        )
    return float(min_voltage_kv)


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(float(v), 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_query_url(
    bbox: tuple[float, float, float, float],
    *,
    min_voltage_kv: float | None = None,
    result_offset: int = 0,
) -> tuple[str, dict[str, str]]:
    """Build the HIFLD transmission-line ArcGIS REST query URL + params for one page.

    Queries layer 0 of the service for all polyline features intersecting the
    bbox in EPSG:4326, returned as GeoJSON, with stable pagination. When
    ``min_voltage_kv`` is set, the server-side ``where`` clause filters to
    ``VOLTAGE >= min_voltage_kv``.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    where = "1=1"
    if min_voltage_kv is not None:
        # VOLTAGE is a numeric (Double) field; -999999 / negative sentinels in
        # HIFLD denote unknown voltage and are naturally excluded by >=.
        where = f"VOLTAGE >= {min_voltage_kv:g}"
    params: dict[str, str] = {
        "where": where,
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
        "resultOffset": str(result_offset),
        "resultRecordCount": str(_PAGE_SIZE),
        # Stable cursor so pagination doesn't drop rows across page boundaries.
        "orderByFields": "OBJECTID ASC",
    }
    return TRANSMISSION_SERVICE_URL, params


# ---------------------------------------------------------------------------
# HTTP fetch with pagination.
# ---------------------------------------------------------------------------


def _fetch_one_page(url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    """GET one page of the HIFLD transmission FeatureService query.

    Raises ``HIFLDTransmissionUpstreamError`` on network / HTTP / parse /
    error-envelope.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise HIFLDTransmissionUpstreamError(
            f"HIFLD transmission request failed url={url}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise HIFLDTransmissionUpstreamError(
            f"HIFLD transmission returned HTTP {resp.status_code} url={url}: "
            f"{resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HIFLDTransmissionUpstreamError(
            f"HIFLD transmission returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise HIFLDTransmissionUpstreamError(
            f"HIFLD transmission response is not a JSON object: "
            f"type={type(body).__name__!r}"
        )

    if "error" in body:
        raise HIFLDTransmissionUpstreamError(
            f"HIFLD transmission query returned error envelope url={url}: "
            f"{body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise HIFLDTransmissionUpstreamError(
            f"HIFLD transmission response is not a GeoJSON FeatureCollection: "
            f"type={body.get('type')!r}"
        )

    return body.get("features", []) or []


def _fetch_features_paginated(
    bbox: tuple[float, float, float, float],
    *,
    min_voltage_kv: float | None = None,
    max_features: int = _MAX_TOTAL_FEATURES,
) -> list[dict[str, Any]]:
    """Page through the HIFLD transmission FeatureService up to ``max_features``."""
    accumulated: list[dict[str, Any]] = []
    offset = 0
    while True:
        url, params = _build_query_url(
            bbox, min_voltage_kv=min_voltage_kv, result_offset=offset
        )
        logger.info(
            "fetch_hifld_transmission_lines: GET %s offset=%d min_voltage_kv=%s",
            url,
            offset,
            min_voltage_kv,
        )
        page = _fetch_one_page(url, params)
        accumulated.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        if len(accumulated) >= max_features:
            logger.warning(
                "fetch_hifld_transmission_lines: hit max_features=%d cap; truncating",
                max_features,
            )
            accumulated = accumulated[:max_features]
            break
        offset += _PAGE_SIZE
    logger.info(
        "fetch_hifld_transmission_lines: bbox=%s -> %d feature(s)",
        bbox,
        len(accumulated),
    )
    return accumulated


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert HIFLD transmission-line features to FlatGeobuf bytes.

    Preserves the HIFLD source attributes (coercing non-scalar values to JSON
    strings) and adds ``infra_type`` + ``infra_label`` columns. Always emits a
    valid FlatGeobuf — an empty input yields a header-only FGB so the cache
    shim has a concrete artifact to persist (honest-empty path).

    Keeps both ``LineString`` and ``MultiLineString`` geometries; non-line
    geometries are dropped defensively.

    Raises ``HIFLDTransmissionUpstreamError`` if geopandas is unavailable or
    the FlatGeobuf write fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise HIFLDTransmissionUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None or geom.get("type") not in ("LineString", "MultiLineString"):
            continue
        coords = geom.get("coordinates")
        if not isinstance(coords, (list, tuple)) or len(coords) < 1:
            continue
        props = feat.get("properties") or {}
        row: dict[str, Any] = {}
        for k, v in props.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row[k] = v
        row["infra_type"] = INFRA_TYPE
        row["infra_label"] = INFRA_LABEL
        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": row,
        })

    if not cleaned:
        import pandas as pd
        empty_df = pd.DataFrame(columns=["infra_type", "infra_label"])
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_hifld_transmission_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise HIFLDTransmissionUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} transmission features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_hifld_transmission_lines: FlatGeobuf = %d bytes (%d feature(s))",
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


def _fetch_transmission_bytes(
    bbox: tuple[float, float, float, float],
    min_voltage_kv: float | None,
) -> bytes:
    """Fetch + paginate + serialize: (bbox, min_voltage_kv) -> FlatGeobuf bytes."""
    features = _fetch_features_paginated(bbox, min_voltage_kv=min_voltage_kv)
    return _features_to_flatgeobuf(features)


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
def fetch_hifld_transmission_lines(
    bbox: tuple[float, float, float, float],
    min_voltage_kv: float | None = None,
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """HIFLD electric power transmission lines within a bbox as a FlatGeobuf layer.

    Fetches national Homeland Infrastructure Foundation-Level Data (HIFLD)
    ``Electric Power Transmission Lines`` polyline segments intersecting a bbox,
    from a public, unauthenticated ArcGIS REST FeatureService. Returns a
    FlatGeobuf of ``LineString`` / ``MultiLineString`` features in EPSG:4326 with
    the HIFLD attribute payload (ID, TYPE, STATUS, OWNER, VOLTAGE, VOLT_CLASS,
    connected substations SUB_1 / SUB_2) plus ``infra_type`` / ``infra_label``.

    This is the LINE (power-grid backbone) complement to the POINT lifeline
    inventory in ``fetch_hifld_critical_infrastructure`` — pair the two
    (power_plants points + transmission lines) for full electric-power exposure.

    **When to use:**
    - "Where are the power transmission lines near [place]?"; "show the electric
      grid in this area"; "what high-voltage lines cross this footprint?".
    - A hazard / exposure workflow needs the transmission network inside a flood
      / fire / surge / earthquake footprint (intersect with
      ``compute_zonal_statistics`` or ``clip_vector_to_polygon``).
    - Lifeline resilience: which corridors / voltage classes are hazard-exposed.

    **When NOT to use:**
    - Power PLANTS / hospitals / schools / fire / police POINTS ->
      ``fetch_hifld_critical_infrastructure``. Dams -> ``fetch_usace_dams``.
    - Non-US areas (HIFLD is US-only). Real-time grid load / outage status
      (HIFLD is static). Last-mile distribution lines (HIFLD is bulk transmission).

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required —
            ``supports_global_query=False``. Example Houston metro:
            ``(-95.8, 29.5, -95.0, 30.1)``.
        min_voltage_kv: Optional nominal-voltage floor in kV. When set, only
            segments with ``VOLTAGE >= min_voltage_kv`` are returned (e.g.
            ``230`` or ``345`` to isolate the high-voltage backbone). Default
            ``None`` returns all voltage classes.

    **Returns:**
        ``LayerURI`` -> FlatGeobuf of ``LineString`` / ``MultiLineString``
        features in EPSG:4326. Properties carry the HIFLD source columns plus
        ``infra_type`` (always ``"transmission_line"``) and ``infra_label``.
        ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="hifld_transmission_lines"``, ``units="kV"``.

    **Error types (FR-AS-11):**
        - ``HIFLDTransmissionInputError``: bad bbox or min_voltage_kv
          (retryable=False).
        - ``HIFLDTransmissionUpstreamError``: HTTP/network failure, ArcGIS error
          envelope, or FlatGeobuf serialization failure (retryable=True).
        - ``HIFLDTransmissionEmptyError``: no segments in bbox (retryable=False;
          not raised by default — empty FGB is returned).

    Cache: ``ttl_class="static-30d"``, ``source_class="hifld_transmission_lines"``.
    Cache key is SHA-256 of ``(bbox-rounded-6dp, min_voltage_kv)``.

    ``supports_global_query=False``. No API key required.
    """
    # ---- Input validation ----
    _validate_bbox(bbox)
    v_floor = _validate_min_voltage(min_voltage_kv)
    q_bbox = _round_bbox_to_6dp(bbox)

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
    }
    if v_floor is not None:
        params["min_voltage_kv"] = v_floor

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_transmission_bytes(q_bbox, v_floor),
    )
    assert result.uri is not None, (
        "fetch_hifld_transmission_lines is cacheable; uri must be set by read_through"
    )

    v_tag = f" >={v_floor:g}kV" if v_floor is not None else ""
    return LayerURI(
        layer_id=(
            f"hifld-transmission-lines-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
            + (f"-ge{v_floor:g}kv" if v_floor is not None else "")
        ),
        name=(
            f"HIFLD Transmission Lines{v_tag} — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="hifld_transmission_lines",
        role="primary",
        units="kV",
        bbox=q_bbox,
    )
