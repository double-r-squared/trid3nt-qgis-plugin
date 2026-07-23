"""``fetch_roads_osm`` atomic tool — OpenStreetMap road LineStrings via Overpass API.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import time
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_roads_osm",
    "OSMError",
    "OSMInputError",
    "OSMUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.fetch_roads_osm")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class OSMError(RuntimeError):
    """Base class for OSM Overpass fetch failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "OSM_ROADS_ERROR"
    retryable: bool = True


class OSMInputError(OSMError):
    """Caller passed an invalid argument (bad bbox, unknown highway class, ...)."""

    error_code = "OSM_ROADS_INPUT_INVALID"
    retryable = False


class OSMUpstreamError(OSMError):
    """Overpass API call failed (network / HTTP 5xx / parse / rate-limit)."""

    error_code = "OSM_ROADS_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

#: HTTP timeout for the Overpass POST — Overpass can be slow; 120 s per audit.
_HTTP_TIMEOUT = 120.0

#: Overpass internal-query timeout (the ``[timeout:N]`` directive). The
#: external httpx timeout is set above; this is Overpass-side.
_OVERPASS_QL_TIMEOUT = 60

#: Polite delay before invoking Overpass, per audit.md ("between calls insert
#: 1s sleep to respect Overpass rate limits"). Applied on the miss path only;
#: cache hits skip this since they never call Overpass.
_POLITE_DELAY_S = 1.0

#: Default highway-tag value set used when ``road_classes`` is None. Covers
#: the major + arterial trunk-and-on/off-ramp tier (kickoff verbatim).
_DEFAULT_ROAD_CLASSES: tuple[str, ...] = (
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "motorway_link",
    "trunk_link",
    "primary_link",
)

#: Full set of acceptable highway tag values for input validation. Drawn from
#: the OSM Highway-tag wiki major-and-arterial-plus-link tiers; values outside
#: this set are typically inconsistent with "roads" intent (e.g. ``footway``,
#: ``cycleway``, ``track`` are not roads in the carriageway sense).
_VALID_ROAD_CLASSES: frozenset[str] = frozenset({
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "service",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
    "living_street",
    "pedestrian",
    "road",
})

#: Polite User-Agent (matches sibling tools).
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_roads_osm",
    ttl_class="static-30d",
    source_class="osm_roads",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Input validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``OSMInputError`` if bbox is invalid."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise OSMInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise OSMInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise OSMInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise OSMInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise OSMInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _validate_and_normalize_road_classes(
    road_classes: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Validate ``road_classes`` and return a *sorted* immutable tuple.

    - ``None`` → the default major-plus-arterial-plus-link tier.
    - Empty list/tuple → ``OSMInputError`` (ambiguous — caller probably meant
      "give me everything"; require an explicit list).
    - Unknown highway tag value → ``OSMInputError``.

    Returned tuple is sorted so the cache key is stable across caller-supplied
    orderings (``["primary", "motorway"]`` and ``["motorway", "primary"]``
    collapse onto the same cache entry).
    """
    if road_classes is None:
        return tuple(sorted(_DEFAULT_ROAD_CLASSES))
    if not isinstance(road_classes, (list, tuple)):
        raise OSMInputError(
            f"road_classes must be a list/tuple of highway tag values or None; "
            f"got {type(road_classes).__name__}"
        )
    if len(road_classes) == 0:
        raise OSMInputError(
            "road_classes is empty; pass None for the default set or supply at least one tag value"
        )
    seen: set[str] = set()
    for cls in road_classes:
        if not isinstance(cls, str):
            raise OSMInputError(
                f"road_classes entries must be strings; got {type(cls).__name__}: {cls!r}"
            )
        if cls not in _VALID_ROAD_CLASSES:
            raise OSMInputError(
                f"unknown highway tag value={cls!r}; allowed: "
                f"{sorted(_VALID_ROAD_CLASSES)}"
            )
        seen.add(cls)
    return tuple(sorted(seen))


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Overpass QL builder + HTTP request.
# ---------------------------------------------------------------------------


def _build_overpass_ql(
    bbox: tuple[float, float, float, float],
    road_classes: tuple[str, ...],
) -> str:
    """Construct the Overpass QL payload for the given bbox + highway-class set.

    Overpass expects the bbox corners as ``(south, west, north, east)``
    (lat first, then lon) — note this is the OPPOSITE ordering from the
    caller's ``(min_lon, min_lat, max_lon, max_lat)``.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    # Regex alternation pinned with ^...$ so partial matches don't leak in
    # (e.g. ``motorway`` should not also match ``motorway_junction``).
    classes_pipe = "|".join(road_classes)
    return (
        f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];"
        f"(way[\"highway\"~\"^({classes_pipe})$\"]({s},{w},{n},{e}););"
        f"out geom;"
    )


def _post_overpass(ql: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """POST ``ql`` to the Overpass interpreter, return the parsed JSON.

    Raises:
        ``OSMUpstreamError``: network/HTTP/parse failure.
    """
    own_client = False
    if client is None:
        client = httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        own_client = True
    try:
        # Polite throttle BEFORE the request fires.
        time.sleep(_POLITE_DELAY_S)
        logger.info(
            "fetch_roads_osm: POST %s ql_bytes=%d", _OVERPASS_URL, len(ql)
        )
        try:
            # Overpass conventionally accepts the QL in the ``data`` form field.
            resp = client.post(_OVERPASS_URL, data={"data": ql})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Overpass uses 429 for rate-limit and 504 for timeout — both
            # retryable. 4xx other than 429 is non-retryable.
            status = exc.response.status_code if exc.response is not None else None
            err = OSMUpstreamError(
                f"Overpass HTTP error status={status}: {exc}"
            )
            if status is not None and 400 <= status < 500 and status != 429:
                err.retryable = False
            raise err from exc
        except httpx.HTTPError as exc:
            raise OSMUpstreamError(
                f"Overpass network/transport error: {exc}"
            ) from exc

        try:
            return resp.json()
        except ValueError as exc:
            raise OSMUpstreamError(
                f"Overpass returned non-JSON response: {exc}"
            ) from exc
    finally:
        if own_client:
            client.close()


# ---------------------------------------------------------------------------
# Overpass → record extraction.
# ---------------------------------------------------------------------------


def _extract_way_record(
    way: dict[str, Any],
) -> dict[str, Any] | None:
    """Project an Overpass ``way`` element to the FlatGeobuf record schema.

    Returns ``None`` if the way is missing geometry or has fewer than 2
    coordinates (a LineString needs at least 2 points).

    Output fields (audit.md):
        ``osm_id``, ``name``, ``highway``, ``lanes``, ``maxspeed``,
        plus a ``coords`` list of ``(lon, lat)`` tuples for the LineString.
    """
    if way.get("type") != "way":
        return None
    geom = way.get("geometry") or []
    if not isinstance(geom, list) or len(geom) < 2:
        return None
    coords: list[tuple[float, float]] = []
    for pt in geom:
        if not isinstance(pt, dict):
            continue
        lat_v = pt.get("lat")
        lon_v = pt.get("lon")
        if lat_v is None or lon_v is None:
            continue
        try:
            lat = float(lat_v)
            lon = float(lon_v)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        coords.append((lon, lat))
    if len(coords) < 2:
        return None

    tags = way.get("tags") or {}
    if not isinstance(tags, dict):
        tags = {}

    return {
        "osm_id": way.get("id"),
        "name": tags.get("name"),
        "highway": tags.get("highway"),
        "lanes": tags.get("lanes"),
        "maxspeed": tags.get("maxspeed"),
        "coords": coords,
    }


def _extract_way_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the Overpass JSON response ``elements`` list, project each way."""
    elements = payload.get("elements") or []
    if not isinstance(elements, list):
        raise OSMUpstreamError(
            f"Overpass 'elements' is not a list: {type(elements).__name__}"
        )
    records: list[dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        rec = _extract_way_record(el)
        if rec is not None:
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Bbox clipping (F39 — job-0178).
#
# Overpass ``out geom`` returns the FULL geometry of every way that has at
# least one node inside the requested bbox, so road LineStrings spill outside
# the AOI. We clip each extracted LineString to the EXACT requested bbox so
# nothing renders beyond the AOI. A way that crosses the bbox boundary several
# times yields several in-AOI segments (clip returns a MultiLineString); each
# part becomes its own record, preserving the way's attributes. A way that only
# grazed the bbox at a node contributes only its in-AOI portion; a way that
# falls entirely outside the bbox (impossible for ``out geom`` results in
# practice, but defended here) contributes nothing.
# ---------------------------------------------------------------------------


def _linestring_parts(geom: Any) -> list[list[tuple[float, float]]]:
    """Flatten a clip result into a list of LineString coord lists.

    ``shapely.clip_by_rect`` can return a ``LineString``, a
    ``MultiLineString``, an empty geometry, or a ``GeometryCollection`` (e.g.
    when the clip degenerates to a point where the line only touches a bbox
    corner). We keep only LineString parts with ≥ 2 distinct coordinates;
    Point / empty parts are dropped (a road that touches the AOI at a single
    point is not a road segment inside the AOI).
    """
    parts: list[list[tuple[float, float]]] = []
    if geom is None or getattr(geom, "is_empty", True):
        return parts
    geom_type = geom.geom_type
    if geom_type == "LineString":
        candidates = [geom]
    elif geom_type in ("MultiLineString", "GeometryCollection"):
        candidates = list(geom.geoms)
    else:
        # Point / MultiPoint / Polygon — not a clipped road segment.
        candidates = []
    for part in candidates:
        if getattr(part, "is_empty", True):
            continue
        if part.geom_type == "LineString":
            coords = [(float(x), float(y)) for x, y in part.coords]
            if len(coords) >= 2:
                parts.append(coords)
        elif part.geom_type in ("MultiLineString", "GeometryCollection"):
            # Nested collection — recurse one level.
            parts.extend(_linestring_parts(part))
    return parts


def _clip_record_to_bbox(
    record: dict[str, Any],
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Clip one way record's LineString to ``bbox``; return 0..N clipped records.

    The returned records carry the same attributes (``osm_id``, ``name``,
    ``highway``, ``lanes``, ``maxspeed``) as the input but with ``coords``
    replaced by an in-AOI segment. A way clipped into several disjoint
    segments yields several records that all share the source way's
    attributes.
    """
    from shapely import clip_by_rect  # type: ignore[import-not-found]
    from shapely.geometry import LineString  # type: ignore[import-not-found]

    coords = record.get("coords") or []
    if len(coords) < 2:
        return []
    min_lon, min_lat, max_lon, max_lat = bbox
    try:
        clipped = clip_by_rect(
            LineString(coords), min_lon, min_lat, max_lon, max_lat
        )
    except Exception as exc:  # noqa: BLE001 — defend against degenerate geom
        logger.warning(
            "fetch_roads_osm: clip failed for osm_id=%s (%s); dropping way",
            record.get("osm_id"),
            exc,
        )
        return []

    out: list[dict[str, Any]] = []
    for seg in _linestring_parts(clipped):
        out.append({
            "osm_id": record.get("osm_id"),
            "name": record.get("name"),
            "highway": record.get("highway"),
            "lanes": record.get("lanes"),
            "maxspeed": record.get("maxspeed"),
            "coords": seg,
        })
    return out


def _clip_records_to_bbox(
    records: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """Clip every way record to ``bbox`` so no road geometry spills outside."""
    clipped: list[dict[str, Any]] = []
    for rec in records:
        clipped.extend(_clip_record_to_bbox(rec, bbox))
    return clipped


# ---------------------------------------------------------------------------
# FlatGeobuf serialization.
# ---------------------------------------------------------------------------


def _records_to_flatgeobuf_bytes(records: list[dict[str, Any]]) -> bytes:
    """Serialize ``records`` to FlatGeobuf bytes via geopandas / pyogrio.

    Each record contributes one ``LineString`` feature in EPSG:4326. An empty
    record list still produces a valid (empty) FlatGeobuf so the cache write
    succeeds — downstream callers identify the empty case by decoding the FGB;
    we never write a sentinel (poisons future reads — see ``cache.py``).

    Raises:
        ``OSMUpstreamError``: serialization failure.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import LineString  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OSMUpstreamError(
            f"geopandas / shapely not available: {exc}"
        ) from exc

    if records:
        geometries = [LineString(r["coords"]) for r in records]
        attrs = [
            {
                "osm_id": r.get("osm_id"),
                "name": r.get("name"),
                "highway": r.get("highway"),
                "lanes": r.get("lanes"),
                "maxspeed": r.get("maxspeed"),
            }
            for r in records
        ]
        gdf = gpd.GeoDataFrame(attrs, geometry=geometries, crs="EPSG:4326")
    else:
        import pandas as pd  # type: ignore[import-not-found]

        empty_df = pd.DataFrame(
            {
                "osm_id": pd.Series(dtype="Int64"),
                "name": pd.Series(dtype="object"),
                "highway": pd.Series(dtype="object"),
                "lanes": pd.Series(dtype="object"),
                "maxspeed": pd.Series(dtype="object"),
            }
        )
        gdf = gpd.GeoDataFrame(
            empty_df,
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_osm_roads_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001 — translate to typed error
            raise OSMUpstreamError(
                f"FlatGeobuf write failed: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Cache miss-path fetcher.
# ---------------------------------------------------------------------------


def _fetch_osm_roads_bytes(
    bbox: tuple[float, float, float, float],
    road_classes: tuple[str, ...],
) -> bytes:
    """The miss-path fetcher passed to ``read_through``.

    Builds the Overpass QL, POSTs it, parses the JSON, extracts LineStrings,
    serializes to FlatGeobuf, returns bytes.
    """
    ql = _build_overpass_ql(bbox, road_classes)
    payload = _post_overpass(ql)
    records = _extract_way_records(payload)
    # F39: Overpass ``out geom`` returns full way geometry for any way with a
    # node inside the bbox; clip every LineString to the EXACT bbox so roads do
    # not spill outside the AOI.
    clipped = _clip_records_to_bbox(records, bbox)
    logger.info(
        "fetch_roads_osm: extracted %d way(s), %d in-AOI segment(s) after "
        "clip for bbox=%s classes=%s",
        len(records),
        len(clipped),
        bbox,
        road_classes,
    )
    return _records_to_flatgeobuf_bytes(clipped)


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
def fetch_roads_osm(
    bbox: tuple[float, float, float, float],
    road_classes: list[str] | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """OpenStreetMap road LineStrings via Overpass API.

    **What it does:** Queries the OpenStreetMap Overpass API for
    ``highway``-tagged way features inside the requested bbox, clips the
    matching road LineStrings to the EXACT requested bbox so no road spills
    outside the AOI, serializes them to FlatGeobuf, and caches the result for
    30 days. Returns one LineString per in-AOI road segment with attributes
    ``osm_id``, ``name``, ``highway``, ``lanes``, and ``maxspeed`` (a road
    that crosses the bbox boundary several times yields several segments that
    share the source way's attributes). The resulting vector renders inline
    on the map automatically — do NOT call ``publish_layer`` on it.

    **When to use:**
    - User asks to "show roads" or "overlay the road network" for any area.
    - Flood-impact map needs road context to communicate which streets are
      inundated ("which roads are under water?").
    - Evacuation-routing or accessibility analysis needs a carriageway
      network as a context or input layer.
    - Any workflow step that needs named road-centreline vectors for CONUS
      or international areas (OSM is global).

    **When NOT to use:**
    - DO NOT use for parcel or lot boundaries — use
      ``fetch_administrative_boundaries`` (TIGER/Line) for those.
    - DO NOT use for pedestrian paths, bicycle routes, or rail lines —
      those OSM tags (``footway``, ``cycleway``, ``railway``) are excluded
      from this tool's valid ``road_classes`` set by design.
    - DO NOT use when turn-by-turn routing, signal phasing, or lane-level
      topology is needed; Overpass returns simplified LineStrings, not a
      routable graph with all OSM topology tags.
    - DO NOT use for highly dynamic road changes — the 30-day cache means
      road geometry is refreshed at most once per month.

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(min_lon, min_lat,
      max_lon, max_lat)`` in EPSG:4326. Required. Example for Fort Myers:
      ``(-82.0, 26.4, -81.7, 26.7)``.
    - ``road_classes`` (list[str] | None, default None): OSM ``highway``
      tag values to include. ``None`` returns the default major-plus-arterial
      set: ``motorway``, ``trunk``, ``primary``, ``secondary``, ``tertiary``,
      plus ``_link`` ramp variants. Pass a narrower list (e.g.
      ``["motorway", "trunk"]``) for interstates-only, or add
      ``["residential"]`` for local streets.

    **Returns:** A ``LayerURI`` pointing at a FlatGeobuf in the cache bucket
    (``s3://trid3nt-cache/cache/static-30d/osm_roads/<key>.fgb``).
    ``layer_type="vector"``, ``role="context"``, ``units=None``. Properties
    per feature: ``osm_id`` (int), ``name`` (str | None), ``highway``
    (str), ``lanes`` (str | None), ``maxspeed`` (str | None).

    **Cross-tool dependencies:**
    - Typically layered over ``fetch_dem`` or flood-output rasters as a
      context overlay.
    - Pairs with ``compute_zonal_statistics`` or intersection tools to
      quantify road-length under flood inundation.
    - Overpass inserts a 1 s polite delay before each network request to
      respect rate limits; cache hits skip this delay.

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key
    is SHA-256 over ``(bbox-6dp, road_classes_sorted)`` so caller-supplied
    ordering does not affect the key.
    """
    # 1. Input validation.
    _validate_bbox(bbox)
    classes_tuple = _validate_and_normalize_road_classes(road_classes)

    # 2. Quantize bbox to 6dp for cache-key stability.
    q_bbox = _round_bbox_to_6dp(bbox)

    # 3. read_through. Params dict drives the cache-key SHA-256.
    params = {
        "bbox": list(q_bbox),
        "road_classes": list(classes_tuple),  # already sorted by normalizer
    }
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_osm_roads_bytes(q_bbox, classes_tuple),
    )
    assert result.uri is not None, (
        "fetch_roads_osm is cacheable; uri must be set by read_through"
    )

    # 4. LayerURI shape per audit.md: vector / context / units=None.
    label_classes = ",".join(classes_tuple) if len(classes_tuple) <= 4 else (
        f"{len(classes_tuple)} classes"
    )
    return LayerURI(
        layer_id=f"osm-roads-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"OSM Roads — {label_classes}",
        layer_type="vector",
        uri=result.uri,
        style_preset="osm_roads",
        role="context",
        units=None,
        bbox=q_bbox,
    )
