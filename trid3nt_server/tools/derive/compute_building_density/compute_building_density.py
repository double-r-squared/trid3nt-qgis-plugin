"""``compute_building_density`` - a count-per-cell raster from ML building footprints.

A cell with no buildings carries 0, not nodata - absence is real signal here -
and a quadkey the index does not list is a zero-count tile, not an error.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import math
import os
import tempfile
from typing import Iterable, Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = ["compute_building_density"]

logger = logging.getLogger("trid3nt_server.tools.derive.compute_building_density.compute_building_density")




class BuildingDensityError(RuntimeError):
    """Base class for compute_building_density failures."""

    error_code: str = "BUILDING_DENSITY_ERROR"
    retryable: bool = True


class BuildingDensityInputError(BuildingDensityError):
    """Bad bbox, cell_size, or source value."""

    error_code = "BUILDING_DENSITY_INPUT_INVALID"
    retryable = False


class BuildingDensityUpstreamError(BuildingDensityError):
    """Microsoft index or tile download / parse failure."""

    error_code = "BUILDING_DENSITY_UPSTREAM_ERROR"
    retryable = True



#: The Microsoft Global ML Building Footprints CSV index URL.
_MS_INDEX_URL = (
    "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
)

#: Native zoom level of the Microsoft tile pyramid.
_MS_QUADKEY_ZOOM = 9

#: Sent on every upstream request.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_VALID_SOURCES = frozenset({"ms_footprints"})

#: Module-level cache of the parsed CSV index. The full index is ~7 MB and its
#: download is the most expensive single step, so re-fetching it per call would
#: defeat the per-bbox cache.
_INDEX_CACHE: dict[str, list[str]] | None = None
_INDEX_CACHE_DOWNLOAD_BYTES: int = 0



_METADATA = AtomicToolMetadata(
    name="compute_building_density",
    ttl_class="static-30d",
    source_class="building_density",
    cacheable=True,
)




def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``BuildingDensityInputError`` if ``bbox`` is invalid."""
    if len(bbox) != 4:
        raise BuildingDensityInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise BuildingDensityInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise BuildingDensityInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    # The Web Mercator pyramid is undefined above ~85.05113 deg latitude.
    if not (-85.05 <= min_lat <= 85.05 and -85.05 <= max_lat <= 85.05):
        raise BuildingDensityInputError(
            f"bbox lat out of Web-Mercator-valid range [-85.05, 85.05]: {bbox!r}"
        )
    if min_lon >= max_lon or min_lat >= max_lat:
        raise BuildingDensityInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


# Quadkey math (Bing tile system).
#
# The canonical Bing Maps Tile System spec:
# https://learn.microsoft.com/en-us/bingmaps/articles/bing-maps-tile-system
# Four short functions rather than a dependency for a one-off operation.


def _lonlat_to_tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Integer Bing tile ``(x, y)`` at ``zoom``; latitude is clamped to the
    Mercator-valid range, which avoids the singularities at the poles.
    """
    lat_clamped = max(-85.05112878, min(85.05112878, lat))
    sin_lat = math.sin(lat_clamped * math.pi / 180.0)
    x = (lon + 180.0) / 360.0
    y = 0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)
    map_size = 1 << zoom  # 2 ** zoom
    tx = int(min(map_size - 1, max(0, math.floor(x * map_size))))
    ty = int(min(map_size - 1, max(0, math.floor(y * map_size))))
    return tx, ty


def _tile_xy_to_quadkey(tx: int, ty: int, zoom: int) -> str:
    """Tile ``(x, y, z)`` as a Bing quadkey; each level is one of 0, 1, 2, 3."""
    parts: list[str] = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if (tx & mask) != 0:
            digit += 1
        if (ty & mask) != 0:
            digit += 2
        parts.append(str(digit))
    return "".join(parts)


def _quadkeys_for_bbox(
    bbox: tuple[float, float, float, float], zoom: int = _MS_QUADKEY_ZOOM
) -> list[str]:
    """Every quadkey at ``zoom`` covering ``bbox``, inclusive on both bounds; a
    1-degree bbox at zoom-9 is 1-4 tiles, a state-sized one 10-40.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    # Tile (0,0) is at the NW corner, so tile-y DECREASES as latitude increases:
    # the SW corner gives the bottom-left tile and the NE corner the top-right.
    tx_min, ty_max = _lonlat_to_tile_xy(min_lon, min_lat, zoom)
    tx_max, ty_min = _lonlat_to_tile_xy(max_lon, max_lat, zoom)
    if tx_min > tx_max:
        tx_min, tx_max = tx_max, tx_min
    if ty_min > ty_max:
        ty_min, ty_max = ty_max, ty_min
    quadkeys: list[str] = []
    for ty in range(ty_min, ty_max + 1):
        for tx in range(tx_min, tx_max + 1):
            quadkeys.append(_tile_xy_to_quadkey(tx, ty, zoom))
    return quadkeys




def _fetch_index() -> dict[str, list[str]]:
    """``{quadkey: [url, ...]}`` for the whole dataset-links index, cached for the
    process; a border quadkey lists every region's URL for it, deduplicated.
    """
    global _INDEX_CACHE, _INDEX_CACHE_DOWNLOAD_BYTES

    if _INDEX_CACHE is not None:
        return _INDEX_CACHE

    logger.info("compute_building_density: fetching MS index %s", _MS_INDEX_URL)
    try:
        resp = requests.get(
            _MS_INDEX_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=60.0,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise BuildingDensityUpstreamError(
            f"MS index download failed url={_MS_INDEX_URL}: {exc}"
        ) from exc

    _INDEX_CACHE_DOWNLOAD_BYTES = len(resp.content)
    logger.info(
        "compute_building_density: MS index downloaded bytes=%d", _INDEX_CACHE_DOWNLOAD_BYTES
    )

    index: dict[str, list[str]] = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        qk = (row.get("QuadKey") or "").strip()
        url = (row.get("Url") or "").strip()
        if not qk or not url:
            continue
        index.setdefault(qk, []).append(url)
    for qk in list(index.keys()):
        seen: set[str] = set()
        dedup: list[str] = []
        for u in index[qk]:
            if u not in seen:
                seen.add(u)
                dedup.append(u)
        index[qk] = dedup

    logger.info(
        "compute_building_density: MS index parsed quadkeys=%d", len(index)
    )
    _INDEX_CACHE = index
    return index


def _index_for_quadkeys(quadkeys: Iterable[str]) -> dict[str, list[str]]:
    """``{quadkey: [url, ...]}`` for the subset of ``quadkeys`` the index lists; an
    absent quadkey is a tile with no detected buildings, not a failure.
    """
    full = _fetch_index()
    return {qk: full[qk] for qk in quadkeys if qk in full}




def _download_tile_features(url: str) -> list[dict]:
    """The features of one tile. Despite the ``.csv.gz`` extension the decompressed
    body is line-delimited GeoJSON, not CSV; network failure raises upstream.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=120.0,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise BuildingDensityUpstreamError(
            f"MS tile download failed url={url}: {exc}"
        ) from exc

    try:
        decompressed = gzip.decompress(resp.content).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildingDensityUpstreamError(
            f"MS tile gunzip / decode failed url={url}: {exc}"
        ) from exc

    features: list[dict] = []
    for lineno, line in enumerate(decompressed.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            feat = json.loads(line)
        except json.JSONDecodeError as exc:
            # The upstream tile generator emits occasional partial lines; one
            # bad line is logged and skipped rather than failing the tile.
            logger.warning(
                "compute_building_density: tile %s line %d JSON parse failed: %s",
                url,
                lineno,
                exc,
            )
            continue
        features.append(feat)
    logger.info(
        "compute_building_density: tile url=%s features=%d", url, len(features)
    )
    return features




def _ring_centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    """The shoelace centroid of an outer ring in (lon, lat) order, closed or not;
    None below three vertices. Cartesian, not geodesic - enough to bin a cell.
    """
    if not ring or len(ring) < 3:
        return None
    pts = ring[:-1] if (len(ring) > 1 and ring[0] == ring[-1]) else ring
    if len(pts) < 3:
        # A tiny or degenerate polygon still contributes one centroid rather
        # than vanishing from the count.
        n = len(pts) or 1
        return (
            sum(p[0] for p in pts) / n,
            sum(p[1] for p in pts) / n,
        )

    # A building is under ~100 m wide while its lon/lat magnitudes run 80-150,
    # so a shoelace on raw coordinates loses the cross product x0*y1 - x1*y0 to
    # catastrophic cancellation between two near-equal large numbers. Shifting
    # every vertex to the first one keeps those magnitudes near 1e-8 instead of
    # 1e4, well inside float64, and the centroid is shifted back at the end.
    ox, oy = pts[0][0], pts[0][1]
    cx = 0.0
    cy = 0.0
    area2 = 0.0  # 2 * signed area
    n = len(pts)
    for i in range(n):
        x0 = pts[i][0] - ox
        y0 = pts[i][1] - oy
        j = (i + 1) % n
        x1 = pts[j][0] - ox
        y1 = pts[j][1] - oy
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area2) < 1e-30:
        return (
            sum(p[0] for p in pts) / n,
            sum(p[1] for p in pts) / n,
        )
    cx /= 3.0 * area2
    cy /= 3.0 * area2
    return cx + ox, cy + oy


def _feature_centroid(feat: dict) -> tuple[float, float] | None:
    """Return lon/lat centroid of a building Feature, or None if unparseable."""
    geom = feat.get("geometry") if isinstance(feat, dict) else None
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon" and isinstance(coords, list) and coords:
        return _ring_centroid(coords[0])
    if gtype == "MultiPolygon" and isinstance(coords, list):
        # One centroid per Feature regardless of part count, so a split
        # building takes the centroid of its largest ring by shoelace area.
        best: tuple[float, float] | None = None
        best_area = -1.0
        for poly in coords:
            if not isinstance(poly, list) or not poly:
                continue
            c = _ring_centroid(poly[0])
            if c is None:
                continue
            ring = poly[0]
            a2 = 0.0
            n = len(ring) - 1 if (len(ring) > 1 and ring[0] == ring[-1]) else len(ring)
            for i in range(n):
                x0, y0 = ring[i][0], ring[i][1]
                x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                a2 += abs(x0 * y1 - x1 * y0)
            if a2 > best_area:
                best_area = a2
                best = c
        return best
    if gtype == "Point" and isinstance(coords, list) and len(coords) >= 2:
        return float(coords[0]), float(coords[1])
    return None




def _build_density_grid(
    centroids_lonlat: Iterable[tuple[float, float]],
    bbox: tuple[float, float, float, float],
    cell_size_m: float,
):
    """``(array, transform, crs, height, width)``: centroid counts binned north-up
    onto EPSG:3857, whose cell covers 1.13-1.41x ``cell_size_m`` at 28-45 deg lat.
    """
    import numpy as np
    from pyproj import Transformer
    from rasterio.transform import from_bounds

    min_lon, min_lat, max_lon, max_lat = bbox

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    sw_x, sw_y = transformer.transform(min_lon, min_lat)
    ne_x, ne_y = transformer.transform(max_lon, max_lat)
    if sw_x > ne_x:
        sw_x, ne_x = ne_x, sw_x
    if sw_y > ne_y:
        sw_y, ne_y = ne_y, sw_y

    # The extent snaps outward to a whole number of cells; without it the edge
    # is off by up to a cell and the boundary pixel counts are distorted.
    width = max(1, int(math.ceil((ne_x - sw_x) / cell_size_m)))
    height = max(1, int(math.ceil((ne_y - sw_y) / cell_size_m)))
    ne_x_snapped = sw_x + width * cell_size_m
    ne_y_snapped = sw_y + height * cell_size_m

    transform = from_bounds(sw_x, sw_y, ne_x_snapped, ne_y_snapped, width, height)

    arr = np.zeros((height, width), dtype=np.float32)

    for lon, lat in centroids_lonlat:
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        # A tile overlaps the bbox at quadkey resolution, so it carries
        # centroids that fall outside the requested area.
        if lon < min_lon or lon > max_lon or lat < min_lat or lat > max_lat:
            continue
        cx, cy = transformer.transform(lon, lat)
        col = int((cx - sw_x) / cell_size_m)
        row = int((ne_y_snapped - cy) / cell_size_m)
        if 0 <= row < height and 0 <= col < width:
            arr[row, col] += 1.0

    return arr, transform, "EPSG:3857", height, width




def _fetch_building_density_bytes(
    bbox: tuple[float, float, float, float],
    cell_size_m: float,
    source: str,
) -> bytes:
    """Fetch the tiles, rasterize their centroids and return CRS-tagged LZW GeoTIFF
    bytes; a bbox outside coverage yields an empty raster, not an error.
    """
    import rasterio

    if source not in _VALID_SOURCES:
        raise BuildingDensityInputError(
            f"unsupported source={source!r}; allowed: {sorted(_VALID_SOURCES)}"
        )

    quadkeys = _quadkeys_for_bbox(bbox, zoom=_MS_QUADKEY_ZOOM)
    logger.info(
        "compute_building_density: bbox=%s intersects %d quadkey(s) at zoom-%d",
        bbox,
        len(quadkeys),
        _MS_QUADKEY_ZOOM,
    )

    qk_to_urls = _index_for_quadkeys(quadkeys)
    missing = [qk for qk in quadkeys if qk not in qk_to_urls]
    if missing and len(missing) == len(quadkeys):
        logger.warning(
            "compute_building_density: bbox=%s -- every quadkey absent from MS index "
            "(international or ocean coverage gap; emitting empty density raster)",
            bbox,
        )
    logger.info(
        "compute_building_density: %d/%d quadkeys present in MS index",
        len(qk_to_urls),
        len(quadkeys),
    )

    centroids: list[tuple[float, float]] = []
    for qk, urls in qk_to_urls.items():
        # A border quadkey appears under several regions and each region's tile
        # owns distinct buildings, so every URL is downloaded.
        for url in urls:
            feats = _download_tile_features(url)
            for feat in feats:
                c = _feature_centroid(feat)
                if c is not None:
                    centroids.append(c)

    logger.info(
        "compute_building_density: collected %d building centroid(s) across %d tile(s)",
        len(centroids),
        sum(len(u) for u in qk_to_urls.values()),
    )

    arr, transform, crs, height, width = _build_density_grid(
        centroids, bbox, cell_size_m
    )

    profile: dict[str, object] = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    # A raster smaller than one 256x256 block cannot be tiled; rasterio also
    # enforces a multiple-of-16 constraint and would warn.
    if width < 256 or height < 256:
        profile["tiled"] = False
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)

    out_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_building_density_"
        ) as f:
            out_tmp = f.name
        with rasterio.open(out_tmp, "w", **profile) as dst:
            dst.write(arr, 1)
            dst.update_tags(
                source=source,
                source_class="building_density",
                tool="compute_building_density",
                bbox=str(bbox),
                cell_size_m=str(cell_size_m),
                building_centroids_total=str(len(centroids)),
                quadkey_zoom=str(_MS_QUADKEY_ZOOM),
                quadkeys_total=str(len(quadkeys)),
                quadkeys_present_in_index=str(len(qk_to_urls)),
                units="buildings_per_cell",
                grid_crs="EPSG:3857",
            )
        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass




@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (reads input raster/vector; writes cache
    # artifact only via the read-through shim), openWorldHint=False (all
    # computation is local GDAL/numpy; no external API calls),
    # destructiveHint=False, idempotentHint=True (deterministic transform;
    # same inputs always produce the same output pixels).
)
def compute_building_density(
    bbox: tuple[float, float, float, float],
    cell_size_m: float = 100.0,
    source: str = "ms_footprints",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Building density raster (count-per-cell) from Microsoft Global ML Building Footprints.

    Use this when: the user wants "building density"/"urban density"/"how
    developed is this area" as a raster, or exposure analysis needs a
    buildings-per-cell signal to normalize hazard layers. Do NOT use for:
    individual footprint polygons/heights (``fetch_buildings``); a
    population-density proxy (``fetch_hrsl_population``); precise
    admin-polygon counts (use ``fetch_buildings`` + zonal stats instead --
    the raster grid can drift ~1 cell at bbox edges).

    Params:
        bbox: (min_lon, min_lat, max_lon, max_lat) EPSG:4326.
        cell_size_m: grid cell size in metres on EPSG:3857 (default 100,
            suggested range 25-500; sub-25 mostly empty outside dense cores).
        source: only ``"ms_footprints"`` supported (v0.1).

    Returns:
        ``LayerURI`` for a float32 LZW GeoTIFF (cache bucket, TTL 30d,
        EPSG:3857; units semantic ``"buildings_per_cell"``).
    """
    if not isinstance(bbox, (tuple, list)):
        raise BuildingDensityInputError(
            f"bbox must be a tuple/list of 4 floats; got {type(bbox).__name__}"
        )
    bbox_t: tuple[float, float, float, float] = tuple(bbox)  # type: ignore[assignment]
    _validate_bbox(bbox_t)
    if not math.isfinite(cell_size_m) or cell_size_m <= 0:
        raise BuildingDensityInputError(
            f"cell_size_m must be positive and finite; got {cell_size_m!r}"
        )
    if source not in _VALID_SOURCES:
        raise BuildingDensityInputError(
            f"unsupported source={source!r}; allowed: {sorted(_VALID_SOURCES)}"
        )

    q_bbox = _round_bbox_to_6dp(bbox_t)

    params = {
        "bbox": list(q_bbox),
        "cell_size_m": float(cell_size_m),
        "source": source,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_building_density_bytes(q_bbox, float(cell_size_m), source),
    )
    assert result.uri is not None, (
        "compute_building_density is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=f"building-density-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{int(cell_size_m)}m",
        name=f"Building Density (MS Global ML; {int(cell_size_m)} m cells)",
        layer_type="raster",
        uri=result.uri,
        style={"kind": "continuous"},
        role="context",
        units=None,
        bbox=q_bbox,
    )
