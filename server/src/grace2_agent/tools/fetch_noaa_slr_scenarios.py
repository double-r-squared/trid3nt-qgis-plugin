"""``fetch_noaa_slr_scenarios`` atomic tool — NOAA Sea Level Rise scenario
inundation polygons (job A10).

Wraps NOAA's Office for Coastal Management (OCM) Sea Level Rise (SLR) Viewer
ArcGIS REST MapServer endpoint, which publishes inundation-area polygons for
21 scenario levels from 0 ft through 10 ft (whole-foot and 0.5-ft intervals).
Each scenario layer contains dissolved inundation polygons for the contiguous
United States (CONUS) coast derived from NOAA's 1/9 arc-second lidar-derived
digital elevation data.

**What it does:**
Fetches dissolved inundation-area polygons for one or more SLR scenario
levels, clipped to a user-specified bbox. Returns a FlatGeobuf vector layer
with one polygon per scenario level intersecting the bbox, annotated with
``slr_ft`` (scenario level in feet) and ``scenario_label`` attributes suitable
for Gemini narration, map display, and downstream habitat / infrastructure
overlay intersections.

**When to use:**
- User asks for the NOAA sea-level-rise map, coastal inundation projection,
  SLR scenarios, or flooding under X feet of sea-level rise.
- User asks "what areas flood under 1ft / 2ft / 3ft of SLR near [coastal city]?"
- Agent needs the static SLR inundation footprint as a planning-level overlay
  (e.g. to intersect with ``fetch_usace_nsi`` building inventory or
  ``fetch_wdpa_protected_areas`` habitat polygons for exposure assessment).
- User wants to compare current FEMA 100-year floodplain with future SLR
  scenarios (side-by-side with ``fetch_fema_nfhl_zones``).

**When NOT to use:**
- For real-time / dynamic storm-surge inundation → use a SFINCS
  ``run_model_flood_scenario`` run or ``fetch_noaa_coops_tides`` + ``fetch_gtsm_tide_surge``.
  SLR polygons are static planning-level products, not event-driven.
- For future probabilistic SLR projections with confidence intervals → the
  NOAA Technical Report 2022 "Sweet et al." scenarios (intermediate, high,
  etc.) are in a separate dataset; this tool returns the OCM Viewer's
  deterministic bathtub inundation footprints only.
- For inland / non-coastal flooding → use ``fetch_fema_nfhl_zones`` (regulatory
  floodplains) or ``run_model_flood_scenario`` (SFINCS pluvial/fluvial).
- For marsh migration or habitat-transition projections → the OCM Viewer's
  ``marsh_*`` services are a separate dataset not covered by this tool.
- For areas outside CONUS → SLR data coverage is CONUS-only (Alaska, Hawaii,
  territories are not in this dataset). Use ``fetch_gtsm_tide_surge`` for
  global coastal flooding context.

**Parameters:**
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 (WGS84
        decimal degrees). Required — ``supports_global_query=False`` (CONUS
        coastal polygon source; a global query would cover millions of polygons).
        Recommended ≤ ~2° per side for tractable response times. Larger extents
        will be accepted but may approach the 1000-feature page cap.
        Example for coastal Lee County FL (Fort Myers / Naples):
        ``(-82.2, 26.2, -81.5, 26.9)``.
    scenario_ft: Scenario level(s) in feet. One of the 21 valid levels:
        ``0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5,
        8, 8.5, 9, 9.5, 10``. Pass a single float (e.g. ``1.0``) or a list
        (e.g. ``[1.0, 2.0, 3.0]`` to fetch multiple scenarios at once). Default
        is ``[1.0, 2.0, 3.0]`` (3 most commonly requested planning levels).
        Each scenario is fetched from its own MapServer service and merged into
        one output FlatGeobuf with a ``slr_ft`` column distinguishing them.

**Returns:**
    ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
    ``s3://trid3nt-cache/cache/static-30d/noaa_slr_scenarios/<key>.fgb``
    Each feature is a Polygon in EPSG:4326. Properties:
    ``slr_ft`` (float, scenario level), ``scenario_label`` (str, e.g.
    "1.0 ft SLR"), ``dissolve`` (int, always 1 — source layer is fully
    dissolved per scenario). ``layer_type="vector"``, ``role="primary"``,
    ``style_preset="noaa_slr_scenarios"``, ``units="feet"``.

**Cross-tool dependencies:**
    - Often paired with ``fetch_usace_nsi`` (building inventory) →
      ``compute_zonal_statistics`` for "how many structures are in the 2 ft SLR
      footprint?" queries.
    - Companion to ``fetch_fema_nfhl_zones`` for regulatory-vs-future comparison.
    - Feeds ``clip_vector_to_polygon`` / ``clip_raster_to_polygon`` when combined
      with ``fetch_administrative_boundaries`` for city/county scoped reports.
    - Sibling tool for confidence assessment: a separate ``fetch_noaa_slr_confidence``
      (not yet implemented) covers the ``conf_*`` services in the same folder.

**Cache:** ``static-30d`` (FR-DC-2). The OCM SLR Viewer base data is derived
from lidar DEMs that update infrequently (major revisions every few years);
a 30-day stale window is fully appropriate for planning-level overlays.

**FR-AS-11 typed-error surface:** ``NOAA_SLR_SCENARIOSError`` (base,
retryable=True), ``NOAA_SLR_SCENARIOSInputError`` (non-retryable bbox/scenario
validation), ``NOAA_SLR_SCENARIOSUpstreamError`` (retryable ArcGIS REST
network / HTTP / parse failure), ``NOAA_SLR_SCENARIOSEmptyError`` (no features
in bbox for all requested scenarios — not retryable, but NOT raised by default;
we serialize an empty FGB instead so the layer still appears in the panel with
a zero-feature notice).

**FR-DC-9 payload estimation:** ~0.3 MB per scenario-level per square degree
of coastal area (SLR polygons are heavily dissolved; a 1° coastal bbox
typically returns 0.1–1.5 MB per scenario). Clipped to [0.02, 50] MB.

``supports_global_query=False`` — CONUS coastal polygon source.

Endpoint pattern (verified live 2026-06-09):
    Base:
        https://coast.noaa.gov/arcgis/rest/services/dc_slr/slr_{scenario}/MapServer/0/query

    Where ``{scenario}`` is one of:
        ``0ft``, ``0_5ft``, ``1ft``, ``1_5ft``, ``2ft``, ..., ``10ft``

    Query parameters::
        where=1=1
        geometry={xmin,ymin,xmax,ymax}
        geometryType=esriGeometryEnvelope
        inSR=4326
        spatialRel=esriSpatialRelIntersects
        outFields=OBJECTID,Dissolve
        outSR=4326
        f=geojson
        resultRecordCount=1000

    Response: GeoJSON FeatureCollection, polygons with ``Dissolve=1``.
    The low-lying-areas layer (layer 0) is the vector polygon layer; layer 1
    is a raster depth layer that cannot be queried for features.

    Coverage: CONUS coastline and tidal areas. Not available for Alaska,
    Hawaii, or territories.
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

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_noaa_slr_scenarios",
    "estimate_payload_mb",
    "NOAA_SLR_SCENARIOSError",
    "NOAA_SLR_SCENARIOSInputError",
    "NOAA_SLR_SCENARIOSUpstreamError",
    "NOAA_SLR_SCENARIOSEmptyError",
    "_scenario_ft_to_service_name",
    "_build_slr_url",
    "_validate_bbox",
    "_validate_scenario_ft",
    "_round_bbox_to_6dp",
    "_fetch_slr_features_one_scenario",
    "_features_to_flatgeobuf",
    "_fetch_slr_bytes",
    "SLR_BASE_URL",
    "VALID_SCENARIO_FT",
    "DEFAULT_SCENARIOS_FT",
]

logger = logging.getLogger("grace2_agent.tools.fetch_noaa_slr_scenarios")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NOAA_SLR_SCENARIOSError(RuntimeError):
    """Base class for fetch_noaa_slr_scenarios failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "NOAA_SLR_SCENARIOS_ERROR"
    retryable: bool = True


class NOAA_SLR_SCENARIOSInputError(NOAA_SLR_SCENARIOSError):
    """Caller passed an invalid bbox or scenario_ft value."""

    error_code = "NOAA_SLR_SCENARIOS_INPUT_INVALID"
    retryable = False


class NOAA_SLR_SCENARIOSUpstreamError(NOAA_SLR_SCENARIOSError):
    """NOAA OCM ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "NOAA_SLR_SCENARIOS_UPSTREAM_ERROR"
    retryable = True


class NOAA_SLR_SCENARIOSEmptyError(NOAA_SLR_SCENARIOSError):
    """No SLR inundation features found in bbox for any requested scenario.

    NOT raised by default (we serialize an empty FGB instead — a bbox over
    an interior non-coastal area legitimately has no SLR footprint), but
    available for future strict-mode opt-in.
    """

    error_code = "NOAA_SLR_SCENARIOS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NOAA OCM SLR Viewer ArcGIS REST MapServer base URL. The per-scenario
#: service name is appended at query time via ``_scenario_ft_to_service_name``.
SLR_BASE_URL = "https://coast.noaa.gov/arcgis/rest/services/dc_slr"

#: Valid SLR scenario levels in feet. The OCM SLR Viewer publishes 21 services
#: covering 0 ft through 10 ft in 0.5-ft increments.
VALID_SCENARIO_FT: frozenset[float] = frozenset({
    0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5,
    4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5,
    8.0, 8.5, 9.0, 9.5, 10.0,
})

#: Default scenario levels fetched when the caller does not specify.
#: 1 ft, 2 ft, 3 ft — the three most-requested NOAA SLR planning benchmarks.
DEFAULT_SCENARIOS_FT: list[float] = [1.0, 2.0, 3.0]

#: Fields to request from the ArcGIS REST endpoint.
#: The low-lying-areas layer has only OBJECTID, Shape_Length, Shape_Area,
#: and Dissolve. We request OBJECTID and Dissolve; area/length are auto-
#: included by the server when the geometry is returned.
_OUT_FIELDS = "OBJECTID,Dissolve"

#: Per-page record cap. The NOAA OCM SLR endpoint allows up to 1000 records
#: per query. SLR polygons are heavily dissolved so a typical coastal bbox
#: returns only a handful of features; 1000 is a safe ceiling.
_PAGE_SIZE = 1000

#: HTTP request timeout (seconds). The NOAA OCM server is generally fast but
#: can be slow during heavy traffic; 45s provides a comfortable margin.
_HTTP_TIMEOUT_S = 45.0

#: User-Agent per NOAA usage policy.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Payload estimation heuristic: MB per scenario per square degree (coastal).
#: SLR polygons are heavily dissolved; a 1° coastal bbox yields ~0.3 MB/scenario.
_PAYLOAD_MB_PER_SCENARIO_PER_SQ_DEG = 0.3
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 50.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_noaa_slr_scenarios",
        ttl_class="static-30d",
        source_class="noaa_slr_scenarios",
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
            "registering fetch_noaa_slr_scenarios without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    scenario_ft: float | list[float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate the FlatGeobuf payload size for an SLR scenarios fetch.

    Heuristic: ~0.3 MB per scenario level per square degree of coastal area.
    SLR polygons are dissolved; coastal FL 1° bbox returns ~0.2–0.5 MB/scenario.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        scenario_ft: single float or list of floats (scenario levels).
    """
    if bbox is None:
        area_sq_deg = 4.0
    else:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
            area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
        except (TypeError, ValueError):
            area_sq_deg = 4.0

    if scenario_ft is None:
        n_scenarios = len(DEFAULT_SCENARIOS_FT)
    elif isinstance(scenario_ft, (int, float)):
        n_scenarios = 1
    else:
        try:
            n_scenarios = max(1, len(list(scenario_ft)))
        except TypeError:
            n_scenarios = 1

    est = n_scenarios * area_sq_deg * _PAYLOAD_MB_PER_SCENARIO_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# Scenario-name helpers.
# ---------------------------------------------------------------------------


def _scenario_ft_to_service_name(scenario_ft: float) -> str:
    """Convert a scenario level in feet to the NOAA OCM service name suffix.

    NOAA's service naming convention:
        0.0  → "slr_0ft"
        0.5  → "slr_0_5ft"
        1.0  → "slr_1ft"
        1.5  → "slr_1_5ft"
        ...
        10.0 → "slr_10ft"

    Args:
        scenario_ft: One of the 21 valid SLR scenario levels.

    Returns:
        Service name string, e.g. ``"slr_1_5ft"`` for 1.5 ft.

    Raises:
        ``NOAA_SLR_SCENARIOSInputError`` if the value is not valid.
    """
    if scenario_ft not in VALID_SCENARIO_FT:
        raise NOAA_SLR_SCENARIOSInputError(
            f"scenario_ft={scenario_ft!r} is not a valid SLR scenario level; "
            f"valid values: {sorted(VALID_SCENARIO_FT)}"
        )
    # Format: integer part followed by optional _<decimal> suffix.
    int_part = int(math.floor(scenario_ft))
    frac_part = round(scenario_ft - int_part, 1)
    if frac_part == 0.0:
        return f"slr_{int_part}ft"
    else:
        # Convert 0.5 → "0_5", 1.5 → "1_5", etc.
        frac_str = str(frac_part).replace("0.", "").replace(".", "")
        return f"slr_{int_part}_{frac_str}ft"


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NOAA_SLR_SCENARIOSInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise NOAA_SLR_SCENARIOSInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NOAA_SLR_SCENARIOSInputError(
            f"bbox contains non-finite values: {bbox!r}"
        )
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NOAA_SLR_SCENARIOSInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NOAA_SLR_SCENARIOSInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NOAA_SLR_SCENARIOSInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _validate_scenario_ft(
    scenario_ft: float | list[float] | None,
) -> list[float]:
    """Normalize and validate scenario_ft to a sorted deduplicated list.

    Accepts:
        - None → returns ``DEFAULT_SCENARIOS_FT``
        - A single float → ``[scenario_ft]``
        - A list of floats → sorted deduped list

    Raises:
        ``NOAA_SLR_SCENARIOSInputError`` for unknown scenario levels or wrong types.
    """
    if scenario_ft is None:
        return list(DEFAULT_SCENARIOS_FT)

    if isinstance(scenario_ft, (int, float)):
        levels = [float(scenario_ft)]
    elif isinstance(scenario_ft, list):
        if not scenario_ft:
            return list(DEFAULT_SCENARIOS_FT)
        levels = []
        for v in scenario_ft:
            if not isinstance(v, (int, float)):
                raise NOAA_SLR_SCENARIOSInputError(
                    f"scenario_ft entries must be numeric; got {type(v).__name__}: {v!r}"
                )
            levels.append(float(v))
    else:
        raise NOAA_SLR_SCENARIOSInputError(
            f"scenario_ft must be a float or list[float]; got {type(scenario_ft).__name__}"
        )

    # Validate all levels.
    for lv in levels:
        if lv not in VALID_SCENARIO_FT:
            raise NOAA_SLR_SCENARIOSInputError(
                f"scenario_ft={lv!r} is not a valid SLR scenario level; "
                f"valid values: {sorted(VALID_SCENARIO_FT)}"
            )

    # Dedup and sort for cache-key stability.
    return sorted(set(levels))


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_slr_url(
    scenario_ft: float,
    bbox: tuple[float, float, float, float],
) -> tuple[str, dict[str, str]]:
    """Build the NOAA OCM SLR ArcGIS REST query URL + params dict.

    Layer 0 (low-lying-areas polygon) is the queryable vector layer. We request
    all features intersecting the bbox in EPSG:4326, returned as GeoJSON.

    Args:
        scenario_ft: One valid SLR scenario level (validated upstream).
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.

    Returns:
        ``(url, params)`` tuple for an httpx GET.
    """
    service_name = _scenario_ft_to_service_name(scenario_ft)
    url = f"{SLR_BASE_URL}/{service_name}/MapServer/0/query"
    min_lon, min_lat, max_lon, max_lat = bbox
    params: dict[str, str] = {
        "where": "1=1",
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
# HTTP fetch for one scenario.
# ---------------------------------------------------------------------------


def _fetch_slr_features_one_scenario(
    scenario_ft: float,
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch all inundation polygon features for one SLR scenario level.

    Queries layer 0 (low-lying-areas polygon) of the NOAA OCM SLR MapServer
    for the specified scenario level. The service returns dissolved polygons
    representing the bathtub inundation area at the given sea-level rise above
    MHHW (Mean Higher High Water).

    Args:
        scenario_ft: One valid SLR scenario level.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.

    Returns:
        List of GeoJSON Feature dicts. Empty list if the bbox has no inundation
        features for this scenario (interior non-coastal area).

    Raises:
        ``NOAA_SLR_SCENARIOSUpstreamError``: on network / HTTP / parse failures.
    """
    url, params = _build_slr_url(scenario_ft, bbox)
    logger.info(
        "fetch_noaa_slr_scenarios: GET %s (scenario=%.1fft, bbox=%s)",
        url,
        scenario_ft,
        bbox,
    )

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"NOAA SLR request failed url={url} scenario={scenario_ft}ft: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"NOAA SLR returned HTTP {resp.status_code} url={url} "
            f"scenario={scenario_ft}ft: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"NOAA SLR returned non-JSON url={url} scenario={scenario_ft}ft: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"NOAA SLR response is not a JSON object url={url}: "
            f"type={type(body).__name__!r}"
        )

    # ArcGIS REST may surface errors inside a 200 envelope.
    if "error" in body:
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"NOAA SLR query returned error envelope url={url} "
            f"scenario={scenario_ft}ft: {body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"NOAA SLR response is not a GeoJSON FeatureCollection url={url}: "
            f"type={body.get('type')!r}"
        )

    features = body.get("features", []) or []
    logger.info(
        "fetch_noaa_slr_scenarios: scenario=%.1fft → %d feature(s)",
        scenario_ft,
        len(features),
    )
    return features


# ---------------------------------------------------------------------------
# Features → FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(
    all_features_by_scenario: dict[float, list[dict[str, Any]]],
) -> bytes:
    """Convert per-scenario SLR inundation features to FlatGeobuf bytes.

    Merges all scenario features into a single GeoDataFrame with a ``slr_ft``
    column distinguishing scenarios and a ``scenario_label`` column for
    human-readable display. Always emits valid FlatGeobuf bytes — an empty
    feature dict yields an empty-schema FGB so the cache shim has something
    concrete to persist.

    Args:
        all_features_by_scenario: ``{scenario_ft: [geojson_feature, ...]}``

    Returns:
        FlatGeobuf bytes in EPSG:4326.

    Raises:
        ``NOAA_SLR_SCENARIOSUpstreamError``: if geopandas is unavailable or
        FlatGeobuf serialization fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NOAA_SLR_SCENARIOSUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for scenario_ft, features in sorted(all_features_by_scenario.items()):
        label = f"{scenario_ft:.1f} ft SLR"
        for feat in features:
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry")
            if geom is None:
                continue
            props = feat.get("properties") or {}
            cleaned.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "slr_ft": scenario_ft,
                    "scenario_label": label,
                    "dissolve": int(props.get("Dissolve", 1)),
                },
            })

    if not cleaned:
        import pandas as pd
        empty_df = pd.DataFrame(columns=["slr_ft", "scenario_label", "dissolve"])
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_noaa_slr_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise NOAA_SLR_SCENARIOSUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} SLR features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_noaa_slr_scenarios: FlatGeobuf = %d bytes (%d feature(s) across "
            "%d scenario(s))",
            len(fgb_bytes),
            len(gdf),
            len(all_features_by_scenario),
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


def _fetch_slr_bytes(
    bbox: tuple[float, float, float, float],
    scenarios: list[float],
) -> bytes:
    """Fetch all SLR scenario features for bbox → FlatGeobuf bytes.

    Iterates ``scenarios``, fetching one HTTP request per scenario level,
    then merges into a single FlatGeobuf. Per-scenario HTTP failures
    propagate immediately as ``NOAA_SLR_SCENARIOSUpstreamError``.
    """
    all_features: dict[float, list[dict[str, Any]]] = {}
    for scenario_ft in scenarios:
        features = _fetch_slr_features_one_scenario(scenario_ft, bbox)
        all_features[scenario_ft] = features
    return _features_to_flatgeobuf(all_features)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------

_METADATA_REGISTERED = _METADATA


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_noaa_slr_scenarios(
    bbox: tuple[float, float, float, float],
    scenario_ft: float | list[float] | None = None,
    # Wave 4.10 convention: absorb LLM-invented kwargs
    **_extra_ignored: Any,
) -> LayerURI:
    """NOAA Sea Level Rise scenario inundation polygons as a FlatGeobuf vector layer.

    Fetches dissolved inundation-area polygons from NOAA's Office for Coastal
    Management (OCM) SLR Viewer for one or more scenario levels (0–10 ft in
    0.5-ft increments). Returns a FlatGeobuf with one polygon feature per
    scenario level intersecting the bbox, annotated with ``slr_ft`` and
    ``scenario_label`` attributes for map display and downstream analysis.

    **When to use:**
    - User asks for the NOAA sea-level-rise map, SLR inundation scenarios,
      coastal flooding under X feet of sea-level rise, or "what areas flood if
      sea level rises by 2 feet?"
    - Agent needs the static SLR inundation footprint for a planning-level
      overlay (intersect with building inventory, habitat polygons, roads, etc.).
    - User wants to compare FEMA 100-year floodplain with future SLR scenarios.
    - User asks for "1 ft SLR", "2 ft SLR", "3 ft SLR" or any combination
      simultaneously (pass a list to ``scenario_ft``).

    **When NOT to use:**
    - For real-time / event-driven storm-surge inundation → use
      ``run_model_flood_scenario`` (SFINCS) or ``fetch_gtsm_tide_surge``.
    - For probabilistic SLR projections with uncertainty ranges → the Sweet et
      al. (2022) NOAA Technical Report scenarios (Intermediate, High, etc.) are
      a separate dataset not in the OCM Viewer.
    - For inland / non-coastal flooding → use ``fetch_fema_nfhl_zones`` or
      ``run_model_flood_scenario``.
    - For areas outside CONUS → use ``fetch_gtsm_tide_surge`` (global coastal
      water-level reanalysis); SLR Viewer data is CONUS-only.
    - For marsh migration / habitat-transition projections → a separate
      ``fetch_noaa_slr_marsh`` tool (not yet implemented) covers the
      ``marsh_*`` MapServer services in the same folder.

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            ``supports_global_query=False`` — CONUS coastal polygon source.
            Recommended ≤ ~2° per side for tractable response times. Example
            for coastal Lee County FL (Fort Myers / Naples area):
            ``(-82.2, 26.2, -81.5, 26.9)``.
        scenario_ft: SLR scenario level(s) in feet. Valid values: ``0, 0.5, 1,
            1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9,
            9.5, 10``. Pass a single float (e.g. ``2.0``) or a list (e.g.
            ``[1.0, 2.0, 3.0]``) to fetch multiple scenarios at once. Defaults
            to ``[1.0, 2.0, 3.0]`` when not specified.

    **Returns:**
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Each feature
        is a Polygon in EPSG:4326. Properties: ``slr_ft`` (float, scenario level
        in feet), ``scenario_label`` (str, e.g. "1.0 ft SLR"), ``dissolve``
        (int, always 1 — source is fully dissolved per scenario). ``layer_type``
        is ``"vector"``, ``role`` is ``"primary"``, ``style_preset`` is
        ``"noaa_slr_scenarios"``, ``units`` is ``"feet"``.

    **Cross-tool dependencies (FR-TA-3):**
        - Feeds INTO: ``compute_zonal_statistics`` (structure count / population
          inside SLR footprint), ``clip_vector_to_polygon`` (admin-bounded SLR
          exposure report), ``run_pelicun_damage_assessment`` (SLR exposure
          footprint × NSI building stock).
        - Compare WITH: ``fetch_fema_nfhl_zones`` (regulatory floodplain
          vs. projected SLR inundation side-by-side), ``fetch_usace_nsi``
          (structure-level asset inventory for exposure counting).
        - Sibling tools: ``fetch_noaa_coops_tides`` (observed tide levels for
          validating SLR baseline), ``fetch_gtsm_tide_surge`` (global coastal
          water-level for non-CONUS).

    **Error types (FR-AS-11):**
        - ``NOAA_SLR_SCENARIOSInputError``: bad bbox or unknown scenario_ft
          (retryable=False).
        - ``NOAA_SLR_SCENARIOSUpstreamError``: HTTP/network failure, ArcGIS
          error envelope, or FlatGeobuf serialization failure (retryable=True).
        - ``NOAA_SLR_SCENARIOSEmptyError``: no features in bbox for any scenario
          (retryable=False; not raised by default — empty FGB is returned).

    Cache: ``ttl_class="static-30d"``, ``source_class="noaa_slr_scenarios"``.
    Cache key is SHA-256 of ``(bbox-rounded-6dp, sorted(scenario_ft_list))``.

    ``supports_global_query=False``. No API key required.
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise NOAA_SLR_SCENARIOSInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc

    _validate_bbox(bbox)  # type: ignore[arg-type]
    scenarios = _validate_scenario_ft(scenario_ft)

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "scenarios_ft": scenarios,  # already sorted + deduped
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_slr_bytes(q_bbox, scenarios),
    )
    assert result.uri is not None, (
        "fetch_noaa_slr_scenarios is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    scenario_tag = "+".join(f"{s:.1f}ft" for s in scenarios)
    return LayerURI(
        layer_id=(
            f"noaa-slr-scenarios-{scenario_tag}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"NOAA SLR Scenarios [{scenario_tag}] — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="noaa_slr_scenarios",
        role="primary",
        units="feet",
        bbox=q_bbox,
    )
