"""``fetch_epa_frs_facilities`` atomic tool — EPA regulated-facility POINTS by bbox.
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
    "fetch_epa_frs_facilities",
    "estimate_payload_mb",
    "EpaFrsError",
    "EpaFrsInputError",
    "EpaFrsUpstreamError",
    "EpaFrsEmptyError",
    "FACILITY_PROGRAMS",
    "PROGRAM_ALIASES",
    "FRS_UNION_PROGRAMS",
    "_resolve_program",
    "_validate_bbox",
    "_round_bbox_to_6dp",
    "_build_query_url",
    "_fetch_layer_paginated",
    "_normalize_point_feature",
    "_normalize_superfund_feature",
    "_features_to_flatgeobuf",
    "_fetch_frs_bytes",
    "EPA_NEPASSIST_BASE",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class EpaFrsError(RuntimeError):
    """Base class for fetch_epa_frs_facilities failures."""

    error_code: str = "EPA_FRS_ERROR"
    retryable: bool = True


class EpaFrsInputError(EpaFrsError):
    """Caller passed an unknown facility_program or invalid bbox."""

    error_code = "EPA_FRS_INPUT_INVALID"
    retryable = False


class EpaFrsUpstreamError(EpaFrsError):
    """EPA ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "EPA_FRS_UPSTREAM_ERROR"
    retryable = True


class EpaFrsEmptyError(EpaFrsError):
    """No EPA facilities of this program found in bbox.

    NOT raised by default (a bbox over open water / rural land legitimately has
    no regulated facilities); an empty FGB is serialized instead. Kept for
    strict-mode opt-in.
    """

    error_code = "EPA_FRS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants — program -> EPA NEPAssist MapServer layer mapping.
# ---------------------------------------------------------------------------

#: Public, unauthenticated EPA ArcGIS REST MapServer hosting the FRS
#: program-interest point layers (NEPAssist public layers). Verified live
#: 2026-06-27 on the EPA geopub cluster (no token).
EPA_NEPASSIST_BASE = (
    "https://geopub.epa.gov/arcgis/rest/services/"
    "NEPAssist/NEPAVELayersPublic_fgdb/MapServer"
)

#: Canonical facility_program -> (layer_id, human label, is_polygon) mapping.
#: Each entry is a layer in the NEPAVELayersPublic MapServer. ``is_polygon``
#: layers (Superfund) carry the point in LATITUDE/LONGITUDE columns.
FACILITY_PROGRAMS: dict[str, tuple[int, str, bool]] = {
    "tri": (15, "Toxic Release (TRI)", False),
    "water": (16, "Water Discharger (NPDES)", False),
    "hazwaste": (17, "Hazardous Waste (RCRA)", False),
    "air": (18, "Air Emissions", False),
    "brownfields": (13, "Brownfield", False),
    "superfund": (14, "Superfund (NPL)", True),
}

#: The set of point programs unioned when ``facility_program="frs"`` (the
#: default "all regulated facilities" sweep). Superfund is excluded from the
#: union because it is a separate, sparse, polygon-sourced layer with a
#: distinct schema (NPL status); ask for it explicitly with
#: ``facility_program="superfund"``.
FRS_UNION_PROGRAMS: list[str] = ["tri", "water", "hazwaste", "air", "brownfields"]

#: Aliases the LLM / user may pass, mapped to a canonical program key (or the
#: special ``"frs"`` union sentinel).
PROGRAM_ALIASES: dict[str, str] = {
    "frs": "frs",
    "all": "frs",
    "facilities": "frs",
    "regulated": "frs",
    "regulated_facilities": "frs",
    "epa": "frs",
    "toxic_release": "tri",
    "toxic_releases": "tri",
    "tris": "tri",
    "toxics": "tri",
    "npl": "superfund",
    "sems": "superfund",
    "cercla": "superfund",
    "superfund_npl": "superfund",
    "npdes": "water",
    "water_discharger": "water",
    "water_dischargers": "water",
    "discharger": "water",
    "wastewater": "water",
    "rcra": "hazwaste",
    "rcrainfo": "hazwaste",
    "hazardous_waste": "hazwaste",
    "hazwaste_facilities": "hazwaste",
    "air_emissions": "air",
    "air_emission": "air",
    "afs": "air",
    "brownfield": "brownfields",
    "acres": "brownfields",
}

# User-Agent — identify this client clearly to the EPA ESRI cluster.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Server-enforced max page size on these MapServer layers.
_PAGE_SIZE = 2000

# HTTP request timeout (seconds).
_HTTP_TIMEOUT_S = 45.0

# Hard cap on features paginated per underlying layer — keeps FGB + S3 write
# tractable. An industrial metro bbox of all programs can run several thousand;
# callers wanting more should narrow the bbox.
_MAX_FEATURES_PER_LAYER = 20_000

# Payload sizing: each point feature serializes to ~0.4 KB of FlatGeobuf
# (point geometry + ~13 scalar FRS attributes).
_BYTES_PER_FEATURE = 420
_PAYLOAD_MIN_MB = 0.02
_PAYLOAD_MAX_MB = 50.0

# Rough national feature density per square degree for the advisory payload
# estimator. The "frs" union is the densest case.
_DENSITY_PER_SQ_DEG: dict[str, int] = {
    "frs": 9000,
    "tri": 1200,
    "water": 4500,
    "hazwaste": 4000,
    "air": 1200,
    "brownfields": 400,
    "superfund": 30,
}
_DEFAULT_DENSITY = 5000


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common = dict(
        name="fetch_epa_frs_facilities",
        ttl_class="static-30d",
        source_class="epa_frs_facilities",
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
            "registering fetch_epa_frs_facilities without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    facility_program: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate the FlatGeobuf payload size for an EPA FRS fetch.

    Scales by bbox area and per-program national density. Advisory only.
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
    if isinstance(facility_program, str):
        key = facility_program.strip().lower().replace(" ", "_").replace("-", "_")
        key = PROGRAM_ALIASES.get(key, key)
        density = _DENSITY_PER_SQ_DEG.get(key, _DEFAULT_DENSITY)

    n_layers = len(FRS_UNION_PROGRAMS) if density == _DENSITY_PER_SQ_DEG["frs"] else 1
    est_features = min(
        _MAX_FEATURES_PER_LAYER * max(1, n_layers),
        max(1, int(area_sq_deg * density)),
    )
    est_mb = (est_features * _BYTES_PER_FEATURE) / (1024 * 1024)
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est_mb))


# ---------------------------------------------------------------------------
# Program + bbox validation helpers.
# ---------------------------------------------------------------------------


def _resolve_program(facility_program: Any) -> str:
    """Normalize and validate facility_program to a canonical key or ``"frs"``.

    Raises ``EpaFrsInputError`` for unknown / non-string values.
    """
    if facility_program is None:
        return "frs"
    if not isinstance(facility_program, str) or not facility_program.strip():
        raise EpaFrsInputError(
            f"facility_program must be a non-empty string; got "
            f"{type(facility_program).__name__}: {facility_program!r}. "
            f"Valid values: {['frs'] + sorted(FACILITY_PROGRAMS)}"
        )
    key = facility_program.strip().lower().replace(" ", "_").replace("-", "_")
    key = PROGRAM_ALIASES.get(key, key)
    if key != "frs" and key not in FACILITY_PROGRAMS:
        raise EpaFrsInputError(
            f"facility_program={facility_program!r} is not supported; "
            f"valid values: {['frs'] + sorted(FACILITY_PROGRAMS)} "
            f"(aliases also accepted, e.g. 'all', 'toxic_release', 'npl', "
            f"'npdes', 'rcra')"
        )
    return key


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``EpaFrsInputError`` if bbox is invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise EpaFrsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
        raise EpaFrsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise EpaFrsInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise EpaFrsInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise EpaFrsInputError(
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
    program_key: str,
    bbox: tuple[float, float, float, float],
    *,
    result_offset: int = 0,
) -> tuple[str, dict[str, str]]:
    """Build the EPA NEPAssist MapServer query URL + params dict for one page.

    Queries the program's layer for all features intersecting the bbox in
    EPSG:4326 with stable pagination. Point layers return GeoJSON; the polygon
    Superfund layer returns ESRI JSON attributes only (geometry is synthesized
    from LATITUDE/LONGITUDE), so ``returnGeometry=false`` and ``f=json``.
    """
    layer_id, _label, is_polygon = FACILITY_PROGRAMS[program_key]
    url = f"{EPA_NEPASSIST_BASE}/{layer_id}/query"
    min_lon, min_lat, max_lon, max_lat = bbox
    params: dict[str, str] = {
        "where": "1=1",
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "resultOffset": str(result_offset),
        "resultRecordCount": str(_PAGE_SIZE),
        # Stable cursor so pagination does not drop rows across page boundaries.
        "orderByFields": "OBJECTID ASC",
    }
    if is_polygon:
        params["f"] = "json"
        params["returnGeometry"] = "false"
    else:
        params["f"] = "geojson"
    return url, params


# ---------------------------------------------------------------------------
# HTTP fetch with pagination.
# ---------------------------------------------------------------------------


def _fetch_one_page(
    url: str, params: dict[str, str], program_key: str, *, is_polygon: bool
) -> list[dict[str, Any]]:
    """GET one page of the EPA MapServer query and return the feature list.

    Returns GeoJSON ``features`` (point layers) or ESRI ``features``
    (each ``{"attributes": {...}}``, polygon layer). Raises
    ``EpaFrsUpstreamError`` on network / HTTP / parse / error-envelope.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise EpaFrsUpstreamError(
            f"EPA FRS request failed program={program_key} url={url}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        raise EpaFrsUpstreamError(
            f"EPA FRS returned HTTP {resp.status_code} program={program_key} "
            f"url={url}: {resp.text[:500]!r}"
        )

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise EpaFrsUpstreamError(
            f"EPA FRS returned non-JSON program={program_key} url={url}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise EpaFrsUpstreamError(
            f"EPA FRS response is not a JSON object program={program_key}: "
            f"type={type(body).__name__!r}"
        )

    if "error" in body:
        raise EpaFrsUpstreamError(
            f"EPA FRS query returned error envelope program={program_key} "
            f"url={url}: {body['error']}"
        )

    if is_polygon:
        # ESRI JSON: {"features": [{"attributes": {...}}, ...]}.
        feats = body.get("features")
        if feats is None:
            raise EpaFrsUpstreamError(
                f"EPA FRS ESRI-JSON response missing 'features' "
                f"program={program_key}: keys={sorted(body)[:8]}"
            )
        return feats or []

    if body.get("type") != "FeatureCollection":
        raise EpaFrsUpstreamError(
            f"EPA FRS response is not a GeoJSON FeatureCollection "
            f"program={program_key}: type={body.get('type')!r}"
        )
    return body.get("features", []) or []


def _fetch_layer_paginated(
    program_key: str,
    bbox: tuple[float, float, float, float],
    *,
    max_features: int = _MAX_FEATURES_PER_LAYER,
) -> list[dict[str, Any]]:
    """Page through one program's EPA layer, accumulating up to ``max_features``."""
    _layer_id, _label, is_polygon = FACILITY_PROGRAMS[program_key]
    accumulated: list[dict[str, Any]] = []
    offset = 0
    while True:
        url, params = _build_query_url(program_key, bbox, result_offset=offset)
        logger.info(
            "fetch_epa_frs_facilities: GET %s program=%s offset=%d",
            url,
            program_key,
            offset,
        )
        page = _fetch_one_page(url, params, program_key, is_polygon=is_polygon)
        accumulated.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        if len(accumulated) >= max_features:
            logger.warning(
                "fetch_epa_frs_facilities: hit max_features=%d cap "
                "(program=%s); truncating",
                max_features,
                program_key,
            )
            accumulated = accumulated[:max_features]
            break
        offset += _PAGE_SIZE
    logger.info(
        "fetch_epa_frs_facilities: program=%s -> %d feature(s)",
        program_key,
        len(accumulated),
    )
    return accumulated


# ---------------------------------------------------------------------------
# Feature normalization -> common point schema.
# ---------------------------------------------------------------------------


def _normalize_point_feature(
    feat: dict[str, Any], program_key: str, label: str
) -> dict[str, Any] | None:
    """Normalize one GeoJSON FRS point feature to the common schema.

    Returns a GeoJSON Feature dict, or ``None`` if the geometry is missing /
    non-point / non-finite (skipped).
    """
    if not isinstance(feat, dict):
        return None
    geom = feat.get("geometry")
    if geom is None or geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    if not (math.isfinite(coords[0]) and math.isfinite(coords[1])):
        return None
    props = feat.get("properties") or {}
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [coords[0], coords[1]]},
        "properties": {
            "registry_id": props.get("registry_id"),
            "program": program_key,
            "program_label": label,
            "program_acronym": props.get("pgm_sys_acrnm"),
            "program_id": props.get("pgm_sys_id"),
            "facility_name": props.get("primary_name"),
            "address": props.get("location_address"),
            "city": props.get("city_name"),
            "county": props.get("county_name"),
            "state": props.get("state_code"),
            "postal_code": props.get("postal_code"),
            "epa_region": props.get("epa_region"),
            "facility_url": props.get("facility_url"),
            "npl_status": None,
        },
    }


def _normalize_superfund_feature(
    feat: dict[str, Any], label: str
) -> dict[str, Any] | None:
    """Normalize one ESRI-JSON Superfund record to the common point schema.

    The Superfund layer is a polygon layer, but every record carries populated
    ``LATITUDE`` / ``LONGITUDE`` site-centroid columns; we synthesize the Point
    from those. Returns ``None`` if lat/lon are missing / non-finite.
    """
    if not isinstance(feat, dict):
        return None
    attrs = feat.get("attributes") if "attributes" in feat else feat
    if not isinstance(attrs, dict):
        return None
    lat = attrs.get("LATITUDE")
    lon = attrs.get("LONGITUDE")
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (-180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0):
        return None
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
        "properties": {
            "registry_id": attrs.get("EPA_ID"),
            "program": "superfund",
            "program_label": label,
            "program_acronym": "SEMS/NPL",
            "program_id": attrs.get("EPA_ID"),
            "facility_name": attrs.get("Site_Name"),
            "address": attrs.get("Address"),
            "city": attrs.get("City"),
            "county": attrs.get("County"),
            "state": attrs.get("State"),
            "postal_code": attrs.get("Zip_Code"),
            "epa_region": attrs.get("Region"),
            "facility_url": attrs.get("FACILITY_URL"),
            "npl_status": attrs.get("NPL_Status"),
        },
    }


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------

#: Stable column order for the emitted FlatGeobuf (so the schema is identical
#: whether or not any features were found — important for the honest-empty path
#: and for downstream consumers).
_OUTPUT_COLUMNS: list[str] = [
    "registry_id",
    "program",
    "program_label",
    "program_acronym",
    "program_id",
    "facility_name",
    "address",
    "city",
    "county",
    "state",
    "postal_code",
    "epa_region",
    "facility_url",
    "npl_status",
]


def _features_to_flatgeobuf(cleaned: list[dict[str, Any]]) -> bytes:
    """Convert normalized FRS point features to FlatGeobuf bytes.

    Always emits a valid FlatGeobuf — an empty input yields a header-only FGB
    (with the stable column schema) so the cache shim has a concrete artifact to
    persist (honest-empty path).

    Raises ``EpaFrsUpstreamError`` if geopandas is unavailable or the
    FlatGeobuf write fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EpaFrsUpstreamError(
            f"geopandas/pandas not available for FlatGeobuf conversion: {exc}"
        ) from exc

    if not cleaned:
        empty_df = pd.DataFrame(columns=_OUTPUT_COLUMNS)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs="EPSG:4326")
    else:
        gdf = gpd.GeoDataFrame.from_features(cleaned, crs="EPSG:4326")
        gdf = gdf.dropna(subset=["geometry"]).copy()
        # Enforce the stable column order; add any missing column as null.
        for col in _OUTPUT_COLUMNS:
            if col not in gdf.columns:
                gdf[col] = None
        gdf = gdf[_OUTPUT_COLUMNS + ["geometry"]]

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_epa_frs_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise EpaFrsUpstreamError(
                f"FlatGeobuf write failed for {len(gdf)} EPA FRS features: {exc}"
            ) from exc

        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()

        logger.info(
            "fetch_epa_frs_facilities: FlatGeobuf = %d bytes (%d feature(s))",
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


def _fetch_frs_bytes(
    program_key: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Fetch + paginate + normalize + serialize: (program, bbox) -> FGB bytes.

    For ``program_key == "frs"`` this fetches each of ``FRS_UNION_PROGRAMS`` and
    unions the normalized points. Otherwise it fetches the single program layer.
    """
    if program_key == "frs":
        programs = FRS_UNION_PROGRAMS
    else:
        programs = [program_key]

    cleaned: list[dict[str, Any]] = []
    for prog in programs:
        _layer_id, label, is_polygon = FACILITY_PROGRAMS[prog]
        raw = _fetch_layer_paginated(prog, bbox)
        for feat in raw:
            norm = (
                _normalize_superfund_feature(feat, label)
                if is_polygon
                else _normalize_point_feature(feat, prog, label)
            )
            if norm is not None:
                cleaned.append(norm)

    logger.info(
        "fetch_epa_frs_facilities: program=%s -> %d normalized point(s)",
        program_key,
        len(cleaned),
    )
    return _features_to_flatgeobuf(cleaned)


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
def fetch_epa_frs_facilities(
    bbox: tuple[float, float, float, float],
    facility_program: str = "frs",
    # Wave 4.10 convention: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """EPA regulated-facility points by program as a FlatGeobuf layer.

    Fetches EPA Facility Registry Service (FRS) program-interest point facilities
    of one ``facility_program`` intersecting a bbox, from a public, unauthenticated
    EPA ArcGIS REST MapServer (the EPA NEPAssist public layers). Returns a
    FlatGeobuf of ``Point`` features in EPSG:4326 with a common FRS attribute
    schema (``registry_id``, ``program``, ``program_id``, ``facility_name``,
    address columns, ``facility_url``).

    **When to use:**
    - "Show EPA regulated facilities / TRI sites / Superfund sites near [place]".
    - The MODFLOW contamination-plume demo: locate a candidate chemical-source
      facility, or determine which regulated facilities a modeled plume reaches
      (intersect with ``compute_zonal_statistics`` / ``clip_vector_to_polygon``).
    - Any hazard / exposure workflow needing regulated industrial / waste facility
      locations inside a flood / fire / surge / plume footprint.

    **When NOT to use:**
    - Life-safety facilities (hospitals / schools / fire / police / power) ->
      ``fetch_hifld_critical_infrastructure``.
    - Dams -> ``fetch_usace_dams``. Buildings -> ``fetch_usace_nsi``.
    - Non-US facilities (FRS is US-only). Real-time emission / discharge readings
      (FRS is a static inventory).

    **Parameters:**
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. Required —
            ``supports_global_query=False``. Example Houston Ship Channel:
            ``(-95.30, 29.68, -95.05, 29.80)``.
        facility_program: One of ``"frs"`` (default — union of TRI + water +
            hazardous-waste + air + brownfields point layers), ``"tri"``,
            ``"superfund"``, ``"air"``, ``"water"``, ``"hazwaste"``,
            ``"brownfields"`` (aliases accepted: ``"all"``, ``"toxic_release"``,
            ``"npl"``, ``"npdes"``, ``"rcra"``, etc.).

    **Returns:**
        ``LayerURI`` -> FlatGeobuf of ``Point`` features in EPSG:4326. Properties
        carry ``registry_id``, ``program``, ``program_label``, ``program_acronym``,
        ``program_id``, ``facility_name``, ``address``, ``city``, ``county``,
        ``state``, ``postal_code``, ``epa_region``, ``facility_url`` (and
        ``npl_status`` for Superfund). ``layer_type="vector"``, ``role="primary"``,
        ``style_preset="epa_frs_facilities"``, ``units=None``.

    **Error types:**
        - ``EpaFrsInputError``: unknown facility_program or bad bbox
          (retryable=False).
        - ``EpaFrsUpstreamError``: HTTP/network failure, ArcGIS error envelope,
          or FlatGeobuf serialization failure (retryable=True).
        - ``EpaFrsEmptyError``: no features in bbox (retryable=False; not raised
          by default — empty FGB is returned).

    Cache: ``ttl_class="static-30d"``, ``source_class="epa_frs_facilities"``.
    Cache key is SHA-256 of ``(program_key, bbox-rounded-6dp)``.

    ``supports_global_query=False``. No API key required.
    """
    # ---- Input validation ----
    program_key = _resolve_program(facility_program)
    _validate_bbox(bbox)
    q_bbox = _round_bbox_to_6dp(bbox)

    # ---- Cache-key params ----
    params: dict[str, Any] = {
        "program": program_key,
        "bbox": list(q_bbox),
    }

    # ---- Read-through cache ----
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_frs_bytes(program_key, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_epa_frs_facilities is cacheable; uri must be set by read_through"
    )

    if program_key == "frs":
        label = "Regulated Facilities (FRS)"
    else:
        _layer_id, label, _is_polygon = FACILITY_PROGRAMS[program_key]
        label = f"{label} Facilities"

    return LayerURI(
        layer_id=(
            f"epa-frs-{program_key}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}"
        ),
        name=(
            f"EPA {label} — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="vector",
        uri=result.uri,
        style_preset="epa_frs_facilities",
        role="primary",
        units=None,
        bbox=q_bbox,
    )
