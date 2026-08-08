"""ESRI-World-Imagery mesh-proof renderer for the ADR 0192 mesh-front sandbox.

SANDBOX ONLY. Renders a coastal TIN wireframe over ESRI World Imagery satellite
tiles to the project proof norms: white box = AOI extent only, wireframe a single
colour (element-size gradation reads through the mesh density), a caption strip
stating the sizing settings + element count + resolution range.

Self-contained: fetches ESRI World Imagery XYZ tiles directly
(server.arcgisonline.com World_Imagery MapServer) with urllib + PIL, reprojects
the lon/lat mesh to Web Mercator (EPSG:3857), and composites with matplotlib.
"""

from __future__ import annotations

import io
import math
import urllib.request

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

_R = 6378137.0
_TILE = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


def _ll_to_merc(lon, lat):
    x = _R * np.radians(lon)
    y = _R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
    return x, y


def _lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    xt = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    yt = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return xt, yt


def _tile_merc_bounds(x, y, z):
    n = 2 ** z
    lon1 = x / n * 360.0 - 180.0
    lon2 = (x + 1) / n * 360.0 - 180.0
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat2 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    xa, ya = _ll_to_merc(lon1, lat1)
    xb, yb = _ll_to_merc(lon2, lat2)
    return xa, xb, ya, yb


def _pick_zoom(bbox, target_tiles=4):
    xmin, ymin, xmax, ymax = bbox
    for z in range(16, 5, -1):
        x0, _ = _lonlat_to_tile(xmin, ymax, z)
        x1, _ = _lonlat_to_tile(xmax, ymin, z)
        _, y0 = _lonlat_to_tile(xmin, ymax, z)
        _, y1 = _lonlat_to_tile(xmax, ymin, z)
        if (abs(x1 - x0) <= 8) and (abs(y1 - y0) <= 8):
            return z
    return 10


def _fetch_basemap(bbox, zoom):
    xmin, ymin, xmax, ymax = bbox
    xt0 = int(math.floor(_lonlat_to_tile(xmin, ymax, zoom)[0]))
    xt1 = int(math.floor(_lonlat_to_tile(xmax, ymin, zoom)[0]))
    yt0 = int(math.floor(_lonlat_to_tile(xmin, ymax, zoom)[1]))
    yt1 = int(math.floor(_lonlat_to_tile(xmax, ymin, zoom)[1]))
    xa = min(xt0, xt1)
    xb = max(xt0, xt1)
    ya = min(yt0, yt1)
    yb = max(yt0, yt1)
    cols = xb - xa + 1
    rows = yb - ya + 1
    mosaic = Image.new("RGB", (cols * 256, rows * 256))
    for j, ty in enumerate(range(ya, yb + 1)):
        for i, tx in enumerate(range(xa, xb + 1)):
            url = _TILE.format(z=zoom, x=tx, y=ty)
            req = urllib.request.Request(url, headers={"User-Agent": "trid3nt-mesh-sandbox"})
            with urllib.request.urlopen(req, timeout=30) as r:
                tile = Image.open(io.BytesIO(r.read())).convert("RGB")
            mosaic.paste(tile, (i * 256, j * 256))
    left, _, _, top = _tile_merc_bounds(xa, ya, zoom)
    _, right, bottom, _ = _tile_merc_bounds(xb, yb, zoom)
    return mosaic, (left, right, bottom, top)


def render(
    points: np.ndarray,
    cells: np.ndarray,
    bbox,
    out_path,
    *,
    aoi_name: str,
    caption: str,
) -> str:
    """Render mesh over ESRI World Imagery. ``points`` lon/lat, ``cells`` (M,3)."""
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    # Fetch tiles for a padded geographic bbox so the basemap fully covers the
    # padded view (no white margins), then frame to that same padded box.
    plon = (bbox[2] - bbox[0]) * 0.05
    plat = (bbox[3] - bbox[1]) * 0.05
    fetch_bbox = (bbox[0] - plon, bbox[1] - plat, bbox[2] + plon, bbox[3] + plat)
    zoom = _pick_zoom(fetch_bbox)
    basemap, (left, right, bottom, top) = _fetch_basemap(fetch_bbox, zoom)

    mx, my = _ll_to_merc(points[:, 0], points[:, 1])
    bx0, by0 = _ll_to_merc(bbox[0], bbox[1])
    bx1, by1 = _ll_to_merc(bbox[2], bbox[3])
    xlo, yl0 = _ll_to_merc(fetch_bbox[0], fetch_bbox[1])
    xhi, yh0 = _ll_to_merc(fetch_bbox[2], fetch_bbox[3])
    ylo, yhi = yl0, yh0

    # Match the figure aspect to the AOI so imshow(aspect='equal') fills the map
    # axes with no white letterboxing; a fixed caption strip sits underneath.
    map_w = 10.0
    aspect = (yhi - ylo) / (xhi - xlo)
    map_h = float(np.clip(map_w * aspect, 4.0, 15.0))
    cap_h = 1.7
    fig = plt.figure(figsize=(map_w, map_h + cap_h))
    fig.patch.set_facecolor("#111111")
    ax = fig.add_axes([0.0, cap_h / (map_h + cap_h), 1.0, map_h / (map_h + cap_h)])
    ax.imshow(np.asarray(basemap), extent=[left, right, bottom, top], origin="upper")
    ax.triplot(mx, my, cells, color="#00e5ff", linewidth=0.4, alpha=0.9)
    ax.plot([bx0, bx1, bx1, bx0, bx0], [by0, by0, by1, by1, by0],
            color="white", linewidth=2.4)
    ax.set_xlim(xlo, xhi)
    ax.set_ylim(ylo, yhi)
    ax.set_aspect("auto")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(f"OceanMesh2D coastal TIN  -  {aoi_name}", color="white",
                 fontsize=14, pad=6)

    cap = fig.add_axes([0.0, 0.0, 1.0, cap_h / (map_h + cap_h)])
    cap.axis("off")
    cap.text(0.012, 0.5, caption, fontsize=10, family="monospace",
             color="white", va="center", ha="left", transform=cap.transAxes)
    fig.savefig(out_path, dpi=130, facecolor="#111111")
    plt.close(fig)
    return str(out_path)
