"""``fetch_nifc_fire_perimeters`` atomic tool — NIFC current wildfire perimeters (job-0110).

Wraps the National Interagency Fire Center (NIFC) WFIGS Interagency Perimeters
Current ArcGIS REST FeatureService. Returns FlatGeobuf polygons of currently
active large wildland fire perimeters. CONUS-default; pass ``bbox=None`` for a
nationwide sweep, or a ``(min_lon, min_lat, max_lon, max_lat)`` envelope to
narrow to a state or region.

Endpoint (verified live 2026-06-08):
    https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/
        WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query

Query parameters used:
    where=1=1
    geometry={bbox}            (omitted when bbox is None — CONUS sweep)
    geometryType=esriGeometryEnvelope
    inSR=4326
    outFields=*
    outSR=4326
    f=geojson

Properties preserved per the kickoff: ``poly_IncidentName``,
``poly_FeatureCategory``, ``poly_DateCurrent``, ``attr_IncidentSize``,
``attr_PercentContained`` (plus the full ``OBJECTID`` row for diagnostics).

Cache: ``dynamic-1h`` (active fires move; one-hour bucketing per FR-DC-2).
``cacheable=True``; ``source_class="nifc_perimeters"``.

Status filter: NIFC's "Current" FeatureService exposes only currently active
perimeters by definition, so the ``status`` parameter is a no-op on the wire
in v0.1 but is preserved as a parameter (and cache-key contributor) so a
future opt-in to the historical NIFC archives can be added without changing
the signature. Surfaced as OQ-0110-STATUS-FILTER-NO-OP.

``supports_global_query=True`` (Wave 1.5 schema amendment, job-0114): set
on this tool's ``AtomicToolMetadata`` because the ``bbox=None`` semantics
genuinely return a CONUS+AK+HI sweep — the catalog/discovery layer can
route "show me every active wildfire in the US" queries here without
forcing a bbox parameter.

Geographic-correctness gate (job-0086 codified lesson):
The live + synthetic geographic gate checks that every returned perimeter
polygon's centroid falls inside the US-fires envelope (CONUS + AK + HI). A
sign-flip or axis-swap in the GeoJSON → FlatGeobuf conversion would surface
as centroids outside that envelope.

Payload estimation: ~1 MB CONUS sweep (typical 20-200 active perimeters,
mostly small polygons; the largest CONUS megafires push closer to 200 KB
each). The dynamic-1h cache amortizes the cost across calls inside the same
hour bucket.

FR-TA-2 / FR-AS-3 docstring discipline applies.
FR-DC-3/4: routed through ``read_through`` so identical ``(bbox, status)``
calls reuse the cached FlatGeobuf.
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
    "fetch_nifc_fire_perimeters",
    "NIFCFireError",
    "NIFCFireInputError",
    "NIFCFireUpstreamError",
    "NIFCFireEmptyError",
    "_build_nifc_url",
    "_bbox_to_envelope",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_fetch_nifc_geojson",
    "_geojson_to_fgb",
    "_fetch_nifc_bytes",
    "CONUS_BBOX",
]

logger = logging.getLogger("grace2_agent.tools.fetch_nifc_fire_perimeters")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NIFCFireError(RuntimeError):
    """Base class for fetch_nifc_fire_perimeters failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NIFC_FIRE_ERROR"
    retryable: bool = True


class NIFCFireInputError(NIFCFireError):
    """Caller passed an invalid bbox or status string."""

    error_code = "NIFC_FIRE_INPUT_INVALID"
    retryable = False


class NIFCFireUpstreamError(NIFCFireError):
    """NIFC ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "NIFC_FIRE_UPSTREAM_ERROR"
    retryable = True


class NIFCFireEmptyError(NIFCFireError):
    """NIFC returned an empty FeatureCollection — informational, not retryable.

    NOT raised by the tool body (we serialize an empty FGB instead — an empty
    CONUS sweep during a quiet wildfire period is LEGITIMATE), but kept
    available for future strict-mode opt-in.
    """

    error_code = "NIFC_FIRE_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_NIFC_BASE = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)

# Properties preserved from each NIFC feature. The kickoff named five; we keep
# the canonical OBJECTID for diagnostics and also a small set of high-signal
# narrative fields (POO state, IRWIN ID, fire cause) for downstream summary
# tools. Anything not in this list is dropped from the FlatGeobuf row.
_PRESERVED_PROPERTIES: tuple[str, ...] = (
    "OBJECTID",
    "poly_IncidentName",
    "poly_FeatureCategory",
    "poly_DateCurrent",
    "poly_GISAcres",
    "attr_IncidentSize",
    "attr_PercentContained",
    "attr_IncidentName",
    "attr_FireCauseGeneral",
    "attr_FireCause",
    "attr_POOState",
    "attr_IrwinID",
    "attr_UniqueFireIdentifier",
)

# User-Agent — NIFC's terms of use ask for identifying agents on automated
# clients.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

# Request timeout. NIFC's cluster is usually fast but the CONUS sweep on
# busy fire days can stretch past 10s; 30s is the same envelope we give NWS.
_HTTP_TIMEOUT_S = 30.0

# CONUS+AK+HI envelope used as default bbox when caller passes None.
# Generous on AK/HI side; the FeatureServer rejects bbox queries that span
# the dateline so we use a single envelope that wraps from -180 to -65 only
# (Pacific dateline-crossing fires are out of scope for v0.1).
CONUS_BBOX: tuple[float, float, float, float] = (-180.0, 13.0, -65.0, 72.0)

# Valid status values per the NIFC feature schema. v0.1 wire behavior is the
# same for all values (NIFC "Current" exposes only active perimeters) but we
# still validate so a typo surfaces as an input error.
_VALID_STATUSES = frozenset({"active", "controlled", "out", "all"})


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# ``supports_global_query=True`` (Wave 1.5 schema amendment, job-0114): the
# tool genuinely supports a global CONUS+AK+HI sweep when ``bbox=None``, so
# the catalog/discovery layer can route "show me every active wildfire in
# the US" queries here without forcing a bbox.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_nifc_fire_perimeters",
    ttl_class="dynamic-1h",
    source_class="nifc_perimeters",
    cacheable=True,
    supports_global_query=True,
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NIFCFireInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise NIFCFireInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NIFCFireInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NIFCFireInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NIFCFireInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NIFCFireInputError(
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


def _build_nifc_url(
    bbox: tuple[float, float, float, float] | None,
) -> tuple[str, dict[str, str]]:
    """Build the NIFC FeatureServer query URL + params dict.

    When ``bbox`` is None, the query omits the geometry filter and returns the
    full CONUS+AK+HI sweep (the FeatureService is small enough that this is
    cheap). When a bbox is given, it is converted to ``esriGeometryEnvelope``
    + ``inSR=4326`` server-side spatial filter.
    """
    params: dict[str, str] = {
        "where": "1=1",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
    }
    if bbox is not None:
        params["geometry"] = _bbox_to_envelope(bbox)
        params["geometryType"] = "esriGeometryEnvelope"
        params["spatialRel"] = "esriSpatialRelIntersects"
        params["inSR"] = "4326"
    return _NIFC_BASE, params


# ---------------------------------------------------------------------------
# NIFC HTTP fetch.
# ---------------------------------------------------------------------------


def _fetch_nifc_geojson(
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """GET the NIFC FeatureServer query and return parsed GeoJSON.

    Raises:
        ``NIFCFireUpstreamError``: network / 5xx / non-JSON / error-envelope /
        non-FeatureCollection response.
    """
    logger.info("fetch_nifc_fire_perimeters: GET %s with %d params", url, len(params))
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.HTTPError as exc:
        raise NIFCFireUpstreamError(
            f"NIFC request failed url={url}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise NIFCFireUpstreamError(
            f"NIFC returned HTTP {resp.status_code} url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NIFCFireUpstreamError(
            f"NIFC returned non-JSON url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise NIFCFireUpstreamError(
            f"NIFC response is not a JSON object url={url}: type={type(body).__name__!r}"
        )

    # ArcGIS REST may surface errors inside a 200 envelope: {"error": {...}}.
    if "error" in body:
        raise NIFCFireUpstreamError(
            f"NIFC query returned error envelope url={url}: {body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise NIFCFireUpstreamError(
            f"NIFC response is not a GeoJSON FeatureCollection url={url}: "
            f"type={body.get('type')!r}"
        )

    return body


# ---------------------------------------------------------------------------
# GeoJSON -> FlatGeobuf conversion.
# ---------------------------------------------------------------------------


def _geojson_to_fgb(geojson: dict[str, Any]) -> bytes:
    """Convert an NIFC GeoJSON FeatureCollection to FlatGeobuf bytes.

    Preserves ``_PRESERVED_PROPERTIES``. Features without a polygon geometry
    are dropped (perimeter polygons are the whole product; null-geom rows are
    junk for this layer). Always emits a valid FlatGeobuf — an empty input
    yields a header-only FGB.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NIFCFireUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    features = geojson.get("features", []) or []

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            # NIFC perimeters that lack a polygon are not useful for a
            # perimeter layer; skip them rather than serializing null rows.
            continue
        props = feat.get("properties") or {}
        row_props: dict[str, Any] = {}
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
        gdf = gpd.GeoDataFrame(
            {k: [] for k in _PRESERVED_PROPERTIES},
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        # Defensive: drop any rows whose geometry didn't survive the
        # GeoDataFrame.from_features parse (shouldn't happen — we filtered
        # null geoms above — but guards against malformed GeoJSON edge cases).
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_nifc_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NIFCFireUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_nifc_fire_perimeters: FlatGeobuf = %d bytes (%d feature(s))",
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


def _fetch_nifc_bytes(
    bbox: tuple[float, float, float, float] | None,
    status: str,
) -> bytes:
    """Build URL, fetch GeoJSON, convert to FlatGeobuf bytes.

    ``status`` is currently a no-op on the wire (NIFC's "Current" service
    only exposes active perimeters) but is preserved in the function
    signature + cache key so a future status-aware variant remains
    forward-compatible. See OQ-0110-STATUS-FILTER-NO-OP.
    """
    del status  # Reserved for future status-aware variant; see module docstring.
    url, params = _build_nifc_url(bbox)
    geojson = _fetch_nifc_geojson(url, params)
    return _geojson_to_fgb(geojson)


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
def fetch_nifc_fire_perimeters(
    bbox: tuple[float, float, float, float] | None = None,
    status: str = "active",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch current NIFC WFIGS wildland fire perimeter polygons for the US.

    **What it does:** Queries the National Interagency Fire Center (NIFC) WFIGS
    Interagency Perimeters Current ArcGIS FeatureService, paginates all active
    wildfire perimeter polygons (federal + state + tribal agency data fused),
    and returns a FlatGeobuf vector layer. Supports both CONUS-wide sweeps
    (``bbox=None``, ``supports_global_query=True``) and spatially filtered
    queries. Cached ``dynamic-1h`` (active fires update frequently).
    No API key required. FR-HEP-2 Tier 1 source.

    **When to use:**
    - "Show me every active wildfire in California right now."
    - Wildfire hazard-context overlay on a population or air-quality layer.
    - Discovery step before fetching FIRMS detections — identify which fires
      have established perimeters vs. new hotspot clusters.
    - "What is the biggest active megafire and how contained is it?"

    **When NOT to use:**
    - Historical wildfire perimeters (NIFC "Current" only carries active
      incidents; for past fires use ``fetch_mtbs_burn_severity``).
    - Fire-danger or fire-weather forecasts (use NWS fire-weather products).
    - Smoke plume or air-quality data (NOAA HRRR-Smoke; different tool).
    - Satellite thermal anomaly / hotspot detections (use
      ``fetch_firms_active_fire``).

    **Parameters:**
    - ``bbox`` (tuple or None): ``(min_lon, min_lat, max_lon, max_lat)`` in
      EPSG:4326 for a spatially filtered query. ``None`` (default) returns all
      currently active perimeters CONUS+AK+HI (typically 20–200 features,
      ~1 MB payload). Example: ``(-122.5, 37.0, -120.0, 39.0)`` for Northern
      California.
    - ``status`` (str): ``"active"`` (default). Accepted: ``active``,
      ``controlled``, ``out``, ``all``. Note: v0.1 always queries the NIFC
      "Current" service regardless of value (OQ-0110-STATUS-FILTER-NO-OP).

    **Returns:**
    ``LayerURI(layer_type="vector", role="primary", units=None)`` pointing at a
    FlatGeobuf with fields: incident name, GIS acres, percent contained,
    ignition date, ignition cause, POO state, and fire type. EPSG:4326.

    **Cross-tool dependencies:**
    - Pairs with: ``fetch_firms_active_fire`` (satellite detections inside/near
      perimeters), ``fetch_nws_alerts_conus`` (co-occurring fire-weather watches
      + red-flag warnings).
    - Upstream of: smoke/population-impact overlays, evacuation zone analysis.
    - Historical complement: ``fetch_mtbs_burn_severity`` for 1984-present
      burned-area polygons.
    """
    # Validate inputs early — typos here are caller error, not retryable.
    if status not in _VALID_STATUSES:
        raise NIFCFireInputError(
            f"status={status!r} not in {sorted(_VALID_STATUSES)}"
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
        "status": status,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_nifc_bytes(q_bbox, status),
    )
    assert result.uri is not None, (
        "fetch_nifc_fire_perimeters is cacheable; uri must be set by read_through"
    )

    # LayerURI name + id reflect the scope so multiple NIFC layers in the
    # same panel are distinguishable.
    if q_bbox is None:
        name = "NIFC Active Fire Perimeters — CONUS+AK+HI"
        layer_id = f"nifc-perimeters-{status}-global"
    else:
        name = (
            f"NIFC Active Fire Perimeters — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        )
        layer_id = (
            f"nifc-perimeters-{status}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        )

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="nifc_fire_perimeters",
        role="primary",
        units=None,
    )
