"""``fetch_cdc_svi`` atomic tool -- CDC/ATSDR Social Vulnerability Index (SVI)
census-tract choropleth polygons.

Wraps CDC/ATSDR's public ArcGIS REST (OneMap) FeatureServer for the
**Social Vulnerability Index 2022** (the most-recent published vintage). The
SVI ranks every U.S. census tract on 16 social factors grouped into four
themes, producing percentile-rank scores in ``[0, 1]`` where higher = more
socially vulnerable. This tool returns the **tract-level** layer (layer 2),
clipped to a user bbox, as a FlatGeobuf choropleth polygon layer.

**What it does:**
Fetches census-tract polygons intersecting a bbox and returns a FlatGeobuf
with the overall summary ranking ``rpl_themes`` (``RPL_THEMES``) plus the four
theme percentile rankings:

    - ``rpl_theme1`` -> Theme 1: Socioeconomic Status
    - ``rpl_theme2`` -> Theme 2: Household Characteristics
    - ``rpl_theme3`` -> Theme 3: Racial & Ethnic Minority Status
    - ``rpl_theme4`` -> Theme 4: Housing Type & Transportation

plus tract identity attributes (``fips``, ``county``, ``state_abbr``,
``location``, ``total_pop``) for narration, choropleth display, and downstream
exposure/vulnerability intersections. CDC's ``-999`` null sentinel is
normalized to ``null`` so the choropleth does not render suppressed tracts as
extreme-low values.

**When to use:**
- User asks for the CDC SVI, social vulnerability, "which neighborhoods are
  most vulnerable", or an equity / environmental-justice overlay.
- Agent needs a vulnerability surface to combine with a hazard footprint
  (flood inundation, wildfire perimeter, heat, plume) to find the most
  socially vulnerable exposed population.
- User asks "show social vulnerability for <city/county>" or "rank tracts by
  vulnerability in this area".

**When NOT to use:**
- For raw demographic counts (population, income, race) -> use a Census/ACS
  fetcher; SVI publishes *percentile rankings*, not the underlying estimates.
- For county-level SVI (coarser) -> this tool returns tract-level (layer 2).
  A county variant could query layer 1; not exposed here (tracts are the
  standard analytical unit for vulnerability work).
- For areas outside the United States -> SVI coverage is U.S.-only (50 states
  + DC; tract geography). Honest empty FGB outside coverage.
- For a different vintage (2020, 2018, ...) -> this tool pins SVI **2022**
  (the latest published). Older vintages are separate services.

**Parameters:**
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 (WGS84
        decimal degrees). Required -- ``supports_global_query=False`` (a
        nationwide tract query would return ~85k polygons). Recommended a
        county-or-smaller extent. Example for urban Houston / Harris County TX:
        ``(-95.45, 29.65, -95.25, 29.85)``.

**Returns:**
    ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Each feature is
    a Polygon (census tract) in EPSG:4326. Properties: ``fips`` (str, 11-digit
    tract FIPS), ``county`` (str), ``state_abbr`` (str), ``location`` (str,
    human label), ``total_pop`` (int|null), ``rpl_themes`` (float|null, overall
    percentile rank in [0,1]), ``rpl_theme1..rpl_theme4`` (float|null, per-theme
    percentile ranks). ``layer_type="vector"``, ``role="primary"``,
    ``style_preset="cdc_svi"``, ``units="percentile"``.

**Cross-tool dependencies:**
    - Often paired with a hazard footprint (``run_model_flood_scenario`` /
      ``fetch_noaa_slr_scenarios`` / ``fetch_firms_active_fire``) ->
      ``compute_zonal_statistics`` or vector intersection for
      "most-vulnerable exposed tracts".
    - Feeds ``clip_vector_to_polygon`` when combined with
      ``fetch_administrative_boundaries`` for city/county-scoped reports.

**Cache:** ``static-30d`` (FR-DC-2). SVI is published annually; a 30-day
stale window is fully appropriate.

**FR-AS-11 typed-error surface:** ``CDC_SVIError`` (base, retryable=True),
``CDC_SVIInputError`` (non-retryable bbox validation), ``CDC_SVIUpstreamError``
(retryable ArcGIS REST network / HTTP / parse failure), ``CDC_SVIEmptyError``
(no tracts in bbox -- NOT raised by default; an empty FGB is serialized so the
layer still appears with a zero-feature notice -- e.g. a bbox over open ocean
or outside the U.S.).

**FR-DC-9 payload estimation:** ~3 MB per square degree of urban area (tract
polygons are detailed). Clipped to [0.02, 80] MB.

``supports_global_query=False`` -- U.S. tract polygon source.

Endpoint (verified live 2026-06-27):
    https://onemap.cdc.gov/onemapservices/rest/services/SVI/
        CDC_ATSDR_Social_Vulnerability_Index_2022_USA/FeatureServer/2/query

    Query parameters::
        where=1=1
        geometry={xmin,ymin,xmax,ymax}
        geometryType=esriGeometryEnvelope
        inSR=4326
        spatialRel=esriSpatialRelIntersects
        outFields=FIPS,STATE,ST_ABBR,COUNTY,LOCATION,E_TOTPOP,
                  RPL_THEMES,RPL_THEME1,RPL_THEME2,RPL_THEME3,RPL_THEME4
        outSR=4326
        f=geojson
        resultRecordCount=2000
        resultOffset={offset}   (paginated; maxRecordCount=2000)

    Layer 1 = county, layer 2 = tract (used here). Coverage: 50 states + DC.
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
    "fetch_cdc_svi",
    "estimate_payload_mb",
    "CDC_SVIError",
    "CDC_SVIInputError",
    "CDC_SVIUpstreamError",
    "CDC_SVIEmptyError",
    "_build_svi_url",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_fetch_svi_features",
    "_features_to_flatgeobuf",
    "_normalize_score",
    "_fetch_svi_bytes",
    "SVI_QUERY_URL",
    "SVI_OUT_FIELDS",
    "SVI_NULL_SENTINEL",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.fetch_cdc_svi")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class CDC_SVIError(RuntimeError):
    """Base class for fetch_cdc_svi failures."""

    error_code: str = "CDC_SVI_ERROR"
    retryable: bool = True


class CDC_SVIInputError(CDC_SVIError):
    """Caller passed an invalid bbox."""

    error_code = "CDC_SVI_INPUT_INVALID"
    retryable = False


class CDC_SVIUpstreamError(CDC_SVIError):
    """CDC/ATSDR ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "CDC_SVI_UPSTREAM_ERROR"
    retryable = True


class CDC_SVIEmptyError(CDC_SVIError):
    """No SVI tracts found in bbox.

    NOT raised by default (we serialize an empty FGB instead -- a bbox over
    open ocean or outside the U.S. legitimately has no tracts), but available
    for future strict-mode opt-in.
    """

    error_code = "CDC_SVI_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: CDC/ATSDR OneMap ArcGIS REST FeatureServer query endpoint for the SVI 2022
#: tract layer (layer 2). Layer 1 is the coarser county layer.
SVI_QUERY_URL = (
    "https://onemap.cdc.gov/onemapservices/rest/services/SVI/"
    "CDC_ATSDR_Social_Vulnerability_Index_2022_USA/FeatureServer/2/query"
)

#: Fields requested from the SVI tract layer: tract identity + overall summary
#: ranking + the four theme percentile rankings.
SVI_OUT_FIELDS = (
    "FIPS,STATE,ST_ABBR,COUNTY,LOCATION,E_TOTPOP,"
    "RPL_THEMES,RPL_THEME1,RPL_THEME2,RPL_THEME3,RPL_THEME4"
)

#: CDC's null sentinel. SVI suppresses tracts with too-small populations and
#: encodes missing percentile ranks as ``-999`` (values <= -999 are sentinels,
#: never legitimate percentile ranks which live in [0, 1]).
SVI_NULL_SENTINEL = -999.0

#: Per-page record cap. The service reports maxRecordCount=2000; we page with
#: resultOffset until a short page is returned.
_PAGE_SIZE = 2000

#: Hard cap on total features to fetch (defensive -- a too-large bbox should be
#: rejected by payload warning upstream, but we never page unboundedly).
_MAX_FEATURES = 20000

#: HTTP request timeout (seconds).
_HTTP_TIMEOUT_S = 60.0

#: User-Agent.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Payload estimation heuristic: MB per square degree of (urban) tract polygons.
_PAYLOAD_MB_PER_SQ_DEG = 3.0
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 80.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata -- registered once at import time.
# ---------------------------------------------------------------------------

def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_cdc_svi",
        ttl_class="static-30d",
        source_class="cdc_svi",
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
            "registering fetch_cdc_svi without them"
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
    """Estimate the FlatGeobuf payload size for an SVI tract fetch.

    Heuristic: ~3 MB per square degree of urban tract polygons. An urban
    county (~0.04 sq deg) returns ~0.15 MB; a 1 sq deg metro returns ~3 MB.
    """
    if bbox is None:
        area_sq_deg = 1.0
    else:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
            area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
        except (TypeError, ValueError):
            area_sq_deg = 1.0

    est = area_sq_deg * _PAYLOAD_MB_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``CDC_SVIInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise CDC_SVIInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise CDC_SVIInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise CDC_SVIInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise CDC_SVIInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise CDC_SVIInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _normalize_score(v: Any) -> float | None:
    """Normalize an SVI percentile-rank value, mapping the -999 null sentinel.

    Legitimate SVI percentile ranks live in ``[0, 1]``; CDC encodes suppressed /
    missing values as ``-999``. Returns a float in [0,1] or ``None``.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= SVI_NULL_SENTINEL:
        return None
    return f


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_svi_url(
    bbox: tuple[float, float, float, float],
    offset: int,
) -> tuple[str, dict[str, str]]:
    """Build the CDC SVI ArcGIS REST query URL + params dict for one page."""
    min_lon, min_lat, max_lon, max_lat = bbox
    params: dict[str, str] = {
        "where": "1=1",
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": SVI_OUT_FIELDS,
        "outSR": "4326",
        "returnGeometry": "true",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
        "f": "geojson",
    }
    return SVI_QUERY_URL, params


# ---------------------------------------------------------------------------
# HTTP fetch (paginated).
# ---------------------------------------------------------------------------


def _fetch_svi_features(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch all SVI tract features intersecting bbox, paging by resultOffset.

    Returns a list of GeoJSON Feature dicts (possibly empty for a bbox with no
    tracts -- e.g. open ocean or outside the U.S.).

    Raises:
        ``CDC_SVIUpstreamError``: on network / HTTP / parse failures.
    """
    all_features: list[dict[str, Any]] = []
    offset = 0

    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        while True:
            url, params = _build_svi_url(bbox, offset)
            logger.info(
                "fetch_cdc_svi: GET %s (bbox=%s offset=%d)", url, bbox, offset
            )
            try:
                resp = client.get(
                    url, params=params, headers={"User-Agent": _USER_AGENT}
                )
            except httpx.HTTPError as exc:
                raise CDC_SVIUpstreamError(
                    f"CDC SVI request failed url={url} bbox={bbox} offset={offset}: {exc}"
                ) from exc

            if resp.status_code >= 400:
                raise CDC_SVIUpstreamError(
                    f"CDC SVI returned HTTP {resp.status_code} url={url} "
                    f"offset={offset}: {resp.text[:500]!r}"
                )

            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise CDC_SVIUpstreamError(
                    f"CDC SVI returned non-JSON url={url} offset={offset}: {exc}"
                ) from exc

            if not isinstance(body, dict):
                raise CDC_SVIUpstreamError(
                    f"CDC SVI response is not a JSON object url={url}: "
                    f"type={type(body).__name__!r}"
                )

            # ArcGIS REST may surface errors inside a 200 envelope.
            if "error" in body:
                raise CDC_SVIUpstreamError(
                    f"CDC SVI query returned error envelope url={url} "
                    f"offset={offset}: {body['error']}"
                )

            if body.get("type") != "FeatureCollection":
                raise CDC_SVIUpstreamError(
                    f"CDC SVI response is not a GeoJSON FeatureCollection url={url}: "
                    f"type={body.get('type')!r}"
                )

            page = body.get("features", []) or []
            all_features.extend(page)
            logger.info(
                "fetch_cdc_svi: page offset=%d -> %d feature(s) (total=%d)",
                offset,
                len(page),
                len(all_features),
            )

            # Stop on a short page (no more records) or a defensive cap.
            if len(page) < _PAGE_SIZE:
                break
            if len(all_features) >= _MAX_FEATURES:
                logger.warning(
                    "fetch_cdc_svi: hit _MAX_FEATURES cap (%d); bbox likely too large",
                    _MAX_FEATURES,
                )
                break
            offset += _PAGE_SIZE

    return all_features


# ---------------------------------------------------------------------------
# Features → FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert SVI tract GeoJSON features to FlatGeobuf bytes.

    Selects the overall + 4-theme percentile rankings and tract identity
    attributes; normalizes CDC's ``-999`` null sentinel to ``null``. Always
    emits valid FlatGeobuf bytes -- an empty feature list yields an empty-schema
    FGB so the cache shim has something concrete to persist.

    Raises:
        ``CDC_SVIUpstreamError``: if geopandas is unavailable or serialization
        fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CDC_SVIUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None:
            continue
        p = feat.get("properties") or {}

        total_pop_raw = p.get("E_TOTPOP")
        try:
            total_pop: int | None = (
                int(total_pop_raw)
                if total_pop_raw is not None and float(total_pop_raw) > SVI_NULL_SENTINEL
                else None
            )
        except (TypeError, ValueError):
            total_pop = None

        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "fips": p.get("FIPS"),
                "county": p.get("COUNTY"),
                "state_abbr": p.get("ST_ABBR"),
                "location": p.get("LOCATION"),
                "total_pop": total_pop,
                "rpl_themes": _normalize_score(p.get("RPL_THEMES")),
                "rpl_theme1": _normalize_score(p.get("RPL_THEME1")),
                "rpl_theme2": _normalize_score(p.get("RPL_THEME2")),
                "rpl_theme3": _normalize_score(p.get("RPL_THEME3")),
                "rpl_theme4": _normalize_score(p.get("RPL_THEME4")),
            },
        })

    _COLS = [
        "fips", "county", "state_abbr", "location", "total_pop",
        "rpl_themes", "rpl_theme1", "rpl_theme2", "rpl_theme3", "rpl_theme4",
    ]
    if not cleaned:
        import pandas as pd
        empty_df = pd.DataFrame(columns=_COLS)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_cdc_svi_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise CDC_SVIUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} SVI tract(s): {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_cdc_svi: FlatGeobuf = %d bytes (%d tract(s))",
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


def _fetch_svi_bytes(bbox: tuple[float, float, float, float]) -> bytes:
    """Fetch all SVI tracts for bbox -> FlatGeobuf bytes."""
    features = _fetch_svi_features(bbox)
    return _features_to_flatgeobuf(features)


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
def fetch_cdc_svi(
    bbox: tuple[float, float, float, float],
    # Wave 4.10 convention: absorb LLM-invented kwargs
    **_extra_ignored: Any,
) -> LayerURI:
    """CDC/ATSDR Social Vulnerability Index (SVI 2022) census-tract choropleth.

    Use this (not fetch_census_acs or fetch_epa_ejscreen) when you specifically want the CDC/ATSDR Social Vulnerability Index (SVI).

    Fetches U.S. census-tract polygons intersecting a bbox from CDC/ATSDR's
    public ArcGIS REST FeatureServer and returns a FlatGeobuf choropleth with
    the overall SVI percentile ranking (``rpl_themes``) plus the four theme
    rankings (socioeconomic status, household characteristics, racial/ethnic
    minority status, housing type & transportation). Higher percentile = more
    socially vulnerable. CDC's ``-999`` null sentinel is normalized to ``null``.

    **When to use:**
    - User asks for the CDC SVI, social vulnerability, equity / environmental-
      justice overlay, or "which neighborhoods are most vulnerable".
    - Agent needs a vulnerability surface to intersect with a hazard footprint
      (flood, wildfire, heat, plume) to find the most vulnerable exposed
      population.

    **When NOT to use:**
    - For raw demographic counts (population, income, race) -> use a Census/ACS
      fetcher (SVI publishes percentile rankings, not estimates).
    - For areas outside the United States -> SVI is U.S.-only (50 states + DC);
      an empty FGB is returned for non-U.S. bboxes.
    - For a non-2022 vintage -> this tool pins SVI 2022 (latest published).

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            ``supports_global_query=False`` -- U.S. tract polygon source; use a
            county-or-smaller extent. Example for urban Houston / Harris County
            TX: ``(-95.45, 29.65, -95.25, 29.85)``.

    **Returns:**
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Each feature
        is a Polygon (census tract) in EPSG:4326. Properties: ``fips`` (str,
        11-digit tract FIPS), ``county`` (str), ``state_abbr`` (str),
        ``location`` (str), ``total_pop`` (int|null), ``rpl_themes``
        (float|null, overall percentile [0,1]), ``rpl_theme1..rpl_theme4``
        (float|null, per-theme percentiles). ``layer_type="vector"``,
        ``role="primary"``, ``style_preset="cdc_svi"``, ``units="percentile"``.

    **Cross-tool dependencies (FR-TA-3):**
        - Feeds INTO: ``compute_zonal_statistics`` (vulnerable population inside
          a hazard footprint), ``clip_vector_to_polygon`` (admin-bounded SVI).
        - Combine WITH: ``run_model_flood_scenario`` / ``fetch_noaa_slr_scenarios``
          / ``fetch_firms_active_fire`` (hazard footprints) for vulnerability-
          weighted exposure; ``fetch_administrative_boundaries`` (county scope).

    **Error types (FR-AS-11):**
        - ``CDC_SVIInputError``: bad bbox (retryable=False).
        - ``CDC_SVIUpstreamError``: HTTP/network failure, ArcGIS error envelope,
          or FlatGeobuf serialization failure (retryable=True).
        - ``CDC_SVIEmptyError``: no tracts in bbox (retryable=False; not raised
          by default -- an empty FGB is returned instead).

    Cache: ``ttl_class="static-30d"``, ``source_class="cdc_svi"``. Cache key is
    SHA-256 of the bbox rounded to 6 dp.

    ``supports_global_query=False``. No API key required.
    """
    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise CDC_SVIInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc

    _validate_bbox(bbox)  # type: ignore[arg-type]

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "vintage": "2022",
        "geography": "tract",
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_svi_bytes(q_bbox),
    )
    assert result.uri is not None, (
        "fetch_cdc_svi is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    return LayerURI(
        layer_id=(
            f"cdc-svi-2022-tract-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"CDC/ATSDR SVI 2022 (tract) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="cdc_svi",
        role="primary",
        units="percentile",
        bbox=q_bbox,
    )
