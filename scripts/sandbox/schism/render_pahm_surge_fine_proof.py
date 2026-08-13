"""SCHISM PaHM storm-surge proof render: FINE-AOI variant. Peak-surge COG over
ESRI World Imagery (EPSG:3857 tiles AND data) with Hurricane Ike best track
overlaid. Writes docs/proof/templates/schism_pahm_surge_fine.png as a NEW
sibling file - does NOT overwrite schism_pahm_surge.png.

Inputs: run 01KZSS2EJ962MFQ9YTRT1CMARY, case 01KZSS0PJXTA6534NJ847XGAGC -
schism_elev_max.tif at s3://trid3nt-runs/01KZSS2EJ962MFQ9YTRT1CMARY/.
Small explicit-resolution AOI [-95.05, 29.2, -94.6, 29.65], resolution_m=30
(user coarsening). Bathymetry = REAL NOAA NCEI CUDEM 1/9" (8 tiles intersect
this AOI, read + composited over the ETOPO 2022 shelf base) -- the resolution
doctrine (ADR 0224) fix: the prior render used ETOPO ~450 m because skip_cudem
was FORCED unconditionally, NOT because CUDEM omits this coast (it does not).
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts" / "sandbox" / "oceanmesh"))
import merc_render as MR  # noqa: E402

SCRATCH = Path(
    "/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
    "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/schism_surge_fine"
)
OUT = REPO / "docs" / "proof" / "templates"
COG = SCRATCH / "schism_elev_max.tif"

RUN_ID = "01KZSS2EJ962MFQ9YTRT1CMARY"
CASE_ID = "01KZSS0PJXTA6534NJ847XGAGC"
AOI = (-95.05, 29.2, -94.6, 29.65)
PEAK_SURGE_REPORTED_M = 2.86
RESOLUTION_M = 30

# Published Hurricane Ike (2008, bal092008) best track, verbatim from
# pahm_surge.py PUBLISHED_IKE_TRACK: (time_hr, lon, lat, pres_mb, wind_kt, rmw_nmi).
IKE_TRACK = (
    (0.0, -91.5, 26.6, 948.0, 95.0, 50.0),
    (12.0, -93.6, 28.1, 950.0, 80.0, 55.0),
    (18.0, -94.4, 28.8, 951.0, 80.0, 55.0),
    (24.0, -94.7, 29.3, 952.0, 80.0, 50.0),   # ~landfall Galveston
    (30.0, -95.4, 30.5, 964.0, 60.0, 60.0),
)
LANDFALL_IDX = 3

VMAX_PINNED = 3.0  # m, pinned scale per kickoff


def _reproject_to_merc(cog_path):
    with rasterio.open(cog_path) as src:
        dst_crs = "EPSG:3857"
        transform, w, h = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        arr = np.full((h, w), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1), destination=arr,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=transform, dst_crs=dst_crs,
            resampling=Resampling.bilinear, src_nodata=src.nodata, dst_nodata=np.nan,
        )
        left, top = transform * (0, 0)
        right, bottom = transform * (w, h)
        bounds_ll = src.bounds
    return arr, (left, right, bottom, top), bounds_ll


def render_map():
    arr, extent, bounds_ll = _reproject_to_merc(COG)
    bbox_ll = (bounds_ll.left, bounds_ll.bottom, bounds_ll.right, bounds_ll.top)
    pad_lon, pad_lat = 0.06, 0.06
    bbox_pad = (bbox_ll[0] - pad_lon, bbox_ll[1] - pad_lat,
                bbox_ll[2] + pad_lon, bbox_ll[3] + pad_lat)

    finite = arr[np.isfinite(arr)]
    peak_surge_m = float(finite.max()) if finite.size else 0.0

    fig, ax = plt.subplots(figsize=(9, 8))
    z = MR.pick_zoom(bbox_pad, max_tiles=8)
    img, tile_extent = MR.fetch_basemap(bbox_pad, z)
    ax.imshow(img, extent=tile_extent, origin="upper", zorder=0)

    surge = np.clip(arr, 0.0, None)
    im = ax.imshow(
        surge, extent=extent, origin="upper", cmap="YlOrRd",
        vmin=0.0, vmax=VMAX_PINNED, alpha=0.62, zorder=2,
    )

    # white AOI box = the requested fine AOI (not the raster's own bounds,
    # per kickoff - AOI = [-95.05, 29.2, -94.6, 29.65])
    ax0, ay0 = MR.ll_to_merc(AOI[0], AOI[1])
    ax1, ay1 = MR.ll_to_merc(AOI[2], AOI[3])
    ax.plot([ax0, ax1, ax1, ax0, ax0], [ay0, ay0, ay1, ay1, ay0],
            color="white", lw=1.8, zorder=4)

    tx, ty = [], []
    for time_hr, lon, lat, *_ in IKE_TRACK:
        mx, my = MR.ll_to_merc(lon, lat)
        tx.append(mx); ty.append(my)
    ax.plot(tx, ty, color="#ff3b30", lw=2.4, zorder=5, solid_capstyle="round")
    for i, (time_hr, lon, lat, *_ ) in enumerate(IKE_TRACK):
        mx, my = tx[i], ty[i]
        marker = "*" if i == LANDFALL_IDX else "o"
        ms = 18 if i == LANDFALL_IDX else 6
        ax.plot(mx, my, marker=marker, ms=ms, color="#ff3b30",
                 mec="white", mew=0.8, zorder=6)
        if i in (LANDFALL_IDX - 1, LANDFALL_IDX, LANDFALL_IDX + 1):
            label = ("LANDFALL" if i == LANDFALL_IDX else "")
            if label:
                ax.annotate(label, (mx, my), textcoords="offset points", xytext=(7, 7),
                            fontsize=8, color="white", weight="bold",
                            bbox=dict(fc="#ff3b30", alpha=0.75, pad=1.2, lw=0), zorder=6)

    ax.set_xlim(tile_extent[0], tile_extent[1])
    ax.set_ylim(tile_extent[2], tile_extent[3])
    ax.set_xticks([]); ax.set_yticks([])

    cb = fig.colorbar(im, ax=ax, shrink=0.62, pad=0.02)
    cb.set_label(f"peak storm-surge water level (m, above still water)  --  pinned 0-{VMAX_PINNED:.1f} m")

    ax.set_title(
        "schism_pahm_surge FINE-AOI (SCHISM barotropic + Holland-1980 sflux winds) -- "
        "Hurricane Ike (2008), Bolivar Peninsula / Galveston Bay entrance, TX",
        fontsize=10,
    )
    ax.text(
        0.01, 0.01,
        "EPSG:3857 -- ESRI World Imagery -- red = Ike best track (HURDAT2) -- "
        "white box = AOI",
        transform=ax.transAxes, fontsize=7, color="w",
        bbox=dict(fc="k", alpha=0.45, pad=1.5), zorder=7,
    )

    caption = (
        f"workflow: schism_pahm_surge (fine AOI)  |  storm: Hurricane Ike (2008, bal092008)  |  "
        f"AOI [-95.05, 29.2, -94.6, 29.65], explicit resolution_m={RESOLUTION_M} (user coarsening)  |  "
        f"peak surge {peak_surge_m:.2f} m (reported {PEAK_SURGE_REPORTED_M:.2f} m)  |  "
        f"case {CASE_ID}  |  run {RUN_ID}  |  "
        "bathymetry = REAL NOAA NCEI CUDEM 1/9\" (8 tiles read + composited over the ETOPO "
        "shelf base) -- resolution doctrine (ADR 0224): the prior render was ETOPO ~450 m "
        "because skip_cudem was FORCED, not because CUDEM omits this coast  |  "
        f"color scale pinned 0-{VMAX_PINNED:.1f} m"
    )
    fig.text(0.5, -0.02, caption, ha="center", va="top", fontsize=8, color="#3a3a3c",
              wrap=True)

    fig.tight_layout()
    fig.savefig(OUT / "schism_pahm_surge_fine.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"[render] schism_pahm_surge_fine.png peak_surge_m={peak_surge_m:.3f} "
          f"bbox_ll={bbox_ll} bbox_pad={bbox_pad} zoom={z}")
    return peak_surge_m, bbox_ll


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    render_map()
    print("[render] done ->", OUT)
