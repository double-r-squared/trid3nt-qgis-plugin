"""``fetch_overpass_pois`` atomic tool — generic OpenStreetMap POI / tagged-feature points via Overpass API.

The flexible exposure-layer complement to the FIXED ``fetch_hifld_critical_infrastructure``
tool. Where HIFLD exposes a curated, fixed set of US critical-infrastructure
categories, this tool fetches ANY OpenStreetMap ``key=value`` tag as Point
features inside a WGS84 bounding box — global coverage, the full OSM tag
vocabulary, and consistent point geometry for exposure / context overlays.

A caller-supplied ``tag`` (``"amenity=hospital"``), or the friendlier
``amenity`` / ``category`` parameter, maps to an Overpass QL element filter
queried across ``node``, ``way``, and ``relation`` element types. ``way`` and
``relation`` matches are reduced to their **centroid** point (Overpass ``out
center``) so every feature is a single Point — the same uniform shape the HIFLD
tool emits, so the inline-GeoJSON vector path (job-0175) renders them
identically. The full OSM tag dictionary for each feature is preserved inline as
a ``tags_json`` attribute.

**API surface** (OpenStreetMap Overpass, free, NO API key required):

    [out:json][timeout:60];
    (node["amenity"="hospital"](s,w,n,e);
     way["amenity"="hospital"](s,w,n,e);
     relation["amenity"="hospital"](s,w,n,e););
    out center;

Overpass returns the bbox corners as ``(south, west, north, east)`` (lat first,
then lon) — the OPPOSITE corner-pair ordering from the caller's
``(min_lon, min_lat, max_lon, max_lat)``.

**Data-source fallback norm** (primary -> fallback -> honest typed error): the
public ``overpass-api.de`` endpoint is rate-limited and prone to HTTP 429 / 504
under load, so the tool walks a list of independent public Overpass mirrors
(``overpass-api.de`` -> ``overpass.kumi.systems`` -> ``overpass.private.coffee``);
if EVERY mirror fails it raises a retryable ``OverpassUpstreamError``. When the
query succeeds but matches ZERO features in scope it raises a non-retryable
``OverpassNoFeaturesError`` — never an empty success-shaped layer.

FR-TA-2 atomic tool, returns ``LayerURI`` (vector, role="primary", units=None).
FR-CE-8 / FR-DC-3: identical ``(bbox, key, value, element_types)`` calls reuse
the cached FlatGeobuf within the 30-day TTL window.

Pattern reference: ``fetch_roads_osm.py`` (Overpass query + parse + clip),
``fetch_hifld_critical_infrastructure.py`` (point exposure-layer shape +
payload estimator), ``fetch_usgs_nwis_gauges.py`` (point FlatGeobuf builder +
extent-bbox camera fit).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_overpass_pois",
    "estimate_payload_mb",
    "OverpassPoiError",
    "OverpassInputError",
    "OverpassUpstreamError",
    "OverpassNoFeaturesError",
    "_validate_bbox",
    "_resolve_tag",
    "_build_overpass_ql",
    "_extract_point_records",
    "_records_bbox",
    "_records_to_flatgeobuf_bytes",
    "OVERPASS_ENDPOINTS",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_overpass_pois")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class OverpassPoiError(RuntimeError):
    """Base class for fetch_overpass_pois failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "OVERPASS_POIS_ERROR"
    retryable: bool = True


class OverpassInputError(OverpassPoiError):
    """Caller passed an invalid argument (bad bbox, missing/garbled tag).

    Not retryable: the caller must fix the argument.
    """

    error_code = "OVERPASS_POIS_INPUT_INVALID"
    retryable = False


class OverpassUpstreamError(OverpassPoiError):
    """Every Overpass mirror failed (network / HTTP 5xx / 429 / parse).

    Retryable — transient Overpass load recovers on retry.
    """

    error_code = "OVERPASS_POIS_UPSTREAM_ERROR"
    retryable = True


class OverpassNoFeaturesError(OverpassPoiError):
    """The query succeeded but matched ZERO features in scope.

    Not retryable — the area genuinely has no OSM features carrying that tag.
    Either widen the bbox or pick a different tag.
    """

    error_code = "OVERPASS_POIS_NO_FEATURES"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Public Overpass interpreter endpoints, tried in order (data-source fallback
#: norm). All are keyless public mirrors of the same OSM planet database.
OVERPASS_ENDPOINTS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

#: HTTP timeout for each Overpass POST — Overpass can be slow; 120 s.
_HTTP_TIMEOUT = 120.0

#: Overpass internal-query timeout (the ``[timeout:N]`` directive).
_OVERPASS_QL_TIMEOUT = 60

#: Polite delay before invoking Overpass, to respect public-endpoint rate
#: limits. Applied on the miss path only; cache hits never call Overpass.
_POLITE_DELAY_S = 1.0

#: Polite User-Agent (matches sibling OSM tools).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: OSM element types queried. ``way`` / ``relation`` are reduced to a centroid
#: point via Overpass ``out center`` so every feature is a single Point.
_ELEMENT_TYPES: tuple[str, ...] = ("node", "way", "relation")

#: Friendly aliases mapping a bare value to its OSM key. When the caller passes
#: ``amenity="hospital"`` we already know the key; when they pass only
#: ``category="hospital"`` (a value) we look the key up here. Covers the common
#: exposure / context POI classes; anything not listed must be passed as an
#: explicit ``key=value`` ``tag``.
_VALUE_KEY_ALIASES: dict[str, str] = {
    "hospital": "amenity",
    "clinic": "amenity",
    "doctors": "amenity",
    "pharmacy": "amenity",
    "school": "amenity",
    "college": "amenity",
    "university": "amenity",
    "kindergarten": "amenity",
    "fire_station": "amenity",
    "police": "amenity",
    "townhall": "amenity",
    "place_of_worship": "amenity",
    "shelter": "amenity",
    "community_centre": "amenity",
    "fuel": "amenity",
    "bank": "amenity",
    "restaurant": "amenity",
    "supermarket": "shop",
    "convenience": "shop",
}

#: A small allow-list of OSM keys that take free-form values (used to validate
#: an explicit ``key=value`` tag has a plausible key). NOT exhaustive — OSM has
#: thousands of keys; this just rejects obvious garbage (e.g. a key with a
#: space). A key outside this set is still allowed as long as it is a clean
#: token, so the tool stays a GENERIC fetcher.
_COMMON_KEYS: frozenset[str] = frozenset(
    {
        "amenity", "shop", "emergency", "healthcare", "building", "office",
        "leisure", "tourism", "man_made", "landuse", "natural", "power",
        "public_transport", "railway", "aeroway", "military", "historic",
        "craft", "club", "government", "social_facility",
    }
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    """Construct AtomicToolMetadata defensively against schema flag variants."""
    common: dict[str, Any] = dict(
        name="fetch_overpass_pois",
        ttl_class="static-30d",
        source_class="overpass_pois",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        logger.debug(
            "AtomicToolMetadata does not support supports_global_query; "
            "registering fetch_overpass_pois without it"
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

    Each POI is one Point feature carrying a handful of scalars plus an inline
    ``tags_json`` blob (~300 bytes serialized on average). POI density varies
    wildly by tag, but a conservative ~80 features per 1 deg^2 of populated
    CONUS keeps the estimate from under-warning on dense tags (e.g. ``shop=*``
    over a metro). The estimate is intentionally generous; most POI layers are
    tiny (a few KB).
    """
    n_features = 50  # default guess
    if bbox is not None:
        try:
            min_lon, min_lat, max_lon, max_lat = bbox
            sq_deg = max(0.0, max_lon - min_lon) * max(0.0, max_lat - min_lat)
            n_features = max(1, int(sq_deg * 80))
        except (TypeError, ValueError):
            pass
    return max(0.001, n_features * 300 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation + tag resolution helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Validate + return the bbox as a float tuple. Raise ``OverpassInputError``.

    Expects ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise OverpassInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise OverpassInputError(f"bbox values must be numeric: {bbox!r}") from exc
    vals = (min_lon, min_lat, max_lon, max_lat)
    if not all(math.isfinite(v) for v in vals):
        raise OverpassInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise OverpassInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise OverpassInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise OverpassInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    return vals


def _is_clean_token(s: str) -> bool:
    """True if ``s`` is a plausible OSM key/value token (no spaces / quotes / QL metachars)."""
    if not s:
        return False
    # OSM keys/values are typically [A-Za-z0-9_:-] plus a few others; reject
    # anything that could break out of the quoted Overpass QL string.
    for ch in s:
        if ch.isspace() or ch in '"\\[](){};':
            return False
    return True


def _resolve_tag(
    tag: str | None,
    amenity: str | None,
    category: str | None,
    value: str | None,
) -> tuple[str, str]:
    """Resolve the caller's tag inputs to a single ``(key, value)`` pair.

    Accepts several friendly forms, in priority order:

    1. ``tag="key=value"`` — an explicit OSM tag (e.g. ``"emergency=fire_hydrant"``).
       Also accepts a bare value (``tag="hospital"``) resolved via the alias map.
    2. ``amenity="hospital"`` — the ``amenity`` key shortcut (value-only).
    3. ``category="hospital"`` — alias-resolved value, or ``"key=value"``.
    4. ``value="hospital"`` paired with no key — alias-resolved.

    Raises ``OverpassInputError`` if no usable tag is supplied or the value
    cannot be mapped to a key.
    """
    candidate: str | None = None
    forced_key: str | None = None

    if isinstance(amenity, str) and amenity.strip():
        forced_key, candidate = "amenity", amenity.strip()
    elif isinstance(tag, str) and tag.strip():
        candidate = tag.strip()
    elif isinstance(category, str) and category.strip():
        candidate = category.strip()
    elif isinstance(value, str) and value.strip():
        candidate = value.strip()

    if candidate is None:
        raise OverpassInputError(
            "no POI tag supplied; pass one of: tag='key=value' (e.g. "
            "'amenity=hospital' or 'emergency=fire_hydrant'), amenity='hospital', "
            "or category='school'."
        )

    if forced_key is not None:
        key, val = forced_key, candidate
    elif "=" in candidate:
        key, _, val = candidate.partition("=")
        key, val = key.strip(), val.strip()
    else:
        # A bare value — resolve its key via the alias map.
        val = candidate
        key = _VALUE_KEY_ALIASES.get(val.lower(), "")
        if not key:
            raise OverpassInputError(
                f"could not infer an OSM key for value={candidate!r}; pass an "
                f"explicit tag='key=value' (e.g. 'amenity={candidate}' or "
                f"'shop={candidate}'). Known bare values: "
                f"{sorted(_VALUE_KEY_ALIASES)}"
            )

    if not _is_clean_token(key) or not _is_clean_token(val):
        raise OverpassInputError(
            f"tag key/value must be clean OSM tokens (no spaces / quotes / "
            f"brackets); got key={key!r} value={val!r}"
        )
    return key, val


# ---------------------------------------------------------------------------
# Overpass QL builder + HTTP request with mirror fallback.
# ---------------------------------------------------------------------------


def _build_overpass_ql(
    bbox: tuple[float, float, float, float],
    key: str,
    value: str,
) -> str:
    """Construct the Overpass QL payload querying node/way/relation for key=value.

    Overpass expects the bbox corners as ``(south, west, north, east)`` —
    OPPOSITE the caller's ``(min_lon, min_lat, max_lon, max_lat)``. ``out
    center`` yields a centroid for each way/relation so every feature is a Point.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    parts = "".join(
        f'{et}["{key}"="{value}"]({s},{w},{n},{e});' for et in _ELEMENT_TYPES
    )
    return (
        f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];"
        f"({parts});"
        f"out center;"
    )


def _post_overpass(ql: str) -> dict[str, Any]:
    """POST ``ql`` to each Overpass mirror in turn; return the first JSON success.

    Data-source fallback norm: walk ``OVERPASS_ENDPOINTS`` in order. A non-retryable
    4xx (other than 429) on an endpoint short-circuits the fallback (the QL is
    bad — trying another mirror will not help). A 5xx / 429 / network error
    advances to the next mirror. If EVERY mirror fails, raise
    ``OverpassUpstreamError`` (retryable).
    """
    last_exc: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            with httpx.Client(
                timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT}
            ) as client:
                # Polite throttle BEFORE each request fires.
                time.sleep(_POLITE_DELAY_S)
                logger.info(
                    "fetch_overpass_pois: POST %s ql_bytes=%d", endpoint, len(ql)
                )
                resp = client.post(endpoint, data={"data": ql})
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError as exc:
                    raise OverpassUpstreamError(
                        f"Overpass mirror {endpoint} returned non-JSON: {exc}"
                    ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            # A non-429 4xx means the QL itself is malformed — trying another
            # mirror will not help, so fail fast as a non-retryable input error.
            if status is not None and 400 <= status < 500 and status != 429:
                raise OverpassInputError(
                    f"Overpass rejected the query (HTTP {status}); the tag or "
                    f"bbox is likely malformed: {exc}"
                ) from exc
            logger.warning(
                "fetch_overpass_pois: mirror %s failed (HTTP %s); trying next",
                endpoint,
                status,
            )
            last_exc = exc
        except httpx.HTTPError as exc:
            logger.warning(
                "fetch_overpass_pois: mirror %s network error (%s); trying next",
                endpoint,
                exc,
            )
            last_exc = exc

    raise OverpassUpstreamError(
        f"all {len(OVERPASS_ENDPOINTS)} Overpass mirrors failed for the query; "
        f"last error: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Overpass -> point record extraction.
# ---------------------------------------------------------------------------


def _extract_point_records(
    payload: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Project the Overpass JSON ``elements`` to one Point record per feature.

    A ``node`` uses its own ``lat``/``lon``; a ``way``/``relation`` uses the
    ``center`` centroid from ``out center``. Features without a parseable
    coordinate are dropped. The centroid of a way/relation can fall just outside
    the requested bbox (the element only had to TOUCH the bbox); such records are
    dropped so every returned point is strictly inside the AOI.

    Output fields: ``osm_id`` (int), ``osm_type`` (node/way/relation),
    ``name`` (str | None), ``key``, ``value``, ``tags_json`` (the full OSM tag
    dict serialized), plus ``lon`` / ``lat``.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    elements = payload.get("elements")
    if elements is None:
        elements = []
    if not isinstance(elements, list):
        raise OverpassUpstreamError(
            f"Overpass 'elements' is not a list: {type(elements).__name__}"
        )

    records: list[dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        if etype == "node":
            lat_v, lon_v = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            lat_v, lon_v = center.get("lat"), center.get("lon")
        if lat_v is None or lon_v is None:
            continue
        try:
            lat = float(lat_v)
            lon = float(lon_v)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        # Keep only points strictly inside the requested AOI.
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue

        tags = el.get("tags")
        if not isinstance(tags, dict):
            tags = {}
        # Derive the matched key/value from the tags when available so the
        # record reflects the ACTUAL tag carried (robust to mixed payloads).
        records.append(
            {
                "osm_id": el.get("id"),
                "osm_type": etype,
                "name": tags.get("name"),
                "lon": lon,
                "lat": lat,
                "tags_json": json.dumps(tags, separators=(",", ":"), sort_keys=True),
            }
        )
    return records


def _records_bbox(
    records: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Compute the (min_lon, min_lat, max_lon, max_lat) extent of the points.

    Pads a degenerate single-point extent by ~0.02 deg so the camera does not
    zoom to an infinite level. Returns ``None`` for an empty list.
    """
    if not records:
        return None
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    if min_lon == max_lon:
        min_lon -= 0.02
        max_lon += 0.02
    if min_lat == max_lat:
        min_lat -= 0.02
        max_lat += 0.02
    return (min_lon, min_lat, max_lon, max_lat)


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _records_to_flatgeobuf_bytes(
    records: list[dict[str, Any]],
    key: str,
    value: str,
) -> bytes:
    """Serialize ``records`` to FlatGeobuf bytes (Point geometry, EPSG:4326).

    One Point feature per POI carrying ``osm_id``, ``osm_type``, ``name``,
    ``key``, ``value``, ``tags_json``. ``records`` must be non-empty (the caller
    enforces the no-features honest-error gate before calling this).

    Raises ``OverpassUpstreamError`` if geopandas / shapely are unavailable or
    the write fails.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OverpassUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    data = {
        "osm_id": [r.get("osm_id") for r in records],
        "osm_type": [str(r.get("osm_type") or "") for r in records],
        "name": [r.get("name") for r in records],
        "key": [key for _ in records],
        "value": [value for _ in records],
        "tags_json": [str(r.get("tags_json") or "{}") for r in records],
    }
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_overpass_pois_"
        ) as f:
            tmp_fgb = f.name
        gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_fgb, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 — translate to typed error
        raise OverpassUpstreamError(
            f"FlatGeobuf write failed for {len(records)} POI(s): {exc}"
        ) from exc
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Cache miss-path fetcher. Returns (fgb_bytes, extent_bbox).
# ---------------------------------------------------------------------------


def _fetch_overpass_pois_bytes(
    bbox: tuple[float, float, float, float],
    key: str,
    value: str,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """End-to-end fetch: build QL -> POST (mirror fallback) -> parse -> FGB bytes.

    Returns ``(fgb_bytes, extent_bbox)``. Raises ``OverpassNoFeaturesError`` when
    the query matched zero features in scope.
    """
    ql = _build_overpass_ql(bbox, key, value)
    payload = _post_overpass(ql)
    records = _extract_point_records(payload, bbox)
    logger.info(
        "fetch_overpass_pois: %s=%s in bbox=%s -> %d POI point(s)",
        key,
        value,
        bbox,
        len(records),
    )
    if not records:
        raise OverpassNoFeaturesError(
            f"No OpenStreetMap features carrying {key}={value!r} were found in "
            f"bbox={bbox!r}. The area genuinely has no such tagged features in "
            f"OSM, or the tag is misspelled. Widen the area or try a different "
            f"tag (e.g. 'amenity=clinic' instead of 'amenity=hospital')."
        )
    extent = _records_bbox(records)
    assert extent is not None  # records is non-empty here
    return _records_to_flatgeobuf_bytes(records, key, value), extent


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
def fetch_overpass_pois(
    bbox: tuple[float, float, float, float],
    tag: str | None = None,
    amenity: str | None = None,
    category: str | None = None,
    value: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch OpenStreetMap POIs / tagged features as a point FlatGeobuf overlay.

    **What it does:** Queries the OpenStreetMap Overpass API for any
    ``key=value``-tagged feature (``node``, ``way``, or ``relation``) inside the
    requested bbox, reduces every match to a single Point (way/relation matches
    use their centroid), and serializes the points to FlatGeobuf cached for 30
    days. Each feature carries ``osm_id``, ``osm_type``, ``name``, the matched
    ``key`` / ``value``, and the full OSM tag dictionary as ``tags_json``. The
    resulting vector renders inline on the map automatically — do NOT call
    ``publish_layer`` on it. This is the FLEXIBLE, global exposure-layer
    complement to the FIXED, US-only ``fetch_hifld_critical_infrastructure``
    tool: same uniform point shape, but the full OSM tag vocabulary worldwide.

    **When to use:**
    - User asks to show / overlay a class of facilities or features by name:
      "show the hospitals", "where are the fire stations", "schools in this
      area", "pharmacies near the flood zone", "gas stations", "places of
      worship", "supermarkets".
    - You need an EXPOSURE layer for a hazard footprint (which hospitals /
      schools / shelters fall inside an inundation or smoke plume?) anywhere in
      the world, including outside the US where HIFLD has no coverage.
    - Any ad-hoc OSM tag the user names that is not a curated HIFLD category
      (``emergency=fire_hydrant``, ``man_made=water_tower``, ``power=substation``,
      ``shop=supermarket``, ``tourism=hotel``, ...).

    **When NOT to use:**
    - For US critical-infrastructure categories where an authoritative federal
      dataset is preferable, ``fetch_hifld_critical_infrastructure`` is the
      curated source (schools, hospitals, fire/police, power plants) — use that
      when the user wants the official US layer; use THIS tool for global
      coverage, niche tags, or when HIFLD lacks the category.
    - For ROAD / street centrelines use ``fetch_roads_osm`` (LineStrings).
    - For administrative / parcel boundaries use
      ``fetch_administrative_boundaries``.
    - For building FOOTPRINT polygons use the building-footprint fetcher (this
      tool returns building CENTROIDS only when ``building=*`` is queried).

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(min_lon, min_lat,
      max_lon, max_lat)`` in EPSG:4326. Required. Example San Francisco core:
      ``(-122.45, 37.74, -122.38, 37.80)``. A bbox is REQUIRED — there is no
      global sweep (``supports_global_query=False``); a global POI query would
      time out and return an unbounded payload.
    - Tag selector (pass ONE; checked in this priority order):
        - ``amenity`` (str): an ``amenity`` key value, e.g. ``"hospital"``,
          ``"school"``, ``"fire_station"`` — the most common shortcut.
        - ``tag`` (str): an explicit ``"key=value"`` OSM tag, e.g.
          ``"emergency=fire_hydrant"``, ``"shop=supermarket"``,
          ``"power=substation"``. A bare value (``"hospital"``) is also accepted
          and its key inferred for common classes.
        - ``category`` (str): alias for ``tag`` — a ``"key=value"`` or a known
          bare value.
        - ``value`` (str): a bare value resolved to its key for common classes.

    **Returns:** A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
    (``.../cache/static-30d/overpass_pois/<key>.fgb``).
    ``layer_type="vector"``, ``role="primary"``,
    ``style_preset="overpass_pois"``, ``units=None``. ``bbox`` is set to the
    features' extent so the camera auto-zooms. Properties per feature:
    ``osm_id`` (int), ``osm_type`` (str), ``name`` (str | None), ``key`` (str),
    ``value`` (str), ``tags_json`` (str — full OSM tag dict).

    **Fallback behaviour (data-source fallback norm — primary -> fallback ->
    honest typed error):** the request is tried across several independent
    public Overpass mirrors in turn; if every mirror fails a retryable
    ``OverpassUpstreamError`` is raised. When the query succeeds but matches
    ZERO features, a non-retryable ``OverpassNoFeaturesError`` is raised — never
    an empty success-shaped layer.

    **Cross-tool dependencies (FR-TA-3):**
    - Composes WITH: ``geocode_location`` (derive a bbox from a place name
      BEFORE this call), ``compute_zonal_statistics`` /
      intersection tools (count POIs inside a hazard footprint),
      ``fetch_hifld_critical_infrastructure`` (the US-curated companion).
    - Upstream data source: OpenStreetMap via the Overpass API.

    **Errors (FR-AS-11 typed-error surface):**
    - ``OverpassInputError``: bad bbox / missing or garbled tag / Overpass
      rejected the query (retryable=False).
    - ``OverpassUpstreamError``: every Overpass mirror failed (retryable=True).
    - ``OverpassNoFeaturesError``: zero features matched in scope
      (retryable=False).

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key is
    SHA-256 over ``(bbox-6dp, key, value)``. Tier-1 free, no API key. OSM is
    global, but a bbox is required (``supports_global_query=False``).
    """
    # 1. Input validation + tag resolution.
    vbox = _validate_bbox(bbox)
    key, val = _resolve_tag(tag, amenity, category, value)

    # 2. Quantize bbox to 6dp for cache-key stability.
    q_bbox: tuple[float, float, float, float] = tuple(round(v, 6) for v in vbox)  # type: ignore[assignment]

    # 3. read_through. The fetch_fn returns (bytes, extent); read_through caches
    #    only the bytes, so capture the extent via a closure side-channel for
    #    LayerURI.bbox.
    params = {
        "bbox": list(q_bbox),
        "key": key,
        "value": val,
    }
    captured: dict[str, Any] = {}

    def _fetch_bytes() -> bytes:
        fgb, extent = _fetch_overpass_pois_bytes(q_bbox, key, val)
        captured["extent"] = extent
        return fgb

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=_fetch_bytes,
    )
    assert result.uri is not None, (
        "fetch_overpass_pois is cacheable; uri must be set by read_through"
    )

    # 4. Resolve the camera extent. On a cache HIT the fetch_fn never ran so
    #    ``captured`` is empty — fall back to the requested bbox.
    extent_bbox: tuple[float, float, float, float] | None = captured.get("extent")
    if extent_bbox is None:
        extent_bbox = q_bbox

    # 5. Descriptive layer name + stable id.
    return LayerURI(
        layer_id=f"overpass-pois-{key}-{val}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"OSM POIs — {key}={val}",
        layer_type="vector",
        uri=result.uri,
        style_preset="overpass_pois",
        role="primary",
        units=None,
        bbox=extent_bbox,
    )
