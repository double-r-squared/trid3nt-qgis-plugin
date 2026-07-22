"""``fetch_usgs_groundwater_levels`` atomic tool — real USGS NWIS groundwater
levels (monitoring wells + their latest water-level) as points.

Fetches **real, observed** USGS groundwater monitoring wells and their latest
groundwater-level reading from the modernized USGS Water Data OGC API — the
machine API behind ``waterdata.usgs.gov`` (the same network that the legacy
``waterservices.usgs.gov/nwis/gwlevels/`` service exposed). One Point feature
per well-level reading at the well's coordinates, carrying the latest
depth-to-water or groundwater-elevation value plus its parameter code, unit,
vertical datum and reading timestamp.

This is the OBSERVED head record the MODFLOW groundwater demos need to compare
against modeled heads — the real instrument/field-measurement record of how deep
the water table is, NOT a model estimate.

**Why the OGC API (NOT the legacy gwlevels service)**: the legacy
``waterservices.usgs.gov/nwis/gwlevels/`` endpoint was DECOMMISSIONED beginning
2025-11-01 (it now serves only an HTML decommissioning notice for every format).
The replacement is the USGS Water Data OGC API — keyless, OGC-Features-compliant,
at ``api.waterdata.usgs.gov/ogcapi/v0``.

**API surface** (USGS Water Data OGC API, free, NO API key required):

    PRIMARY — latest field measurements (the latest discrete reading per series):
        https://api.waterdata.usgs.gov/ogcapi/v0/collections/
            latest-field-measurements/items
            ?f=json
            &parameter_code=72019,72150,62610,62611,61055
            &bbox=west,south,east,north        (area asks)   -- OR --
            &state_code=20                      (FIPS, state-level asks)
            &limit=10000
        Returns a GeoJSON FeatureCollection — one Feature per (well x parameter)
        latest field measurement. Each feature's ``properties`` carry
        ``monitoring_location_id`` (e.g. ``USGS-383047098095901`` or an agency
        prefix like ``KS014-...``), ``parameter_code``, ``value``,
        ``unit_of_measure``, ``time`` (ISO-8601), ``vertical_datum``,
        ``approval_status``; geometry is a Point ``[lon, lat]``.

    ENRICHMENT — monitoring-locations (well metadata: human name + aquifer):
        https://api.waterdata.usgs.gov/ogcapi/v0/collections/
            monitoring-locations/items
            ?f=json&site_type_code=GW
            &bbox=... | &state_code=...
            &limit=...
        Carries ``monitoring_location_number``, ``monitoring_location_name``,
        ``national_aquifer_code``, ``well_constructed_depth`` keyed by the same
        ``id`` as the measurement's ``monitoring_location_id``. We join on it to
        attach a human well name + aquifer + well depth to each reading. The join
        is best-effort (some agency series have no matching GW location) — a
        missing name leaves ``site_name=""`` but never drops the reading.

**Groundwater parameter codes** (NWIS pcodes for water-level):
    - ``72019`` depth to water level, FEET below land surface (the most common).
    - ``72150`` groundwater level relative to NAVD88 (ft).
    - ``62610`` groundwater level above NGVD29 (ft).
    - ``62611`` groundwater level above NAVD88 (ft).
    - ``61055`` water level in well, ft below measuring point.
We request all of them; each feature carries its own ``parameter_code`` so the
client / a downstream comparison can distinguish depth-to-water from elevation.

**Spatial selector handling**: the OGC API places NO area limit on ``bbox``
(unlike the legacy NWIS ``bBox`` ~25 deg^2 cap), so a sub-state bbox of any size
is accepted. For a state-level ask PREFER ``state_code`` — passed as a 2-letter
USPS code (``"KS"``) and mapped internally to the FIPS numeric code the OGC API
expects (``"20"``). When both are given, ``state_code`` wins. When neither is
given, ``GwInputError`` is raised.

**Fallback norm** (primary -> honest typed error): the OGC API returns an HTTP
200 FeatureCollection with ZERO features for an area with no instrumented
groundwater wells (e.g. open ocean). That is a legitimate "no wells" answer, not
a success — so ``GwNoWellsError`` (retryable=False) is raised carrying the scope.
We never return an empty success-shaped layer. If the measurement collection is
itself unreachable we raise the retryable ``GwUpstreamError``.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
point FeatureCollection (one point per well-level reading), serialized as
FlatGeobuf and rendered via the inline-GeoJSON vector path. ``style_preset`` =
``"usgs_groundwater"``; ``LayerURI.bbox`` is set to the wells' extent so the
camera auto-zooms.

Tier-1, no auth, ``supports_global_query=False`` (US + territories only).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_usgs_groundwater_levels",
    "estimate_payload_mb",
    "GwLevelsError",
    "GwInputError",
    "GwUpstreamError",
    "GwNoWellsError",
    "_validate_bbox",
    "_validate_state_code",
    "_round_bbox_to_6dp",
    "_build_measurements_url",
    "_build_locations_url",
    "_parse_measurements_geojson",
    "_parse_locations_geojson",
    "_join_records",
    "_records_bbox",
    "_build_flatgeobuf",
    "_fetch_usgs_groundwater_levels_bytes",
    "MEASUREMENTS_URL",
    "LOCATIONS_URL",
    "GW_PARAMETER_CODES",
    "USPS_TO_FIPS",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_usgs_groundwater_levels")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class GwLevelsError(RuntimeError):
    """Base class for fetch_usgs_groundwater_levels failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "USGS_GROUNDWATER_ERROR"
    retryable: bool = True


class GwInputError(GwLevelsError):
    """Invalid inputs — bad bbox shape, bad state code, no spatial selector.

    Not retryable: the caller must fix the argument.
    """

    error_code = "USGS_GROUNDWATER_INPUT_ERROR"
    retryable = False


class GwUpstreamError(GwLevelsError):
    """USGS Water Data OGC API request failed (network error, HTTP 5xx, bad body).

    Retryable — transient USGS outages recover on retry.
    """

    error_code = "USGS_GROUNDWATER_UPSTREAM_ERROR"
    retryable = True


class GwNoWellsError(GwLevelsError):
    """No USGS groundwater wells with a water-level reading found in scope.

    Not retryable — the area genuinely has no instrumented groundwater
    monitoring wells reporting a level. Either widen the scope or pick an area
    with a known monitoring network (e.g. the High Plains aquifer).
    """

    error_code = "USGS_GROUNDWATER_NO_WELLS"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: USGS Water Data OGC API — latest discrete field measurements (per series).
MEASUREMENTS_URL = (
    "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
    "latest-field-measurements/items"
)

#: USGS Water Data OGC API — monitoring-location metadata (well name/aquifer).
LOCATIONS_URL = (
    "https://api.waterdata.usgs.gov/ogcapi/v0/collections/"
    "monitoring-locations/items"
)

#: NWIS groundwater water-level parameter codes:
#:   72019 depth to water level, ft below land surface (most common)
#:   72150 groundwater level relative to NAVD88 (ft)
#:   62610 groundwater level above NGVD29 (ft)
#:   62611 groundwater level above NAVD88 (ft)
#:   61055 water level in well, ft below measuring point
GW_PARAMETER_CODES: tuple[str, ...] = ("72019", "72150", "62610", "62611", "61055")

#: Human-readable label for each pcode (overlay legend / tooltips).
_PCODE_LABEL: dict[str, str] = {
    "72019": "depth to water (ft below land surface)",
    "72150": "groundwater level (ft, NAVD88)",
    "62610": "groundwater elevation (ft, NGVD29)",
    "62611": "groundwater elevation (ft, NAVD88)",
    "61055": "water level (ft below measuring point)",
}

#: User-Agent per USGS usage guidance (a descriptive UA is recommended).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds).
_HTTP_TIMEOUT = 90.0

#: OGC API page size. Wells per area are small; 10000 covers a dense aquifer.
_PAGE_LIMIT = 10000

#: 2-letter USPS state/territory code -> FIPS numeric code (the OGC API's
#: ``state_code`` filter expects the FIPS form, e.g. Kansas = "20").
USPS_TO_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72", "VI": "78", "GU": "66",
    "AS": "60", "MP": "69",
}


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_usgs_groundwater_levels",
        ttl_class="dynamic-1h",
        source_class="usgs_groundwater_levels",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_usgs_groundwater_levels without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    state_code: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    Each well-level reading is one Point feature with a handful of small scalar
    properties (~180 bytes serialized). Well density:

    - 1deg x 1deg bbox over an instrumented aquifer -> ~100s of readings -> ~50 KB
    - whole-state ``state_code`` query -> up to ~1000s of readings -> ~0.3 MB

    The estimate is conservative; groundwater point layers are always tiny.
    """
    n_readings = 800  # default guess (state-level)
    if state_code is None and bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # ~200 readings per 1deg square in an instrumented aquifer region.
            n_readings = max(1, int(sq_deg * 200))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_readings * 180 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``GwInputError`` if the bbox is malformed or out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GwInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise GwInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise GwInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise GwInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise GwInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _validate_state_code(state_code: str) -> tuple[str, str]:
    """Normalize + validate a 2-letter USPS code -> (usps, fips).

    The OGC API's ``state_code`` filter expects the FIPS numeric form, so this
    returns BOTH the canonical USPS code (for the layer name / cache key) and
    the FIPS code (for the query URL).
    """
    if not isinstance(state_code, str):
        raise GwInputError(
            f"state_code must be a 2-letter string; got {type(state_code).__name__}"
        )
    sc = state_code.strip().upper()
    if sc not in USPS_TO_FIPS:
        raise GwInputError(
            f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
            f"expected one of e.g. 'KS', 'CA', 'FL' (mapped to a FIPS code for "
            f"the USGS OGC API state_code filter)"
        )
    return sc, USPS_TO_FIPS[sc]


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``GwUpstreamError`` on failure.

    A 404 on the OGC items endpoint means "no items matched" — surfaced as an
    empty body so the caller's honest-empty gate engages instead of aborting.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info(
                "fetch_usgs_groundwater_levels: OGC API 404 (no items) for %s", url
            )
            return b""
        raise GwUpstreamError(
            f"USGS Water Data OGC API returned HTTP {exc.code} for {url}: "
            f"{exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise GwUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise GwUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def _build_measurements_url(
    *,
    state_fips: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Build the latest-field-measurements OGC items URL for the scope."""
    params: list[tuple[str, str]] = [
        ("f", "json"),
        ("parameter_code", ",".join(GW_PARAMETER_CODES)),
        ("limit", str(_PAGE_LIMIT)),
    ]
    if state_fips is not None:
        params.append(("state_code", state_fips))
    elif bbox is not None:
        params.append(("bbox", ",".join(repr(float(v)) for v in bbox)))
    return MEASUREMENTS_URL + "?" + urllib.parse.urlencode(params)


def _build_locations_url(
    *,
    state_fips: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Build the monitoring-locations OGC items URL (GW wells) for the scope."""
    params: list[tuple[str, str]] = [
        ("f", "json"),
        ("site_type_code", "GW"),
        ("limit", str(_PAGE_LIMIT)),
        (
            "properties",
            "monitoring_location_number,monitoring_location_name,"
            "national_aquifer_code,well_constructed_depth",
        ),
    ]
    if state_fips is not None:
        params.append(("state_code", state_fips))
    elif bbox is not None:
        params.append(("bbox", ",".join(repr(float(v)) for v in bbox)))
    return LOCATIONS_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Parsers — OGC GeoJSON FeatureCollections.
# ---------------------------------------------------------------------------


def _site_no_from_mlid(mlid: str) -> str:
    """Derive the bare NWIS site number from a monitoring_location_id.

    The OGC API ids carry an agency prefix: ``USGS-383047098095901`` or
    ``KS014-380041098033401``. We strip the leading ``<agency>-`` so the
    overlay shows the familiar NWIS site number.
    """
    mlid = str(mlid or "").strip()
    return mlid.split("-", 1)[1] if "-" in mlid else mlid


def _parse_measurements_geojson(raw: bytes) -> list[dict[str, Any]]:
    """Parse the latest-field-measurements GeoJSON -> one record per reading.

    Each ``features[]`` is one (well x parameter) latest field measurement. We
    emit one record per feature carrying:

        {monitoring_location_id, site_no, lon, lat, parameter_code,
         water_level, unit, vertical_datum, datetime, approval_status}

    Features with no parseable lon/lat are dropped. Returns ``[]`` when the body
    is empty or carries zero features.
    """
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GwUpstreamError(
            f"USGS OGC measurements response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise GwUpstreamError(
            f"USGS OGC measurements response is not a GeoJSON FeatureCollection: "
            f"type={obj.get('type') if isinstance(obj, dict) else type(obj).__name__!r}"
        )

    records: list[dict[str, Any]] = []
    for feat in obj.get("features") or []:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue

        props = feat.get("properties") or {}
        mlid = str(props.get("monitoring_location_id") or "").strip()

        raw_val = props.get("value")
        water_level: float | None = None
        if raw_val not in (None, ""):
            try:
                fv = float(raw_val)
                if math.isfinite(fv):
                    water_level = fv
            except (TypeError, ValueError):
                water_level = None

        records.append(
            {
                "monitoring_location_id": mlid,
                "site_no": _site_no_from_mlid(mlid),
                "lon": lon,
                "lat": lat,
                "parameter_code": str(props.get("parameter_code") or "").strip()
                or None,
                "water_level": water_level,
                "unit": str(props.get("unit_of_measure") or "").strip() or None,
                "vertical_datum": str(props.get("vertical_datum") or "").strip()
                or None,
                "datetime": str(props.get("time") or "").strip() or None,
                "approval_status": str(props.get("approval_status") or "").strip()
                or None,
            }
        )
    return records


def _parse_locations_geojson(raw: bytes) -> dict[str, dict[str, Any]]:
    """Parse the monitoring-locations GeoJSON -> {mlid: metadata} lookup.

    Metadata per well: ``site_name`` (human name), ``aquifer_code``,
    ``well_depth_ft``. Returns ``{}`` for an empty body — the join then simply
    leaves the enrichment fields blank (best-effort; never an error).
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Enrichment is best-effort; a bad locations body must not abort.
        logger.info(
            "fetch_usgs_groundwater_levels: monitoring-locations body unparseable; "
            "proceeding without well-name enrichment"
        )
        return {}
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    for feat in obj.get("features") or []:
        if not isinstance(feat, dict):
            continue
        mlid = str(feat.get("id") or "").strip()
        if not mlid:
            continue
        props = feat.get("properties") or {}
        depth = props.get("well_constructed_depth")
        try:
            depth_f: float | None = (
                float(depth) if depth not in (None, "") else None
            )
            if depth_f is not None and not math.isfinite(depth_f):
                depth_f = None
        except (TypeError, ValueError):
            depth_f = None
        lookup[mlid] = {
            "site_name": str(props.get("monitoring_location_name") or "").strip(),
            "aquifer_code": str(props.get("national_aquifer_code") or "").strip()
            or None,
            "well_depth_ft": depth_f,
        }
    return lookup


def _join_records(
    measurements: list[dict[str, Any]],
    locations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach well-name / aquifer / depth metadata to each measurement record.

    Best-effort join on ``monitoring_location_id``. A measurement with no
    matching location keeps ``site_name=""`` (and null aquifer/depth) but is
    never dropped — the reading itself is the load-bearing datum.
    """
    joined: list[dict[str, Any]] = []
    for rec in measurements:
        meta = locations.get(rec.get("monitoring_location_id", "")) or {}
        out = dict(rec)
        out["site_name"] = meta.get("site_name") or ""
        out["aquifer_code"] = meta.get("aquifer_code")
        out["well_depth_ft"] = meta.get("well_depth_ft")
        # A human label for the measured quantity (overlay legend / tooltip).
        out["parameter_label"] = _PCODE_LABEL.get(
            str(rec.get("parameter_code") or ""), rec.get("parameter_code") or ""
        )
        joined.append(out)
    return joined


# ---------------------------------------------------------------------------
# Extent + FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the well points.

    Pads a degenerate single-point extent by ~0.05deg so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.05
        east += 0.05
    if south == north:
        south -= 0.05
        north += 0.05
    return (west, south, east, north)


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize well-level records -> FlatGeobuf bytes (Point, EPSG:4326).

    One Point feature per well-level reading carrying ``site_no``, ``site_name``,
    ``parameter_code``, ``parameter_label``, ``water_level``, ``unit``,
    ``vertical_datum``, ``datetime``, ``approval_status``, ``aquifer_code``,
    ``well_depth_ft``.

    Raises ``GwUpstreamError`` if geopandas/shapely are unavailable or the write
    fails. ``records`` must be non-empty (the caller enforces the no-wells
    honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GwUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "site_no": [str(r["site_no"]) for r in records],
        "site_name": [str(r.get("site_name") or "") for r in records],
        "parameter_code": [str(r.get("parameter_code") or "") for r in records],
        "parameter_label": [str(r.get("parameter_label") or "") for r in records],
        "water_level": [r.get("water_level") for r in records],
        "unit": [str(r.get("unit") or "") for r in records],
        "vertical_datum": [str(r.get("vertical_datum") or "") for r in records],
        "datetime": [str(r.get("datetime") or "") for r in records],
        "approval_status": [str(r.get("approval_status") or "") for r in records],
        "aquifer_code": [str(r.get("aquifer_code") or "") for r in records],
        "well_depth_ft": [r.get("well_depth_ft") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_gwlevels_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise GwUpstreamError(
            f"FlatGeobuf write failed for {len(records)} groundwater readings: {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Top-level fetch (passed to read_through). Returns (fgb_bytes, extent_bbox).
# ---------------------------------------------------------------------------


def _fetch_usgs_groundwater_levels_bytes(
    *,
    state_fips: str | None,
    bbox: tuple[float, float, float, float] | None,
    scope_label: str,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: measurements (+ best-effort location join) -> FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``GwNoWellsError`` when the
    measurement collection returns zero readings in scope.
    """
    # 1. PRIMARY: latest field measurements (the observed water-level readings).
    meas_url = _build_measurements_url(state_fips=state_fips, bbox=bbox)
    logger.info("fetch_usgs_groundwater_levels: MEAS GET %s", meas_url)
    meas_raw = _http_get(meas_url)
    measurements = _parse_measurements_geojson(meas_raw)

    # 2. Honest typed error if the primary misses — never an empty success layer.
    if not measurements:
        raise GwNoWellsError(
            f"No USGS groundwater monitoring wells reporting a water-level "
            f"reading (pcodes {','.join(GW_PARAMETER_CODES)}) found for "
            f"{scope_label}. The USGS Water Data OGC API "
            f"(latest-field-measurements) returned zero readings. Either the area "
            f"has no instrumented groundwater wells or none have a level on "
            f"record; try a different area or a state-level query (e.g. a High "
            f"Plains aquifer state like state_code='KS')."
        )

    logger.info(
        "fetch_usgs_groundwater_levels: %d groundwater reading(s) for %s",
        len(measurements),
        scope_label,
    )

    # 3. ENRICHMENT: monitoring-locations join for well name/aquifer/depth.
    #    Best-effort — a failure here must NOT abort a successful primary fetch.
    locations: dict[str, dict[str, Any]] = {}
    try:
        loc_url = _build_locations_url(state_fips=state_fips, bbox=bbox)
        logger.info("fetch_usgs_groundwater_levels: LOC GET %s", loc_url)
        loc_raw = _http_get(loc_url)
        locations = _parse_locations_geojson(loc_raw)
    except GwUpstreamError as exc:
        logger.info(
            "fetch_usgs_groundwater_levels: location enrichment failed (%s); "
            "proceeding with readings only",
            exc,
        )

    records = _join_records(measurements, locations)

    extent = _records_bbox(records)
    assert extent is not None  # records is non-empty here
    return _build_flatgeobuf(records), extent


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_usgs_groundwater_levels(
    state_code: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL, OBSERVED USGS groundwater monitoring wells as a point FlatGeobuf.

    Retrieves active USGS groundwater monitoring wells and their LATEST
    water-level reading — depth-to-water (pcode 72019, ft below land surface)
    and/or groundwater elevation (62611/62610/72150, ft) — from the modernized
    USGS Water Data OGC API (the machine API behind ``waterdata.usgs.gov``, which
    replaced the decommissioned ``nwis/gwlevels`` service). Returns one Point
    feature per well-level reading at the well's coordinates, carrying the latest
    value, its parameter code, unit, vertical datum and reading timestamp. This
    is the canonical OBSERVED groundwater-head source — the field/instrument
    record of how deep the water table is, NOT a model estimate.

    When to use:
        - The user asks for USGS groundwater wells / monitoring wells, "water
          table", "depth to water", "groundwater levels", "observed heads", or
          "well water-level readings" (e.g. "show me the groundwater monitoring
          wells in the High Plains aquifer", "where are the wells near this farm
          and how deep is the water table", "plot the observed groundwater
          levels in central Kansas").
        - You need the actual measured groundwater head at instrumented wells —
          the real instrument record — to map, annotate, or ground-truth.
        - Cross-checking / calibrating a MODELED groundwater head field (a
          MODFLOW run) against the observed monitoring-well network ("observed
          vs modeled heads").

    When NOT to use:
        - MODELED groundwater heads / drawdown / a contaminant plume — those come
          from a MODFLOW run (``run_solver`` with a MODFLOW deck), not this
          observed-well tool. This tool supplies the OBSERVED comparison points.
        - SURFACE-water stream gauges (discharge / gage height at river
          stations) — use ``fetch_usgs_nwis_gauges`` (the observed stream-gauge
          companion). This tool is the GROUNDWATER (subsurface well) network.
        - Soil-moisture / shallow vadose-zone water content — that is a different
          measurement; this is the saturated-zone water table.
        - Global / non-US wells — this tool is US + territories only
          (``supports_global_query=False``).

    Spatial selector (pass EXACTLY ONE):
        state_code: Optional 2-letter USPS state/territory code (e.g. ``"KS"``,
            ``"CA"``, ``"FL"``). PREFER THIS for state-level asks. Mapped
            internally to the FIPS code the OGC API's ``state_code`` filter
            expects.
        bbox: Optional ``(west, south, east, north)`` in EPSG:4326 for an
            area-of-interest query (a farm, aquifer sub-region, or county). The
            OGC API places NO area limit on bbox, so any sub-state extent is
            accepted.
        When both are given, ``state_code`` wins. When neither is given,
        ``GwInputError`` is raised.

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket.
        - ``layer_type="vector"``, ``role="primary"``,
          ``style_preset="usgs_groundwater"``, ``units="ft (water level)"``.
        - Geometry: Point at each well's coordinates, EPSG:4326.
        - ``bbox`` is set to the wells' extent so the client camera auto-zooms
          (the layer renders via the inline-GeoJSON vector path).
        - Properties per reading: ``site_no`` (NWIS site number),
          ``site_name`` (human well name; "" when no metadata match),
          ``parameter_code`` (72019 / 72150 / 62610 / 62611 / 61055),
          ``parameter_label`` (human label for the measured quantity),
          ``water_level`` (the latest value, ft; null if not parseable),
          ``unit`` (e.g. "ft"), ``vertical_datum`` (e.g. "NAVD88", "NGVD29",
          or "Local Assumed Datum" / "" for depth-below-surface readings),
          ``datetime`` (ISO-8601 timestamp of the reading), ``approval_status``,
          ``aquifer_code`` (national aquifer code; "" if unknown),
          ``well_depth_ft`` (constructed well depth, ft; null if unknown).

    Fallback behaviour (data-source fallback norm — primary -> honest typed
    error): the latest-field-measurements collection is the primary (observed
    readings). The monitoring-locations join (for well name / aquifer / depth) is
    a best-effort ENRICHMENT — a failure there leaves names blank but never
    aborts. If the primary returns zero readings in scope, ``GwNoWellsError`` is
    raised — never an empty success-shaped layer.

    Cache: ``ttl_class="dynamic-1h"``, ``source_class="usgs_groundwater_levels"``.
    Cache key is SHA-256 of the resolved selector (``state_code`` or
    bbox-rounded-6dp), so identical-scope calls within the hour reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox or surface a state from a place name BEFORE this call),
          ``fetch_administrative_boundaries`` (state/county framing),
          ``compute_zonal_statistics`` (aggregate readings inside a polygon).
        - Cross-checks: a MODFLOW modeled-head layer (observed-vs-modeled head
          comparison — the MODFLOW groundwater demos).
        - Upstream data source: USGS Water Data OGC API
          (api.waterdata.usgs.gov/ogcapi/v0 — latest-field-measurements +
          monitoring-locations).

    Errors (FR-AS-11 typed-error surface):
        - ``GwInputError``: no selector / bad bbox / bad state code
          (retryable=False).
        - ``GwUpstreamError``: USGS network failure / HTTP 5xx / bad body
          (retryable=True).
        - ``GwNoWellsError``: no groundwater wells with a reading in scope
          (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (USGS federal groundwater network). Claims from
    these well readings should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=False`` (US + territories).
    """
    # 1. Resolve the spatial selector. state_code wins when both are given.
    resolved_state_usps: str | None = None
    resolved_state_fips: str | None = None
    resolved_bbox: tuple[float, float, float, float] | None = None

    if state_code is not None and str(state_code).strip() != "":
        resolved_state_usps, resolved_state_fips = _validate_state_code(state_code)
    elif bbox is not None:
        if not isinstance(bbox, (tuple, list)):
            raise GwInputError(
                f"bbox must be a 4-tuple/list or omitted; got {type(bbox).__name__}"
            )
        bbox_t: tuple[float, float, float, float] = tuple(
            float(v) for v in bbox
        )  # type: ignore[assignment]
        _validate_bbox(bbox_t)
        resolved_bbox = _round_bbox_to_6dp(bbox_t)
    else:
        raise GwInputError(
            "fetch_usgs_groundwater_levels requires a spatial selector: pass "
            "state_code (2-letter USPS, e.g. 'KS') for a state-level query, or "
            "bbox=(west, south, east, north) for an area query."
        )

    scope_label = (
        f"state_code={resolved_state_usps!r}"
        if resolved_state_usps is not None
        else f"bbox={resolved_bbox!r}"
    )

    # 2. Cache-key params (resolved selector).
    params: dict[str, Any] = {
        "state_code": resolved_state_usps,
        "bbox": list(resolved_bbox) if resolved_bbox is not None else None,
    }

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_usgs_groundwater_levels_bytes(
            state_fips=resolved_state_fips,
            bbox=resolved_bbox,
            scope_label=scope_label,
        )
        captured["extent"] = extent
        return fgb

    # 3. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_usgs_groundwater_levels is cacheable; uri must be set by read_through"
    )

    # 4. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty — fall back to the requested bbox (state-level
    # queries have no requested bbox, so leave it None: the inline-GeoJSON
    # vector path still fits the map to the rendered features).
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 5. Build a descriptive layer name + stable id.
    scope_tag = resolved_state_usps if resolved_state_usps is not None else (
        f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
        f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
        if resolved_bbox is not None
        else "?"
    )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    name = f"USGS groundwater levels — {scope_tag}"
    layer_id = f"usgs-groundwater-{seed}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="usgs_groundwater",
        role="primary",
        units="ft (water level)",
        bbox=extent_bbox,
    )
