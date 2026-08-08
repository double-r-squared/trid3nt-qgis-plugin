"""Shared ESRI-World-Imagery tile + Web-Mercator primitives for the mesh-proof
renderers (STANDALONE sandbox).

Single source of truth for the basemap math used by every mesh/watershed proof
render (build_coastal_mesh via render_mesh, build_coastal_water_edge_mesh,
build_watershed_mesh, and pysheds_watershed/proof_watershed). Each renderer keeps
its own matplotlib composition but MUST get its tiles + extent from ``fetch_basemap``
so the imagery placement is defined in exactly one place.

CRS: ESRI World_Imagery tiles are spherical Web Mercator (EPSG:3857). Meshes are
EPSG:4326 lon/lat and are projected to the SAME spherical mercator via
``ll_to_merc`` before plotting, so imagery and mesh share one coordinate frame.
"""

from __future__ import annotations

import io
import math
import urllib.request

import numpy as np
from PIL import Image

LL_R = 6378137.0  # spherical Web-Mercator radius (EPSG:3857)
_TILE = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


def ll_to_merc(lon, lat):
    """EPSG:4326 lon/lat -> EPSG:3857 x/y (metres). Scalar or numpy array."""
    return LL_R * np.radians(lon), LL_R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def lonlat_to_tile(lon, lat, z):
    """lon/lat -> fractional slippy-map tile index at zoom ``z``."""
    n = 2 ** z
    lat_r = math.radians(lat)
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)


def tile_merc_bounds(x, y, z):
    """Mercator bounds of tile (x, y, z) as (west, east, north, south) metres.

    Tile y grows southward, so ``north`` (3rd) is the y-tile's top edge and
    ``south`` (4th) is its bottom edge.
    """
    n = 2 ** z
    lon_w, lon_e = x / n * 360.0 - 180.0, (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    west, north = ll_to_merc(lon_w, lat_n)
    east, south = ll_to_merc(lon_e, lat_s)
    return west, east, north, south


def pick_zoom(bbox, max_tiles=8, zmax=17, zmin=6, fallback=11):
    """Highest zoom whose tile span stays within ``max_tiles`` per axis."""
    xmin, ymin, xmax, ymax = bbox
    for z in range(zmax, zmin - 1, -1):
        x0, y0 = lonlat_to_tile(xmin, ymax, z)
        x1, y1 = lonlat_to_tile(xmax, ymin, z)
        if abs(x1 - x0) <= max_tiles and abs(y1 - y0) <= max_tiles:
            return z
    return fallback


def fetch_basemap(bbox, zoom, user_agent="trid3nt-mesh"):
    """Fetch + mosaic ESRI World Imagery tiles covering ``bbox`` at ``zoom``.

    Returns (PIL.Image, (left, right, bottom, top)) where the extent is the OUTER
    mercator edge of the mosaic -- north tile's north edge, south tile's south
    edge -- so ``imshow(extent=...)`` places every pixel at its true mercator
    position. Selecting the inner tile edges here vertically compresses the
    imagery and is the classic mesh-vs-basemap misalignment bug.
    """
    xmin, ymin, xmax, ymax = bbox
    xt0 = int(math.floor(lonlat_to_tile(xmin, ymax, zoom)[0]))
    xt1 = int(math.floor(lonlat_to_tile(xmax, ymin, zoom)[0]))
    yt0 = int(math.floor(lonlat_to_tile(xmin, ymax, zoom)[1]))
    yt1 = int(math.floor(lonlat_to_tile(xmax, ymin, zoom)[1]))
    xa, xb = min(xt0, xt1), max(xt0, xt1)
    ya, yb = min(yt0, yt1), max(yt0, yt1)  # ya = north-most tile row, yb = south-most
    mosaic = Image.new("RGB", ((xb - xa + 1) * 256, (yb - ya + 1) * 256))
    for j, ty in enumerate(range(ya, yb + 1)):
        for i, tx in enumerate(range(xa, xb + 1)):
            url = _TILE.format(z=zoom, x=tx, y=ty)
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=30) as rsp:
                tile = Image.open(io.BytesIO(rsp.read())).convert("RGB")
            mosaic.paste(tile, (i * 256, j * 256))
    left, _, top, _ = tile_merc_bounds(xa, ya, zoom)   # north tile: west edge + NORTH edge
    _, right, _, bottom = tile_merc_bounds(xb, yb, zoom)  # south tile: east edge + SOUTH edge
    return mosaic, (left, right, bottom, top)
