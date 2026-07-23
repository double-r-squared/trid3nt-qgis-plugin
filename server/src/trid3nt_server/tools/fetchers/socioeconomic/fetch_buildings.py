"""Building-footprint fetcher (``fetch_buildings``): OSM Overpass ways+relations primary, MS Open Maps ML footprints fallback -> FlatGeobuf + tags sidecar.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    FetchError,
    UpstreamAPIError,
    BboxInvalidError,
    _DEFAULT_USER_AGENT,
    _validate_bbox,
    round_bbox_to_resolution,
    _bbox_area_km2,
)

__all__ = [
    "fetch_buildings",
    "BUILDINGS_TAGS_SIDECAR_EXT",
    "buildings_cache_uri",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings")


# ---------------------------------------------------------------------------
# fetch_buildings — OSM Overpass (reliable primary) + Microsoft (fallback)
# ---------------------------------------------------------------------------


_FETCH_BUILDINGS_METADATA = AtomicToolMetadata(
    name="fetch_buildings",
    ttl_class="static-30d",
    source_class="buildings",
    cacheable=True,
)

# BEST-EFFORT FALLBACK source (``source="msft"``). MS Open Maps publishes
# Global ML Building Footprints sharded by quadkey under a public Azure Blob
# container. The official catalog index is at:
#   https://minedbuildings.blob.core.windows.net/global-buildings/dataset-links.csv
# Each row is (QuadKey, Location, Url) — the URL is a GZIP'd line-delimited
# GeoJSON. The MS Open Maps STAC catalog (an alternative entry point referenced
# in the kickoff) at planetarycomputer.microsoft.com wraps this same data under
# a STAC API.
#
# KNOWN LIMITATION (job-0331): the Planetary Computer ``ms-buildings``
# collection typically returns a single whole-country item whose only asset is
# an ``abfs://`` (Azure Blob Filesystem) GeoParquet store — ``requests.get`` on
# an ``abfs://`` URL cannot work, so this branch frequently fails to download.
# It is retained as a best-effort fallback only; OSM Overpass (source="osm") is
# the reliable primary. When the STAC search yields no items or no downloadable
# asset, an ``UpstreamAPIError`` surfaces and ``fetch_buildings`` falls back to
# OSM (or raises an honest both-failed error).


# ---------------------------------------------------------------------------
# OSM Overpass building-footprint fetcher (job-0331).
#
# ROOT CAUSE (live 2026-06-16): the ``source="msft"`` path queries the
# Planetary Computer ``ms-buildings`` STAC collection, whose only asset is an
# ``abfs://`` (Azure Blob Filesystem) GeoParquet store — ``requests.get`` on an
# ``abfs://`` URL cannot work, so MS footprints NEVER download. The previous
# ``source="osm"`` branch only raised ``NotImplementedError``, leaving the tool
# with NO working footprint source.
#
# Fix: OSM Overpass is the reliable PRIMARY (verified: 578 building polygons for
# one Chattanooga block). This fetcher mirrors ``fetch_roads_osm.py``'s Overpass
# pattern (``out geom`` + geometry assembly + clip-to-bbox + FlatGeobuf write)
# but assembles building POLYGONS (closed ways) and MULTIPOLYGONS (relations)
# rather than road LineStrings. MS stays as a best-effort FALLBACK.
# ---------------------------------------------------------------------------

#: Overpass interpreter endpoint (same public endpoint as fetch_roads_osm).
_OVERPASS_BUILDINGS_URL = "https://overpass-api.de/api/interpreter"

#: External HTTP timeout for the Overpass POST — Overpass is slow under load.
_OVERPASS_BUILDINGS_HTTP_TIMEOUT = 120.0

#: Overpass-side query timeout (the ``[timeout:N]`` QL directive).
_OVERPASS_BUILDINGS_QL_TIMEOUT = 90

#: Polite delay before the Overpass request to respect rate limits (miss-path
#: only; cache hits never reach this fetcher).
_OVERPASS_BUILDINGS_POLITE_DELAY_S = 1.0

def _build_overpass_buildings_ql(
    bbox: tuple[float, float, float, float],
) -> str:
    """Construct the Overpass QL selecting building ways AND relations in ``bbox``.

    Overpass expects bbox corners as ``(south, west, north, east)`` (lat first)
    — the OPPOSITE corner ordering from the caller's ``(min_lon, min_lat,
    max_lon, max_lat)``. ``out geom`` returns full node geometry inline (plus,
    for relations, the geometry of every member way) so we can assemble
    polygons without a second resolve pass.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    return (
        f"[out:json][timeout:{_OVERPASS_BUILDINGS_QL_TIMEOUT}];"
        f"("
        f"way[\"building\"]({s},{w},{n},{e});"
        f"relation[\"building\"]({s},{w},{n},{e});"
        f");"
        f"out geom;"
    )

def _post_overpass_buildings(ql: str) -> dict[str, Any]:
    """POST ``ql`` to the Overpass interpreter and return parsed JSON.

    Raises ``UpstreamAPIError`` on network / HTTP / parse failure so the
    ``read_through`` "re-raise on fetcher failure; no sentinel" contract holds.
    Uses ``httpx`` to match ``fetch_roads_osm``'s transport; a polite 1 s sleep
    fires BEFORE the request.
    """
    import httpx  # local import: keeps registry import light + mirrors roads tool

    try:
        time.sleep(_OVERPASS_BUILDINGS_POLITE_DELAY_S)
        logger.info(
            "fetch_buildings(osm): POST %s ql_bytes=%d",
            _OVERPASS_BUILDINGS_URL,
            len(ql),
        )
        with httpx.Client(
            timeout=_OVERPASS_BUILDINGS_HTTP_TIMEOUT,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
        ) as client:
            resp = client.post(_OVERPASS_BUILDINGS_URL, data={"data": ql})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        raise UpstreamAPIError(
            f"OSM Overpass buildings HTTP error status={status}: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamAPIError(
            f"OSM Overpass buildings network/transport error: {exc}"
        ) from exc
    except ValueError as exc:
        raise UpstreamAPIError(
            f"OSM Overpass buildings returned non-JSON response: {exc}"
        ) from exc

def _ring_from_geom(geom: Any) -> list[tuple[float, float]]:
    """Extract a ``(lon, lat)`` coordinate ring from an Overpass ``geometry`` list.

    Drops malformed / non-finite points. Returns the raw ring (NOT forced
    closed) — the caller decides whether ≥ 3 distinct vertices make a polygon.
    """
    ring: list[tuple[float, float]] = []
    if not isinstance(geom, list):
        return ring
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
        ring.append((lon, lat))
    return ring

def _way_to_polygon(way: dict[str, Any]) -> Any | None:
    """Assemble a closed-way Overpass element into a shapely ``Polygon``.

    Returns ``None`` if the ring has fewer than 3 distinct vertices or the
    resulting polygon is empty / invalid-and-unfixable.
    """
    from shapely.geometry import Polygon  # type: ignore[import-not-found]

    ring = _ring_from_geom(way.get("geometry"))
    # Need at least 3 distinct vertices for an areal ring. Overpass closed ways
    # repeat the first node as the last; dedup the closure before counting.
    distinct = list(dict.fromkeys(ring))
    if len(distinct) < 3:
        return None
    try:
        poly = Polygon(ring)
    except Exception:  # noqa: BLE001 — degenerate ring
        return None
    if poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)  # standard self-intersection repair
        if poly.is_empty or poly.geom_type not in ("Polygon", "MultiPolygon"):
            return None
    return poly

def _relation_to_multipolygon(rel: dict[str, Any]) -> Any | None:
    """Assemble an Overpass ``multipolygon`` relation into a (Multi)Polygon.

    ``out geom`` returns each member way's geometry inline under
    ``members[].geometry`` with ``role`` in ``{"outer", "inner"}``. We build
    outer-ring polygons, subtract inner rings (holes), and union the result.
    Returns ``None`` if no usable outer ring exists.
    """
    from shapely.geometry import Polygon  # type: ignore[import-not-found]
    from shapely.ops import unary_union  # type: ignore[import-not-found]

    members = rel.get("members") or []
    if not isinstance(members, list):
        return None
    outers: list[Any] = []
    inners: list[Any] = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        ring = _ring_from_geom(member.get("geometry"))
        distinct = list(dict.fromkeys(ring))
        if len(distinct) < 3:
            continue
        try:
            poly = Polygon(ring)
        except Exception:  # noqa: BLE001
            continue
        if poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty:
                continue
        role = member.get("role")
        if role == "inner":
            inners.append(poly)
        else:
            # Default unrolled members (role "" / "outer") are treated as outer.
            outers.append(poly)
    if not outers:
        return None
    outer_union = unary_union(outers)
    if inners:
        hole_union = unary_union(inners)
        try:
            outer_union = outer_union.difference(hole_union)
        except Exception:  # noqa: BLE001 — keep solid footprint if hole-cut fails
            pass
    if outer_union.is_empty or outer_union.geom_type not in (
        "Polygon",
        "MultiPolygon",
    ):
        return None
    return outer_union

def _building_fid(el_type: Any, osm_id: Any) -> str:
    """Stable composite feature id ``"<first-letter-of-osm_type><osm_id>"``.

    e.g. a ``way`` id ``123456`` -> ``"w123456"``, a ``relation`` id ``222`` ->
    ``"r222"``. The ``(osm_type, osm_id)`` pair is the Overpass-by-id key; this
    single string is the slim inline join-key the popup enrich path sends back to
    ``/api/building-detail`` and the sidecar tag-map is keyed by.
    """
    prefix = str(el_type or "")[:1]
    return f"{prefix}{osm_id}"

def _extract_building_features(
    payload: dict[str, Any],
) -> tuple[list[tuple[Any, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Walk Overpass ``elements`` -> ``(features, tags_by_fid)`` for buildings.

    Ways become ``Polygon``s; multipolygon relations become ``(Multi)Polygon``s.
    Non-areal / malformed elements are skipped.

    INLINE payload is SLIM (frontend-perf fix, NATE 2026-06-27 "footprint layers
    store too much in the frontend GeoJSON"): each feature carries ONLY id props
    -- ``osm_id``, ``osm_type``, and a stable composite ``fid`` (e.g. ``"w123456"``)
    -- and DROPS ``building`` + ``name`` from the inline properties. The full tag
    bag (``building``, ``height``, ``levels``, ``name``, ``addr:*`` ...) is
    captured separately in the returned ``tags_by_fid`` map for the
    click-to-enrich sidecar; the popup fetches it on demand by ``(osm_type,
    osm_id)`` so the inline GeoJSON stays tiny.
    """
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise UpstreamAPIError(
            f"OSM Overpass buildings 'elements' is not a list: "
            f"{type(elements).__name__}"
        )
    features: list[tuple[Any, dict[str, Any]]] = []
    tags_by_fid: dict[str, dict[str, Any]] = {}
    for el in elements:
        if not isinstance(el, dict):
            continue
        el_type = el.get("type")
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        if el_type == "way":
            geom = _way_to_polygon(el)
        elif el_type == "relation":
            geom = _relation_to_multipolygon(el)
        else:
            geom = None
        if geom is None:
            continue
        osm_id = el.get("id")
        fid = _building_fid(el_type, osm_id)
        features.append(
            (
                geom,
                {
                    "osm_id": osm_id,
                    "osm_type": el_type,
                    "fid": fid,
                },
            )
        )
        # Capture the FULL tag bag for the click-to-enrich sidecar. Only retain
        # a non-empty bag (a building with no tags contributes nothing to enrich).
        if tags:
            tags_by_fid[fid] = dict(tags)
    return features, tags_by_fid

def _fetch_osm_buildings_bytes(
    bbox: tuple[float, float, float, float],
    on_tags: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> bytes:
    """Fetch OSM building footprints for ``bbox`` and return FlatGeobuf bytes.

    Queries the OpenStreetMap Overpass API for ``building``-tagged ways AND
    relations intersecting the bbox, assembles closed ways into ``Polygon``s
    and multipolygon relations into ``(Multi)Polygon``s, retains EVERY footprint
    whose geometry INTERSECTS the requested bbox (whole, un-sliced — a building
    straddling any AOI edge is kept intact, not chopped at the boundary), and
    serializes the result to FlatGeobuf — the SAME output format the ``msft``
    branch produces, so the cache write + downstream consumers are
    source-agnostic.

    Edge-coverage note (the "missed buildings on the LEFT" fix): a previous
    revision ran ``gpd.clip(gdf, bbox)``, which geometrically slices every
    footprint at the bbox boundary. That dropped/mangled buildings straddling
    the AOI edge. We now filter by INTERSECTS instead of clipping, so any
    building touching the bbox is returned whole — symmetric on all four sides.

    Raises ``UpstreamAPIError`` on Overpass failure OR when no building
    footprints intersect the bbox (honest typed empty per the data-source
    fallback norm — the caller decides whether to fall back to ``msft``).
    """
    _validate_bbox(bbox)
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely import box  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UpstreamAPIError(
            f"geopandas / shapely not available for OSM buildings: {exc}"
        ) from exc

    ql = _build_overpass_buildings_ql(bbox)
    payload = _post_overpass_buildings(ql)
    features, tags_by_fid = _extract_building_features(payload)

    # Surface the full per-fid tag bag to the caller so it can persist the
    # click-to-enrich sidecar under the SAME cache key as the .fgb. Best-effort:
    # a sidecar callback fault must NEVER fail the fetch (the slim layer still
    # renders; enrich then degrades to a live Overpass-by-id query).
    if on_tags is not None and tags_by_fid:
        try:
            on_tags(tags_by_fid)
        except Exception as exc:  # noqa: BLE001 -- sidecar is best-effort
            logger.warning(
                "fetch_buildings(osm): tag-sidecar callback failed: %s", exc
            )

    if not features:
        raise UpstreamAPIError(
            f"OSM Overpass returned no building footprints for bbox={bbox} "
            f"(area may be unmapped — caller may fall back to source='msft')"
        )

    geometries = [geom for geom, _attrs in features]
    attrs = [a for _g, a in features]
    gdf = gpd.GeoDataFrame(attrs, geometry=geometries, crs="EPSG:4326")

    # Retain every footprint that INTERSECTS the requested bbox — do NOT clip.
    #
    # Overpass ``out geom`` returns the FULL footprint of any building with a
    # node inside the bbox, so a building straddling an AOI edge spills outside.
    # The previous revision ran ``gpd.clip(gdf, bbox)``, which geometrically
    # slices each footprint at the boundary. That dropped/mangled edge buildings
    # (NATE: "missed some on the LEFT"). We instead keep footprints whole when
    # they intersect the bbox and exclude only those that fall entirely outside.
    # ``intersects`` is symmetric on all four edges (left/right/top/bottom), so
    # no side is preferentially dropped. Geometries are left un-sliced.
    min_lon, min_lat, max_lon, max_lat = bbox
    bbox_geom = box(min_lon, min_lat, max_lon, max_lat)
    # Defend against degenerate / non-areal geometry surviving assembly.
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    try:
        gdf = gdf[gdf.geometry.intersects(bbox_geom)]
    except Exception as exc:  # noqa: BLE001 — defend against degenerate geom
        raise UpstreamAPIError(
            f"OSM buildings bbox-intersects filter failed for bbox={bbox}: {exc}"
        ) from exc

    if len(gdf) == 0:
        raise UpstreamAPIError(
            f"OSM Overpass building footprints all fell outside bbox={bbox} "
            f"(none intersect the AOI — caller may fall back to source='msft')"
        )

    logger.info(
        "fetch_buildings(osm): %d building footprint(s) intersecting AOI for bbox=%s",
        len(gdf),
        bbox,
    )

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_osm_buildings_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001 — translate to typed error
            raise UpstreamAPIError(
                f"OSM buildings FlatGeobuf write failed: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass

def _fetch_msft_buildings_bytes(
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Query MS Open Maps Building Footprints for ``bbox`` and return FlatGeobuf bytes.

    Uses the Microsoft Planetary Computer STAC API as the query surface
    (https://planetarycomputer.microsoft.com/api/stac/v1) — the same catalog
    that backs the public MS Open Maps releases. Items in the
    ``ms-buildings`` collection point at PMTiles / FlatGeobuf assets we can
    download by-asset.

    Implementation note (M4 scope): this is a minimal request → response
    path. A production-grade implementation would use ``pystac-client`` for
    pagination and ``stackstac`` for asset materialization; for the M4
    substrate we issue a single ``POST /search`` with the bbox + intersects
    filter, take the first matching item's FlatGeobuf asset (or fall back
    to GeoJSON serialization of the geometry), and return raw bytes.
    """
    _validate_bbox(bbox)
    # Planetary Computer STAC endpoint. The ms-buildings collection is the
    # public catalog wrapping the Open Data ML footprints.
    pc_stac_url = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
    search_body = {
        "collections": ["ms-buildings"],
        "bbox": list(bbox),
        "limit": 1,
    }
    try:
        resp = requests.post(
            pc_stac_url,
            json=search_body,
            headers={"User-Agent": _DEFAULT_USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        catalog = resp.json()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"MS Open Maps STAC search failed for bbox={bbox}: {exc}"
        ) from exc

    features = catalog.get("features", []) or []
    if not features:
        # No ML coverage in this bbox; surface a typed error so the agent can
        # choose to fall back to OSM via ``source="osm"`` in a future call.
        raise UpstreamAPIError(
            f"no MS Open Maps building items intersect bbox={bbox} "
            f"(coverage may be missing — fall back via source='osm' in a follow-up)"
        )

    # Asset preference: FlatGeobuf if present, GeoParquet next, GeoJSON last.
    item = features[0]
    assets = item.get("assets", {}) or {}

    preferred_asset = None
    for asset_key in ("data", "footprints", "flatgeobuf"):
        if asset_key in assets:
            preferred_asset = assets[asset_key]
            break
    if preferred_asset is None and assets:
        # Fall back to the first asset listed.
        preferred_asset = next(iter(assets.values()))
    if preferred_asset is None or "href" not in preferred_asset:
        # No downloadable asset; serialize the bbox as a placeholder
        # FeatureCollection so the path completes deterministically. A
        # follow-up job replaces this with proper PMTiles materialization.
        placeholder = {
            "type": "FeatureCollection",
            "features": [],
            "_trid3nt_note": (
                "STAC item had no downloadable asset; placeholder emitted. "
                "Replace via PMTiles materialization in M5 follow-up."
            ),
            "_trid3nt_item_id": item.get("id"),
            "_trid3nt_bbox": list(bbox),
        }
        return json.dumps(placeholder).encode("utf-8")

    asset_url = preferred_asset["href"]
    try:
        asset_resp = requests.get(
            asset_url,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=60.0,
        )
        asset_resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"MS Open Maps asset download failed url={asset_url}: {exc}"
        ) from exc

    return asset_resp.content

# Sidecar suffix for the click-to-enrich tag bag written alongside the buildings
# .fgb. The detail endpoint (tool_catalog_http /api/building-detail) and the
# enrich-fallback both derive the same key, so this constant is the single
# source of truth for the suffix on both the write and read paths.
BUILDINGS_TAGS_SIDECAR_EXT = "tags.json"

def buildings_cache_uri(
    bbox: tuple[float, float, float, float],
    source: str,
    ext: str,
) -> str:
    """Resolve the ``s3://`` URI the buildings cache write uses for ``ext``.

    Mirrors ``read_through``'s bucket + key derivation EXACTLY so a sibling
    artifact (the ``.tags.json`` sidecar) lands under the SAME ``<key>`` as the
    ``.fgb``. The ``params`` dict + quantization must match the
    ``_fetch_for_source`` call site (``{"bbox": list(quantized), "source":
    src}``, 10 m snap) or the keys diverge.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
    )

    quantized = round_bbox_to_resolution(bbox, 10)
    params = {"bbox": list(quantized), "source": source}
    meta = _FETCH_BUILDINGS_METADATA
    source_id = meta.source_class or meta.name
    key = compute_cache_key(source_id, params, meta.ttl_class)
    path = cache_path(meta.source_class, meta.ttl_class, key, ext)
    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    return f"s3://{bucket}/{path}"

def _write_buildings_tags_sidecar(
    bbox: tuple[float, float, float, float],
    source: str,
    tags_by_fid: dict[str, dict[str, Any]],
) -> None:
    """Persist the ``{fid -> full tags}`` sidecar next to the buildings ``.fgb``.

    Best-effort (NATE 2026-06-27 click-to-enrich): a write failure must NOT fail
    the fetch -- the slim layer still renders; the popup enrich path degrades to a
    live Overpass-by-id query when the sidecar is absent. The sidecar key is the
    SAME ``<key>`` as the ``.fgb`` with a ``.tags.json`` suffix
    (``cache/static-30d/buildings/<key>.tags.json``).
    """
    try:
        import boto3

        uri = buildings_cache_uri(bbox, source, BUILDINGS_TAGS_SIDECAR_EXT)
        rest = uri[len("s3://"):]
        bucket, _, obj_key = rest.partition("/")
        body = json.dumps(tags_by_fid, separators=(",", ":")).encode("utf-8")
        s3 = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        s3.put_object(
            Bucket=bucket,
            Key=obj_key,
            Body=body,
            ContentType="application/json",
        )
        logger.info(
            "fetch_buildings: wrote tags sidecar key=%s fids=%d bytes=%d",
            obj_key,
            len(tags_by_fid),
            len(body),
        )
    except Exception as exc:  # noqa: BLE001 -- sidecar is best-effort
        logger.warning(
            "fetch_buildings: tags sidecar write degraded (%s); enrich will "
            "fall back to live Overpass-by-id",
            exc,
        )

@register_tool(
    _FETCH_BUILDINGS_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (MS Open Maps buildings),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_buildings(
    bbox: tuple[float, float, float, float],
    source: str = "osm",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch building footprints (polygons) for a bbox.

    Use this when: the agent needs building polygons for damage / exposure
    estimation, risk scoring, or display of the built environment.

    Sources (data-source fallback norm — primary → fallback, never a silent
    dead-end):
        - ``"osm"`` (DEFAULT, RELIABLE PRIMARY): OpenStreetMap building
          footprints via the Overpass API. Global, free, no API key. Returns
          building ``Polygon``s (closed ways) and ``MultiPolygon``s
          (multipolygon relations with holes), clipped to the exact bbox.
          This is the dependable path — use it unless you have a specific
          reason to prefer MS.
        - ``"msft"`` (BEST-EFFORT FALLBACK): Microsoft Open Maps ML-derived
          footprints via the Planetary Computer STAC catalog. Wider rural
          coverage in some areas, but the public catalog often exposes only
          ``abfs://`` GeoParquet stores that cannot be downloaded by-asset, so
          this path frequently fails — treat it as best-effort only.

    Robustness: whichever ``source`` you request is tried FIRST; if it raises
    an ``UpstreamAPIError`` (upstream failure, no coverage, empty result), the
    tool automatically FALLS BACK to the other source. If BOTH fail, an honest
    ``UpstreamAPIError`` naming both attempts is raised — the agent never
    receives a fabricated success. The cache key reflects the source actually
    used, so the two sources never collide and a fallback result is cached
    under its real source.

    Do NOT use this for: live address/parcel lookups (those need a different
    cadastral source); per-structure replacement cost / occupancy / HAZUS
    attributes for loss modeling (use ``fetch_usace_nsi`` — the National
    Structure Inventory point tool — instead); 3D building heights (heights
    are a separate dataset); querying buildings by name or use class (filter
    post-fetch).

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        source: ``"osm"`` (default, reliable primary) or ``"msft"``
            (best-effort fallback). The requested source is tried first; the
            tool falls back to the other on ``UpstreamAPIError``.

    Returns:
        A ``LayerURI`` (``layer_type="vector"``) pointing at a FlatGeobuf in
        the cache bucket:
        ``s3://trid3nt-cache/cache/static-30d/buildings/<key>.fgb``.
        The ``name`` and ``layer_id`` reflect the source actually used.

    FR-CE-8: The fetch is routed through ``read_through`` so identical
    quantized-bbox + source calls reuse the cached artifact.
    """
    if source not in ("msft", "osm"):
        raise BboxInvalidError(
            f"unsupported source={source!r}; allowed: 'osm' (default), 'msft'"
        )
    # Quantize bbox to 10m: building footprint polygons are at sub-meter
    # precision but the bbox boundary is the cache-key driver, and a 10m
    # snap is plenty for the dedup goal (same neighborhood query == same key).
    quantized = round_bbox_to_resolution(bbox, 10)
    if _bbox_area_km2(quantized) > 5_000.0:
        raise BboxInvalidError(
            f"bbox area {_bbox_area_km2(quantized):.1f} km^2 exceeds 5000 km^2 "
            "guardrail for fetch_buildings (a single source query will not "
            "reliably cover that; use a tiled workflow)."
        )

    # Per-source miss-path fetchers. Each goes through read_through under a
    # cache key that reflects the source actually used, so a fallback result
    # caches under its real source and never collides with the other source.
    def _fetch_for_source(src: str) -> LayerURI:
        params = {"bbox": list(quantized), "source": src}
        if src == "osm":
            # Click-to-enrich (NATE 2026-06-27): the OSM fetcher surfaces the
            # full per-fid tag bag so we can persist the sidecar under the SAME
            # cache key as the .fgb. Best-effort -- _write_..._sidecar swallows
            # its own failures so the fetch never fails on a sidecar write.
            def _on_tags(tags_by_fid: dict[str, dict[str, Any]]) -> None:
                _write_buildings_tags_sidecar(quantized, "osm", tags_by_fid)

            fetch_fn = lambda: _fetch_osm_buildings_bytes(  # noqa: E731
                quantized, on_tags=_on_tags
            )
        else:
            fetch_fn = lambda: _fetch_msft_buildings_bytes(quantized)  # noqa: E731
        result = read_through(
            metadata=_FETCH_BUILDINGS_METADATA,
            params=params,
            ext="fgb",
            fetch_fn=fetch_fn,
        )
        assert result.uri is not None
        return LayerURI(
            layer_id=f"buildings-{quantized[0]:.4f}-{quantized[1]:.4f}-{src}",
            name=f"Buildings ({src.upper()})",
            layer_type="vector",
            uri=result.uri,
            style_preset="affected_buildings",
            role="input",
        )

    # Data-source fallback norm: try requested source first; on upstream
    # failure, fall back to the OTHER source; if both fail, raise an honest
    # typed error naming both attempts (never a silent dead-end).
    fallback = "msft" if source == "osm" else "osm"
    try:
        return _fetch_for_source(source)
    except UpstreamAPIError as primary_exc:
        logger.warning(
            "fetch_buildings: source=%r failed (%s); falling back to %r",
            source,
            primary_exc,
            fallback,
        )
        try:
            return _fetch_for_source(fallback)
        except UpstreamAPIError as fallback_exc:
            raise UpstreamAPIError(
                f"fetch_buildings failed for both sources: "
                f"{source!r} -> {primary_exc}; "
                f"{fallback!r} (fallback) -> {fallback_exc}"
            ) from fallback_exc
