"""``fetch_epa_ejscreen`` atomic tool -- EPA EJScreen environmental-justice
indices by census **block group** within a bbox, as a choropleth FlatGeobuf.

EJScreen is EPA's environmental-justice screening tool: for every U.S. census
block group it publishes a panel of environmental burden indicators (PM2.5,
ozone, diesel particulate matter, air-toxics cancer risk & respiratory hazard,
traffic proximity, lead-paint / pre-1960 housing, proximity to NPL Superfund /
RMP / TSDF sites, and major water dischargers), each as a raw value AND as a
nationwide **percentile** in ``[0, 100]``, plus demographic indicators (percent
minority, percent low-income, the supplemental demographic index). EPA removed
EJScreen from its own site on 2025-02-05; this tool reads the preserved national
EJScreen 2.x block-group FeatureServer (Esri "US Federal Data" ArcGIS Online
org), queried by envelope exactly like ``fetch_cdc_svi`` /
``fetch_epa_frs_facilities``.

**What it does:**
Fetches all EJScreen block-group polygons intersecting a bbox and returns a
FlatGeobuf choropleth. One ``indicator`` parameter selects WHICH EJScreen index
drives the primary choropleth column ``value`` (its nationwide percentile,
``[0, 100]``) -- e.g. ``pm25``, ``ozone``, ``diesel``, ``cancer``, ``resp``,
``traffic``, ``lead_paint``, ``superfund_proximity``, ``rmp_proximity``,
``tsdf_proximity``, ``wastewater``, or the ``demographic_index`` /
``ej_index_*`` rollups. Every feature ALSO carries the full panel of percentile
columns (``p_pm25``, ``p_ozone``, ...), the demographic context
(``minority_pct``, ``lowincome_pct``, ``demographic_index``), the block-group
id (``bg_id``, 12-digit GEOID), ``state_name``, and ``total_pop`` -- so a single
fetch supports cumulative-exposure narration, multiple choropleth re-styles, and
downstream zonal intersections without a re-fetch.

**When to use:**
- User asks for EJScreen, environmental justice, cumulative environmental
  burden / exposure, "which block groups have the worst air quality / PM2.5 /
  ozone / traffic / Superfund proximity", or an EJ overlay for a hazard footprint.
- Agent needs an environmental-burden surface to combine with
  ``fetch_cdc_svi`` (social vulnerability) and ``fetch_epa_frs_facilities``
  (the regulated facilities themselves) for a full cumulative-EJ exposure story.

**When NOT to use:**
- For social-vulnerability percentile rankings (SVI themes) -> ``fetch_cdc_svi``
  (tract-level; EJScreen is block-group and is environment+demographics, not the
  16-factor SVI).
- For the regulated-facility POINTS that drive the proximity indices ->
  ``fetch_epa_frs_facilities`` (TRI / Superfund / RMP / TSDF point inventory).
- For raw ACS demographic counts -> ``fetch_census_acs``.
- For areas outside the United States -> EJScreen is U.S.-only (50 states + DC +
  territories at block-group geography). An honest empty FGB is returned.

**Parameters:**
    indicator: which EJScreen index drives the primary ``value`` choropleth
        column. Default ``"pm25"``. One of the canonical keys (aliases
        accepted, case-insensitive): ``pm25``, ``ozone``, ``diesel`` /
        ``dslpm``, ``cancer``, ``resp`` / ``respiratory``, ``traffic`` /
        ``ptraf``, ``lead_paint`` / ``lead`` / ``ldpnt`` / ``pre1960``,
        ``superfund_proximity`` / ``superfund`` / ``npl`` / ``pnpl``,
        ``rmp_proximity`` / ``rmp`` / ``prmp``, ``tsdf_proximity`` / ``tsdf`` /
        ``ptsdf``, ``wastewater`` / ``water`` / ``pwdis``,
        ``demographic_index`` / ``demographic`` / ``minority`` (percent
        minority percentile), or one of the EJ-index rollups
        ``ej_pm25`` / ``ej_ozone`` / ``ej_traffic`` / ... (the
        demographic-weighted "EJ Index" percentile for that burden). Unknown
        values raise a typed input error listing the valid set.
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 (WGS84 decimal
        degrees). Required -- ``supports_global_query=False`` (a national
        block-group sweep is ~220k polygons). Recommended a county-or-smaller
        extent. Example urban Houston Ship Channel:
        ``(-95.30, 29.68, -95.05, 29.80)``.

**Returns:**
    ``LayerURI`` pointing at a FlatGeobuf in the cache bucket. Each feature is a
    Polygon (census block group) in EPSG:4326. Properties: ``bg_id`` (str,
    12-digit block-group GEOID), ``state_name`` (str), ``total_pop`` (int|null),
    ``value`` (float|null, the SELECTED indicator's percentile in [0,100]),
    ``indicator`` (str, the selected key, so the layer self-describes), plus the
    full percentile panel ``p_pm25``, ``p_ozone``, ``p_diesel``, ``p_cancer``,
    ``p_resp``, ``p_traffic``, ``p_lead_paint``, ``p_superfund``, ``p_rmp``,
    ``p_tsdf``, ``p_wastewater`` (float|null in [0,100]), the demographic
    context ``minority_pct`` (float|null in [0,1]), ``lowincome_pct``
    (float|null), ``demographic_index`` (float|null, supplemental demographic
    index 0..1), and a few raw values ``pm25_raw`` / ``ozone_raw`` (float|null).
    ``layer_type="vector"``, ``role="primary"``, ``style_preset="epa_ejscreen"``,
    ``units="percentile"``.

**Cross-tool dependencies (FR-TA-3):**
    - Combine WITH ``fetch_cdc_svi`` (social vulnerability) +
      ``fetch_epa_frs_facilities`` (the facilities) for cumulative
      environmental-justice exposure.
    - Feeds ``compute_zonal_statistics`` / ``clip_vector_to_polygon`` to find the
      most-burdened block groups inside a hazard footprint
      (``run_model_flood_scenario`` / ``fetch_firms_active_fire`` / a modeled
      plume).

**Cache:** ``static-30d`` (FR-DC-2). EJScreen is published annually; a 30-day
stale window is appropriate.

**FR-AS-11 typed-error surface:** ``EPA_EJScreenError`` (base, retryable=True),
``EPA_EJScreenInputError`` (non-retryable bbox / indicator validation),
``EPA_EJScreenUpstreamError`` (retryable ArcGIS REST network / HTTP / parse
failure), ``EPA_EJScreenEmptyError`` (no block groups in bbox -- NOT raised by
default; an empty FGB is serialized so the layer still appears -- e.g. a bbox
over open ocean or outside the U.S.).

**FR-DC-9 payload estimation:** ~4 MB per square degree of (urban) block-group
polygons. Clipped to [0.02, 90] MB.

``supports_global_query=False`` -- U.S. block-group polygon source. No API key.

Endpoint (verified live 2026-06-27):
    https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/
        EPA_EJ_Screen/FeatureServer/0/query

    The single layer ``EJSCREEN_Full`` (220,333 block-group polygons, national)
    is queried by ``esriGeometryEnvelope`` (passed as a JSON envelope object,
    NOT a comma string -- the comma form is rejected by this hosted service),
    ``f=json`` (Esri JSON; ``f=geojson`` is NOT supported here, so Esri rings are
    converted to GeoJSON Polygons in-code), ``returnGeometry=true`` (the service
    rejects attribute-only queries), ``outFields=*`` (naming the ``P_*``
    percentile fields explicitly trips an ArcGIS field-resolution quirk on this
    layer -- ``*`` is reliable and we select columns in-code), paginated via
    ``resultOffset`` / ``exceededTransferLimit`` (maxRecordCount=2000).
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
    "fetch_epa_ejscreen",
    "estimate_payload_mb",
    "EPA_EJScreenError",
    "EPA_EJScreenInputError",
    "EPA_EJScreenUpstreamError",
    "EPA_EJScreenEmptyError",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_resolve_indicator",
    "_build_query_params",
    "_fetch_ejscreen_features",
    "_esri_rings_to_geojson_geometry",
    "_features_to_flatgeobuf",
    "_normalize_percentile",
    "_normalize_fraction",
    "_fetch_ejscreen_bytes",
    "EJSCREEN_QUERY_URL",
    "EJSCREEN_INDICATORS",
    "EJSCREEN_NULL_SENTINELS",
]

logger = logging.getLogger("grace2_agent.tools.fetch_epa_ejscreen")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class EPA_EJScreenError(RuntimeError):
    """Base class for fetch_epa_ejscreen failures."""

    error_code: str = "EPA_EJSCREEN_ERROR"
    retryable: bool = True


class EPA_EJScreenInputError(EPA_EJScreenError):
    """Caller passed an invalid bbox or unknown indicator."""

    error_code = "EPA_EJSCREEN_INPUT_INVALID"
    retryable = False


class EPA_EJScreenUpstreamError(EPA_EJScreenError):
    """EJScreen ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "EPA_EJSCREEN_UPSTREAM_ERROR"
    retryable = True


class EPA_EJScreenEmptyError(EPA_EJScreenError):
    """No EJScreen block groups found in bbox.

    NOT raised by default (we serialize an empty FGB instead -- a bbox over open
    ocean or outside the U.S. legitimately has no block groups), but available
    for future strict-mode opt-in.
    """

    error_code = "EPA_EJSCREEN_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: National EJScreen 2.x block-group FeatureServer query endpoint (Esri
#: "US Federal Data" ArcGIS Online org). The single layer ``EJSCREEN_Full`` is
#: the full 220k-block-group panel. EPA removed its own EJScreen site
#: 2025-02-05; this is the preserved national mirror.
EJSCREEN_QUERY_URL = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "EPA_EJ_Screen/FeatureServer/0/query"
)

#: Indicator selection map: canonical key + accepted aliases -> the EJScreen
#: percentile FIELD that drives the primary ``value`` choropleth column. All are
#: nationwide percentiles in [0, 100]. ``demographic_index`` maps to the percent-
#: minority percentile (EJScreen 2.0-schema demographic index proxy). The
#: ``ej_*`` keys select the demographic-weighted "EJ Index" percentiles.
EJSCREEN_INDICATORS: dict[str, str] = {
    # --- environmental burden percentiles ---
    "pm25": "P_PM25",
    "ozone": "P_OZONE",
    "diesel": "P_DSLPM",
    "dslpm": "P_DSLPM",
    "cancer": "P_CANCER",  # not on this layer; falls back to D index below
    "resp": "P_RESP",
    "respiratory": "P_RESP",
    "traffic": "P_PTRAF",
    "ptraf": "P_PTRAF",
    "lead_paint": "P_LDPNT",
    "lead": "P_LDPNT",
    "ldpnt": "P_LDPNT",
    "pre1960": "P_LDPNT",
    "superfund_proximity": "P_PNPL",
    "superfund": "P_PNPL",
    "npl": "P_PNPL",
    "pnpl": "P_PNPL",
    "rmp_proximity": "P_PRMP",
    "rmp": "P_PRMP",
    "prmp": "P_PRMP",
    "tsdf_proximity": "P_PTSDF",
    "tsdf": "P_PTSDF",
    "ptsdf": "P_PTSDF",
    "wastewater": "P_PWDIS",
    "water": "P_PWDIS",
    "pwdis": "P_PWDIS",
    # --- demographic ---
    "demographic_index": "P_MINORPCT",
    "demographic": "P_MINORPCT",
    "minority": "P_MINORPCT",
    "p_minorpct": "P_MINORPCT",
    # --- EJ-index (demographic-weighted) rollups ---
    "ej_pm25": "P_PM25_D2",
    "ej_ozone": "P_OZONE_D2",
    "ej_diesel": "P_DSLPM_D2",
    "ej_resp": "P_RESP_D2",
    "ej_traffic": "P_PTRAF_D2",
    "ej_lead_paint": "P_LDPNT_D2",
    "ej_superfund": "P_PNPL_D2",
    "ej_rmp": "P_PRMP_D2",
    "ej_tsdf": "P_PTSDF_D2",
    "ej_wastewater": "P_PWDIS_D2",
}

#: Default indicator if none supplied.
_DEFAULT_INDICATOR = "pm25"

#: The full panel of percentile fields we always emit (so one fetch supports
#: any re-style). Maps the OUTPUT column name -> the EJScreen source field.
#: Only fields known to exist on the live ``EJSCREEN_Full`` layer.
_PANEL_PERCENTILE_FIELDS: dict[str, str] = {
    "p_pm25": "P_PM25",
    "p_ozone": "P_OZONE",
    "p_diesel": "P_DSLPM",
    "p_resp": "P_RESP",
    "p_traffic": "P_PTRAF",
    "p_lead_paint": "P_LDPNT",
    "p_superfund": "P_PNPL",
    "p_rmp": "P_PRMP",
    "p_tsdf": "P_PTSDF",
    "p_wastewater": "P_PWDIS",
    "p_minority": "P_MINORPCT",
}

#: Output column order for the FlatGeobuf (stable schema for empty-FGB too).
_OUTPUT_COLS: list[str] = [
    "bg_id",
    "state_name",
    "total_pop",
    "indicator",
    "value",
    *list(_PANEL_PERCENTILE_FIELDS.keys()),
    "minority_pct",
    "lowincome_pct",
    "demographic_index",
    "pm25_raw",
    "ozone_raw",
]

#: ArcGIS / EJScreen null sentinels. Suppressed / missing values are encoded as
#: very large negatives (e.g. -999, -2222222) or None. Legitimate percentiles
#: live in [0, 100]; legitimate fractions live in [0, 1].
EJSCREEN_NULL_SENTINELS = (-999.0, -1000.0)

#: Per-page record cap (service reports maxRecordCount=2000).
_PAGE_SIZE = 2000

#: Hard cap on total features (defensive -- a too-large bbox is rejected by the
#: payload warning upstream; we never page unboundedly).
_MAX_FEATURES = 30000

#: HTTP request timeout (seconds).
_HTTP_TIMEOUT_S = 60.0

#: User-Agent.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: Payload estimation heuristic: MB per square degree of urban block-group
#: polygons (block groups are finer than tracts -> a touch heavier than SVI).
_PAYLOAD_MB_PER_SQ_DEG = 4.0
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 90.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata -- registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_epa_ejscreen",
        ttl_class="static-30d",
        source_class="epa_ejscreen",
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
            "registering fetch_epa_ejscreen without them"
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
    """Estimate the FlatGeobuf payload size for an EJScreen block-group fetch.

    Heuristic: ~4 MB per square degree of urban block-group polygons.
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
# Validation / normalization helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``EPA_EJScreenInputError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise EPA_EJScreenInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise EPA_EJScreenInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise EPA_EJScreenInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise EPA_EJScreenInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise EPA_EJScreenInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _resolve_indicator(indicator: str | None) -> tuple[str, str]:
    """Resolve an indicator key/alias to ``(canonical_key, source_field)``.

    Returns the lower-cased canonical key (echoed into the layer's ``indicator``
    column) and the EJScreen percentile source field that drives ``value``.

    Raises ``EPA_EJScreenInputError`` for an unknown indicator, listing the
    valid canonical keys.
    """
    key = (indicator or _DEFAULT_INDICATOR).strip().lower()
    field = EJSCREEN_INDICATORS.get(key)
    if field is None:
        # canonical keys = those whose value is also a key target (dedup by field)
        valid = sorted(set(EJSCREEN_INDICATORS.keys()))
        raise EPA_EJScreenInputError(
            f"unknown indicator {indicator!r}; valid options (aliases accepted): "
            f"{valid}"
        )
    return key, field


def _normalize_percentile(v: Any) -> float | None:
    """Normalize an EJScreen percentile value, mapping null sentinels.

    Legitimate percentiles live in ``[0, 100]``; EJScreen suppresses
    small-population block groups and encodes missing percentiles as large
    negatives. Returns a float in [0,100] or ``None``.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if f <= EJSCREEN_NULL_SENTINELS[0]:
        return None
    # Clamp tiny floating overshoots; reject absurd values as sentinels.
    if f < -0.001 or f > 100.001:
        return None
    return max(0.0, min(100.0, f))


def _normalize_fraction(v: Any) -> float | None:
    """Normalize an EJScreen [0,1] fraction (e.g. MINORPCT), mapping sentinels."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if f <= EJSCREEN_NULL_SENTINELS[0]:
        return None
    if f < -0.001 or f > 1.001:
        return None
    return max(0.0, min(1.0, f))


def _normalize_raw(v: Any) -> float | None:
    """Normalize a raw environmental value (PM2.5 ug/m3, ozone ppb), sentinels->None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= EJSCREEN_NULL_SENTINELS[0]:
        return None
    return f


# ---------------------------------------------------------------------------
# Esri JSON geometry -> GeoJSON geometry.
# ---------------------------------------------------------------------------


def _esri_rings_to_geojson_geometry(esri_geom: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert an Esri-JSON polygon (``rings``) to a GeoJSON geometry dict.

    The EJScreen FeatureServer does not support ``f=geojson`` for this layer, so
    we request Esri JSON (``f=json``) and convert here. Esri ``rings`` are a flat
    list of rings (outer + holes, by winding); we keep ALL rings under a single
    GeoJSON Polygon -- that is the standard ArcGIS->GeoJSON convention and
    geopandas/pyogrio repair ring orientation on write. Multi-part block groups
    are vanishingly rare; if present, extra outer rings simply become additional
    polygon rings (acceptable for a choropleth). Returns ``None`` if there is no
    usable geometry.
    """
    if not isinstance(esri_geom, dict):
        return None
    rings = esri_geom.get("rings")
    if not rings or not isinstance(rings, list):
        return None
    # Drop empty/degenerate rings.
    good = [r for r in rings if isinstance(r, list) and len(r) >= 4]
    if not good:
        return None
    return {"type": "Polygon", "coordinates": good}


# ---------------------------------------------------------------------------
# URL / params building.
# ---------------------------------------------------------------------------


def _build_query_params(
    bbox: tuple[float, float, float, float],
    offset: int,
) -> dict[str, str]:
    """Build the EJScreen ArcGIS REST query params for one page.

    Geometry MUST be a JSON envelope object (the comma-string form is rejected
    by this hosted service). ``outFields=*`` is used because naming the ``P_*``
    percentile fields explicitly trips a field-resolution quirk on this layer.
    ``returnGeometry=true`` is required (attribute-only queries are rejected).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    envelope = json.dumps(
        {
            "xmin": min_lon,
            "ymin": min_lat,
            "xmax": max_lon,
            "ymax": max_lat,
            "spatialReference": {"wkid": 4326},
        }
    )
    return {
        "where": "1=1",
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
        "f": "json",
    }


# ---------------------------------------------------------------------------
# HTTP fetch (paginated, Esri JSON).
# ---------------------------------------------------------------------------


def _fetch_ejscreen_features(
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Fetch all EJScreen block-group features intersecting bbox.

    Returns a list of Esri-JSON feature dicts (``attributes`` + ``geometry``),
    possibly empty for a bbox with no block groups (open ocean / outside U.S.).
    Pages by ``resultOffset`` until a short page or ``exceededTransferLimit`` is
    cleared.

    Raises ``EPA_EJScreenUpstreamError`` on network / HTTP / parse failures.
    """
    all_features: list[dict[str, Any]] = []
    offset = 0

    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        while True:
            params = _build_query_params(bbox, offset)
            logger.info(
                "fetch_epa_ejscreen: GET %s (bbox=%s offset=%d)",
                EJSCREEN_QUERY_URL,
                bbox,
                offset,
            )
            try:
                resp = client.get(
                    EJSCREEN_QUERY_URL,
                    params=params,
                    headers={"User-Agent": _USER_AGENT},
                )
            except httpx.HTTPError as exc:
                raise EPA_EJScreenUpstreamError(
                    f"EJScreen request failed url={EJSCREEN_QUERY_URL} "
                    f"bbox={bbox} offset={offset}: {exc}"
                ) from exc

            if resp.status_code >= 400:
                raise EPA_EJScreenUpstreamError(
                    f"EJScreen returned HTTP {resp.status_code} offset={offset}: "
                    f"{resp.text[:500]!r}"
                )

            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise EPA_EJScreenUpstreamError(
                    f"EJScreen returned non-JSON offset={offset}: {exc}"
                ) from exc

            if not isinstance(body, dict):
                raise EPA_EJScreenUpstreamError(
                    f"EJScreen response is not a JSON object: "
                    f"type={type(body).__name__!r}"
                )

            # ArcGIS REST surfaces errors inside a 200 envelope.
            if "error" in body:
                raise EPA_EJScreenUpstreamError(
                    f"EJScreen query returned error envelope offset={offset}: "
                    f"{body['error']}"
                )

            page = body.get("features", []) or []
            all_features.extend(page)
            logger.info(
                "fetch_epa_ejscreen: page offset=%d -> %d feature(s) (total=%d)",
                offset,
                len(page),
                len(all_features),
            )

            exceeded = bool(body.get("exceededTransferLimit"))
            # Stop on a short page (no more records) unless the server explicitly
            # signals more via exceededTransferLimit.
            if len(page) < _PAGE_SIZE and not exceeded:
                break
            if len(page) == 0:
                break
            if len(all_features) >= _MAX_FEATURES:
                logger.warning(
                    "fetch_epa_ejscreen: hit _MAX_FEATURES cap (%d); bbox too large",
                    _MAX_FEATURES,
                )
                break
            offset += len(page)

    return all_features


# ---------------------------------------------------------------------------
# Esri features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(
    features: list[dict[str, Any]],
    value_field: str,
    indicator_key: str,
) -> bytes:
    """Convert EJScreen Esri-JSON features to FlatGeobuf bytes.

    ``value_field`` is the EJScreen source field whose percentile fills the
    primary ``value`` column; ``indicator_key`` is echoed into the ``indicator``
    column. The full percentile panel + demographic context are always emitted.
    Always serializes valid FlatGeobuf bytes -- an empty feature list yields an
    empty-schema FGB so the cache shim has something concrete to persist.

    Raises ``EPA_EJScreenUpstreamError`` if geopandas is unavailable or
    serialization fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EPA_EJScreenUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = _esri_rings_to_geojson_geometry(feat.get("geometry"))
        if geom is None:
            continue
        a = feat.get("attributes") or {}

        pop_raw = a.get("ACSTOTPOP")
        try:
            total_pop: int | None = (
                int(pop_raw)
                if pop_raw is not None and float(pop_raw) > EJSCREEN_NULL_SENTINELS[0]
                else None
            )
        except (TypeError, ValueError):
            total_pop = None

        props: dict[str, Any] = {
            "bg_id": a.get("ID"),
            "state_name": a.get("STATE_NAME"),
            "total_pop": total_pop,
            "indicator": indicator_key,
            "value": _normalize_percentile(a.get(value_field)),
            "minority_pct": _normalize_fraction(a.get("MINORPCT")),
            "lowincome_pct": _normalize_fraction(a.get("LOWINCPCT")),
            "demographic_index": _normalize_fraction(a.get("VULEOPCT")),
            "pm25_raw": _normalize_raw(a.get("PM25")),
            "ozone_raw": _normalize_raw(a.get("OZONE")),
        }
        for out_col, src_field in _PANEL_PERCENTILE_FIELDS.items():
            props[out_col] = _normalize_percentile(a.get(src_field))

        cleaned.append({"type": "Feature", "geometry": geom, "properties": props})

    if not cleaned:
        import pandas as pd

        empty_df = pd.DataFrame(columns=_OUTPUT_COLS)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()
        # Stable column order (geometry kept by geopandas).
        ordered = [c for c in _OUTPUT_COLS if c in gdf.columns]
        gdf = gdf[[*ordered, "geometry"]]

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_epa_ejscreen_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise EPA_EJScreenUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} block group(s): {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_epa_ejscreen: FlatGeobuf = %d bytes (%d block group(s))",
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


def _fetch_ejscreen_bytes(
    bbox: tuple[float, float, float, float],
    value_field: str,
    indicator_key: str,
) -> bytes:
    """Fetch all EJScreen block groups for bbox -> FlatGeobuf bytes."""
    features = _fetch_ejscreen_features(bbox)
    return _features_to_flatgeobuf(features, value_field, indicator_key)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # readOnlyHint=True (read-only), openWorldHint=True (external public API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_epa_ejscreen(
    bbox: tuple[float, float, float, float],
    indicator: str = "pm25",
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """EPA EJScreen environmental-justice indices by census block group.

    Fetches U.S. census-block-group polygons intersecting a bbox from the
    preserved national EJScreen 2.x FeatureServer and returns a FlatGeobuf
    choropleth. The ``indicator`` parameter selects which EJScreen percentile
    fills the primary ``value`` column (PM2.5, ozone, diesel PM, respiratory
    hazard, traffic proximity, lead-paint, Superfund/RMP/TSDF proximity, water
    dischargers, or a demographic / EJ-index rollup). Every feature also carries
    the full percentile panel + demographic context, so one fetch supports
    re-styling and cumulative-exposure narration without a re-fetch.

    **When to use:**
    - User asks for EJScreen, environmental justice, cumulative environmental
      burden / exposure, or "which block groups have the worst PM2.5 / ozone /
      traffic / Superfund proximity".
    - Agent needs an environmental-burden surface to pair with ``fetch_cdc_svi``
      (social vulnerability) + ``fetch_epa_frs_facilities`` (the facilities).

    **When NOT to use:**
    - For the 16-factor social-vulnerability index -> ``fetch_cdc_svi``.
    - For the regulated-facility POINTS -> ``fetch_epa_frs_facilities``.
    - For raw ACS demographic counts -> ``fetch_census_acs``.
    - For areas outside the United States -> EJScreen is U.S.-only; an empty FGB
      is returned for non-U.S. bboxes.

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required.
            ``supports_global_query=False`` -- use a county-or-smaller extent.
            Example urban Houston Ship Channel: ``(-95.30, 29.68, -95.05, 29.80)``.
        indicator: which EJScreen index drives the ``value`` choropleth column.
            Default ``"pm25"``. Aliases accepted (case-insensitive): ``ozone``,
            ``diesel``, ``resp``, ``traffic``, ``lead_paint``,
            ``superfund_proximity``, ``rmp_proximity``, ``tsdf_proximity``,
            ``wastewater``, ``demographic_index``, or an ``ej_*`` rollup.

    **Returns:**
        ``LayerURI`` -> FlatGeobuf in the cache bucket. Each feature is a Polygon
        (block group) in EPSG:4326. Properties: ``bg_id`` (str, 12-digit GEOID),
        ``state_name`` (str), ``total_pop`` (int|null), ``indicator`` (str),
        ``value`` (float|null, selected percentile [0,100]), the percentile panel
        ``p_pm25``/``p_ozone``/``p_diesel``/``p_resp``/``p_traffic``/
        ``p_lead_paint``/``p_superfund``/``p_rmp``/``p_tsdf``/``p_wastewater``/
        ``p_minority`` (float|null [0,100]), ``minority_pct``/``lowincome_pct``/
        ``demographic_index`` (float|null [0,1]), ``pm25_raw``/``ozone_raw``
        (float|null). ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="epa_ejscreen"``, ``units="percentile"``.

    **Cross-tool dependencies (FR-TA-3):**
        - Combine WITH ``fetch_cdc_svi`` + ``fetch_epa_frs_facilities`` for
          cumulative environmental-justice exposure.
        - Feeds ``compute_zonal_statistics`` / ``clip_vector_to_polygon`` to find
          the most-burdened block groups inside a hazard footprint.

    **Error types (FR-AS-11):**
        - ``EPA_EJScreenInputError``: bad bbox or unknown indicator
          (retryable=False).
        - ``EPA_EJScreenUpstreamError``: HTTP/network failure, ArcGIS error
          envelope, or FlatGeobuf serialization failure (retryable=True).
        - ``EPA_EJScreenEmptyError``: no block groups in bbox (retryable=False;
          not raised by default -- an empty FGB is returned instead).

    Cache: ``ttl_class="static-30d"``, ``source_class="epa_ejscreen"``. Cache key
    is SHA-256 of the bbox (6 dp) + indicator. ``supports_global_query=False``.
    No API key required.
    """
    # ---- Indicator resolution (before bbox so a bad key errors cheaply) ----
    indicator_key, value_field = _resolve_indicator(indicator)

    # ---- Input validation ----
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise EPA_EJScreenInputError(
                f"bbox must be a 4-tuple; got {type(bbox).__name__}"
            ) from exc

    _validate_bbox(bbox)  # type: ignore[arg-type]

    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "indicator": indicator_key,
        "geography": "blockgroup",
        "vintage": "2.x",
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_ejscreen_bytes(q_bbox, value_field, indicator_key),
    )
    assert result.uri is not None, (
        "fetch_epa_ejscreen is cacheable; uri must be set by read_through"
    )

    # ---- Build LayerURI ----
    return LayerURI(
        layer_id=(
            f"epa-ejscreen-{indicator_key}-bg-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"EPA EJScreen {indicator_key} (block group) -- bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="epa_ejscreen",
        role="primary",
        units="percentile",
        bbox=q_bbox,
    )
