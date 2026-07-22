"""``fetch_usgs_water_quality`` atomic tool — real USGS / EPA Water Quality
Portal (WQP) sample sites + a chosen water-quality characteristic.

Fetches **real, observed** water-quality monitoring sites and their latest
measured value for one characteristic (e.g. nitrate, lead, arsenic, pH,
dissolved oxygen, specific conductance) within a bounding box, from the EPA /
USGS **Water Quality Portal** machine API (the federation behind
``waterqualitydata.us`` spanning NWIS, STORET/WQX, and the Biodata system).
One Point feature per monitoring location, carrying the latest result value /
units / sample date for the requested characteristic.

This is the **observed contaminant-concentration** counterpart to the MODFLOW
groundwater-transport (GWT) contamination demos: a modeled plume can be
ground-truthed against the real sampled concentrations the WQP serves. Where
``model_groundwater_contamination_scenario`` produces a *modeled* plume raster,
this tool produces the *observed* point samples to overlay and compare against.

**API surface** (Water Quality Portal REST, free, NO API key required):

    STATION service (monitoring-location LOCATIONS, as GeoJSON):
        https://www.waterqualitydata.us/data/Station/search
            ?bBox=west,south,east,north
            &characteristicName=Nitrate
            &mimeType=geojson
        Returns a FeatureCollection of Point sites that have at least one
        sample of the requested characteristic. This is the authoritative
        COORDINATE source (every feature carries a Point geometry).

    RESULT service (the measured VALUES, as CSV — resultPhysChem profile):
        https://www.waterqualitydata.us/data/Result/search
            ?bBox=west,south,east,north
            &characteristicName=Nitrate
            &mimeType=csv&dataProfile=resultPhysChem
        Returns one row per sample result, keyed by
        ``MonitoringLocationIdentifier`` and carrying ``ResultMeasureValue``,
        ``ResultMeasure/MeasureUnitCode``, ``ActivityStartDate``,
        ``ResultSampleFractionText``. We keep the LATEST numeric result per
        site.

**Join model**: fetch Station GeoJSON (locations) + Result CSV (values), JOIN
by ``MonitoringLocationIdentifier``, and keep the single most-recent numeric
result per site. A site with coordinates but no numeric result still survives
as a Point (with null value) so the monitoring network stays visible; the join
is left-on-stations. Prototype against a real Iowa corn-belt bbox confirmed
182 sites / 172 with a latest value for ``Nitrate``.

**Characteristic names**: the WQP ``characteristicName`` is case-sensitive and
uses canonical CharacteristicName vocabulary (e.g. ``"Nitrate"``,
``"Dissolved oxygen (DO)"``, ``"Specific conductance"``, ``"pH"``). We accept
a small set of friendly aliases (``"nitrate"``, ``"dissolved_oxygen"``,
``"specific_conductance"``, ``"do"``, ``"sc"``, ...) and map them to the
canonical spelling; any other string is passed through verbatim so the full
WQP vocabulary stays reachable. An unrecognized canonical name makes the WQP
return HTTP 400, which we surface as a typed ``WqpInputError`` (never a silent
empty).

**Spatial selector**: a ``bbox=(west, south, east, north)`` in EPSG:4326. WQP
imposes no hard area cap, but a very large bbox returns a large payload; we cap
the bbox area at a generous ~100 deg^2 and raise ``WqpInputError`` above that
so the LLM narrows the scope (a contamination demo is always local).

**Fallback norm** (primary -> fallback -> honest typed error): the Station
service is primary (locations). If it returns zero sites, the area genuinely
has no WQP monitoring locations for that characteristic and we raise
``WqpNoSitesError`` (retryable=False). If the Station service returns sites but
the Result service yields no numeric values (rare — sites with only
non-detect / qualitative samples), the sites still render with null values
(the network is real even without a quantified latest reading). We never
return an empty success-shaped layer.

**Output**: a vector ``LayerURI`` (``layer_type="vector"``) whose artifact is a
point FeatureCollection (one point per monitoring location), serialized as
FlatGeobuf. ``style_preset="water_quality"``; ``LayerURI.bbox`` is set to the
sites' extent so the camera auto-zooms.

Tier-1, no auth, ``supports_global_query=False`` (US + territories; WQP is a
US federal/state federation).

FR-AS-11 typed-error surface; FR-TA-2 / FR-AS-3 docstring discipline applies.
"""

from __future__ import annotations

import csv
import hashlib
import io
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

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_usgs_water_quality",
    "estimate_payload_mb",
    "WqpError",
    "WqpInputError",
    "WqpUpstreamError",
    "WqpNoSitesError",
    "_validate_bbox",
    "_resolve_characteristic",
    "_round_bbox_to_6dp",
    "_build_station_url",
    "_build_result_url",
    "_parse_station_geojson",
    "_parse_result_csv",
    "_join_sites",
    "_records_bbox",
    "_build_flatgeobuf",
    "_fetch_water_quality_bytes",
    "STATION_URL",
    "RESULT_URL",
    "CHARACTERISTIC_ALIASES",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_usgs_water_quality")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class WqpError(RuntimeError):
    """Base class for fetch_usgs_water_quality failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "WQP_ERROR"
    retryable: bool = True


class WqpInputError(WqpError):
    """Invalid inputs — bad bbox shape/area, missing bbox, bad characteristic.

    Not retryable: the caller must fix the argument.
    """

    error_code = "WQP_INPUT_ERROR"
    retryable = False


class WqpUpstreamError(WqpError):
    """Water Quality Portal request failed (network error, HTTP 5xx, bad body).

    Retryable — transient WQP outages recover on retry.
    """

    error_code = "WQP_UPSTREAM_ERROR"
    retryable = True


class WqpNoSitesError(WqpError):
    """No WQP monitoring sites found for the characteristic in the bbox.

    Not retryable — the area genuinely has no Water Quality Portal sampling
    locations for that characteristic. Either widen the bbox or pick an area
    with known monitoring (an agricultural watershed for nitrate, etc.).
    """

    error_code = "WQP_NO_SITES"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: WQP Station service (monitoring-location locations) endpoint.
STATION_URL = "https://www.waterqualitydata.us/data/Station/search"

#: WQP Result service (measured sample values) endpoint.
RESULT_URL = "https://www.waterqualitydata.us/data/Result/search"

#: User-Agent (a descriptive UA is courteous to the public WQP service).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout (seconds). The Result service can be slow for dense bboxes.
_HTTP_TIMEOUT = 180.0

#: Generous bbox-area cap (deg^2). WQP has no hard cap, but a contamination
#: demo is always local; above this we ask the caller to narrow scope so the
#: Result payload stays bounded.
_MAX_BBOX_SQ_DEG = 100.0

#: Friendly-alias -> canonical WQP CharacteristicName. The WQP vocabulary is
#: case-sensitive; these map the common LLM/user spellings to the exact name.
#: Any string NOT in this map is passed through verbatim (the full WQP
#: characteristic vocabulary stays reachable).
CHARACTERISTIC_ALIASES: dict[str, str] = {
    "nitrate": "Nitrate",
    "nitrite": "Nitrite",
    "nitrogen": "Nitrogen",
    "lead": "Lead",
    "arsenic": "Arsenic",
    "mercury": "Mercury",
    "cadmium": "Cadmium",
    "chromium": "Chromium",
    "copper": "Copper",
    "zinc": "Zinc",
    "iron": "Iron",
    "chloride": "Chloride",
    "sulfate": "Sulfate",
    "phosphorus": "Phosphorus",
    "atrazine": "Atrazine",
    "ph": "pH",
    "dissolved_oxygen": "Dissolved oxygen (DO)",
    "dissolved oxygen": "Dissolved oxygen (DO)",
    "do": "Dissolved oxygen (DO)",
    "specific_conductance": "Specific conductance",
    "specific conductance": "Specific conductance",
    "conductance": "Specific conductance",
    "conductivity": "Specific conductance",
    "sc": "Specific conductance",
    "temperature": "Temperature, water",
    "water_temperature": "Temperature, water",
    "turbidity": "Turbidity",
    "salinity": "Salinity",
    "total_dissolved_solids": "Total dissolved solids",
    "tds": "Total dissolved solids",
    "e_coli": "Escherichia coli",
    "ecoli": "Escherichia coli",
    "fecal_coliform": "Fecal Coliform",
}


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_usgs_water_quality",
        ttl_class="semi-static-7d",
        source_class="usgs_water_quality",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_usgs_water_quality without it"
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
    """Estimate output FlatGeobuf size in MB.

    Each monitoring site is one Point feature with a handful of small scalar
    properties (~200 bytes serialized). Site density for a sampled
    characteristic in the populated CONUS is roughly a few hundred sites per
    1 deg square in a well-monitored agricultural basin.

    The estimate is conservative; the WQP point layer is always small.
    """
    n_sites = 200  # default guess
    if bbox is not None:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.0, east - west) * max(0.0, north - south)
            # ~200 sites per 1 deg square in a well-monitored basin.
            n_sites = max(1, int(sq_deg * 200))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_sites * 200 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``WqpInputError`` if the bbox is malformed or out of range."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise WqpInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise WqpInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise WqpInputError(f"bbox lon values out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise WqpInputError(f"bbox lat values out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise WqpInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _bbox_area_sq_deg(bbox: tuple[float, float, float, float]) -> float:
    west, south, east, north = bbox
    return max(0.0, east - west) * max(0.0, north - south)


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _resolve_characteristic(characteristic: str) -> str:
    """Map a friendly characteristic alias to the canonical WQP name.

    Lower-cases + trims the input for the alias lookup; an unmapped string is
    passed through VERBATIM (preserving the caller's casing) so the full WQP
    CharacteristicName vocabulary stays reachable. Raises ``WqpInputError`` on
    an empty / non-string characteristic.
    """
    if not isinstance(characteristic, str) or characteristic.strip() == "":
        raise WqpInputError(
            "characteristic is required (e.g. 'nitrate', 'lead', 'arsenic', "
            "'pH', 'dissolved_oxygen', 'specific_conductance')"
        )
    key = characteristic.strip().lower()
    if key in CHARACTERISTIC_ALIASES:
        return CHARACTERISTIC_ALIASES[key]
    # Pass through verbatim (preserve casing) for the full WQP vocabulary.
    return characteristic.strip()


# ---------------------------------------------------------------------------
# HTTP helper.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``WqpUpstreamError`` / ``WqpInputError``.

    A WQP HTTP 400 means the request itself is bad — almost always an
    unrecognized ``characteristicName``. We surface that as ``WqpInputError``
    (not retryable) so the caller fixes the characteristic rather than retrying
    a doomed request.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise WqpInputError(
                f"Water Quality Portal rejected the request (HTTP 400) for "
                f"{url}: {exc.reason}. This usually means an unrecognized "
                f"characteristicName; use a canonical WQP name (e.g. 'Nitrate', "
                f"'Lead', 'Arsenic', 'pH', 'Dissolved oxygen (DO)', "
                f"'Specific conductance')."
            ) from exc
        raise WqpUpstreamError(
            f"Water Quality Portal returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise WqpUpstreamError(
            f"Network error fetching {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise WqpUpstreamError(
            f"Timed out after {timeout}s fetching {url}"
        ) from exc


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def _build_station_url(
    *,
    bbox: tuple[float, float, float, float],
    characteristic: str,
) -> str:
    """Build the WQP Station-service URL (GeoJSON locations) for a bbox."""
    west, south, east, north = bbox
    params: list[tuple[str, str]] = [
        ("bBox", f"{west},{south},{east},{north}"),
        ("characteristicName", characteristic),
        ("mimeType", "geojson"),
    ]
    return STATION_URL + "?" + urllib.parse.urlencode(params)


def _build_result_url(
    *,
    bbox: tuple[float, float, float, float],
    characteristic: str,
) -> str:
    """Build the WQP Result-service URL (CSV sample values) for a bbox."""
    west, south, east, north = bbox
    params: list[tuple[str, str]] = [
        ("bBox", f"{west},{south},{east},{north}"),
        ("characteristicName", characteristic),
        ("mimeType", "csv"),
        ("dataProfile", "resultPhysChem"),
    ]
    return RESULT_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Parsers — Station GeoJSON and Result CSV.
# ---------------------------------------------------------------------------


def _parse_station_geojson(raw: bytes) -> dict[str, dict[str, Any]]:
    """Parse the Station-service GeoJSON body -> {site_id: location record}.

    Each feature is one monitoring location. We key by
    ``MonitoringLocationIdentifier`` and keep the Point coordinate + name +
    type. Features with no parseable Point geometry are dropped. Returns ``{}``
    for an empty body or a FeatureCollection with zero features.
    """
    if not raw:
        return {}
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WqpUpstreamError(
            f"WQP Station response is not valid GeoJSON: {exc}"
        ) from exc

    out: dict[str, dict[str, Any]] = {}
    for feat in obj.get("features") or []:
        props = feat.get("properties") or {}
        site_id = str(props.get("MonitoringLocationIdentifier") or "").strip()
        if not site_id:
            continue
        geom = feat.get("geometry") or {}
        if (geom.get("type") or "") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        out[site_id] = {
            "site_id": site_id,
            "site_name": str(props.get("MonitoringLocationName") or "").strip(),
            "site_type": str(
                props.get("ResolvedMonitoringLocationTypeName")
                or props.get("MonitoringLocationTypeName")
                or ""
            ).strip(),
            "lon": lon,
            "lat": lat,
        }
    return out


def _parse_result_csv(raw: bytes) -> dict[str, dict[str, Any]]:
    """Parse the Result-service CSV body -> {site_id: latest-result record}.

    Keeps the single most-recent NUMERIC result per
    ``MonitoringLocationIdentifier`` (compared by ``ActivityStartDate``,
    ISO ``YYYY-MM-DD`` lexicographic order). Rows with no parseable numeric
    ``ResultMeasureValue`` are skipped (non-detects / qualitative samples).
    Returns ``{}`` for an empty body or a header-only CSV.

    Each record: ``{value, unit, date, fraction, characteristic}``.
    """
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {}
    if "MonitoringLocationIdentifier" not in reader.fieldnames:
        raise WqpUpstreamError(
            f"WQP Result CSV missing MonitoringLocationIdentifier column; "
            f"got header {reader.fieldnames[:8]}"
        )

    latest: dict[str, dict[str, Any]] = {}
    for row in reader:
        site_id = str(row.get("MonitoringLocationIdentifier") or "").strip()
        if not site_id:
            continue
        raw_val = str(row.get("ResultMeasureValue") or "").strip()
        if not raw_val:
            continue
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        date = str(row.get("ActivityStartDate") or "").strip()
        cur = latest.get(site_id)
        if cur is not None and date <= cur["date"]:
            continue
        latest[site_id] = {
            "value": value,
            "unit": str(row.get("ResultMeasure/MeasureUnitCode") or "").strip(),
            "date": date,
            "fraction": str(row.get("ResultSampleFractionText") or "").strip(),
            "characteristic": str(row.get("CharacteristicName") or "").strip(),
        }
    return latest


def _join_sites(
    stations: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Left-join station locations with their latest result.

    One record per station (the authoritative coordinate source). The latest
    result (if any) decorates the record with ``value``/``unit``/``result_date``
    /``fraction``/``characteristic``; a station with no numeric result keeps
    null value fields but still renders (the monitoring network is real even
    without a quantified latest reading). Returns ``[]`` if ``stations`` empty.
    """
    out: list[dict[str, Any]] = []
    for site_id, loc in stations.items():
        res = results.get(site_id)
        rec = {
            "site_id": site_id,
            "site_name": loc.get("site_name", ""),
            "site_type": loc.get("site_type", ""),
            "lon": loc["lon"],
            "lat": loc["lat"],
            "characteristic": (res or {}).get("characteristic") or "",
            "value": (res or {}).get("value"),
            "unit": (res or {}).get("unit") or "",
            "result_date": (res or {}).get("date") or "",
            "fraction": (res or {}).get("fraction") or "",
        }
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Records-extent helper.
# ---------------------------------------------------------------------------


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (west, south, east, north) extent of the site points.

    Pads a degenerate single-point extent by ~0.05 deg so the camera does not
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


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize site records -> FlatGeobuf bytes (Point geometry, EPSG:4326).

    One Point feature per monitoring location carrying ``site_id``,
    ``site_name``, ``site_type``, ``characteristic``, ``value``, ``unit``,
    ``result_date``, ``fraction``.

    Raises ``WqpUpstreamError`` if geopandas/shapely are unavailable or the
    write fails. ``records`` must be non-empty (the caller enforces the
    no-sites honest-error gate before calling this).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise WqpUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "site_id": [str(r["site_id"]) for r in records],
        "site_name": [str(r.get("site_name") or "") for r in records],
        "site_type": [str(r.get("site_type") or "") for r in records],
        "characteristic": [str(r.get("characteristic") or "") for r in records],
        "value": [r.get("value") for r in records],
        "unit": [str(r.get("unit") or "") for r in records],
        "result_date": [str(r.get("result_date") or "") for r in records],
        "fraction": [str(r.get("fraction") or "") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_wqp_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:
        raise WqpUpstreamError(
            f"FlatGeobuf write failed for {len(records)} water-quality sites: {exc}"
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


def _fetch_water_quality_bytes(
    *,
    bbox: tuple[float, float, float, float],
    characteristic: str,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: Station (locations) + Result (values) -> joined FGB.

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``WqpNoSitesError`` when the
    Station service returns zero monitoring locations for the characteristic in
    the bbox.
    """
    # 1. Station service — the authoritative monitoring-location locations.
    station_url = _build_station_url(bbox=bbox, characteristic=characteristic)
    logger.info("fetch_usgs_water_quality: STATION GET %s", station_url)
    station_raw = _http_get(station_url)
    stations = _parse_station_geojson(station_raw)

    # 2. Honest typed error if no sites — never an empty success layer.
    if not stations:
        raise WqpNoSitesError(
            f"No Water Quality Portal monitoring sites found for "
            f"characteristic={characteristic!r} in bbox={bbox!r}. The WQP "
            f"Station service returned zero locations. Either the area has no "
            f"sampled sites for that characteristic, or the bbox is over water / "
            f"outside the US monitoring network; try a different area, a "
            f"different characteristic, or a wider bbox over a monitored "
            f"watershed."
        )

    # 3. Result service — the measured sample values (latest per site).
    result_url = _build_result_url(bbox=bbox, characteristic=characteristic)
    logger.info("fetch_usgs_water_quality: RESULT GET %s", result_url)
    result_raw = _http_get(result_url)
    results = _parse_result_csv(result_raw)

    records = _join_sites(stations, results)
    n_with_value = sum(1 for r in records if r.get("value") is not None)
    logger.info(
        "fetch_usgs_water_quality: %d site(s); %d carry a latest %s value",
        len(records),
        n_with_value,
        characteristic,
    )

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
def fetch_usgs_water_quality(
    bbox: tuple[float, float, float, float] | None = None,
    characteristic: str = "Nitrate",
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch REAL, OBSERVED water-quality sample sites as a point FlatGeobuf.

    Retrieves USGS / EPA Water Quality Portal (WQP) monitoring locations and
    their latest measured value for ONE characteristic — nitrate, lead,
    arsenic, pH, dissolved oxygen, specific conductance, etc. — from the public
    machine API behind ``waterqualitydata.us`` (the federation spanning NWIS,
    STORET/WQX, and Biodata). Returns one Point feature per monitoring location
    at the site's coordinates, carrying the most-recent numeric result. This is
    the canonical **observed** contaminant-concentration source.

    When to use:
        - The user asks for water-quality monitoring sites, sampled
          contaminant concentrations, "nitrate levels", "lead in the water",
          "arsenic samples", pH / dissolved oxygen / specific conductance at
          monitoring stations (e.g. "show nitrate sample sites in this
          watershed", "where has arsenic been measured near here").
        - You need the OBSERVED, measured concentration at instrumented sample
          sites — the real lab/field record, NOT a model estimate.
        - GROUND-TRUTHING a MODELED contaminant plume: overlay these observed
          point samples against the modeled plume from
          ``model_groundwater_contamination_scenario`` /
          ``model_contamination_affected_fields`` (the MODFLOW-GWT demos) to
          compare modeled-vs-observed concentration.

    When NOT to use:
        - MODELED groundwater contaminant transport / a plume raster — that is
          ``model_groundwater_contamination_scenario`` (FloPy MODFLOW-GWT).
          This tool is the OBSERVED point-sample companion.
        - Stream DISCHARGE / gage height (flow, not chemistry) — use
          ``fetch_usgs_nwis_gauges`` (observed) or ``fetch_noaa_nwm_streamflow``
          (modeled).
        - Drinking-water-system violations / regulated facilities — use
          ``fetch_epa_frs_facilities`` (EPA Facility Registry).
        - Global / non-US water quality — this tool is US + territories only
          (``supports_global_query=False``).

    Args:
        bbox: REQUIRED ``(west, south, east, north)`` in EPSG:4326 for the
            area of interest (a watershed, county, or sub-basin). The bbox area
            is capped at ~100 deg^2; above that ``WqpInputError`` is raised so
            you narrow the scope (a contamination question is always local).
        characteristic: the water-quality characteristic to fetch. Friendly
            aliases are accepted and mapped to the canonical WQP name —
            ``"nitrate"`` -> ``"Nitrate"``, ``"lead"`` -> ``"Lead"``,
            ``"arsenic"`` -> ``"Arsenic"``, ``"ph"`` -> ``"pH"``,
            ``"dissolved_oxygen"`` / ``"do"`` -> ``"Dissolved oxygen (DO)"``,
            ``"specific_conductance"`` / ``"sc"`` -> ``"Specific conductance"``,
            plus common metals/ions/nutrients. Any other string is passed
            through verbatim, so the full WQP CharacteristicName vocabulary is
            reachable. An unrecognized canonical name makes WQP return HTTP 400,
            surfaced as ``WqpInputError``. Defaults to ``"Nitrate"`` (the
            agricultural-contamination demo characteristic).

    Returns:
        ``LayerURI`` pointing at a FlatGeobuf in the cache bucket:
        ``s3://<cache-bucket>/cache/semi-static-7d/usgs_water_quality/<key>.fgb``
        - ``layer_type="vector"``, ``role="primary"``,
          ``style_preset="water_quality"``.
        - Geometry: Point at each monitoring location, EPSG:4326.
        - ``bbox`` is set to the sites' extent so the client camera auto-zooms
          (the layer renders via the inline-GeoJSON vector path).
        - Properties per site: ``site_id`` (MonitoringLocationIdentifier),
          ``site_name``, ``site_type``, ``characteristic`` (canonical WQP name),
          ``value`` (latest numeric result; null if no numeric sample),
          ``unit`` (result units, e.g. ``"mg/l as N"``), ``result_date``
          (ISO sample date of the latest result; "" if none), ``fraction``
          (sample fraction, e.g. ``"Dissolved"``/``"Total"``).

    Fallback behaviour (data-source fallback norm — primary -> fallback ->
    honest typed error): the Station service (locations) is primary. If it
    returns zero sites, ``WqpNoSitesError`` is raised (the area genuinely has no
    WQP locations for that characteristic). If the Station service returns sites
    but the Result service yields no numeric values, the sites still render with
    null values (the monitoring network is real). Never an empty success layer.

    Cache: ``ttl_class="semi-static-7d"``, ``source_class="usgs_water_quality"``.
    Cache key is SHA-256 of the resolved characteristic + bbox-rounded-6dp, so
    identical-scope calls within the day reuse the FGB.

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``publish_layer`` (map overlay), ``geocode_location``
          (derive a bbox from a place name BEFORE this call),
          ``fetch_field_boundaries`` / ``fetch_administrative_boundaries``
          (watershed/field framing).
        - Cross-checks: ``model_groundwater_contamination_scenario`` and
          ``model_contamination_affected_fields`` (MODFLOW-GWT modeled plume —
          observed-vs-modeled concentration comparison),
          ``fetch_epa_frs_facilities`` (potential contamination sources).
        - Upstream data source: USGS / EPA Water Quality Portal
          (waterqualitydata.us/data/Station + /data/Result).

    Errors (FR-AS-11 typed-error surface):
        - ``WqpInputError``: missing/bad bbox, bbox too large, bad/empty
          characteristic, or a WQP HTTP 400 (unrecognized characteristicName)
          (retryable=False).
        - ``WqpUpstreamError``: WQP network failure / HTTP 5xx / bad body
          (retryable=True).
        - ``WqpNoSitesError``: no monitoring sites for the characteristic in
          the bbox (retryable=False).

    Source-tier: FR-HEP-2 Tier 1 (USGS/EPA federal monitoring federation).
    Claims from WQP samples should be marked ``source_authority_tier=1``.

    Tier-1 free. No API key. ``supports_global_query=False`` (US + territories).
    """
    # 1. Resolve + validate the spatial selector.
    if bbox is None:
        raise WqpInputError(
            "fetch_usgs_water_quality requires bbox=(west, south, east, north) "
            "in EPSG:4326 for the area of interest (a watershed/sub-basin)."
        )
    if not isinstance(bbox, (tuple, list)):
        raise WqpInputError(
            f"bbox must be a 4-tuple/list; got {type(bbox).__name__}"
        )
    bbox_t: tuple[float, float, float, float] = tuple(
        float(v) for v in bbox
    )  # type: ignore[assignment]
    _validate_bbox(bbox_t)
    area = _bbox_area_sq_deg(bbox_t)
    if area > _MAX_BBOX_SQ_DEG:
        raise WqpInputError(
            f"bbox area {area:.1f} deg^2 exceeds the {_MAX_BBOX_SQ_DEG:.0f} deg^2 "
            f"cap; a water-quality contamination question is always local. "
            f"Re-issue with a smaller bbox over the watershed of interest."
        )
    resolved_bbox = _round_bbox_to_6dp(bbox_t)

    # 2. Resolve the characteristic (alias -> canonical WQP name).
    resolved_char = _resolve_characteristic(characteristic)

    # 3. Cache-key params.
    params: dict[str, Any] = {
        "bbox": list(resolved_bbox),
        "characteristic": resolved_char,
    }

    # The fetch_fn returns (bytes, extent); read_through caches the bytes. We
    # need the extent for LayerURI.bbox, so capture it via a closure side-channel.
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_water_quality_bytes(
            bbox=resolved_bbox, characteristic=resolved_char
        )
        captured["extent"] = extent
        return fgb

    # 4. Read-through cache.
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_usgs_water_quality is cacheable; uri must be set by read_through"
    )

    # 5. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    # ``captured`` is empty — fall back to the requested bbox.
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = resolved_bbox

    # 6. Build a descriptive layer name + stable id.
    scope_tag = (
        f"{resolved_bbox[0]:.2f},{resolved_bbox[1]:.2f}->"
        f"{resolved_bbox[2]:.2f},{resolved_bbox[3]:.2f}"
    )
    seed = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]

    name = f"Water quality ({resolved_char}) sample sites — {scope_tag}"
    layer_id = f"wqp-{seed}"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="water_quality",
        role="primary",
        units=resolved_char,
        bbox=extent_bbox,
    )
