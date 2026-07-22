"""``fetch_mtbs_burn_severity`` atomic tool — MTBS burn-severity polygon fetcher (job-0109).

Queries the MTBS (Monitoring Trends in Burn Severity) public ArcGIS REST
FeatureServer endpoint and returns a FlatGeobuf of historic burn-severity
polygons clipped to the requested bbox. CONUS + Alaska + Hawaii coverage.
No authentication required for read access.

MTBS is the joint USFS / USGS program that publishes consistent burn-severity
mapping for all fires ≥1000 acres in the West and ≥500 acres in the East,
1984–present. The polygons used by this tool are the BurnBndAreas (burned-area
boundaries; one polygon per fire-event) — not the per-pixel severity rasters.

Endpoint pattern (verified 2026-06-08 against the live Esri_US_Federal_Data
``EDW_MTBS_v1`` FeatureServer — OQ-0109-MTBS-URL-CORRECTED):

    https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/
        EDW_MTBS_v1/FeatureServer/0/query
    ?where=1=1
    &geometry={xmin,ymin,xmax,ymax}
    &geometryType=esriGeometryEnvelope
    &spatialRel=esriSpatialRelIntersects
    &inSR=4326
    &outFields=FIRE_ID,FIRE_NAME,YEAR,FIRE_TYPE,ACRES,LATITUDE,LONGITUDE,MAP_ID,MAP_PROG,ASMNT_TYPE,IRWINID,IG_DATE
    &outSR=4326
    &f=geojson
    &resultRecordCount=1000
    &resultOffset={offset}

The ``year_range`` filter is translated to a ``YEAR >= start AND
YEAR <= end`` clause on the ``where=`` parameter (server-side filter; the
MTBS server respects it consistently across its single mirror, unlike WDPA's
designation filter which we run client-side).

NOTE on schema field names (OQ-0109-MTBS-URL-CORRECTED): the audit.md kickoff
cited URL ``services1.arcgis.com/ESMARspQHYMw9BZ9/.../MTBS_BAreas/FeatureServer/0``
with fields ``Event_ID``, ``Incid_Name``, ``Ig_Year``, ``BurnBndAc``,
``BurnBndLat``, ``BurnBndLon``. Probing the live ArcGIS Online portal at agent
author time revealed (a) the actual canonical MTBS FeatureServer is hosted by
Esri_US_Federal_Data at ``services2.arcgis.com/FiaPA4ga0iQKduv3/.../EDW_MTBS_v1/FeatureServer/0``
(layer id 0, not 1), and (b) the live schema uses different field names —
``FIRE_ID``, ``FIRE_NAME``, ``YEAR``, ``FIRE_TYPE``, ``ACRES``, ``LATITUDE``,
``LONGITUDE`` etc. The kickoff fields appear to be from an older MTBS schema
that no longer exists in the live service. We use the live field names. The
``maxRecordCount`` on this service is 1000, so we set page size accordingly.

Tier-1 free fetcher (no API key). Cached with TTL ``static-30d`` since MTBS
publishes annual updates (typically lagged ~1 year behind the wildfire season),
so the 30-day stale window is comfortably inside the publication cadence.

FR-TA-2: atomic tool, returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, year_range)`` calls reuse the cached FlatGeobuf.

CONUS-only / bbox-required: the kickoff specifies ``supports_global_query=False``
("NEW Wave 1.5 metadata") but that field does NOT yet exist on
``AtomicToolMetadata`` (job-0114-schema is adding it). Passing it would raise
``pydantic.ValidationError`` at import time and break the agent service.
Surfaced as ``OQ-0109-GLOBAL-QUERY-FIELD``: once job-0114 lands, a one-line
follow-up adds ``supports_global_query=False`` to the metadata literal below.

OQ-0109-YEAR-RANGE-SEMANTICS (TENTATIVE): ``year_range`` is interpreted as
INCLUSIVE on both endpoints (Ig_Year >= start AND Ig_Year <= end), matching
the kickoff's ``Ig_Year >= {start} AND Ig_Year <= {end}`` clause. None is
treated as "all years" (no Ig_Year filter clause). A list-form
``year_range=(start, start)`` selects a single year.

OQ-0109-INCID-TYPE-FILTER (TENTATIVE): MTBS includes "Wildfire", "Prescribed
Fire", "Unknown", and "Wildland Fire Use" event types. The kickoff does not
specify an ``incident_type_filter`` parameter, so we include all types. A
future enrichment job can add a filter argument if downstream tools surface a
prescribed-fire-vs-wildfire selection need.
"""

from __future__ import annotations

import io
import json
import logging
import math
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = ["fetch_mtbs_burn_severity"]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class MTBSError(RuntimeError):
    """Base class for fetch_mtbs_burn_severity failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "MTBS_ERROR"
    retryable: bool = True


class MTBSUpstreamError(MTBSError):
    """MTBS ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "MTBS_UPSTREAM_ERROR"
    retryable = True


class MTBSBboxError(MTBSError):
    """The bbox failed validation (degenerate, out of range, non-finite)."""

    error_code = "MTBS_BBOX_INVALID"
    retryable = False


class MTBSYearRangeError(MTBSError):
    """The year_range failed validation (start > end, non-int, out of [1984, current+1])."""

    error_code = "MTBS_YEAR_RANGE_INVALID"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_MTBS_BASE = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "EDW_MTBS_v1/FeatureServer/0/query"
)

# MTBS OutFields we keep — live schema field names (UPPERCASE per the
# Esri_US_Federal_Data EDW_MTBS_v1 FeatureServer/0 schema as of 2026-06-08).
# Including FIRE_TYPE so callers can distinguish wildfires from prescribed
# fires, and the MAP_*/ASMNT_TYPE provenance fields for downstream use.
_MTBS_OUT_FIELDS = (
    "FIRE_ID,FIRE_NAME,YEAR,FIRE_TYPE,ACRES,"
    "LATITUDE,LONGITUDE,MAP_ID,MAP_PROG,ASMNT_TYPE,IRWINID,IG_DATE"
)

# Page size. EDW_MTBS_v1 FeatureServer's maxRecordCount is 1000; request it
# explicitly so server-side defaults don't surprise us.
_PAGE_SIZE = 1000

# Per-request timeout. MTBS's cluster is generally fast (single-mirror), but
# allow 60s for cross-CONUS bbox queries that can return thousands of polys.
_REQUEST_TIMEOUT = 60.0

# Safety cap on pagination iterations. 50 * 2000 = 100k features. A bbox
# returning more than that is almost certainly an unintentional CONUS-wide
# query; fail loudly rather than silently paginate forever.
_MAX_PAGES = 50

# MTBS data starts in 1984 (the program's reporting baseline).
_MTBS_MIN_YEAR = 1984

# User-Agent — USFS/USGS ArcGIS clusters appreciate identifying agents.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# NOTE on supports_global_query (kickoff): the Wave 1.5 schema amendment
# (job-0114) is adding ``supports_global_query: bool = False`` to
# AtomicToolMetadata. As of this job's authoring, that field does NOT exist
# in trid3nt_contracts.tool_registry.AtomicToolMetadata yet — passing it would
# raise pydantic ValidationError at import time and break the agent service.
# Surfaced as OQ-0109-GLOBAL-QUERY-FIELD. Once job-0114 lands, a one-line
# follow-up adds ``supports_global_query=False`` to this metadata literal.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_mtbs_burn_severity",
    ttl_class="static-30d",
    source_class="mtbs_burn_severity",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``MTBSBboxError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise MTBSBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise MTBSBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise MTBSBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise MTBSBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise MTBSBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _validate_year_range(
    year_range: tuple[int, int] | None,
) -> tuple[int, int] | None:
    """Validate ``year_range``; return the normalized (start, end) tuple or None."""
    if year_range is None:
        return None
    if len(year_range) != 2:
        raise MTBSYearRangeError(
            f"year_range must be (start, end) of length 2; got {year_range!r}"
        )
    start, end = year_range
    if not (isinstance(start, int) and isinstance(end, int)) or isinstance(start, bool) or isinstance(end, bool):
        raise MTBSYearRangeError(
            f"year_range endpoints must be int; got {year_range!r}"
        )
    if start > end:
        raise MTBSYearRangeError(
            f"year_range start > end: {year_range!r}"
        )
    if start < _MTBS_MIN_YEAR:
        raise MTBSYearRangeError(
            f"year_range start {start} predates MTBS coverage ({_MTBS_MIN_YEAR})"
        )
    # Allow up to current calendar year + 1 (the program publishes in arrears
    # but allow forward-looking requests to fail empty, not hard-fail).
    if end > 2100:
        raise MTBSYearRangeError(
            f"year_range end {end} is implausibly far in the future"
        )
    return (start, end)


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``geometryType=esriGeometryEnvelope`` string."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


def _build_where_clause(year_range: tuple[int, int] | None) -> str:
    """Build the ``where=`` clause for the FeatureServer query.

    When ``year_range`` is None, returns ``"1=1"`` (no year filter).
    Otherwise returns ``"YEAR >= {start} AND YEAR <= {end}"`` (the live
    EDW_MTBS_v1 schema uses ``YEAR``, not the kickoff's older ``Ig_Year``;
    see OQ-0109-MTBS-URL-CORRECTED). Inclusive on both endpoints.
    """
    if year_range is None:
        return "1=1"
    start, end = year_range
    return f"YEAR >= {start} AND YEAR <= {end}"


# ---------------------------------------------------------------------------
# MTBS HTTP fetch.
# ---------------------------------------------------------------------------


def _mtbs_query_one_page(
    bbox: tuple[float, float, float, float],
    year_range: tuple[int, int] | None,
    offset: int,
) -> dict[str, Any]:
    """Fetch one page of the MTBS FeatureServer query, returning parsed GeoJSON.

    Returns the parsed response dict (the FeatureServer wraps GeoJSON in a
    standard envelope: ``{"type": "FeatureCollection", "features": [...],
    "exceededTransferLimit": bool}``).
    """
    params = {
        "where": _build_where_clause(year_range),
        "geometry": _bbox_to_envelope(bbox),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": _MTBS_OUT_FIELDS,
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
    }
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(
                _MTBS_BASE,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.RequestError as exc:
        raise MTBSUpstreamError(
            f"MTBS query failed (network) offset={offset}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise MTBSUpstreamError(
            f"MTBS query returned HTTP {resp.status_code} offset={offset}: "
            f"{resp.text[:200]}"
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise MTBSUpstreamError(
            f"MTBS returned non-JSON body offset={offset}: {exc}"
        ) from exc

    # ArcGIS REST surfaces errors inside a 200 envelope: {"error": {...}}.
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        raise MTBSUpstreamError(
            f"MTBS query returned error envelope offset={offset}: {err}"
        )

    return payload


def _fetch_mtbs_features(
    bbox: tuple[float, float, float, float],
    year_range: tuple[int, int] | None,
) -> list[dict[str, Any]]:
    """Fetch all features in the bbox (and optional year_range), paginating as needed.

    Returns a list of GeoJSON Feature dicts (possibly empty).
    """
    all_features: list[dict[str, Any]] = []
    offset = 0

    for page_idx in range(_MAX_PAGES):
        payload = _mtbs_query_one_page(bbox, year_range, offset)
        page_features = payload.get("features", []) or []
        all_features.extend(page_features)

        logger.info(
            "fetch_mtbs_burn_severity: page %d offset=%d -> %d feature(s) "
            "(total so far: %d)",
            page_idx,
            offset,
            len(page_features),
            len(all_features),
        )

        # MTBS / ArcGIS REST tells us if more is available via
        # exceededTransferLimit. Check both the top-level and properties-nested
        # location for robustness across server versions.
        more = bool(
            payload.get("exceededTransferLimit")
            or (payload.get("properties") or {}).get("exceededTransferLimit")
        )
        if not more:
            break
        if len(page_features) == 0:
            # Defensive: server says "more" but returned 0; avoid infinite loop.
            break
        offset += len(page_features)
    else:
        raise MTBSUpstreamError(
            f"MTBS pagination exceeded {_MAX_PAGES} pages for bbox={bbox}; "
            "bbox is probably too large — reduce bbox extent or narrow year_range."
        )

    return all_features


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert a list of GeoJSON Features to FlatGeobuf bytes via geopandas.

    An empty feature list is returned as an empty FlatGeobuf (still valid
    bytes) — callers (and the cache shim) treat that as a successful
    "no-features-in-bbox" response per the "Empty bbox over open water /
    no fires in window → 0 features without error" test.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MTBSUpstreamError(
            f"geopandas not available for FlatGeobuf encode: {exc}"
        ) from exc

    import os as _os
    import tempfile

    if not features:
        # Empty geodataframe with the MTBS schema columns. Use string dtype
        # for IDs / names so the empty FGB has a deterministic schema —
        # downstream consumers (overlay/intersect) appreciate stable column
        # types even on empty results.
        empty_gdf = gpd.GeoDataFrame(
            {
                "FIRE_ID": [],
                "FIRE_NAME": [],
                "YEAR": [],
                "FIRE_TYPE": [],
                "ACRES": [],
                "LATITUDE": [],
                "LONGITUDE": [],
                "MAP_ID": [],
                "MAP_PROG": [],
                "ASMNT_TYPE": [],
                "IRWINID": [],
                "IG_DATE": [],
                "geometry": [],
            },
            crs="EPSG:4326",
        )
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
            tmp_path = tf.name
        try:
            empty_gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
            with open(tmp_path, "rb") as f:
                return f.read()
        except Exception as exc:  # noqa: BLE001
            raise MTBSUpstreamError(
                f"failed to write empty FlatGeobuf: {exc}"
            ) from exc
        finally:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass

    # Build a FeatureCollection and let geopandas parse it.
    fc = {"type": "FeatureCollection", "features": features}
    try:
        gdf = gpd.GeoDataFrame.from_features(fc, crs="EPSG:4326")
    except Exception as exc:  # noqa: BLE001
        raise MTBSUpstreamError(
            f"geopandas could not parse MTBS features: {exc}"
        ) from exc

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tmp_path = tf.name
    try:
        gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_path, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise MTBSUpstreamError(
            f"failed to write FlatGeobuf: {exc}"
        ) from exc
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fetch function — builds the bytes callable for read_through.
# ---------------------------------------------------------------------------


def _fetch_mtbs_bytes(
    bbox: tuple[float, float, float, float],
    year_range: tuple[int, int] | None,
) -> bytes:
    """Download MTBS features, filter, and serialize to FlatGeobuf bytes."""
    features = _fetch_mtbs_features(bbox, year_range)
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
def fetch_mtbs_burn_severity(
    bbox: tuple[float, float, float, float],
    year_range: tuple[int, int] | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch MTBS historic burn-severity boundary polygons clipped to a bbox.

    **What it does:** Queries the USFS/USGS MTBS (Monitoring Trends in Burn
    Severity) ArcGIS FeatureServer (EDW_MTBS_v1) for burned-area boundary
    polygons — one polygon per fire event — filtered to a bbox and optional
    year range. Returns a FlatGeobuf with fire name, year, type, acres, and
    provenance fields. MTBS covers CONUS + AK + HI + PR, 1984–present, for
    fires ≥1000 acres in the West and ≥500 acres in the East. Cached
    ``static-30d``. No API key required.

    **When to use:**
    - Post-fire hazard setup: identify burn scars in a study area for debris
      flow, post-fire flood, or erosion risk assessment.
    - Wildfire history overlay: "show all fires larger than 500 acres that
      burned in this watershed since 2010."
    - Conservation context: filter species occurrence queries by
      recently-burned habitat (GBIF/iNat occurrences inside MTBS polygons).
    - Mapping wildfire risk exposure near critical infrastructure using
      historical burn frequency.

    **When NOT to use:**
    - Active or current-year fire perimeters (MTBS lags ~1 year behind the
      season; use ``fetch_nifc_fire_perimeters`` for current incidents).
    - Fires smaller than the MTBS minimum thresholds (use a state-level or
      CAL FIRE / AK DNR dataset).
    - Per-pixel burn-severity rasters (MTBS BurnSeverityImages are a separate
      product; this tool returns polygon boundaries only).
    - Fire-weather forecasts (use NWS fire-weather watches).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Required. Example: ``(-124.0, 40.0, -120.0, 43.0)`` for Northern CA/OR.
    - ``year_range`` (tuple or None): ``(start_year, end_year)`` inclusive,
      both integers, minimum start 1984. Filters server-side via
      ``YEAR >= start AND YEAR <= end``. ``None`` returns all years 1984–present.

    **Returns:**
    ``LayerURI(layer_type="vector", role="primary", units=None)`` pointing at a
    FlatGeobuf with fields: ``FIRE_ID``, ``FIRE_NAME``, ``YEAR``,
    ``FIRE_TYPE`` (Wildfire/Prescribed Fire/Wildland Fire Use/Unknown),
    ``ACRES``, ``LATITUDE``, ``LONGITUDE``, ``MAP_ID``, ``MAP_PROG``,
    ``ASMNT_TYPE``, ``IRWINID``, ``IG_DATE``. EPSG:4326.

    **Cross-tool dependencies:**
    - Pairs with: ``fetch_nifc_fire_perimeters`` (current active fires),
      ``fetch_firms_active_fire`` (near-real-time detections in or near scars).
    - Upstream of: debris-flow / post-fire flood risk workflows, species habitat
      analysis via ``fetch_gbif_occurrences`` / ``fetch_inaturalist_observations``.
    - Complements: ``fetch_dem`` + ``compute_slope`` for post-fire watershed
      erosion context.
    """
    _validate_bbox(bbox)
    normalized_year_range = _validate_year_range(year_range)

    # Quantize bbox to 6dp for cache-key stability.
    q_bbox = _round_bbox_to_6dp(bbox)

    params = {
        "bbox": list(q_bbox),
        "year_range": list(normalized_year_range) if normalized_year_range else None,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_mtbs_bytes(q_bbox, normalized_year_range),
    )
    assert result.uri is not None, (
        "fetch_mtbs_burn_severity is cacheable; uri must be set by read_through"
    )

    # Layer name encodes the year range when present, so multiple MTBS layers
    # in the same panel are distinguishable.
    if normalized_year_range is not None:
        start, end = normalized_year_range
        if start == end:
            year_label = f" ({start})"
        else:
            year_label = f" ({start}–{end})"
    else:
        year_label = ""
    name = f"MTBS Burn Severity — Burned Areas{year_label}"

    return LayerURI(
        layer_id=f"mtbs-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="mtbs_burn_severity",
        role="primary",
        units=None,
    )
