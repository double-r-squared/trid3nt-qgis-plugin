"""``fetch_usace_levees`` atomic tool — USACE National Levee Database (NLD) fetcher (job A4, Wave 4.10).

Wraps the USACE National Levee Database (NLD) ArcGIS REST FeatureService and
returns FlatGeobuf vector layers describing the nation's federally inventoried
levee systems — the canonical critical-infrastructure layer for flood demos and
flood-impact analysis.

Endpoint (verified live 2026-06-09):
    https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/
        NLD2_PUBLIC_v1/FeatureServer/{layer_id}/query

The NLD FeatureService publishes 17 layers; this tool exposes the three that
match the FR-PHC-4 critical-infrastructure use case:

    layer="leveed_areas"   (FeatureServer/16, esriGeometryPolygon)
        Polygons of the protected ("leveed") areas behind each levee system —
        the demographic + asset footprint that benefits from levee protection.
    layer="system_routes"  (FeatureServer/14, esriGeometryPolyline)
        Centerline routes of each levee system as authoritative single
        polylines (one per system_id). Best for overview / context layers.
    layer="embankments"    (FeatureServer/10, esriGeometryPolyline)
        Engineering-grade embankment alignment polylines (segment-level
        precision). Best when an analysis needs per-segment height /
        condition attributes.

Query parameters used:
    where=1=1
    geometry={bbox}            (esriGeometryEnvelope, inSR=4326)
    outFields=*
    outSR=4326
    f=geojson
    resultRecordCount={page}   (server-side pagination via resultOffset)

NLD properties preserved per use-case relevance (system identity, flood
risk, sponsor, condition rating). Anything else from the upstream is dropped
from the FlatGeobuf row.

Cache: ``static-30d`` (the NLD updates on a quarterly cadence; daily-actively-
changing data is the per-route inspection database, not the public catalog).
``cacheable=True``; ``source_class="usace_nld"``.

``supports_global_query=True`` (Wave 1.5 schema flag): when ``bbox=None``,
returns a CONUS sweep — the NLD's full national inventory is ~1500 systems
totalling ~10-30 MB GeoJSON, which the FeatureService paginates server-side.

FR-TA-2 / FR-AS-3 docstring discipline applies.
FR-DC-3/4: routed through ``read_through`` so identical ``(bbox, layer)``
calls reuse the cached FlatGeobuf.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from typing import Any, Literal

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_usace_levees",
    "USACELeveeError",
    "USACELeveeInputError",
    "USACELeveeUpstreamError",
    "USACELeveeEmptyError",
    "estimate_payload_mb",
    "_build_nld_url",
    "_bbox_to_envelope",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_fetch_nld_geojson",
    "_geojson_to_fgb",
    "_fetch_nld_bytes",
    "CONUS_BBOX",
    "LAYER_TO_FS_ID",
    "_LAYER_PROPERTIES",
]

logger = logging.getLogger("grace2_agent.tools.fetch_usace_levees")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class USACELeveeError(RuntimeError):
    """Base class for ``fetch_usace_levees`` failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback
    logic.
    """

    error_code: str = "USACE_LEVEES_ERROR"
    retryable: bool = True


class USACELeveeInputError(USACELeveeError):
    """Caller passed an invalid bbox or layer string."""

    error_code = "USACE_LEVEES_INPUT_INVALID"
    retryable = False


class USACELeveeUpstreamError(USACELeveeError):
    """NLD ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "USACE_LEVEES_UPSTREAM_ERROR"
    retryable = True


class USACELeveeEmptyError(USACELeveeError):
    """NLD returned an empty FeatureCollection — informational, not retryable.

    NOT raised by the tool body (we serialize an empty FGB instead — a bbox
    that contains zero federally inventoried levees is LEGITIMATE), but kept
    available for future strict-mode opt-in.
    """

    error_code = "USACE_LEVEES_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_NLD_BASE = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "NLD2_PUBLIC_v1/FeatureServer"
)

# Logical layer name → FeatureServer sub-layer id. Verified 2026-06-09 against
# https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NLD2_PUBLIC_v1/FeatureServer?f=json.
LAYER_TO_FS_ID: dict[str, int] = {
    "leveed_areas": 16,    # polygons — protected/leveed footprints
    "system_routes": 14,   # polylines — system centerlines (one per system_id)
    "embankments": 10,     # polylines — engineering-grade segment alignments
}

_VALID_LAYERS = frozenset(LAYER_TO_FS_ID.keys())

# Properties preserved per layer. NLD upstream rows carry ~30 fields each;
# we trim to the high-signal subset that downstream tools (Pelicun damage
# assessment, NWS flood-warning intersections, demographic overlays) actually
# consume. The set is per-layer because the layers carry different fields.
_LAYER_PROPERTIES: dict[str, tuple[str, ...]] = {
    "leveed_areas": (
        "OBJECTID",
        "SYSTEM_ID",
        "SYSTEM_NAME",
        "LEVEED_ID",
        "LEVEED_AREA_SQ_MI",
        "LEVEED_AREA_METHOD",
        "STATES",
        "COUNTIES",
        "COMMUNITY_NAMES",
        "DISTRICTS",
        "FEMA_REGION_NAMES",
        "FEMA_ACCREDITATION_RATING",
        "OVERTOPPING_ACE",
        "REHAB_PROGRAM_STATUS",
        "RESPONSIBLE_ORGANIZATION",
        "SPONSORS",
        "SPONSOR_TYPE",
        "FLOOD_SOURCES",
        "WARNING_SYSTEM",
    ),
    "system_routes": (
        "OBJECTID",
        "SYSTEM_ID",
        "SYSTEM_NAME",
        "ROUTE_ID",
        "SYSTEM_TYPE",
        "SYSTEM_AUTHORIZATION",
        "SYSTEM_IS_USACE",
        "AVERAGE_HEIGHT",
        "MAX_HEIGHT",
        "MIN_HEIGHT",
        "STATES",
        "COUNTIES",
        "DISTRICTS",
        "FEMA_REGION_NAMES",
        "FEMA_ACCREDITATION_RATING",
        "OVERTOPPING_ACE",
        "REHAB_PROGRAM_STATUS",
        "RESPONSIBLE_ORGANIZATION",
        "SPONSORS",
        "FLOOD_SOURCES",
    ),
    "embankments": (
        "OBJECTID",
        "SYSTEM_ID",
        "SYSTEM_NAME",
        # Embankment-segment-level fields use the same SYSTEM_* identity but
        # the upstream layer carries fewer engineering fields per row; we
        # keep a conservative high-signal subset.
        "STATES",
        "COUNTIES",
        "FEMA_REGION_NAMES",
        "FEMA_ACCREDITATION_RATING",
        "RESPONSIBLE_ORGANIZATION",
        "SPONSORS",
    ),
}

# User-Agent — USACE / Esri Hosted FeatureServices throttle anonymous clients;
# identifying the agent helps if NLD ops needs to contact us about heavy use.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Request timeout — Esri-hosted FeatureServer queries are usually fast, but a
# CONUS sweep with pagination can exceed 10 s on cold cache.
_HTTP_TIMEOUT_S = 45.0

# Server-side page size for the FeatureServer. The default cap is 2000 features
# per request; we ask for 1000 to stay comfortably under any per-tenant ceiling
# and use resultOffset to walk pages until exhausted.
_PAGE_SIZE = 1000

# Max pages to walk before bailing — defensive guard against pathological
# pagination loops if the FeatureServer mis-reports ``exceededTransferLimit``.
_MAX_PAGES = 20

# CONUS+AK+HI default envelope for bbox=None (NLD coverage is US-only).
CONUS_BBOX: tuple[float, float, float, float] = (-180.0, 13.0, -65.0, 72.0)


# ---------------------------------------------------------------------------
# Payload estimation (FR-DC-9 / Wave-1.5).
# ---------------------------------------------------------------------------

# Rough NLD payload size envelopes (verified 2026-06-09 against a NOLA bbox + a
# nationwide aggregate). Numbers are the FlatGeobuf size, not raw GeoJSON —
# FlatGeobuf compresses ~3x against GeoJSON for polyline / polygon vector data.
_PAYLOAD_PER_FEATURE_MB: dict[str, float] = {
    "leveed_areas": 0.020,    # polygons, ~20 KB per feature on average
    "system_routes": 0.005,   # polylines, single line per system_id
    "embankments": 0.008,     # polylines, segment-level alignments
}

# Approximate national feature counts (NLD ~1500 systems, ~3000 leveed areas,
# ~25000 embankment segments). Used to upper-bound the CONUS-sweep payload.
_APPROX_NATIONAL_COUNT: dict[str, int] = {
    "leveed_areas": 3000,
    "system_routes": 1500,
    "embankments": 25000,
}


def estimate_payload_mb(**args: Any) -> float:
    """FR-DC-9 / Wave-1.5 payload estimator hook (called by chat-warning gate).

    Conservatively estimates the FlatGeobuf payload size in MB based on
    ``layer`` and bbox area. The chat-warning gate uses this to surface a
    >25 MB warning or >250 MB hard-block on the user-facing chat before the
    fetch is dispatched.

    Args:
        layer: one of ``"leveed_areas"``, ``"system_routes"``, ``"embankments"``.
            Defaults to ``"leveed_areas"``.
        bbox: optional ``(min_lon, min_lat, max_lon, max_lat)``. ``None`` →
            CONUS upper bound.

    Returns:
        Estimated MB. For a CONUS sweep of "embankments" the upper bound is
        ~200 MB (25000 polyline segments). For a single-state bbox the
        estimate is typically <5 MB.
    """
    layer = args.get("layer", "leveed_areas")
    per_feat = _PAYLOAD_PER_FEATURE_MB.get(layer, 0.020)
    bbox = args.get("bbox")
    if bbox is None:
        return per_feat * _APPROX_NATIONAL_COUNT.get(layer, 3000)

    # Scale by bbox area as a fraction of CONUS. NLD coverage is concentrated
    # along major river systems, so this overstates payload for arid regions
    # and understates for the Mississippi corridor — acceptable for a warning
    # gate.
    try:
        min_lon, min_lat, max_lon, max_lat = bbox
        bbox_area = max(0.0, (max_lon - min_lon)) * max(0.0, (max_lat - min_lat))
    except (TypeError, ValueError):
        return per_feat * _APPROX_NATIONAL_COUNT.get(layer, 3000)
    # CONUS roughly 60° × 25° = 1500 sq deg.
    conus_area = 1500.0
    frac = min(1.0, bbox_area / conus_area) if conus_area > 0 else 0.05
    estimated_features = max(
        5, int(_APPROX_NATIONAL_COUNT.get(layer, 3000) * frac)
    )
    return per_feat * estimated_features


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# ``supports_global_query=True``: the tool meaningfully supports a CONUS
# sweep when ``bbox=None`` (NLD covers the entire US).
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_usace_levees",
    ttl_class="static-30d",
    source_class="usace_nld",
    cacheable=True,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``USACELeveeInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise USACELeveeInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise USACELeveeInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise USACELeveeInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise USACELeveeInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise USACELeveeInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``geometryType=esriGeometryEnvelope`` string.

    ArcGIS REST envelope format is literal ``xmin,ymin,xmax,ymax`` — no JSON
    wrapping when ``geometryType=esriGeometryEnvelope`` is set.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_nld_url(
    layer: str,
    bbox: tuple[float, float, float, float] | None,
    *,
    result_offset: int = 0,
    page_size: int = _PAGE_SIZE,
) -> tuple[str, dict[str, str]]:
    """Build the NLD FeatureServer query URL + params for one paginated page.

    When ``bbox`` is None, omits the geometry filter and returns a sweep over
    all features (pagination still applies). When a bbox is supplied, it is
    rendered as ``esriGeometryEnvelope`` + ``inSR=4326`` server-side spatial
    filter.
    """
    fs_id = LAYER_TO_FS_ID[layer]
    url = f"{_NLD_BASE}/{fs_id}/query"
    params: dict[str, str] = {
        "where": "1=1",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(page_size),
        "resultOffset": str(result_offset),
    }
    if bbox is not None:
        params["geometry"] = _bbox_to_envelope(bbox)
        params["geometryType"] = "esriGeometryEnvelope"
        params["spatialRel"] = "esriSpatialRelIntersects"
        params["inSR"] = "4326"
    return url, params


# ---------------------------------------------------------------------------
# NLD HTTP fetch (single page + pagination loop).
# ---------------------------------------------------------------------------


def _fetch_nld_page(
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """GET one NLD FeatureServer page and return parsed GeoJSON.

    Raises:
        ``USACELeveeUpstreamError``: network / 5xx / non-JSON / error-envelope /
        non-FeatureCollection response.
    """
    logger.info("fetch_usace_levees: GET %s with %d params", url, len(params))
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.HTTPError as exc:
        raise USACELeveeUpstreamError(
            f"USACE NLD request failed url={url}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise USACELeveeUpstreamError(
            f"USACE NLD returned HTTP {resp.status_code} url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise USACELeveeUpstreamError(
            f"USACE NLD returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise USACELeveeUpstreamError(
            f"USACE NLD response is not a JSON object url={url}: "
            f"type={type(body).__name__!r}"
        )

    # ArcGIS REST may surface errors inside a 200 envelope: {"error": {...}}.
    if "error" in body:
        raise USACELeveeUpstreamError(
            f"USACE NLD query returned error envelope url={url}: {body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise USACELeveeUpstreamError(
            f"USACE NLD response is not a GeoJSON FeatureCollection url={url}: "
            f"type={body.get('type')!r}"
        )

    return body


def _fetch_nld_geojson(
    layer: str,
    bbox: tuple[float, float, float, float] | None,
) -> dict[str, Any]:
    """Walk the FeatureServer pages until exhausted (or ``_MAX_PAGES``).

    Concatenates all feature pages into a single FeatureCollection.
    ``exceededTransferLimit`` (or ``properties.exceededTransferLimit`` in the
    GeoJSON dialect) drives the loop; absent the flag, we stop after the
    first page.
    """
    all_features: list[dict[str, Any]] = []
    offset = 0
    for page_idx in range(_MAX_PAGES):
        url, params = _build_nld_url(
            layer, bbox, result_offset=offset, page_size=_PAGE_SIZE
        )
        body = _fetch_nld_page(url, params)
        feats = body.get("features", []) or []
        all_features.extend(feats)
        logger.info(
            "fetch_usace_levees: page %d offset=%d got %d features",
            page_idx, offset, len(feats),
        )
        # ArcGIS GeoJSON output flags pagination via either top-level
        # ``exceededTransferLimit`` or ``properties.exceededTransferLimit``.
        exceeded = bool(
            body.get("exceededTransferLimit")
            or (body.get("properties") or {}).get("exceededTransferLimit")
        )
        if not exceeded or len(feats) == 0:
            break
        offset += len(feats)
    else:
        logger.warning(
            "fetch_usace_levees: hit _MAX_PAGES=%d without exhausting pagination",
            _MAX_PAGES,
        )

    return {"type": "FeatureCollection", "features": all_features}


# ---------------------------------------------------------------------------
# GeoJSON -> FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _geojson_to_fgb(
    geojson: dict[str, Any],
    layer: str,
) -> bytes:
    """Convert an NLD GeoJSON FeatureCollection to FlatGeobuf bytes.

    Preserves the layer-specific subset of properties (``_LAYER_PROPERTIES``).
    Features without a geometry are dropped (a null-geom levee row carries no
    spatial value for downstream consumers). Always emits a valid FlatGeobuf —
    an empty input yields a header-only FGB so the downstream chain stays
    happy.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise USACELeveeUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    preserved = _LAYER_PROPERTIES.get(layer, ())
    features = geojson.get("features", []) or []

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        props = feat.get("properties") or {}
        row_props: dict[str, Any] = {}
        for key in preserved:
            v = props.get(key)
            # Coerce non-scalar values to JSON strings — FlatGeobuf needs
            # scalar column types per field. NLD often carries list-shaped
            # fields like STATES, COUNTIES, COMMUNITY_NAMES.
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row_props[key] = v
        cleaned.append({
            "type": "Feature",
            "properties": row_props,
            "geometry": geom,
        })

    if not cleaned:
        gdf = gpd.GeoDataFrame(
            {k: [] for k in preserved},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        # Defensive: drop rows whose geometry didn't survive parse.
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_usace_levees_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise USACELeveeUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_usace_levees: FlatGeobuf = %d bytes (%d feature(s), layer=%s)",
            len(fgb_bytes), len(gdf), layer,
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# End-to-end fetcher (URL build → HTTP → GeoJSON → FGB bytes).
# ---------------------------------------------------------------------------


def _fetch_nld_bytes(
    layer: str,
    bbox: tuple[float, float, float, float] | None,
) -> bytes:
    """Build URL, walk pagination, fetch GeoJSON, convert to FlatGeobuf bytes."""
    geojson = _fetch_nld_geojson(layer, bbox)
    return _geojson_to_fgb(geojson, layer)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_usace_levees(
    bbox: tuple[float, float, float, float] | None = None,
    layer: Literal["leveed_areas", "system_routes", "embankments"] = "leveed_areas",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """USACE National Levee Database (NLD) features as a FlatGeobuf layer.

    The US-authoritative inventory of federally inspected levee infrastructure
    (~1500 levee systems / ~3000 leveed areas / ~25000 embankment segments).
    Wraps the USACE NLD ArcGIS REST FeatureService with server-side bbox
    filtering, pagination, and a per-layer property subset.

    Use this when:
    - User asks "what levees protect [city]?" / "where are the levees in
      [state]?" / "show me the New Orleans levees".
    - A flood-modeling workflow needs a levee footprint as a critical-
      infrastructure context overlay (breach risk, overtopping, FEMA
      accreditation).
    - Intersecting flood-warning polygons (``fetch_nws_alerts_conus``) or
      FEMA flood zones (``fetch_fema_nfhl_zones``) with leveed-area polygons
      to surface "X people / Y assets in protected areas under warning".

    Do NOT use this for:
    - Dam infrastructure (use a future ``fetch_usace_nid``).
    - Building / structure inventories behind levees (use
      ``fetch_usace_nsi`` or ``fetch_buildings``).
    - Flood-zone or floodplain regulatory polygons (use
      ``fetch_fema_nfhl_zones``).
    - Private / non-federal levees outside the NLD (small agricultural
      levees may not appear).
    - Historical breach / failure case studies (publications, not a
      FeatureService).

    Parameters:
        bbox: Optional ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326,
            each value in ``[-180, 180]`` / ``[-90, 90]`` with min < max.
            None → CONUS+AK+HI sweep. Example: ``(-90.3, 29.7, -89.7, 30.2)``
            for the New Orleans metro area.
        layer: One of ``"leveed_areas"`` (default; protected-area polygons,
            best for impact analysis), ``"system_routes"`` (centerline
            polylines, one per system_id, best for map overview), or
            ``"embankments"`` (segment-level alignment polylines, best for
            per-segment height / condition analysis).

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/usace_nld/<key>.fgb``.
        ``layer_type="vector"``, ``role="context"``, ``units=None``.
        Properties preserved per ``_LAYER_PROPERTIES[layer]``: at minimum
        ``SYSTEM_ID``, ``SYSTEM_NAME``, ``STATES``, ``COUNTIES``,
        ``FEMA_ACCREDITATION_RATING``, ``RESPONSIBLE_ORGANIZATION``.

    Cross-tool dependencies: typically downstream of ``geocode_location`` /
    ``fetch_administrative_boundaries`` to derive bbox; feeds
    ``clip_vector_to_polygon`` (clip to a state/county polygon),
    ``compute_zonal_statistics`` (population/asset rollups via
    ``fetch_buildings`` + ``fetch_hrsl_population``), and intersection with
    ``fetch_nws_alerts_conus`` flood warnings.

    Cache: ``static-30d`` (NLD updates quarterly). Cache key: SHA-256 of
    ``(bbox-rounded-6dp-or-"global", layer)``. Source-tier: FR-HEP-2 Tier 1
    (USACE federal authority). On upstream failure raises
    ``USACELeveeUpstreamError(retryable=True)`` per FR-AS-11.
    """
    # Validate inputs early — typos here are caller error, not retryable.
    if layer not in _VALID_LAYERS:
        raise USACELeveeInputError(
            f"layer={layer!r} not in {sorted(_VALID_LAYERS)}"
        )

    # bbox quantization for cache-key stability.
    q_bbox: tuple[float, float, float, float] | None
    if bbox is None:
        q_bbox = None
        bbox_for_params: list[float] | None = None
    else:
        _validate_bbox(bbox)
        q_bbox = _round_bbox_to_6dp(bbox)
        bbox_for_params = list(q_bbox)

    params = {
        "bbox": bbox_for_params,
        "layer": layer,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nld_bytes(layer, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_usace_levees is cacheable; uri must be set by read_through"
    )

    layer_labels = {
        "leveed_areas": "Leveed Areas (polygons)",
        "system_routes": "Levee System Routes (lines)",
        "embankments": "Embankments (lines)",
    }
    label = layer_labels.get(layer, layer.replace("_", " ").title())

    if q_bbox is None:
        name = f"USACE NLD — {label} — CONUS+AK+HI"
        layer_id = f"usace-levees-{layer}-global"
    else:
        name = (
            f"USACE NLD — {label} — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        )
        layer_id = (
            f"usace-levees-{layer}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="usace_levees",
        role="context",
        units=None,
    )
