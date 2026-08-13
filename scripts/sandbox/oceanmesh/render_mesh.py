"""ESRI-World-Imagery mesh-proof renderer for the ADR 0192 mesh-front sandbox.

SANDBOX ONLY. Renders a coastal TIN wireframe over ESRI World Imagery satellite
tiles to the project proof norms: white box = AOI extent only, wireframe a single
colour (element-size gradation reads through the mesh density), a caption strip
stating the sizing settings + element count + resolution range.

Tiles + Web-Mercator math come from merc_render (single source of truth); the
lon/lat mesh is reprojected to EPSG:3857 to share the imagery's frame.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from merc_render import fetch_basemap, ll_to_merc, pick_zoom  # noqa: E402


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
    zoom = pick_zoom(fetch_bbox, max_tiles=8, zmax=16, fallback=10)
    basemap, (left, right, bottom, top) = fetch_basemap(
        fetch_bbox, zoom, user_agent="trid3nt-mesh-sandbox"
    )

    mx, my = ll_to_merc(points[:, 0], points[:, 1])
    bx0, by0 = ll_to_merc(bbox[0], bbox[1])
    bx1, by1 = ll_to_merc(bbox[2], bbox[3])
    xlo, ylo = ll_to_merc(fetch_bbox[0], fetch_bbox[1])
    xhi, yhi = ll_to_merc(fetch_bbox[2], fetch_bbox[3])

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
