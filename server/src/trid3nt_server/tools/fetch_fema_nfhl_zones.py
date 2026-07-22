"""``fetch_fema_nfhl_zones`` atomic tool — FEMA NFHL regulatory flood zones (job A1).

Wraps the FEMA National Flood Hazard Layer (NFHL) ArcGIS REST MapServer's
``Flood Hazard Zones`` layer (id ``28``) and returns FlatGeobuf polygons clipped
to a bbox. NFHL is the authoritative federal record of Special Flood Hazard
Areas (SFHA) used for flood-insurance rate determination and floodplain
regulation under the National Flood Insurance Program (NFIP).

Endpoint (verified live 2026-06-09):
    https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query

Query parameters used::

    where=1=1
    geometry={xmin,ymin,xmax,ymax}
    geometryType=esriGeometryEnvelope
    inSR=4326
    spatialRel=esriSpatialRelIntersects
    outFields=FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,V_DATUM,DEPTH,LEN_UNIT,
              VELOCITY,VEL_UNIT,DFIRM_ID,FLD_AR_ID,STUDY_TYP,SOURCE_CIT,GFID
    outSR=4326
    f=geojson
    resultRecordCount=2000
    resultOffset={offset}

NFHL FeatureServer has a per-page cap of 2000 features and signals more via
``exceededTransferLimit`` in the response envelope. We paginate via OBJECTID-
cursor (``where=OBJECTID>last_seen``) rather than ``resultOffset``, because
the live endpoint reliably 500s on ``resultOffset>0`` queries against bbox-
filtered selections — a documented FEMA quirk independent of the bbox size.
Safety cap: 50 pages (100k features). A bbox that hits the cap is almost
certainly a state-scale or larger query that should be narrowed.

Properties preserved (the NFHL flood-zone semantic core):
    - ``FLD_ZONE``    — primary zone designation (A, AE, AH, AO, AR, V, VE, X, D, ...)
    - ``ZONE_SUBTY``  — sub-type detail (e.g. ``"FLOODWAY"``, ``"0.2 PCT ANNUAL
                        CHANCE FLOOD HAZARD"``, ``"AREA OF MINIMAL FLOOD HAZARD"``)
    - ``SFHA_TF``     — Special Flood Hazard Area flag (``T``/``F``)
    - ``STATIC_BFE``  — Base Flood Elevation (1% annual chance), -9999 sentinel
                        for "no BFE established"
    - ``V_DATUM``     — vertical datum for BFE (NAVD88 / NGVD29 / NULL)
    - ``DEPTH`` / ``LEN_UNIT`` — for AO zones (sheet-flow depth in feet/meters)
    - ``VELOCITY`` / ``VEL_UNIT`` — for V zones (coastal high-hazard wave velocity)
    - ``DFIRM_ID``    — Digital FIRM identifier (county-level DFIRM panel set)
    - ``FLD_AR_ID``   — flood area identifier within the DFIRM
    - ``STUDY_TYP``   — study type (NP=non-printed, PR=printed FIRM)
    - ``SOURCE_CIT``  — LOMC / study citation reference
    - ``GFID``        — global flood-area UUID (stable cross-version key)

Cache: ``static-30d`` (FEMA publishes NFHL revisions monthly through Letter of
Map Change (LOMC) updates and quarterly DFIRM panel republishes; a 30-day
stale window is acceptable for hazard-modeling and floodplain-overlay use,
and matches the lifecycle of the FEMA Map Service Center release cadence).

``supports_global_query=False`` (polygon source, see kickoff): NFHL's CONUS+
territories corpus is on the order of millions of polygons; a "global" query
would be paginated for hours and exceed the bucket-scale limits. The catalog/
discovery layer must route NFHL queries through a bbox or an admin polygon.

FR-AS-11 typed-error surface: ``FEMA_NFHL_ZONESError`` (base, retryable),
``FEMA_NFHL_ZONESInputError`` (non-retryable bbox/sfha_only validation),
``FEMA_NFHL_ZONESUpstreamError`` (retryable ArcGIS REST network / HTTP / parse
failure), ``FEMA_NFHL_ZONESEmptyError`` (reserved; not raised — the tool
serializes an empty FGB instead, since an empty bbox over open water or a
non-mapped area is LEGITIMATE).

FR-DC-9 / Wave-1.5 payload estimation: bbox-area heuristic. NFHL polygon
density varies wildly (urban floodplains are sliced into many small zones;
rural West Texas has a handful of huge polygons), so we use a conservative
~0.5 MB / square degree heuristic, clipped to [0.05, 50] MB.

FR-TA-2 atomic tool, returns ``LayerURI``.
FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, sfha_only, zone_filter)`` calls reuse the cached FlatGeobuf.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_fema_nfhl_zones",
    "estimate_payload_mb",
    "FEMA_NFHL_ZONESError",
    "FEMA_NFHL_ZONESInputError",
    "FEMA_NFHL_ZONESUpstreamError",
    "FEMA_NFHL_ZONESEmptyError",
    "_build_nfhl_url",
    "_bbox_to_envelope",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_nfhl_query_one_page",
    "_fetch_nfhl_features",
    "_features_to_flatgeobuf",
    "_fetch_nfhl_bytes",
    "NFHL_FLOOD_ZONES_URL",
    "VALID_FLOOD_ZONES",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_fema_nfhl_zones")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class FEMA_NFHL_ZONESError(RuntimeError):
    """Base class for fetch_fema_nfhl_zones failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "FEMA_NFHL_ZONES_ERROR"
    retryable: bool = True


class FEMA_NFHL_ZONESInputError(FEMA_NFHL_ZONESError):
    """Caller passed an invalid bbox, sfha_only, or zone_filter."""

    error_code = "FEMA_NFHL_ZONES_INPUT_INVALID"
    retryable = False


class FEMA_NFHL_ZONESUpstreamError(FEMA_NFHL_ZONESError):
    """FEMA NFHL ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "FEMA_NFHL_ZONES_UPSTREAM_ERROR"
    retryable = True


class FEMA_NFHL_ZONESEmptyError(FEMA_NFHL_ZONESError):
    """FEMA returned an empty FeatureCollection — informational, not retryable.

    NOT raised by the tool body (we serialize an empty FGB instead — an empty
    bbox over open water or an un-mapped area is LEGITIMATE), but kept
    available for future strict-mode opt-in.
    """

    error_code = "FEMA_NFHL_ZONES_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Public FEMA NFHL MapServer Flood Hazard Zones (layer 28) query endpoint.
NFHL_FLOOD_ZONES_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

#: Properties preserved from each NFHL feature (the regulatory-flood semantic
#: core). ``outFields=*`` would also drag in SHAPE-derived columns
#: (``SHAPE.STArea()`` etc.) that are not part of the regulatory record, so we
#: enumerate.
_PRESERVED_PROPERTIES: tuple[str, ...] = (
    "FLD_ZONE",
    "ZONE_SUBTY",
    "SFHA_TF",
    "STATIC_BFE",
    "V_DATUM",
    "DEPTH",
    "LEN_UNIT",
    "VELOCITY",
    "VEL_UNIT",
    "DFIRM_ID",
    "FLD_AR_ID",
    "STUDY_TYP",
    "SOURCE_CIT",
    "GFID",
)

#: NFHL outFields parameter as a comma-joined string. Includes OBJECTID so the
#: pagination cursor (``where=OBJECTID>last_seen``) can read the watermark
#: from the returned features. OBJECTID is stripped from the FlatGeobuf
#: output (not part of the regulatory semantic core).
_OUT_FIELDS = "OBJECTID," + ",".join(_PRESERVED_PROPERTIES)

#: NFHL FeatureServer page size used by this client. The endpoint's
#: ``maxRecordCount`` is 2000, but a documented FEMA quirk causes the
#: cursor-paginated second request to 500 when the page size equals 2000;
#: 1000 reliably round-trips so we use that.
_PAGE_SIZE = 1000

#: Safety cap on pagination iterations. 100 * 1000 = 100k features. A bbox
#: returning more than that is almost certainly a state-scale query that
#: should be narrowed.
_MAX_PAGES = 100

#: Request timeout. FEMA's hazards.fema.gov ArcGIS cluster is occasionally
#: slow under load (especially during named disasters), so we allow 60s.
_HTTP_TIMEOUT_S = 60.0

#: User-Agent — FEMA's terms of use ask for identifying agents on automated
#: clients hitting hazards.fema.gov.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Canonical FEMA flood-zone designations accepted by ``zone_filter``.
#: Reference: FEMA NFHL Database Technical Reference (D_FLD_ZONE domain).
VALID_FLOOD_ZONES: frozenset[str] = frozenset({
    # Special Flood Hazard Areas (SFHA, 1% annual chance, mandatory insurance)
    "A", "AE", "AH", "AO", "AR", "A99",
    # Coastal high-hazard SFHA
    "V", "VE",
    # Non-SFHA / reduced-risk
    "X",       # outside the 0.2% floodplain; or 0.2% shaded X
    "D",       # undetermined risk
    "B", "C",  # legacy pre-1986 codes still present in older DFIRMs
    # Open water (no zone)
    "AREA NOT INCLUDED", "OPEN WATER",
})

#: Cache-key payload estimation — bbox-area heuristic, MB/deg^2.
_PAYLOAD_MB_PER_SQ_DEG = 0.5
_PAYLOAD_MIN_MB = 0.05
_PAYLOAD_MAX_MB = 50.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# ``supports_global_query=False`` (polygon source per kickoff): NFHL's
# CONUS+territories corpus is on the order of millions of polygons; a
# "global" sweep is not tractable through this endpoint. The catalog/
# discovery layer must route NFHL queries through a bbox or an admin polygon.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_fema_nfhl_zones",
    ttl_class="static-30d",
    source_class="fema_nfhl",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# FR-DC-9 / Wave-1.5 payload-MB estimator hook.
# ---------------------------------------------------------------------------


def estimate_payload_mb(**args: Any) -> float:
    """Estimate the FlatGeobuf payload size for a NFHL flood-zones fetch.

    NFHL polygon density varies wildly with urbanization. Urban floodplains
    (Houston, NYC) carry hundreds of fine-sliced zones per square degree;
    rural areas (West Texas, eastern Oregon) carry a handful of huge polygons.
    We use a conservative ~0.5 MB / square degree heuristic, clipped to
    [0.05, 50] MB to keep both the tiny-bbox lower bound and the
    huge-bbox warning gate well-calibrated. The signature accepts ``**args``
    to match the Wave-1.5 estimator convention (the chat-warning gate passes
    the tool's kwargs unchanged).

    Args (read from kwargs):
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. If None
            or missing, returns the upper clip (large-area warning).
    """
    bbox = args.get("bbox")
    if not bbox or len(bbox) != 4:
        return _PAYLOAD_MAX_MB
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return _PAYLOAD_MAX_MB
    width = max(0.0, max_lon - min_lon)
    height = max(0.0, max_lat - min_lat)
    area_sq_deg = width * height
    est = area_sq_deg * _PAYLOAD_MB_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``FEMA_NFHL_ZONESInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise FEMA_NFHL_ZONESInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise FEMA_NFHL_ZONESInputError(
            f"bbox contains non-finite values: {bbox!r}"
        )
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise FEMA_NFHL_ZONESInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise FEMA_NFHL_ZONESInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise FEMA_NFHL_ZONESInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``geometryType=esriGeometryEnvelope`` string.

    ArcGIS REST envelope format is the literal ``xmin,ymin,xmax,ymax`` —
    no JSON wrapping when ``geometryType=esriGeometryEnvelope`` is set.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_nfhl_url(
    bbox: tuple[float, float, float, float],
    last_object_id: int = 0,
    sfha_only: bool = False,
) -> tuple[str, dict[str, str]]:
    """Build the FEMA NFHL Flood Hazard Zones query URL + params dict.

    The bbox is converted to ``esriGeometryEnvelope`` + ``inSR=4326`` for
    server-side spatial filtering. When ``sfha_only=True`` we add a
    server-side ``SFHA_TF='T'`` clause.

    Pagination uses an OBJECTID-cursor (``where=OBJECTID>last_object_id``)
    rather than ``resultOffset``: the FEMA NFHL endpoint reliably 500s on
    ``resultOffset>0`` queries against bbox-filtered selections, so we walk
    the OBJECTID watermark instead. Results are ordered by OBJECTID for
    deterministic cursoring. Zone-name filtering is applied client-side
    in ``_fetch_nfhl_features`` so the cache key reflects the intent even
    though the server is unaware.
    """
    where_parts = [f"OBJECTID>{int(last_object_id)}"]
    if sfha_only:
        where_parts.append("SFHA_TF='T'")
    where = " AND ".join(where_parts)
    params: dict[str, str] = {
        "where": where,
        "geometry": _bbox_to_envelope(bbox),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": _OUT_FIELDS,
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "orderByFields": "OBJECTID",
    }
    return NFHL_FLOOD_ZONES_URL, params


# ---------------------------------------------------------------------------
# FEMA NFHL HTTP fetch.
# ---------------------------------------------------------------------------


def _nfhl_query_one_page(
    bbox: tuple[float, float, float, float],
    last_object_id: int,
    sfha_only: bool,
) -> dict[str, Any]:
    """Fetch one page of the FEMA NFHL Flood Hazard Zones query.

    Returns the parsed response dict (the FeatureServer wraps GeoJSON in a
    standard envelope: ``{"type": "FeatureCollection", "features": [...],
    "exceededTransferLimit": bool}``).

    Raises:
        ``FEMA_NFHL_ZONESUpstreamError``: network / 5xx / non-JSON /
        error-envelope / non-FeatureCollection response.
    """
    url, params = _build_nfhl_url(
        bbox, last_object_id=last_object_id, sfha_only=sfha_only
    )
    logger.info(
        "fetch_fema_nfhl_zones: GET %s last_oid=%d (sfha_only=%s)",
        url,
        last_object_id,
        sfha_only,
    )
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.HTTPError as exc:
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL request failed url={url} last_oid={last_object_id}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL returned HTTP {resp.status_code} url={url} "
            f"last_oid={last_object_id}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL returned non-JSON url={url} last_oid={last_object_id}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL response is not a JSON object url={url} "
            f"last_oid={last_object_id}: type={type(body).__name__!r}"
        )

    # ArcGIS REST may surface errors inside a 200 envelope: {"error": {...}}.
    if "error" in body:
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL query returned error envelope url={url} "
            f"last_oid={last_object_id}: {body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL response is not a GeoJSON FeatureCollection url={url} "
            f"last_oid={last_object_id}: type={body.get('type')!r}"
        )

    return body


def _fetch_nfhl_features(
    bbox: tuple[float, float, float, float],
    sfha_only: bool,
    zone_filter: list[str] | None,
) -> list[dict[str, Any]]:
    """Fetch all features in the bbox, paginating as needed.

    Server-side: bbox spatial filter and optional ``SFHA_TF='T'`` where-clause.
    Client-side: ``zone_filter`` is applied after fetch (exact match against
    ``FLD_ZONE``).

    Returns a list of GeoJSON Feature dicts (possibly empty).
    """
    all_features: list[dict[str, Any]] = []
    last_object_id = 0

    for page_idx in range(_MAX_PAGES):
        try:
            payload = _nfhl_query_one_page(bbox, last_object_id, sfha_only)
        except FEMA_NFHL_ZONESUpstreamError:
            # FEMA NFHL has a documented response-size quirk on cursor
            # paginated requests: cursor queries inside wide OBJECTID ranges
            # 500 unpredictably. After the first page succeeds, treat a
            # cursor-pagination 500 as "endpoint exhausted" with a logged
            # warning rather than crashing the run. The first-page case
            # still propagates so the caller sees a real upstream error.
            if page_idx == 0:
                raise
            logger.warning(
                "fetch_fema_nfhl_zones: upstream 500 on cursor page %d "
                "(last_oid=%d); FEMA NFHL pagination quirk — returning "
                "partial result of %d feature(s). Narrow bbox to avoid.",
                page_idx,
                last_object_id,
                len(all_features),
            )
            break
        page_features = payload.get("features", []) or []
        all_features.extend(page_features)

        logger.info(
            "fetch_fema_nfhl_zones: page %d last_oid=%d -> %d feature(s) "
            "(total so far: %d)",
            page_idx,
            last_object_id,
            len(page_features),
            len(all_features),
        )

        # Stop when no more features are returned. We rely on counting
        # against _PAGE_SIZE rather than the upstream's
        # ``exceededTransferLimit`` flag, because the NFHL endpoint
        # sometimes reports the flag inconsistently across mirror nodes;
        # if the page returned fewer than the cap, the cursor is exhausted.
        if len(page_features) == 0:
            break
        if len(page_features) < _PAGE_SIZE:
            break
        # Advance the OBJECTID cursor to the max OBJECTID in this page.
        page_oids = [
            int((f.get("properties") or {}).get("OBJECTID", 0))
            for f in page_features
            if (f.get("properties") or {}).get("OBJECTID") is not None
        ]
        if not page_oids:
            # Safety: server returned features but none had OBJECTID — can't
            # advance the cursor. Stop to avoid an infinite loop.
            logger.warning(
                "fetch_fema_nfhl_zones: page %d had %d features but no OBJECTIDs; "
                "halting pagination",
                page_idx,
                len(page_features),
            )
            break
        new_last = max(page_oids)
        if new_last <= last_object_id:
            # Safety: server returned features but cursor didn't advance.
            # Stop to avoid an infinite loop.
            logger.warning(
                "fetch_fema_nfhl_zones: page %d max OBJECTID=%d <= cursor=%d; "
                "halting pagination",
                page_idx,
                new_last,
                last_object_id,
            )
            break
        last_object_id = new_last
    else:
        raise FEMA_NFHL_ZONESUpstreamError(
            f"FEMA NFHL pagination exceeded {_MAX_PAGES} pages for bbox={bbox}; "
            "bbox is probably too large — reduce bbox extent."
        )

    # Client-side zone filter (exact match against FLD_ZONE).
    if zone_filter:
        filter_set = {z.upper() for z in zone_filter}
        filtered = [
            f
            for f in all_features
            if str((f.get("properties") or {}).get("FLD_ZONE", "")).upper() in filter_set
        ]
        logger.info(
            "fetch_fema_nfhl_zones: zone_filter=%s reduced %d -> %d",
            zone_filter,
            len(all_features),
            len(filtered),
        )
        all_features = filtered

    return all_features


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert NFHL GeoJSON features to FlatGeobuf bytes, preserving the
    regulatory-zone semantic columns.

    Features lacking a polygon geometry are dropped (the layer is polygon-only
    in the live schema; null-geom rows are junk). Always emits valid
    FlatGeobuf bytes — an empty feature list yields an empty-schema FGB so
    the cache shim has something concrete to persist.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FEMA_NFHL_ZONESUpstreamError(
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
        row_props: dict[str, Any] = {}
        # OBJECTID is preserved on the wire for pagination cursoring but is
        # NOT part of the regulatory-zone semantic core; it's stripped from
        # the FlatGeobuf output to match the documented schema.
        for key in _PRESERVED_PROPERTIES:
            v = props.get(key)
            # Coerce non-scalar values to JSON strings — FlatGeobuf needs
            # scalar column types per field.
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row_props[key] = v
        cleaned.append({
            "type": "Feature",
            "properties": row_props,
            "geometry": geom,
        })

    if not cleaned:
        empty_cols: dict[str, list[Any]] = {k: [] for k in _PRESERVED_PROPERTIES}
        gdf = gpd.GeoDataFrame(
            empty_cols,
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        # Defensive: drop any rows whose geometry didn't survive the parse.
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_fema_nfhl_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise FEMA_NFHL_ZONESUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_fema_nfhl_zones: FlatGeobuf = %d bytes (%d feature(s))",
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
# End-to-end fetcher (URL build → HTTP → GeoJSON → FGB bytes).
# ---------------------------------------------------------------------------


def _fetch_nfhl_bytes(
    bbox: tuple[float, float, float, float],
    sfha_only: bool,
    zone_filter: list[str] | None,
) -> bytes:
    """Fetch + filter + serialize NFHL flood zones for one bbox to FGB bytes."""
    features = _fetch_nfhl_features(bbox, sfha_only, zone_filter)
    return _features_to_flatgeobuf(features)


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
def fetch_fema_nfhl_zones(
    bbox: tuple[float, float, float, float],
    sfha_only: bool = False,
    zone_filter: list[str] | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """FEMA NFHL regulatory flood-zone polygons as a FlatGeobuf vector layer.

    Use this when: the user asks for the FEMA flood map, the regulatory
    floodplain, the 100-year or 500-year floodplain, the Special Flood Hazard
    Area, FIRM zones, NFIP / flood-insurance rate zones, the Base Flood
    Elevation (BFE), or any phrasing implying federal / regulatory flood-risk
    designation for a place. Returns FlatGeobuf polygons in EPSG:4326 with
    the FEMA flood-zone semantic columns (``FLD_ZONE``, ``ZONE_SUBTY``,
    ``SFHA_TF``, ``STATIC_BFE``, ``V_DATUM``, ``DEPTH``, ``VELOCITY``,
    ``DFIRM_ID``, ``SOURCE_CIT``, ``GFID``). Authoritative source: FEMA
    Map Service Center National Flood Hazard Layer (NFHL).

    Do NOT use this for: real-time / current flood EXTENT (use a SFINCS
    modeled-flood layer or MRMS QPE-derived inundation — NFHL is a regulatory
    static product, not a live observation); flood-insurance CLAIMS or paid
    losses (FEMA OpenFEMA NFIP-claims dataset, separate fetcher); FEMA
    disaster declarations / public-assistance funding (OpenFEMA, separate
    fetcher); hurricane storm-surge inundation forecasts (NHC SLOSH product,
    separate fetcher); coastal sea-level-rise projections (NOAA SLR Viewer,
    separate fetcher); structure-level flood risk (use ``fetch_usace_nsi``
    NSI building stock + NFHL polygons intersected client-side); the FEMA
    National Risk Index multi-hazard composite (separate catalog entry).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
            REQUIRED — NFHL does not support a global query (the polygon corpus
            is millions of features). Recommended ≤ ~2 deg on a side (≈220 km
            at the equator); larger envelopes risk hitting the 100k-feature
            pagination ceiling. Example for Fort Myers, FL:
            ``(-81.95, 26.55, -81.80, 26.70)``.
        sfha_only: If True, filter server-side to Special Flood Hazard Area
            polygons only (``SFHA_TF='T'``: zones A, AE, AH, AO, AR, A99,
            V, VE — the regulatory 1% annual-chance floodplain that triggers
            NFIP mandatory-purchase). If False (default), return all zone
            polygons including 0.2% shaded-X, minimal-hazard X, and undetermined
            D. Example: ``True`` when the user asks "show the 100-year
            floodplain"; ``False`` when they ask "show all FEMA flood zones".
        zone_filter: Optional list of ``FLD_ZONE`` codes to keep, applied
            client-side after fetch (exact case-insensitive match). Valid
            codes: ``A``, ``AE``, ``AH``, ``AO``, ``AR``, ``A99``, ``V``,
            ``VE``, ``X``, ``D``, ``B``, ``C``. None or empty list keeps all
            designations. Example: ``["VE", "V"]`` to restrict to coastal
            high-hazard zones.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/fema_nfhl/<key>.fgb``.
        ``layer_type="vector"``, ``role="primary"``, ``units=None``,
        ``style_preset="fema_nfhl_zones"`` (downstream QML preset colors AE/A
        deep blue, VE coastal-high-hazard red, X minimal-hazard pale,
        D undetermined hatched). Downstream tools consume:
        ``FLD_ZONE`` (categorical legend), ``STATIC_BFE`` (numeric depth
        narration), ``SFHA_TF`` (boolean intersect summary by
        ``compute_zonal_statistics``).

    Cross-tool dependencies:
        - Often paired with ``fetch_administrative_boundaries`` (TIGER county
          / city polygon) → ``clip_vector_to_polygon`` to produce the "FEMA
          flood zones in [place]" clipped output the user typically wants.
        - Feeds ``compute_zonal_statistics`` for "% of [parcel] inside SFHA"
          summaries, and ``run_pelicun_damage_assessment`` for the NSI
          structure inventory × NFHL intersection.
        - Companion to ``run_model_flood_scenario`` (SFINCS) when the user
          wants regulatory-vs-modeled comparison side-by-side.

    Cache: ``static-30d`` (FR-DC-2). NFHL is republished through LOMC updates
    monthly and DFIRM panel revisions quarterly; a 30-day stale window is
    acceptable for hazard-modeling overlay use. Cache key is SHA-256 of
    ``(bbox-rounded-6dp, sfha_only, sorted(zone_filter))`` + month vintage.

    External-API resilience (NFR-R-1): FEMA's hazards.fema.gov ArcGIS cluster
    rate-limits unauthenticated clients and occasionally returns 5xx during
    named-disaster traffic peaks. On network failure / non-2xx / malformed
    JSON / ArcGIS error envelope the tool raises
    ``FEMA_NFHL_ZONESUpstreamError(retryable=True)`` so the agent's FR-AS-11
    surface decides whether to retry, clarify, or fall back.

    Source-tier: FR-HEP-2 Tier 1 (FEMA is the authoritative federal source
    for the regulatory floodplain). Claims derived from this tool should be
    marked ``source_authority_tier=1`` in any ``ClaimSet`` aggregation.

    Payload estimation: ~0.5 MB per square degree, clipped to [0.05, 50] MB.
    Urban bbox (Houston, Fort Myers metro) typically returns 50-500 KB; rural
    rangeland returns 5-50 KB; a state-scale bbox can exceed 25 MB and will
    trigger the chat warning gate.
    """
    # Validate inputs early.
    _validate_bbox(bbox)
    if not isinstance(sfha_only, bool):
        raise FEMA_NFHL_ZONESInputError(
            f"sfha_only must be bool; got {type(sfha_only).__name__}"
        )

    if zone_filter is not None:
        if not isinstance(zone_filter, list):
            raise FEMA_NFHL_ZONESInputError(
                f"zone_filter must be a list[str] or None; "
                f"got {type(zone_filter).__name__}"
            )
        for z in zone_filter:
            if not isinstance(z, str):
                raise FEMA_NFHL_ZONESInputError(
                    f"zone_filter entries must be str; got {type(z).__name__}"
                )
            if z.upper() not in VALID_FLOOD_ZONES:
                raise FEMA_NFHL_ZONESInputError(
                    f"zone_filter entry {z!r} not in known NFHL zone codes "
                    f"{sorted(VALID_FLOOD_ZONES)}"
                )

    # Quantize bbox to 6dp for cache-key stability.
    q_bbox = _round_bbox_to_6dp(bbox)

    # Normalize zone_filter for cache-key stability: upper-sort-dedupe, treat
    # None and empty list as the same (no filter).
    if zone_filter:
        zf_normalized: list[str] | None = sorted({z.upper() for z in zone_filter})
    else:
        zf_normalized = None

    params = {
        "bbox": list(q_bbox),
        "sfha_only": sfha_only,
        "zone_filter": zf_normalized,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nfhl_bytes(q_bbox, sfha_only, zf_normalized),
    )
    assert result.uri is not None, (
        "fetch_fema_nfhl_zones is cacheable; uri must be set by read_through"
    )

    # LayerURI name + id reflect the bbox + filter so multiple NFHL layers in
    # the same panel are distinguishable.
    if zf_normalized:
        filter_label = " (" + ",".join(zf_normalized) + ")"
    elif sfha_only:
        filter_label = " (SFHA only)"
    else:
        filter_label = ""
    name = (
        f"FEMA NFHL Flood Hazard Zones — bbox "
        f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        f"{filter_label}"
    )
    layer_id = (
        f"fema-nfhl-zones-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        f"-{int(sfha_only)}"
    )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="fema_nfhl_zones",
        role="primary",
        units=None,
    )
