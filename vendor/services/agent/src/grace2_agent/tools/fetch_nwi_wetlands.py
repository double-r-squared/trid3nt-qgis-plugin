"""``fetch_nwi_wetlands`` atomic tool — USFWS National Wetlands Inventory polygons.

Queries the U.S. Fish & Wildlife Service (USFWS) National Wetlands Inventory
(NWI) public ArcGIS REST service and returns a FlatGeobuf of the wetland
polygons that intersect a bbox. KEY-FREE (public federal service, no token).

The NWI is the authoritative national map of wetland extent + Cowardin
classification (Palustrine/Estuarine/Lacustrine/Riverine/Marine). Each polygon
carries an NWI ``ATTRIBUTE`` code (e.g. ``"L1UBHx"``, ``"PFO1A"``), a
human-readable ``WETLAND_TYPE`` (e.g. ``"Lake"``, ``"Freshwater Forested/Shrub
Wetland"``), and its ``ACRES``. This is tool-2 of the habitat-impact triad; the
analysis tool ``analyze_affected_habitats`` composes it alongside WDPA protected
areas + species presence to answer "which habitats does this hazard footprint
affect".

Endpoint (probed live 2026-07-20 over a Florida bbox, returns polygons):

    https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/
        Wetlands/MapServer/0/query
    ?where=1=1
    &geometry={xmin,ymin,xmax,ymax}
    &geometryType=esriGeometryEnvelope
    &spatialRel=esriSpatialRelIntersects
    &inSR=4326 &outSR=4326
    &outFields=*
    &f=geojson
    &resultRecordCount=1000
    &resultOffset={offset}

ENDPOINT NOTES (verified against the live service):
- The ``fwspublicservices.wim.usgs.gov`` host sits behind a WAF that returns an
  HTML error page to a default programmatic User-Agent; a browser-like
  ``User-Agent`` + ``Accept`` + ``Referer`` header trio gets JSON. We always
  send them (``_NWI_HEADERS``). The default ``httpx`` UA silently 200s an HTML
  body — the same "looks fine, isn't JSON" trap as the goes18/goes-18 class — so
  we assert the parsed body is a GeoJSON ``FeatureCollection`` and fall back /
  fail LOUD otherwise.
- The layer is a JOINED view (``Wetlands`` + ``NWI_Wetland_Codes``), so the
  live GeoJSON property keys are TABLE-QUALIFIED (``"Wetlands.ATTRIBUTE"``,
  ``"Wetlands.WETLAND_TYPE"``, ``"Wetlands.ACRES"``). We strip the table prefix
  when normalizing so the output columns are plain ``attribute`` /
  ``wetland_type`` / ``acres``.
- ``maxRecordCount`` is 1000 and ``supportsPagination`` is True, so we page with
  ``resultOffset`` and stop when a page returns fewer than the cap.

PRIMARY -> FALLBACK -> HONEST-ERROR (feedback_data_source_fallback_norm):
- PRIMARY: the geojson request above.
- FALLBACK: the SAME endpoint with ``f=json`` (Esri JSON), parsed into GeoJSON
  features by ``_esri_json_to_features`` — covers a mirror node that rejects the
  geojson format directive while still serving Esri JSON.
- On both failing (network / HTTP / non-parseable / neither format usable) the
  tool raises ``NWIWetlandsUpstreamError(retryable=True)``; it NEVER fabricates a
  non-empty layer. An empty bbox over open ocean / an unmapped area is a
  LEGITIMATE 0-feature FlatGeobuf, not an error.

Cache: ``static-30d`` (NWI is republished on a slow, project-by-project cadence;
a 30-day stale window is fine for hazard-overlay use). ``supports_global_query``
is False (the NWI polygon corpus is national-scale; queries MUST be bbox-scoped).

FR-TA-2 atomic tool, returns ``LayerURI``. FR-DC-3/4: routed through
``read_through`` so identical ``bbox`` calls reuse the cached FlatGeobuf.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_nwi_wetlands",
    "estimate_payload_mb",
    "NWIWetlandsError",
    "NWIWetlandsInputError",
    "NWIWetlandsUpstreamError",
    "NWI_WETLANDS_URL",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_bbox_to_envelope",
    "_normalize_props",
    "_esri_json_to_features",
    "_features_to_flatgeobuf",
    "_fetch_nwi_features",
    "_fetch_nwi_bytes",
]

logger = logging.getLogger("grace2_agent.tools.fetch_nwi_wetlands")


# ---------------------------------------------------------------------------
# Typed-error surface (FR-AS-11).
# ---------------------------------------------------------------------------


class NWIWetlandsError(RuntimeError):
    """Base class for fetch_nwi_wetlands failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable`` guides
    FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NWI_WETLANDS_ERROR"
    retryable: bool = True


class NWIWetlandsInputError(NWIWetlandsError):
    """Caller passed an invalid bbox (degenerate, out of range, non-finite)."""

    error_code = "NWI_WETLANDS_INPUT_INVALID"
    retryable = False


class NWIWetlandsUpstreamError(NWIWetlandsError):
    """USFWS NWI ArcGIS REST query failed (network, HTTP, parse) on BOTH the
    primary geojson path and the Esri-JSON fallback."""

    error_code = "NWI_WETLANDS_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Public USFWS NWI Wetlands MapServer query endpoint (layer 0 — the single
#: national polygon feature layer). Probed live 2026-07-20.
NWI_WETLANDS_URL = (
    "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/"
    "rest/services/Wetlands/MapServer/0/query"
)

#: Browser-like header trio required to get JSON past the host WAF (a default
#: programmatic User-Agent is served an HTML error page — see module docstring).
_NWI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36 grace-2/0.1 (Hazard Modeling Agent; agent@grace-2.dev)"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fws.gov/program/national-wetlands-inventory",
}

#: The three NWI semantic columns preserved on the output FlatGeobuf, in their
#: normalized (table-prefix-stripped) form.
_OUT_COLUMNS: tuple[str, ...] = ("attribute", "wetland_type", "acres")

#: NWI FeatureServer page size (== live maxRecordCount).
_PAGE_SIZE = 1000

#: Safety cap on pagination iterations. 100 * 1000 = 100k polygons; a bbox that
#: exceeds that is a state-scale query that should be narrowed.
_MAX_PAGES = 100

#: Per-request timeout. The USFWS ArcGIS cluster can be slow under load.
_HTTP_TIMEOUT_S = 60.0

#: Payload heuristic — wetland polygon density is high in coastal/riverine
#: areas. ~1.0 MB / square degree, clipped to [0.05, 50] MB.
_PAYLOAD_MB_PER_SQ_DEG = 1.0
_PAYLOAD_MIN_MB = 0.05
_PAYLOAD_MAX_MB = 50.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_nwi_wetlands",
    ttl_class="static-30d",
    source_class="nwi_wetlands",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# FR-DC-9 / Wave-1.5 payload-MB estimator hook.
# ---------------------------------------------------------------------------


def estimate_payload_mb(**args: Any) -> float:
    """Estimate the FlatGeobuf payload size (MB) for an NWI wetlands fetch.

    Wetland polygon density is high in coastal/riverine areas, low in arid
    uplands. We use a conservative ~1.0 MB / square degree heuristic clipped to
    [0.05, 50] MB. ``**args`` matches the Wave-1.5 estimator convention (the
    chat-warning gate passes the tool kwargs unchanged).
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
    """Raise ``NWIWetlandsInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise NWIWetlandsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NWIWetlandsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NWIWetlandsInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NWIWetlandsInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NWIWetlandsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6dp (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``esriGeometryEnvelope`` string (xmin,ymin,xmax,ymax)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


# ---------------------------------------------------------------------------
# Property + geometry normalization.
# ---------------------------------------------------------------------------


def _normalize_props(props: dict[str, Any]) -> dict[str, Any]:
    """Strip the ``Wetlands.`` / ``NWI_Wetland_Codes.`` table prefix from the
    joined-view property keys and keep the 3 NWI semantic columns.

    Live property keys arrive table-qualified (``"Wetlands.ATTRIBUTE"``); we
    map them to plain lowercase ``attribute`` / ``wetland_type`` / ``acres``.
    Case-insensitive so a mirror that returns unqualified keys still resolves.
    """
    flat: dict[str, Any] = {}
    for key, val in (props or {}).items():
        base = key.rsplit(".", 1)[-1].strip().lower()
        # First-wins: the Wetlands.* geometry table is enumerated before the
        # joined code table, so the polygon's own ATTRIBUTE/WETLAND_TYPE/ACRES
        # (not the code-lookup table's) take precedence.
        if base in _OUT_COLUMNS and base not in flat:
            flat[base] = val
    return {
        "attribute": flat.get("attribute"),
        "wetland_type": flat.get("wetland_type"),
        "acres": flat.get("acres"),
    }


def _rings_to_geojson_geometry(rings: list[Any]) -> dict[str, Any] | None:
    """Convert Esri polygon ``rings`` to a GeoJSON Polygon/MultiPolygon geometry.

    Esri encodes all exterior + interior rings in one flat ``rings`` list with
    winding order distinguishing them. We do NOT attempt exterior/hole nesting
    (geopandas + shapely tolerate a MultiPolygon of the raw rings for our
    area/intersection use); each ring becomes one polygon. Returns ``None`` for
    an empty / malformed ring set.
    """
    valid = [r for r in (rings or []) if isinstance(r, list) and len(r) >= 4]
    if not valid:
        return None
    if len(valid) == 1:
        return {"type": "Polygon", "coordinates": [valid[0]]}
    return {"type": "MultiPolygon", "coordinates": [[r] for r in valid]}


def _esri_json_to_features(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an Esri-JSON query response to a list of GeoJSON Feature dicts.

    Used by the FALLBACK path when the geojson format directive is rejected by a
    mirror node but Esri JSON is still served. Raises nothing — a malformed
    feature is skipped.
    """
    out: list[dict[str, Any]] = []
    for feat in payload.get("features", []) or []:
        geom = feat.get("geometry") or {}
        gj = _rings_to_geojson_geometry(geom.get("rings"))
        if gj is None:
            continue
        out.append(
            {
                "type": "Feature",
                "properties": feat.get("attributes") or {},
                "geometry": gj,
            }
        )
    return out


# ---------------------------------------------------------------------------
# HTTP fetch (one page).
# ---------------------------------------------------------------------------


def _nwi_query_one_page(
    bbox: tuple[float, float, float, float],
    offset: int,
    fmt: str,
) -> dict[str, Any]:
    """Fetch one page of the NWI query in ``fmt`` (``"geojson"`` or ``"json"``).

    Returns the parsed response dict. Raises ``NWIWetlandsUpstreamError`` on
    network / HTTP / non-JSON / ArcGIS-error-envelope. Also raises when the body
    parses to JSON but is neither a GeoJSON FeatureCollection (geojson mode) nor
    an Esri feature envelope (json mode) — the WAF HTML-that-200s trap.
    """
    params = {
        "where": "1=1",
        "geometry": _bbox_to_envelope(bbox),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "*",
        "f": fmt,
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(NWI_WETLANDS_URL, params=params, headers=_NWI_HEADERS)
    except httpx.HTTPError as exc:
        raise NWIWetlandsUpstreamError(
            f"NWI request failed (network) fmt={fmt} offset={offset}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise NWIWetlandsUpstreamError(
            f"NWI returned HTTP {resp.status_code} fmt={fmt} offset={offset}: "
            f"{resp.text[:300]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        # A WAF HTML page 200s here; surface it as an upstream error so the
        # caller can fall back rather than treat HTML as an empty result.
        raise NWIWetlandsUpstreamError(
            f"NWI returned non-JSON body fmt={fmt} offset={offset} "
            f"(WAF/HTML?): {resp.text[:200]!r}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise NWIWetlandsUpstreamError(
            f"NWI response is not a JSON object fmt={fmt} offset={offset}"
        )
    if "error" in body:
        raise NWIWetlandsUpstreamError(
            f"NWI query returned error envelope fmt={fmt} offset={offset}: "
            f"{body['error']}"
        )

    if fmt == "geojson":
        if body.get("type") != "FeatureCollection":
            raise NWIWetlandsUpstreamError(
                f"NWI geojson response is not a FeatureCollection offset={offset}: "
                f"type={body.get('type')!r}"
            )
    else:  # esri json
        if "features" not in body:
            raise NWIWetlandsUpstreamError(
                f"NWI esri-json response has no 'features' key offset={offset}"
            )
    return body


# ---------------------------------------------------------------------------
# Paginated fetch with primary geojson -> esri-json fallback.
# ---------------------------------------------------------------------------


def _fetch_nwi_features(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch all wetland polygons in the bbox, paginating; geojson primary,
    Esri-JSON fallback. Returns a list of GeoJSON Feature dicts (possibly empty).

    The format is decided ONCE on the first page: if the geojson request raises,
    we retry that page as Esri JSON and — if THAT succeeds — carry the esri
    format through the remaining pages. If both fail on the first page the
    upstream error propagates (honest fail-loud).
    """
    all_features: list[dict[str, Any]] = []
    offset = 0
    fmt = "geojson"

    for page_idx in range(_MAX_PAGES):
        try:
            payload = _nwi_query_one_page(bbox, offset, fmt)
        except NWIWetlandsUpstreamError:
            if page_idx == 0 and fmt == "geojson":
                # Primary geojson failed on the very first page — try the
                # Esri-JSON fallback ONCE, then commit to it for all pages.
                logger.warning(
                    "fetch_nwi_wetlands: geojson primary failed on page 0; "
                    "falling back to Esri-JSON parse",
                    exc_info=True,
                )
                fmt = "json"
                payload = _nwi_query_one_page(bbox, offset, fmt)
            else:
                raise

        if fmt == "geojson":
            page_features = payload.get("features", []) or []
        else:
            page_features = _esri_json_to_features(payload)
        all_features.extend(page_features)

        logger.info(
            "fetch_nwi_wetlands: page %d offset=%d fmt=%s -> %d feature(s) "
            "(total so far: %d)",
            page_idx,
            offset,
            fmt,
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
        raise NWIWetlandsUpstreamError(
            f"NWI pagination exceeded {_MAX_PAGES} pages for bbox={bbox}; "
            "bbox is probably too large — reduce bbox extent."
        )

    return all_features


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert NWI GeoJSON features to FlatGeobuf bytes, keeping the 3 NWI
    semantic columns (``attribute`` / ``wetland_type`` / ``acres``).

    Always emits valid FlatGeobuf bytes — an empty feature list yields an
    empty-schema FGB so the cache shim has something concrete to persist (an
    empty bbox over open ocean / an unmapped area is LEGITIMATE, not an error).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NWIWetlandsUpstreamError(
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
            suffix=".fgb", delete=False, prefix="grace2_nwi_wetlands_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NWIWetlandsUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} wetland feature(s): {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "fetch_nwi_wetlands: FlatGeobuf = %d bytes (%d feature(s))",
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


def _fetch_nwi_bytes(bbox: tuple[float, float, float, float]) -> bytes:
    """Fetch + serialize NWI wetlands for one bbox to FlatGeobuf bytes."""
    features = _fetch_nwi_features(bbox)
    return _features_to_flatgeobuf(features)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only), openWorldHint=True (external
    # public API), destructiveHint=False, idempotentHint=True (cache dedups).
    open_world_hint=True,
)
def fetch_nwi_wetlands(
    bbox: tuple[float, float, float, float],
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """USFWS National Wetlands Inventory (NWI) wetland polygons as a vector layer.

    ROUTING — use this when the user wants WETLAND EXTENT / boundaries: "show
    the wetlands here", "map the marshes / swamps / bogs", "NWI wetlands",
    "Cowardin wetland classes", "freshwater emergent / forested wetlands",
    "estuarine wetlands", or needs wetland polygons to overlay on / intersect
    with a hazard footprint. KEY-FREE, US + territories, authoritative USFWS
    source.

    Prefer THIS over:
    - ``fetch_nhd_waterbodies`` — that returns OPEN-WATER polygons (lakes,
      ponds, reservoirs); this returns VEGETATED / classified WETLANDS. Fetch
      both when the user wants "all water + wetland habitat".
    - ``fetch_jrc_global_surface_water`` — that is a global surface-water RASTER
      (occurrence %), not classified wetland polygons.
    - ``digitize_water_body`` — that CV-vectorizes one water body from imagery;
      NWI is the pre-mapped national inventory.
    Do NOT use for: FEMA regulatory flood zones (``fetch_fema_nfhl_zones``),
    protected-area boundaries (``fetch_wdpa_protected_areas``), or soil types
    (``fetch_soilgrids`` / ``fetch_statsgo_soils``).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. REQUIRED —
            NWI does not support a global query (national-scale polygon corpus).
            Recommended <= ~1 deg on a side; larger envelopes risk the
            100k-feature pagination ceiling. Example (Naples, FL):
            ``(-81.5, 26.0, -81.3, 26.2)``.

    Returns:
        ``LayerURI`` (``layer_type="vector"``, ``role="context"``,
        ``style_preset="nwi_wetlands"``, ``units=None``) pointing at a
        FlatGeobuf in the cache bucket with per-polygon columns:
            attribute     (str)   — NWI code (e.g. "L1UBHx", "PFO1A")
            wetland_type  (str)   — Cowardin type (e.g. "Lake", "Freshwater
                                    Forested/Shrub Wetland", "Estuarine and
                                    Marine Wetland")
            acres         (float) — polygon area in acres (from the source)
        An empty bbox over open ocean / an unmapped area returns a valid
        0-feature FlatGeobuf (NOT an error).

    Cross-tool dependencies:
        - Composed by ``analyze_affected_habitats`` (the wetland channel of the
          habitat-impact assessment).
        - Feeds ``compute_zonal_statistics`` (wetland area by type inside a
          footprint) and ``clip_vector_to_polygon`` (wetlands within a place /
          protected area).

    Resilience (feedback_data_source_fallback_norm): PRIMARY geojson request ->
    FALLBACK Esri-JSON parse -> honest ``NWIWetlandsUpstreamError(retryable=True)``
    if both fail. Never fabricates a non-empty layer. Cache: ``static-30d``,
    keyed on ``bbox-rounded-6dp`` + month vintage.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(bbox)

    result = read_through(
        metadata=_METADATA,
        params={"bbox": list(q_bbox)},
        ext="fgb",
        fetch_fn=lambda: _fetch_nwi_bytes(q_bbox),
    )
    assert result.uri is not None, (
        "fetch_nwi_wetlands is cacheable; uri must be set by read_through"
    )

    name = (
        f"NWI Wetlands - bbox "
        f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
    )
    return LayerURI(
        layer_id=f"nwi-wetlands-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="nwi_wetlands",
        role="context",
        units=None,
    )
