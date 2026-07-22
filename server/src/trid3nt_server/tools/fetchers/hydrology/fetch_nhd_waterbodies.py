"""``fetch_nhd_waterbodies`` atomic tool — USGS NHD waterbody polygons (lakes/ponds).

Queries the USGS National Hydrography Dataset (NHDPlus High Resolution) public
ArcGIS REST service and returns a FlatGeobuf of the WATERBODY polygons (lakes,
ponds, reservoirs, estuaries, swamp/marsh open-water) that intersect a bbox.
KEY-FREE (public USGS TNM service, no token).

This is OPEN-WATER polygon extent — distinct from:
- ``fetch_nhdplus_nldi_navigate`` (NLDI FLOWLINE network navigation — lines, not
  waterbody polygons),
- ``fetch_jrc_global_surface_water`` (a global surface-water RASTER, not vectors),
- ``fetch_nwi_wetlands`` (VEGETATED / classified wetlands, not open water).

Each polygon carries its NHD ``permanent_identifier``, ``gnis_name`` (GNIS
place name, often null for small unnamed ponds), ``ftype`` (feature-type code:
390 LakePond, 436 Reservoir, 466 SwampMarsh, 361 Playa, 493 Estuary, 378
IceMass), ``fcode``, ``reachcode``, ``elevation``, and ``areasqkm``.

Endpoint (probed live 2026-07-20 over a Florida bbox, returns polygons):

    https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/9/query
    ?where=1=1
    &geometry={xmin,ymin,xmax,ymax}
    &geometryType=esriGeometryEnvelope
    &spatialRel=esriSpatialRelIntersects
    &inSR=4326 &outSR=4326
    &outFields=permanent_identifier,gnis_name,ftype,fcode,reachcode,elevation,areasqkm
    &f=geojson
    &resultRecordCount=1000
    &resultOffset={offset}

ENDPOINT NOTES (verified against the live service):
- Layer 9 (``NHDWaterbody``) of the NHDPlus_HR MapServer is the high-resolution
  waterbody polygon layer; ``maxRecordCount`` is 2000 and
  ``supportsPagination`` is True, so we page with ``resultOffset``.
- Field names on NHDPlus_HR are LOWERCASE (``gnis_name``, ``ftype``, ...); the
  medium-resolution fallback layer uses UPPERCASE, so ``_normalize_props``
  matches case-insensitively.

PRIMARY -> FALLBACK -> HONEST-ERROR (feedback_data_source_fallback_norm):
- PRIMARY: NHDPlus_HR MapServer layer 9 (``NHDWaterbody``), high resolution.
- FALLBACK: the medium-resolution ``nhd`` MapServer layer 12 (``Waterbody -
  Large Scale``), same feature class + field set, used when NHDPlus_HR is down.
- On both failing the tool raises
  ``NHDWaterbodiesUpstreamError(retryable=True)``; it NEVER fabricates a
  non-empty layer. An empty bbox over dry upland is a LEGITIMATE 0-feature
  FlatGeobuf, not an error.

Cache: ``static-30d`` (NHD updates on a slow national cadence). FR-TA-2 atomic
tool, returns ``LayerURI``. FR-DC-3/4: routed through ``read_through`` so
identical ``bbox`` calls reuse the cached FlatGeobuf. ``supports_global_query``
is False (national-scale polygon corpus; queries MUST be bbox-scoped).
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
    "fetch_nhd_waterbodies",
    "estimate_payload_mb",
    "NHDWaterbodiesError",
    "NHDWaterbodiesInputError",
    "NHDWaterbodiesUpstreamError",
    "NHD_WATERBODY_URL_PRIMARY",
    "NHD_WATERBODY_URL_FALLBACK",
    "NHD_FTYPE_LABELS",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_bbox_to_envelope",
    "_normalize_props",
    "_features_to_flatgeobuf",
    "_fetch_nhd_features",
    "_fetch_nhd_bytes",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_nhd_waterbodies")


# ---------------------------------------------------------------------------
# Typed-error surface (FR-AS-11).
# ---------------------------------------------------------------------------


class NHDWaterbodiesError(RuntimeError):
    """Base class for fetch_nhd_waterbodies failures."""

    error_code: str = "NHD_WATERBODIES_ERROR"
    retryable: bool = True


class NHDWaterbodiesInputError(NHDWaterbodiesError):
    """Caller passed an invalid bbox (degenerate, out of range, non-finite)."""

    error_code = "NHD_WATERBODIES_INPUT_INVALID"
    retryable = False


class NHDWaterbodiesUpstreamError(NHDWaterbodiesError):
    """USGS NHD ArcGIS REST query failed on BOTH the primary NHDPlus_HR path
    and the medium-resolution fallback."""

    error_code = "NHD_WATERBODIES_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: PRIMARY — USGS NHDPlus HR NHDWaterbody polygon layer (high resolution).
NHD_WATERBODY_URL_PRIMARY = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/"
    "NHDPlus_HR/MapServer/9/query"
)

#: FALLBACK — USGS medium-resolution NHD "Waterbody - Large Scale" layer, same
#: feature class + field set (UPPERCASE field names).
NHD_WATERBODY_URL_FALLBACK = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/"
    "nhd/MapServer/12/query"
)

#: NHD waterbody FTYPE code -> human label (surfaced for readable narration).
NHD_FTYPE_LABELS: dict[int, str] = {
    390: "LakePond",
    436: "Reservoir",
    466: "SwampMarsh",
    361: "Playa",
    493: "Estuary",
    378: "IceMass",
}

#: NHD outFields kept (lowercase; case-insensitively matched on the fallback).
_OUT_FIELDS = "permanent_identifier,gnis_name,ftype,fcode,reachcode,elevation,areasqkm"

#: Normalized output columns on the FlatGeobuf.
_OUT_COLUMNS: tuple[str, ...] = (
    "permanent_identifier",
    "gnis_name",
    "ftype",
    "ftype_label",
    "fcode",
    "reachcode",
    "elevation",
    "areasqkm",
)

#: Page size (== NHDPlus_HR maxRecordCount is 2000; 1000 is a safe round-trip).
_PAGE_SIZE = 1000

#: Safety cap on pagination iterations.
_MAX_PAGES = 100

#: Per-request timeout.
_HTTP_TIMEOUT_S = 60.0

#: User-Agent — USGS TNM asks for identifying agents.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Payload heuristic — waterbody density is moderate. ~0.4 MB / square degree.
_PAYLOAD_MB_PER_SQ_DEG = 0.4
_PAYLOAD_MIN_MB = 0.05
_PAYLOAD_MAX_MB = 50.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_nhd_waterbodies",
    ttl_class="static-30d",
    source_class="nhd_waterbodies",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# FR-DC-9 / Wave-1.5 payload-MB estimator hook.
# ---------------------------------------------------------------------------


def estimate_payload_mb(**args: Any) -> float:
    """Estimate the FlatGeobuf payload size (MB) for an NHD waterbody fetch.

    ~0.4 MB / square degree, clipped to [0.05, 50] MB. ``**args`` matches the
    Wave-1.5 estimator convention.
    """
    bbox = args.get("bbox")
    if not bbox or len(bbox) != 4:
        return _PAYLOAD_MAX_MB
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return _PAYLOAD_MAX_MB
    area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
    est = area_sq_deg * _PAYLOAD_MB_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NHDWaterbodiesInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise NHDWaterbodiesInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NHDWaterbodiesInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NHDWaterbodiesInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NHDWaterbodiesInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NHDWaterbodiesInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``esriGeometryEnvelope`` string."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


# ---------------------------------------------------------------------------
# Property normalization (case-insensitive HR-lowercase / medium-res UPPERCASE).
# ---------------------------------------------------------------------------


def _normalize_props(props: dict[str, Any]) -> dict[str, Any]:
    """Map the raw NHD property keys (any case) to the normalized output columns
    and derive ``ftype_label`` from the numeric ``ftype`` code."""
    low = {str(k).lower(): v for k, v in (props or {}).items()}
    ftype_raw = low.get("ftype")
    ftype_int: int | None
    try:
        ftype_int = int(ftype_raw) if ftype_raw is not None else None
    except (TypeError, ValueError):
        ftype_int = None
    return {
        "permanent_identifier": low.get("permanent_identifier"),
        "gnis_name": low.get("gnis_name"),
        "ftype": ftype_int,
        "ftype_label": NHD_FTYPE_LABELS.get(ftype_int) if ftype_int is not None else None,
        "fcode": low.get("fcode"),
        "reachcode": low.get("reachcode"),
        "elevation": low.get("elevation"),
        "areasqkm": low.get("areasqkm"),
    }


# ---------------------------------------------------------------------------
# HTTP fetch (one page).
# ---------------------------------------------------------------------------


def _nhd_query_one_page(
    url: str,
    bbox: tuple[float, float, float, float],
    offset: int,
) -> dict[str, Any]:
    """Fetch one page of an NHD waterbody query (GeoJSON). Raises
    ``NHDWaterbodiesUpstreamError`` on network / HTTP / non-JSON / error-envelope
    / non-FeatureCollection."""
    params = {
        "where": "1=1",
        "geometry": _bbox_to_envelope(bbox),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": _OUT_FIELDS,
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(
                url, params=params, headers={"User-Agent": _USER_AGENT}
            )
    except httpx.HTTPError as exc:
        raise NHDWaterbodiesUpstreamError(
            f"NHD request failed (network) url={url} offset={offset}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise NHDWaterbodiesUpstreamError(
            f"NHD returned HTTP {resp.status_code} url={url} offset={offset}: "
            f"{resp.text[:300]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NHDWaterbodiesUpstreamError(
            f"NHD returned non-JSON body url={url} offset={offset}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise NHDWaterbodiesUpstreamError(
            f"NHD response is not a JSON object url={url} offset={offset}"
        )
    if "error" in body:
        raise NHDWaterbodiesUpstreamError(
            f"NHD query returned error envelope url={url} offset={offset}: "
            f"{body['error']}"
        )
    if body.get("type") != "FeatureCollection":
        raise NHDWaterbodiesUpstreamError(
            f"NHD response is not a GeoJSON FeatureCollection url={url} "
            f"offset={offset}: type={body.get('type')!r}"
        )
    return body


def _fetch_from(
    url: str, bbox: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    """Paginate one NHD endpoint fully. Returns a list of GeoJSON Feature dicts."""
    all_features: list[dict[str, Any]] = []
    offset = 0
    for page_idx in range(_MAX_PAGES):
        payload = _nhd_query_one_page(url, bbox, offset)
        page_features = payload.get("features", []) or []
        all_features.extend(page_features)
        logger.info(
            "fetch_nhd_waterbodies: %s page %d offset=%d -> %d (total %d)",
            url.rsplit("/", 3)[-3],
            page_idx,
            offset,
            len(page_features),
            len(all_features),
        )
        exceeded = bool(
            payload.get("exceededTransferLimit")
            or (payload.get("properties") or {}).get("exceededTransferLimit")
        )
        if len(page_features) < _PAGE_SIZE and not exceeded:
            break
        if len(page_features) == 0:
            break
        offset += len(page_features)
    else:
        raise NHDWaterbodiesUpstreamError(
            f"NHD pagination exceeded {_MAX_PAGES} pages for bbox={bbox}; "
            "bbox is probably too large — reduce bbox extent."
        )
    return all_features


# ---------------------------------------------------------------------------
# Paginated fetch with primary -> fallback.
# ---------------------------------------------------------------------------


def _fetch_nhd_features(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch all waterbody polygons in the bbox: NHDPlus_HR primary, medium-res
    fallback. Returns a list of GeoJSON Feature dicts (possibly empty)."""
    try:
        return _fetch_from(NHD_WATERBODY_URL_PRIMARY, bbox)
    except NHDWaterbodiesUpstreamError as primary_exc:
        logger.warning(
            "fetch_nhd_waterbodies: NHDPlus_HR primary failed (%s); falling back "
            "to medium-resolution NHD Waterbody layer",
            primary_exc,
        )
        try:
            return _fetch_from(NHD_WATERBODY_URL_FALLBACK, bbox)
        except NHDWaterbodiesUpstreamError as fallback_exc:
            raise NHDWaterbodiesUpstreamError(
                f"both NHD waterbody endpoints failed for bbox={bbox}; "
                f"primary: {primary_exc}; fallback: {fallback_exc}"
            ) from fallback_exc


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert NHD waterbody GeoJSON features to FlatGeobuf bytes, keeping the
    normalized waterbody columns. Always emits valid FlatGeobuf bytes (an empty
    bbox over dry upland yields an empty-schema FGB — LEGITIMATE, not an error).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NHDWaterbodiesUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        cleaned.append(
            {
                "type": "Feature",
                "properties": _normalize_props(feat.get("properties") or {}),
                "geometry": geom,
            }
        )

    if not cleaned:
        empty_cols: dict[str, list[Any]] = {k: [] for k in _OUT_COLUMNS}
        gdf = gpd.GeoDataFrame(
            empty_cols,
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_nhd_waterbodies_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NHDWaterbodiesUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} waterbody feature(s): {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "fetch_nhd_waterbodies: FlatGeobuf = %d bytes (%d feature(s))",
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


def _fetch_nhd_bytes(bbox: tuple[float, float, float, float]) -> bytes:
    """Fetch + serialize NHD waterbodies for one bbox to FlatGeobuf bytes."""
    features = _fetch_nhd_features(bbox)
    return _features_to_flatgeobuf(features)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    payload_mb_estimator_name="estimate_payload_mb",
    # readOnlyHint=True, openWorldHint=True (external public API),
    # destructiveHint=False, idempotentHint=True (cache dedups).
    open_world_hint=True,
)
def fetch_nhd_waterbodies(
    bbox: tuple[float, float, float, float],
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """USGS NHD waterbody polygons (lakes / ponds / reservoirs) as a vector layer.

    ROUTING — use this when the user wants OPEN-WATER BODY extent: "show the
    lakes / ponds / reservoirs here", "map the water bodies", "NHD waterbodies",
    "which lakes are in this area", or needs waterbody polygons to overlay on /
    intersect with a hazard footprint. KEY-FREE, US + territories, authoritative
    USGS National Hydrography Dataset (NHDPlus HR).

    Prefer THIS over:
    - ``fetch_nhdplus_nldi_navigate`` — that navigates the FLOWLINE (river/stream
      LINE) network from a seed point; this returns waterbody POLYGONS.
    - ``fetch_nwi_wetlands`` — that returns VEGETATED / classified wetlands
      (marshes, swamps); this returns OPEN water. Fetch both for "all water +
      wetland habitat".
    - ``fetch_jrc_global_surface_water`` — that is a global surface-water RASTER
      (occurrence %), not vector lake/pond polygons.
    Do NOT use for: coastal shoreline (``fetch_topobathy``), stream gauges
    (``fetch_usgs_nwis_gauges``), or flood extent (a SFINCS modeled layer).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. REQUIRED —
            NHD does not support a global query (national-scale polygon corpus).
            Recommended <= ~1 deg on a side. Example (Naples, FL):
            ``(-81.5, 26.0, -81.3, 26.2)``.

    Returns:
        ``LayerURI`` (``layer_type="vector"``, ``role="context"``,
        ``style_preset="nhd_waterbodies"``, ``units=None``) pointing at a
        FlatGeobuf in the cache bucket with per-polygon columns:
            permanent_identifier (str)   — stable NHD id
            gnis_name            (str)   — GNIS place name (null for many small ponds)
            ftype                (int)   — NHD feature-type code
            ftype_label          (str)   — human label (LakePond / Reservoir /
                                           SwampMarsh / Playa / Estuary / IceMass)
            fcode                (int)   — full NHD feature code
            reachcode            (str)   — NHD reach code
            elevation            (float) — waterbody elevation (source units)
            areasqkm             (float) — polygon area in square km
        An empty bbox over dry upland returns a valid 0-feature FlatGeobuf
        (NOT an error).

    Cross-tool dependencies:
        - Composed by ``analyze_affected_habitats`` (open-water channel of the
          habitat-impact assessment, alongside ``fetch_nwi_wetlands``).
        - Feeds ``compute_zonal_statistics`` (waterbody area inside a footprint)
          and ``clip_vector_to_polygon`` (waterbodies within a place).

    Resilience (feedback_data_source_fallback_norm): PRIMARY NHDPlus_HR ->
    FALLBACK medium-resolution NHD Waterbody -> honest
    ``NHDWaterbodiesUpstreamError(retryable=True)`` if both fail. Never fabricates
    a non-empty layer. Cache: ``static-30d``, keyed on ``bbox-rounded-6dp`` +
    month vintage.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(bbox)

    result = read_through(
        metadata=_METADATA,
        params={"bbox": list(q_bbox)},
        ext="fgb",
        fetch_fn=lambda: _fetch_nhd_bytes(q_bbox),
    )
    assert result.uri is not None, (
        "fetch_nhd_waterbodies is cacheable; uri must be set by read_through"
    )

    name = (
        f"NHD Waterbodies - bbox "
        f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
    )
    return LayerURI(
        layer_id=f"nhd-waterbodies-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="nhd_waterbodies",
        role="context",
        units=None,
    )
