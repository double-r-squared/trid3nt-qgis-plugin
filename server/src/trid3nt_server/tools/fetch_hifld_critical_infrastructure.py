"""``fetch_hifld_critical_infrastructure`` atomic tool — HIFLD critical-
infrastructure POINTS by facility type.

Wraps the national Homeland Infrastructure Foundation-Level Data (HIFLD) Open
critical-infrastructure point inventories, served from a public, unauthenticated
ArcGIS REST FeatureService mirror. Returns a FlatGeobuf POINT layer of facilities
of one ``facility_type`` (hospitals, schools, fire_stations, police, power_plants)
intersecting a user-supplied bbox, together with the canonical HIFLD attribute
payload (name, address, city, state, ZIP, and facility-type-specific fields like
hospital beds / trauma level, school enrollment / grade range, fire-station
apparatus counts).

**Source / endpoint (verified live 2026-06-27):**
    The original ``hifldgeoplatform.opendata.arcgis.com`` Open Data portal was
    retired (shut down 2025-08-26). The HIFLD national point layers are mirrored
    on a stable public ArcGIS Online org (NWS / NOAA-published, no token) at::

        https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/
            <Service>/FeatureServer/0/query

    where ``<Service>`` is one of:
        ``Hospitals``                       (7,570 features, HIFLD Hospitals)
        ``Public_Schools``                  (102,274, HIFLD Public Schools K-12)
        ``Fire_Stations``                   (53,087, HIFLD Fire Stations)
        ``Local_Law_Enforcement_Locations`` (23,611, HIFLD Law Enforcement)
        ``Power_Plants``                    (21,333, EPA/EIA Power Plants)

    Each is an ``esriGeometryPoint`` layer, ``maxRecordCount=2000``, queried by
    ``esriGeometryEnvelope`` + ``inSR=4326`` + ``f=geojson`` exactly like the
    ``fetch_noaa_slr_scenarios`` / ``fetch_usace_dams`` ESRI REST pattern.

**What it does:**
    Fetches all facility points of one ``facility_type`` intersecting the bbox,
    paginating if the bbox holds more than one server page, and serializes them
    to a FlatGeobuf vector layer in EPSG:4326 with the HIFLD attribute columns
    preserved. A ``facility_type`` and ``facility_label`` column are added so the
    layer self-describes for narration and downstream overlay.

**When to use:**
    - User asks "where are the hospitals / schools / fire stations / police
      stations near [place]?" or "show critical infrastructure in [bbox]".
    - A hazard / exposure workflow needs the locations of life-safety facilities
      inside a flood, fire, or surge footprint (intersect with
      ``compute_zonal_statistics`` or ``clip_vector_to_polygon``).
    - Evacuation / shelter planning needs school + fire-station + hospital points.
    - Damage-assessment context: overlay critical facilities on a hazard layer.

**When NOT to use:**
    - For dams → ``fetch_usace_dams`` (NID).
    - For the National Structure Inventory building stock → ``fetch_usace_nsi``.
    - For non-US facilities — HIFLD coverage is US (50 states + DC + territories).
    - For real-time facility status / occupancy — HIFLD is a static inventory.
    - For an arbitrary OSM amenity not in the supported ``facility_type`` set —
      use an OSM Overpass fetcher instead.

**Parameters:**
    facility_type: One of ``"hospitals"``, ``"schools"``, ``"fire_stations"``,
        ``"police"``, ``"power_plants"``. Aliases are accepted (e.g.
        ``"hospital"``, ``"public_schools"``, ``"fire"``, ``"law_enforcement"``,
        ``"ems"`` → fire_stations). Unknown values raise a typed input error
        listing the valid set.
    bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required —
        ``supports_global_query=False`` (a national sweep of schools alone is
        100k+ points). Example for Houston metro: ``(-95.8, 29.5, -95.0, 30.1)``.

**Returns:**
    ``LayerURI`` pointing at a FlatGeobuf of ``Point`` features in EPSG:4326.
    Properties include the HIFLD source columns plus ``facility_type`` (str) and
    ``facility_label`` (str). ``layer_type="vector"``, ``role="primary"``,
    ``style_preset="hifld_critical_infrastructure"``, ``units=None``.

**Cache:** ``static-30d`` — HIFLD inventories update infrequently (annual-ish).

**FR-AS-11 typed-error surface:** ``HIFLDInfraError`` (base, retryable=True),
``HIFLDInfraInputError`` (bad facility_type / bbox, non-retryable),
``HIFLDInfraUpstreamError`` (ArcGIS REST network / HTTP / parse failure,
retryable), ``HIFLDInfraEmptyError`` (no features — NOT raised by default; an
empty FGB is serialized so the layer still appears with a zero-feature notice).

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

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_hifld_critical_infrastructure",
    "estimate_payload_mb",
    "HIFLDInfraError",
    "HIFLDInfraInputError",
    "HIFLDInfraUpstreamError",
    "HIFLDInfraEmptyError",
    "FACILITY_TYPES",
    "FACILITY_ALIASES",
    "_resolve_facility_type",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_build_query_url",
    "_fetch_features_paginated",
    "_features_to_flatgeobuf",
    "_fetch_infra_bytes",
    "HIFLD_ORG_BASE",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_hifld_critical_infrastructure")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class HIFLDInfraError(RuntimeError):
    """Base class for fetch_hifld_critical_infrastructure failures."""

    error_code: str = "HIFLD_INFRA_ERROR"
    retryable: bool = True


class HIFLDInfraInputError(HIFLDInfraError):
    """Caller passed an unknown facility_type or invalid bbox."""

    error_code = "HIFLD_INFRA_INPUT_INVALID"
    retryable = False


class HIFLDInfraUpstreamError(HIFLDInfraError):
    """HIFLD ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "HIFLD_INFRA_UPSTREAM_ERROR"
    retryable = True


class HIFLDInfraEmptyError(HIFLDInfraError):
    """No facilities of this type found in bbox.

    NOT raised by default (a bbox over open water legitimately has no
    facilities); an empty FGB is serialized instead. Kept for strict-mode opt-in.
    """

    error_code = "HIFLD_INFRA_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants — facility-type -> ArcGIS REST service mapping.
# ---------------------------------------------------------------------------

#: Public, unauthenticated ArcGIS Online org hosting the national HIFLD
#: critical-infrastructure point mirror (NWS/NOAA-published). Verified
#: 2026-06-27. The original ``hifldgeoplatform.opendata.arcgis.com`` portal
#: was retired 2025-08-26; this mirror carries the same HIFLD schemas.
HIFLD_ORG_BASE = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services"

#: Canonical facility_type -> (ArcGIS service name, human label) mapping.
#: Each service is a national point FeatureService layer 0.
FACILITY_TYPES: dict[str, tuple[str, str]] = {
    "hospitals": ("Hospitals", "Hospital"),
    "schools": ("Public_Schools", "Public School"),
    "fire_stations": ("Fire_Stations", "Fire Station"),
    "police": ("Local_Law_Enforcement_Locations", "Law Enforcement"),
    "power_plants": ("Power_Plants", "Power Plant"),
}

#: Aliases the LLM / user may pass, mapped to the canonical facility_type key.
FACILITY_ALIASES: dict[str, str] = {
    "hospital": "hospitals",
    "medical_center": "hospitals",
    "medical_centers": "hospitals",
    "school": "schools",
    "public_school": "schools",
    "public_schools": "schools",
    "k12": "schools",
    "k-12": "schools",
    "fire_station": "fire_stations",
    "fire": "fire_stations",
    "fire_department": "fire_stations",
    "fire_departments": "fire_stations",
    "ems": "fire_stations",
    "ems_stations": "fire_stations",
    "police_station": "police",
    "police_stations": "police",
    "law_enforcement": "police",
    "law_enforcement_locations": "police",
    "sheriff": "police",
    "power_plant": "power_plants",
    "powerplant": "power_plants",
    "powerplants": "power_plants",
    "power": "power_plants",
}

# User-Agent — identify this client clearly to the ESRI cluster.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Server-enforced max page size on these FeatureServices.
_PAGE_SIZE = 2000

# HTTP request timeout (seconds).
_HTTP_TIMEOUT_S = 45.0

# Hard cap on features paginated in one call — keeps FGB + S3 write tractable
# (a metro bbox of schools can run several thousand). Callers wanting more
# should narrow the bbox.
_MAX_TOTAL_FEATURES = 30_000

# Payload sizing: each point feature serializes to ~1.2 KB of FlatGeobuf
# (point geometry + ~20-30 scalar HIFLD attributes).
_BYTES_PER_FEATURE = 1200
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 50.0

# Rough national feature density per square degree, used only by the advisory
# payload estimator. Schools are the densest layer.
_DENSITY_PER_SQ_DEG: dict[str, int] = {
    "hospitals": 30,
    "schools": 400,
    "fire_stations": 200,
    "police": 90,
    "power_plants": 70,
}
_DEFAULT_DENSITY = 80


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_hifld_critical_infrastructure",
        ttl_class="static-30d",
        source_class="hifld_critical_infrastructure",
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
            "registering fetch_hifld_critical_infrastructure without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    facility_type: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate the FlatGeobuf payload size for an infrastructure fetch.

    Scales by bbox area and per-type national density. Advisory only.
    """
    if bbox is None:
        area_sq_deg = 1.0
    else:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
            area_sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
        except (TypeError, ValueError):
            area_sq_deg = 1.0

    density = _DEFAULT_DENSITY
    if isinstance(facility_type, str):
        key = FACILITY_ALIASES.get(facility_type.strip().lower(), facility_type.strip().lower())
        density = _DENSITY_PER_SQ_DEG.get(key, _DEFAULT_DENSITY)

    est_features = min(_MAX_TOTAL_FEATURES, max(1, int(area_sq_deg * density)))
    est_mb = (est_features * _BYTES_PER_FEATURE) / (1024 * 1024)
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est_mb))


# ---------------------------------------------------------------------------
# Facility-type + bbox validation helpers.
# ---------------------------------------------------------------------------


def _resolve_facility_type(facility_type: Any) -> str:
    """Normalize and validate facility_type to a canonical key.

    Raises ``HIFLDInfraInputError`` for unknown / non-string values.
    """
    if not isinstance(facility_type, str) or not facility_type.strip():
        raise HIFLDInfraInputError(
            f"facility_type must be a non-empty string; got "
            f"{type(facility_type).__name__}: {facility_type!r}. "
            f"Valid values: {sorted(FACILITY_TYPES)}"
        )
    key = facility_type.strip().lower().replace(" ", "_").replace("-", "_")
    key = FACILITY_ALIASES.get(key, key)
    if key not in FACILITY_TYPES:
        raise HIFLDInfraInputError(
            f"facility_type={facility_type!r} is not supported; "
            f"valid values: {sorted(FACILITY_TYPES)} "
            f"(aliases also accepted, e.g. 'hospital', 'fire', 'law_enforcement')"
        )
    return key


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``HIFLDInfraInputError`` if bbox is invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise HIFLDInfraInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise HIFLDInfraInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise HIFLDInfraInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise HIFLDInfraInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise HIFLDInfraInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(float(v), 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# URL building.
# ---------------------------------------------------------------------------


def _build_query_url(
    facility_key: str,
    bbox: tuple[float, float, float, float],
    *,
    result_offset: int = 0,
) -> tuple[str, dict[str, str]]:
    """Build the HIFLD ArcGIS REST query URL + params dict for one page.

    Queries layer 0 of the service for all point features intersecting the
    bbox in EPSG:4326, returned as GeoJSON, with stable pagination.
    """
    service_name, _label = FACILITY_TYPES[facility_key]
    url = f"{HIFLD_ORG_BASE}/{service_name}/FeatureServer/0/query"
    min_lon, min_lat, max_lon, max_lat = bbox
    params: dict[str, str] = {
        "where": "1=1",
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
    return url, params


# ---------------------------------------------------------------------------
# HTTP fetch with pagination.
# ---------------------------------------------------------------------------


def _fetch_one_page(url: str, params: dict[str, str], facility_key: str) -> list[dict[str, Any]]:
    """GET one page of the HIFLD FeatureService query and return GeoJSON features.

    Raises ``HIFLDInfraUpstreamError`` on network / HTTP / parse / error-envelope.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise HIFLDInfraUpstreamError(
            f"HIFLD request failed facility={facility_key} url={url}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise HIFLDInfraUpstreamError(
            f"HIFLD returned HTTP {resp.status_code} facility={facility_key} "
            f"url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HIFLDInfraUpstreamError(
            f"HIFLD returned non-JSON facility={facility_key} url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise HIFLDInfraUpstreamError(
            f"HIFLD response is not a JSON object facility={facility_key}: "
            f"type={type(body).__name__!r}"
        )

    if "error" in body:
        raise HIFLDInfraUpstreamError(
            f"HIFLD query returned error envelope facility={facility_key} "
            f"url={url}: {body['error']}"
        )

    if body.get("type") != "FeatureCollection":
        raise HIFLDInfraUpstreamError(
            f"HIFLD response is not a GeoJSON FeatureCollection "
            f"facility={facility_key}: type={body.get('type')!r}"
        )

    return body.get("features", []) or []


def _fetch_features_paginated(
    facility_key: str,
    bbox: tuple[float, float, float, float],
    *,
    max_features: int = _MAX_TOTAL_FEATURES,
) -> list[dict[str, Any]]:
    """Page through the HIFLD FeatureService, accumulating up to ``max_features``."""
    accumulated: list[dict[str, Any]] = []
    offset = 0
    while True:
        url, params = _build_query_url(facility_key, bbox, result_offset=offset)
        logger.info(
            "fetch_hifld_critical_infrastructure: GET %s facility=%s offset=%d",
            url,
            facility_key,
            offset,
        )
        page = _fetch_one_page(url, params, facility_key)
        accumulated.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        if len(accumulated) >= max_features:
            logger.warning(
                "fetch_hifld_critical_infrastructure: hit max_features=%d cap "
                "(facility=%s); truncating",
                max_features,
                facility_key,
            )
            accumulated = accumulated[:max_features]
            break
        offset += _PAGE_SIZE
    logger.info(
        "fetch_hifld_critical_infrastructure: facility=%s -> %d feature(s)",
        facility_key,
        len(accumulated),
    )
    return accumulated


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(
    facility_key: str,
    features: list[dict[str, Any]],
) -> bytes:
    """Convert HIFLD point features to FlatGeobuf bytes.

    Preserves the HIFLD source attributes (coercing non-scalar values to JSON
    strings) and adds ``facility_type`` + ``facility_label`` columns. Always
    emits a valid FlatGeobuf — an empty input yields a header-only FGB so the
    cache shim has a concrete artifact to persist (honest-empty path).

    Raises ``HIFLDInfraUpstreamError`` if geopandas is unavailable or the
    FlatGeobuf write fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise HIFLDInfraUpstreamError(
            f"geopandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    _service, label = FACILITY_TYPES[facility_key]

    cleaned: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if geom is None or geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates")
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        if not (math.isfinite(coords[0]) and math.isfinite(coords[1])):
            continue
        props = feat.get("properties") or {}
        row: dict[str, Any] = {}
        for k, v in props.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row[k] = v
        row["facility_type"] = facility_key
        row["facility_label"] = label
        cleaned.append({
            "type": "Feature",
            "geometry": geom,
            "properties": row,
        })

    if not cleaned:
        import pandas as pd
        empty_df = pd.DataFrame(columns=["facility_type", "facility_label"])
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_hifld_infra_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise HIFLDInfraUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} {facility_key} features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_hifld_critical_infrastructure: FlatGeobuf = %d bytes "
            "(%d %s feature(s))",
            len(fgb_bytes),
            len(gdf),
            facility_key,
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


def _fetch_infra_bytes(
    facility_key: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Fetch + paginate + serialize: (facility_key, bbox) -> FlatGeobuf bytes."""
    features = _fetch_features_paginated(facility_key, bbox)
    return _features_to_flatgeobuf(facility_key, features)


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
def fetch_hifld_critical_infrastructure(
    facility_type: str,
    bbox: tuple[float, float, float, float],
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """HIFLD critical-infrastructure points by facility type as a FlatGeobuf layer.

    Fetches national Homeland Infrastructure Foundation-Level Data (HIFLD) point
    facilities of one ``facility_type`` intersecting a bbox, from a public,
    unauthenticated ArcGIS REST FeatureService mirror. Returns a FlatGeobuf of
    ``Point`` features in EPSG:4326 with the HIFLD attribute payload plus
    ``facility_type`` / ``facility_label`` columns.

    **When to use:**
    - "Where are the hospitals / schools / fire stations / police stations near
      [place]?"; "show critical infrastructure in this area".
    - A hazard / exposure workflow needs life-safety facility locations inside a
      flood / fire / surge footprint (intersect with ``compute_zonal_statistics``
      or ``clip_vector_to_polygon``).
    - Evacuation / shelter / damage-assessment context overlays.

    **When NOT to use:**
    - Dams -> ``fetch_usace_dams``. Buildings -> ``fetch_usace_nsi``.
    - Non-US facilities (HIFLD is US-only). Real-time status (HIFLD is static).
    - An amenity not in the supported set -> use an OSM Overpass fetcher.

    **Parameters:**
        facility_type: One of ``"hospitals"``, ``"schools"``, ``"fire_stations"``,
            ``"police"``, ``"power_plants"`` (aliases accepted: ``"hospital"``,
            ``"public_schools"``, ``"fire"``, ``"ems"``, ``"law_enforcement"``,
            ``"power_plant"``, etc.).
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required —
            ``supports_global_query=False``. Example Houston metro:
            ``(-95.8, 29.5, -95.0, 30.1)``.

    **Returns:**
        ``LayerURI`` -> FlatGeobuf of ``Point`` features in EPSG:4326. Properties
        carry the HIFLD source columns (NAME, ADDRESS, CITY, STATE, ZIP, and
        type-specific fields) plus ``facility_type`` and ``facility_label``.
        ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="hifld_critical_infrastructure"``, ``units=None``.

    **Error types (FR-AS-11):**
        - ``HIFLDInfraInputError``: unknown facility_type or bad bbox
          (retryable=False).
        - ``HIFLDInfraUpstreamError``: HTTP/network failure, ArcGIS error
          envelope, or FlatGeobuf serialization failure (retryable=True).
        - ``HIFLDInfraEmptyError``: no features in bbox (retryable=False; not
          raised by default — empty FGB is returned).

    Cache: ``ttl_class="static-30d"``, ``source_class="hifld_critical_infrastructure"``.
    Cache key is SHA-256 of ``(facility_key, bbox-rounded-6dp)``.

    ``supports_global_query=False``. No API key required.
    """
    # ---- Input validation ----
    facility_key = _resolve_facility_type(facility_type)
    _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(bbox)

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "facility_type": facility_key,
        "bbox": list(q_bbox),
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_infra_bytes(facility_key, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_hifld_critical_infrastructure is cacheable; "
        "uri must be set by read_through"
    )

    _service, label = FACILITY_TYPES[facility_key]
    return LayerURI(
        layer_id=(
            f"hifld-{facility_key}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"HIFLD {label}s — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="hifld_critical_infrastructure",
        role="primary",
        units=None,
        bbox=q_bbox,
    )
