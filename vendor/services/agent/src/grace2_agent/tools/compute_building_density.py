"""``compute_building_density`` atomic tool — Microsoft Global ML Building Footprints density raster (job-0096).

Fetches building footprints from Microsoft's Global ML Building Footprints
dataset and rasterizes building centroids onto a regular grid in EPSG:3857
(Web Mercator, metric), producing a float32 count-per-cell COG.

Strategy v0.1 (audit.md):

- Source: Microsoft Global ML Building Footprints — published as a CSV-indexed
  set of GeoJSONL tiles (despite ``.csv.gz`` extension, each line is a
  GeoJSON Feature) keyed by Bing-style zoom-9 quadkey. Static dataset, refreshed
  by Microsoft on an irregular cadence (most recent ~2026-02-03 at time of
  authoring).
- Per-call: compute the set of zoom-9 quadkeys intersecting the bbox; resolve
  each (RegionName, quadkey) → URL via the CSV index; download + parse the
  GeoJSONL features; rasterize building centroids onto a grid at the requested
  ``cell_size_m`` in EPSG:3857.
- Output: float32 single-band COG, value = count of building centroids whose
  centroid falls inside each cell. Cells with no buildings carry the value 0
  (NOT nodata — the absence of buildings is real signal in a density product).
- ``cache_key = (source, bbox-rounded-6dp, cell_size_m)``. ``ttl_class=static-30d``,
  ``source_class=building_density``. Cache prefix:
  ``cache/static-30d/building_density/<key>.tif``.

Index endpoint (verified 2026-06-08):
    https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv

Format: ``Location,QuadKey,Url,Size,UploadDate`` per row. Each tile URL points
at a gzipped CSV file whose rows are raw GeoJSON Feature strings (the same
``geojsonl`` format despite the ``.csv.gz`` extension Microsoft chose).

International coverage (OQ-96-INTL-COVERAGE):
    The index covers >200 ``Location`` entries globally (Africa, Asia, Europe,
    Oceania, Americas). For a non-CONUS bbox we still emit quadkeys and look
    them up — the index returns whatever region(s) the quadkey lives under
    (e.g. "Canada" for a Vancouver bbox, "Mexico" for Baja). When no row
    matches a requested quadkey we treat that tile as "0 buildings" rather
    than erroring out — coverage gaps are legitimately empty.

Codified job-0086 lesson (geographic correctness):
    The acceptance test for this tool MUST assert the density signal is high
    where Fort Myers actually has dense buildings and low over the river/ocean
    pixels in the same COG — not merely that the COG round-trips bytes.
    See ``tests/test_compute_building_density.py::test_geographic_correctness_*``.

FR-TA-2: atomic tool, returns ``LayerURI``. FR-CE-8 / FR-DC-3/4: routed through
``read_through`` so identical ``(bbox, cell_size_m, source)`` calls reuse the
cached COG. Tier-1 free (no API key required).
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

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["compute_building_density"]

logger = logging.getLogger("grace2_agent.tools.compute_building_density")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: The Microsoft Global ML Building Footprints CSV index URL.
_MS_INDEX_URL = (
    "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
)

#: Native zoom level of the Microsoft tile pyramid.
_MS_QUADKEY_ZOOM = 9

#: User-Agent — Microsoft hosts on Azure Storage which doesn't strictly enforce
#: a UA, but be polite.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

_VALID_SOURCES = frozenset({"ms_footprints"})

#: Module-level cache of the parsed CSV index. The full index is ~7 MB
#: (5 columns × ~150k rows) and download is the most expensive single step;
#: re-fetching it per call would defeat the per-bbox cache.
_INDEX_CACHE: dict[str, list[str]] | None = None
_INDEX_CACHE_DOWNLOAD_BYTES: int = 0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compute_building_density",
    ttl_class="static-30d",
    source_class="building_density",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


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
    # Microsoft Web Mercator pyramid is undefined above ~85.05113° lat; clamp.
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


# ---------------------------------------------------------------------------
# Quadkey math (Bing tile system).
#
# Derived from the canonical Microsoft Bing maps Tile System spec
# (https://learn.microsoft.com/en-us/bingmaps/articles/bing-maps-tile-system).
# We re-implement here (a few short functions) so we don't add a dependency
# for a one-off operation.
# ---------------------------------------------------------------------------


def _lonlat_to_tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Convert lon/lat to Bing tile (x, y) at the given zoom.

    Clamps latitude to the Mercator-valid range internally. Returns the integer
    tile coordinates.
    """
    # Clamp to avoid singularities near the poles.
    lat_clamped = max(-85.05112878, min(85.05112878, lat))
    sin_lat = math.sin(lat_clamped * math.pi / 180.0)
    x = (lon + 180.0) / 360.0
    y = 0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)
    map_size = 1 << zoom  # 2 ** zoom
    tx = int(min(map_size - 1, max(0, math.floor(x * map_size))))
    ty = int(min(map_size - 1, max(0, math.floor(y * map_size))))
    return tx, ty


def _tile_xy_to_quadkey(tx: int, ty: int, zoom: int) -> str:
    """Convert tile (x, y, z) to Bing quadkey string.

    Each level of the quadkey is one of {'0','1','2','3'} per Bing spec.
    """
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
    """Return the set of quadkeys at ``zoom`` covering ``bbox``.

    Iterates the rectangle of tiles spanned by the bbox corners and returns
    every quadkey within (inclusive on both bounds). For a typical ≤1° bbox
    at zoom-9 this is 1-4 tiles; for a state-sized bbox 10-40.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    # NOTE: in Bing Web Mercator, tile (0,0) is at the NW corner, so the y
    # coordinate decreases as latitude *increases*. We thus compute tile-x for
    # the lon range and tile-y for the lat range, then iterate the rectangle.
    tx_min, ty_max = _lonlat_to_tile_xy(min_lon, min_lat, zoom)  # SW → bottom-left of tile box
    tx_max, ty_min = _lonlat_to_tile_xy(max_lon, max_lat, zoom)  # NE → top-right of tile box
    if tx_min > tx_max:
        tx_min, tx_max = tx_max, tx_min
    if ty_min > ty_max:
        ty_min, ty_max = ty_max, ty_min
    quadkeys: list[str] = []
    for ty in range(ty_min, ty_max + 1):
        for tx in range(tx_min, tx_max + 1):
            quadkeys.append(_tile_xy_to_quadkey(tx, ty, zoom))
    return quadkeys


# ---------------------------------------------------------------------------
# CSV index — fetch + parse + per-quadkey lookup.
# ---------------------------------------------------------------------------


def _fetch_index() -> dict[str, list[str]]:
    """Fetch + parse the Microsoft global-buildings dataset-links.csv index.

    Returns a dict ``{quadkey: [url, ...]}``. A single quadkey CAN appear under
    multiple (RegionName) rows in border areas, so the value is a list of all
    URLs for that quadkey. We collect all of them and deduplicate by URL.

    Cached at module level for the lifetime of the process — the index is
    static for many days and re-fetching it per call would dominate the
    per-bbox runtime. To force a refresh (e.g. in a long-lived process), set
    ``_INDEX_CACHE = None`` (only the tool itself does this; user code does
    not).
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
    # Deduplicate URL lists in place.
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
    """Return ``{quadkey: [url, ...]}`` for the subset of ``quadkeys`` present.

    A quadkey absent from the index legitimately indicates a tile with no
    detected buildings (ocean, ice cap, unmapped region) — the caller treats
    that as a zero-count tile, not an error.
    """
    full = _fetch_index()
    return {qk: full[qk] for qk in quadkeys if qk in full}


# ---------------------------------------------------------------------------
# Tile download + GeoJSONL streaming.
# ---------------------------------------------------------------------------


def _download_tile_features(url: str) -> list[dict]:
    """Download a single ``.csv.gz`` GeoJSONL tile and return its features.

    The file is gzipped; each line of the decompressed body is a complete
    GeoJSON Feature (despite the ``.csv.gz`` extension Microsoft uses —
    historically the file was once delivered with CSV columns; the current
    format is line-delimited GeoJSON).

    Raises ``BuildingDensityUpstreamError`` on network or parse failure.
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
            # Be tolerant of one bad line — Microsoft's tile generator has
            # historically emitted occasional partial lines. Log and move on.
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


# ---------------------------------------------------------------------------
# Geometry helpers — ring centroid in lon/lat.
# ---------------------------------------------------------------------------


def _ring_centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    """Return the lon/lat centroid of a polygon ring using the shoelace formula.

    The ring is assumed to be the outer ring of a polygon, in (lon, lat)
    order, possibly closed (first vertex repeated as last). Returns None if
    the ring has fewer than 3 vertices.

    Numerical care: building footprints are typically <100 m wide but their
    lon/lat coordinates have magnitudes around 80-150. A naive shoelace on
    raw coordinates suffers catastrophic cancellation — the cross product
    ``x0*y1 - x1*y0`` is the difference of two near-equal large numbers.
    We mitigate by shifting all vertices to a local origin (the first
    vertex) before applying the formula, then shifting the resulting
    centroid back. This keeps the cross-product magnitudes ~1e-8 (10m at
    1e-4° per metre) instead of ~1e4, which is well within float64
    precision.

    For density rasterization we don't need geographic-area exactness — a
    cartesian centroid in lon/lat is consistent across the bbox (we then
    project the centroid into Web Mercator and bin it onto the grid).
    """
    if not ring or len(ring) < 3:
        return None
    # Drop closing vertex if present.
    pts = ring[:-1] if (len(ring) > 1 and ring[0] == ring[-1]) else ring
    if len(pts) < 3:
        # Fall back to the mean of available points so a tiny / degenerate
        # polygon still contributes one centroid rather than vanishing.
        n = len(pts) or 1
        return (
            sum(p[0] for p in pts) / n,
            sum(p[1] for p in pts) / n,
        )

    # Shift to local origin (first vertex) to avoid catastrophic cancellation
    # when the polygon is small relative to its coordinate magnitudes.
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
        # Degenerate ring — fall back to vertex mean (in original coords).
        return (
            sum(p[0] for p in pts) / n,
            sum(p[1] for p in pts) / n,
        )
    cx /= 3.0 * area2
    cy /= 3.0 * area2
    # Shift back from local origin.
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
        # Pick the largest ring's centroid by absolute shoelace area — for
        # MS buildings MultiPolygons are vanishingly rare but possible (a
        # building split by a railway etc.). We want one centroid per
        # building Feature regardless.
        best: tuple[float, float] | None = None
        best_area = -1.0
        for poly in coords:
            if not isinstance(poly, list) or not poly:
                continue
            c = _ring_centroid(poly[0])
            if c is None:
                continue
            # Shoelace area on the outer ring.
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


# ---------------------------------------------------------------------------
# Density grid construction.
# ---------------------------------------------------------------------------


def _build_density_grid(
    centroids_lonlat: Iterable[tuple[float, float]],
    bbox: tuple[float, float, float, float],
    cell_size_m: float,
):
    """Return ``(array, transform, crs, height, width)``.

    Bins building centroids into a regular grid in EPSG:3857 (Web Mercator)
    covering ``bbox``. Cell value = count of centroids whose centroid lies
    inside the cell. ``transform`` is north-up (positive ``a``, negative ``e``).

    The choice of EPSG:3857 over a local UTM is deliberate (audit.md):
    Microsoft data ships in lon/lat covering a global grid, and emitting the
    density in Web Mercator (the same projection QGIS Server WMS serves under
    EPSG:3857) lets the QGIS Server / web client display the layer without
    reprojection. The metric is "buildings per (cell_size_m × cell_size_m)
    cell on the Web Mercator grid" — at temperate US latitudes (~28-45°N) the
    Web Mercator scale distortion is ~1.13-1.41x, so the cell footprint on
    the ground is slightly larger than ``cell_size_m`` square. We note this
    in the docstring rather than reprojecting to UTM (which would require a
    UTM-zone choice per bbox).
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

    # Snap the bbox extent outward to an integer number of cells. This is
    # important: a cell_size of e.g. 100m would otherwise be off by up to
    # ~one cell at each edge, distorting the boundary pixel counts.
    width = max(1, int(math.ceil((ne_x - sw_x) / cell_size_m)))
    height = max(1, int(math.ceil((ne_y - sw_y) / cell_size_m)))
    ne_x_snapped = sw_x + width * cell_size_m
    ne_y_snapped = sw_y + height * cell_size_m

    # rasterio.transform.from_bounds is north-up: row 0 at the top (north).
    transform = from_bounds(sw_x, sw_y, ne_x_snapped, ne_y_snapped, width, height)

    arr = np.zeros((height, width), dtype=np.float32)

    for lon, lat in centroids_lonlat:
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        # Skip centroids outside the bbox — they came from tiles that overlap
        # the bbox at quadkey resolution but fall outside the actual area.
        if lon < min_lon or lon > max_lon or lat < min_lat or lat > max_lat:
            continue
        # Project centroid into EPSG:3857.
        cx, cy = transformer.transform(lon, lat)
        # Column from western edge.
        col = int((cx - sw_x) / cell_size_m)
        # Row from northern edge (row 0 is north).
        row = int((ne_y_snapped - cy) / cell_size_m)
        if 0 <= row < height and 0 <= col < width:
            arr[row, col] += 1.0

    return arr, transform, "EPSG:3857", height, width


# ---------------------------------------------------------------------------
# Core fetch + rasterize.
# ---------------------------------------------------------------------------


def _fetch_building_density_bytes(
    bbox: tuple[float, float, float, float],
    cell_size_m: float,
    source: str,
) -> bytes:
    """Fetch tiles, rasterize centroids, return COG bytes for the bbox.

    Single source supported for v0.1 (``"ms_footprints"``). Surfaces:
    - OQ-96-INTL-COVERAGE if the bbox falls fully outside Microsoft's coverage.
    - BuildingDensityUpstreamError for index / tile network failures.

    Returns the raw bytes of a CRS-tagged, LZW-compressed, tiled GeoTIFF
    suitable for QGIS Server WMS publishing.
    """
    import rasterio

    if source not in _VALID_SOURCES:
        raise BuildingDensityInputError(
            f"unsupported source={source!r}; allowed: {sorted(_VALID_SOURCES)}"
        )

    # 1. Compute intersecting quadkeys at zoom-9.
    quadkeys = _quadkeys_for_bbox(bbox, zoom=_MS_QUADKEY_ZOOM)
    logger.info(
        "compute_building_density: bbox=%s intersects %d quadkey(s) at zoom-%d",
        bbox,
        len(quadkeys),
        _MS_QUADKEY_ZOOM,
    )

    # 2. Resolve quadkeys → URLs via the index. Missing keys = empty tiles.
    qk_to_urls = _index_for_quadkeys(quadkeys)
    missing = [qk for qk in quadkeys if qk not in qk_to_urls]
    if missing and len(missing) == len(quadkeys):
        # Every tile is absent — bbox is outside MS coverage.
        logger.warning(
            "compute_building_density: bbox=%s — every quadkey absent from MS index "
            "(international or ocean coverage gap; emitting empty density raster)",
            bbox,
        )
    logger.info(
        "compute_building_density: %d/%d quadkeys present in MS index",
        len(qk_to_urls),
        len(quadkeys),
    )

    # 3. Download tiles and collect centroids.
    centroids: list[tuple[float, float]] = []
    for qk, urls in qk_to_urls.items():
        # One quadkey can appear in multiple regions in border areas. We
        # download each region's URL — duplicates may exist but the in-bbox
        # filter in _build_density_grid is exact enough to avoid double-
        # counting in practice (each region's tile owns distinct buildings).
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

    # 4. Build the density grid.
    arr, transform, crs, height, width = _build_density_grid(
        centroids, bbox, cell_size_m
    )

    # 5. Write LZW-compressed tiled GeoTIFF (COG-friendly).
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
    # Drop tiling if the raster is too small for a 256x256 block; rasterio
    # also enforces a multiple-of-16 constraint and would otherwise warn.
    if width < 256 or height < 256:
        profile["tiled"] = False
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)

    out_tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="grace2_building_density_"
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


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


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
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Building density raster from Microsoft Global ML Building Footprints.

    **What it does:** Fetches building footprints from Microsoft's Global ML
    Building Footprints dataset (Bing zoom-9 quadkey tiles), rasterizes building
    centroids onto a regular EPSG:3857 grid at the requested cell size, and
    returns a float32 count-per-cell COG via the 30-day cache. Cell values equal
    the count of building centroids whose centroid falls inside each cell.

    **When to use:**
    - User asks for "building density", "urban density", or "how developed is
      this area?" as a raster visualization.
    - Exposure analysis needs a spatial density signal (buildings per unit area)
      to normalize hazard layers (e.g. "buildings per cell under flood
      inundation").
    - Downstream impact computation (Pelicun, loss curves) requires a
      rasterized count of structures rather than individual vector footprints.
    - Any flood-impact or wildfire-exposure map that needs built-area context
      over a large area where vector footprints would be too numerous.

    **When NOT to use:**
    - DO NOT use when individual footprint polygons, building heights, or
      parcel-level attributes are needed — use ``fetch_buildings`` for the
      vector footprint path instead.
    - DO NOT use as a proxy for population density — buildings per cell is
      not residents per cell; use ``fetch_hrsl_population`` or
      ``fetch_population`` for resident counts.
    - DO NOT use for precise administrative-polygon counts — the raster grid
      can drift by ~1 cell at bbox edges; use a vector + zonal-statistics
      pipeline (``fetch_buildings`` + ``compute_zonal_statistics``) for
      authoritative totals.

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(min_lon, min_lat,
      max_lon, max_lat)`` in EPSG:4326. Web-Mercator valid range [-85.05,
      85.05] latitude. Example for Cape Coral FL: ``(-81.9, 26.5, -81.8, 26.6)``.
    - ``cell_size_m`` (float, default 100.0): grid cell size in metres on
      EPSG:3857. Suggested range 25-500 m; sub-25 m produces mostly-empty
      rasters outside dense city cores. Note: EPSG:3857 distorts ground
      footprint by 1.13x at 28 N to 1.41x at 45 N.
    - ``source`` (str, default ``"ms_footprints"``): v0.1 supports only
      ``"ms_footprints"`` (Microsoft Global ML Building Footprints, >200
      country/region entries, latest vintage ~2026-02-03).

    **Returns:** A ``LayerURI`` pointing at a float32, LZW-compressed,
    tiled GeoTIFF in the cache bucket
    (``gs://grace-2-hazard-prod-cache/cache/static-30d/building_density/<key>.tif``).
    ``layer_type="raster"``, ``role="context"``, ``units=None`` (semantic
    is ``"buildings_per_cell"`` encoded in the GeoTIFF ``units`` tag).
    Output CRS: EPSG:3857.

    **Cross-tool dependencies:**
    - Alternative: ``fetch_buildings`` for individual vector footprints;
      ``fetch_hrsl_population`` for resident count.
    - Downstream: ``compute_zonal_statistics`` can aggregate this raster over
      an admin polygon; Pelicun exposure pipelines consume the count raster.
    - Typically requested after ``geocode_location`` to establish the bbox.

    FR-CE-8: ``read_through`` with ``ttl_class="static-30d"``; cache key is
    SHA-256 over ``(bbox-rounded-6dp, cell_size_m, source)``.
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
        style_preset="building_density",
        role="context",
        units=None,
        bbox=q_bbox,
    )
