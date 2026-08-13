"""SCHISM PaHM storm-surge proof render: the peak-surge COG over ESRI World
Imagery (EPSG:3857 tiles AND data, per the ADR 0197 lesson; merc_render.py is
the shared mercator module) with the Hurricane Ike best track overlaid.
Overwrites docs/proof/templates/schism_pahm_surge.png in place. A second file,
the _chart.png, carries the coastal-gauge surge hydrograph (dock-exact panel
size, 6.0x2.2in @ dpi200) kept separate so the map panel stays full-bleed over
the basemap.

Inputs: the seeded showcase run behind case 01KZRWZK2XRF1ADH68NX6SA602 -
schism_elev_max.tif (peak water-surface elevation, EPSG:4326) + outputs/staout_1
(the mesh-centroid gauge timeseries) at
s3://trid3nt-runs/01KZRWHNM33Q4NP99BD1XBKP22/. The Ike best track is the
PUBLISHED_IKE_TRACK constant from pahm_surge.py (HURDAT2 bal092008, hardcoded
here to keep this script import-independent of the server package).
"""
from __future__ import annotations

import datetime as dt
import math
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
    "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/schism_surge"
)
OUT = REPO / "docs" / "proof" / "templates"
COG = SCRATCH / "schism_elev_max.tif"
STAOUT = SCRATCH / "staout_1"

RUN_ID = "01KZS6NG40P717B2FGSSKP4P1N"
CASE_ID = "01KZS6M3TX4QZSVKN5QC6E6EGA"

# Published Hurricane Ike (2008, bal092008) best track, verbatim from
# pahm_surge.py PUBLISHED_IKE_TRACK: (time_hr, lon, lat, pres_mb, wind_kt, rmw_nmi).
IKE_TRACK = (
    (0.0, -91.5, 26.6, 948.0, 95.0, 50.0),
    (12.0, -93.6, 28.1, 950.0, 80.0, 55.0),
    (18.0, -94.4, 28.8, 951.0, 80.0, 55.0),
    (24.0, -94.7, 29.3, 952.0, 80.0, 50.0),   # ~landfall Galveston
    (30.0, -95.4, 30.5, 964.0, 60.0, 60.0),
)
IKE_BASE = dt.datetime(2008, 9, 12, 6, tzinfo=dt.timezone.utc)
LANDFALL_IDX = 3

VMAX_PINNED = 3.5  # m, pinned surge-color scale (peak modeled 3.18 m)


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


def _track_time_label(time_hr):
    t = IKE_BASE + dt.timedelta(hours=time_hr)
    return t.strftime("%m/%d %HZ")


def render_map():
    arr, extent, bounds_ll = _reproject_to_merc(COG)
    bbox_ll = (bounds_ll.left, bounds_ll.bottom, bounds_ll.right, bounds_ll.top)
    # modest context margin -- the greater-Galveston domain already spans the bay,
    # Bolivar Peninsula, the island and the open Gulf shelf.
    pad_lon, pad_lat = 0.12, 0.12
    bbox_pad = (bbox_ll[0] - pad_lon, bbox_ll[1] - pad_lat,
                bbox_ll[2] + pad_lon, bbox_ll[3] + pad_lat)

    finite = arr[np.isfinite(arr)]
    peak_surge_m = float(finite.max()) if finite.size else 0.0

    fig, ax = plt.subplots(figsize=(9, 8))
    z = MR.pick_zoom(bbox_pad, max_tiles=8)
    img, tile_extent = MR.fetch_basemap(bbox_pad, z)
    ax.imshow(img, extent=tile_extent, origin="upper", zorder=0)

    surge = np.clip(arr, 0.0, None)  # below-still-water cells read as 0 (not a "trough")
    im = ax.imshow(
        surge, extent=extent, origin="upper", cmap="YlOrRd",
        vmin=0.0, vmax=VMAX_PINNED, alpha=0.62, zorder=2,
    )

    # white AOI box only (no fill) - the raster's own bounds
    bx0, by0 = MR.ll_to_merc(bbox_ll[0], bbox_ll[1])
    bx1, by1 = MR.ll_to_merc(bbox_ll[2], bbox_ll[3])
    ax.plot([bx0, bx1, bx1, bx0, bx0], [by0, by0, by1, by1, by0],
            color="white", lw=1.8, zorder=4)

    # Ike best track: distinct red line, dated fix markers, arrowhead at the
    # last visible segment showing direction of travel.
    tx, ty = [], []
    for time_hr, lon, lat, *_ in IKE_TRACK:
        mx, my = MR.ll_to_merc(lon, lat)
        tx.append(mx); ty.append(my)
    ax.plot(tx, ty, color="#ff3b30", lw=2.4, zorder=5, solid_capstyle="round")
    for i, (time_hr, lon, lat, *_ ) in enumerate(IKE_TRACK):
        mx, my = tx[i], ty[i]
        marker = "*" if i == LANDFALL_IDX else "o"
        ms = 16 if i == LANDFALL_IDX else 6
        ax.plot(mx, my, marker=marker, ms=ms, color="#ff3b30",
                 mec="white", mew=0.8, zorder=6)
        label = _track_time_label(time_hr) + (" LANDFALL" if i == LANDFALL_IDX else "")
        ax.annotate(label, (mx, my), textcoords="offset points", xytext=(7, 7),
                    fontsize=7, color="white", weight="bold",
                    bbox=dict(fc="#ff3b30", alpha=0.75, pad=1.2, lw=0), zorder=6)
    # fixed-length arrowhead anchored AT the landfall marker (heading toward
    # the next fix) so it stays visible regardless of how much of the full
    # track falls outside the padded AOI view.
    lfx, lfy = tx[LANDFALL_IDX], ty[LANDFALL_IDX]
    dxn, dyn = tx[LANDFALL_IDX + 1] - lfx, ty[LANDFALL_IDX + 1] - lfy
    norm = math.hypot(dxn, dyn) or 1.0
    arrow_len_m = 22000.0
    ahx, ahy = lfx + dxn / norm * arrow_len_m, lfy + dyn / norm * arrow_len_m
    ax.annotate("", xy=(ahx, ahy), xytext=(lfx, lfy),
                arrowprops=dict(arrowstyle="-|>", color="#ff3b30", lw=2.6,
                                 mutation_scale=26), zorder=6)

    # gauge marker (SCHISM station.in point = mesh centroid) referenced in the
    # chart's caption below.
    gx, gy = MR.ll_to_merc((bbox_ll[0] + bbox_ll[2]) / 2.0,
                            (bbox_ll[1] + bbox_ll[3]) / 2.0)
    ax.plot(gx, gy, marker="o", ms=9, mfc="#0a84ff", mec="white", mew=1.2, zorder=6)
    ax.annotate("gauge", (gx, gy), textcoords="offset points", xytext=(8, -12),
                fontsize=7, color="white", weight="bold",
                bbox=dict(fc="#0a84ff", alpha=0.8, pad=1.2, lw=0), zorder=6)

    ax.set_xlim(tile_extent[0], tile_extent[1])
    ax.set_ylim(tile_extent[2], tile_extent[3])
    ax.set_xticks([]); ax.set_yticks([])

    cb = fig.colorbar(im, ax=ax, shrink=0.62, pad=0.02)
    cb.set_label(f"peak storm-surge water level (m, above still water)  --  pinned 0-{VMAX_PINNED:.1f} m")

    ax.set_title(
        "schism_pahm_surge (SCHISM barotropic + Holland-1980 sflux winds) -- "
        "Hurricane Ike (2008), greater Galveston Bay + Gulf shelf, TX",
        fontsize=10,
    )
    ax.text(
        0.01, 0.01,
        "EPSG:3857 -- ESRI World Imagery -- red = Ike best track (HURDAT2) -- "
        "blue dot = gauge -- white box = AOI",
        transform=ax.transAxes, fontsize=7, color="w",
        bbox=dict(fc="k", alpha=0.45, pad=1.5), zorder=7,
    )

    caption = (
        f"workflow: schism_pahm_surge  |  storm: Hurricane Ike (2008, bal092008)  |  "
        f"peak surge {peak_surge_m:.2f} m on/near Bolivar Peninsula + upper bay (right of track)  |  "
        f"case {CASE_ID}  |  run {RUN_ID}  |  "
        "SCREENING: SCHISM barotropic + parametric Holland-1980 winds on the greater-"
        "Galveston TIN with REAL ETOPO 2022 screening bathymetry (deep Gulf shelf + "
        "shallow bay) -- NOT GAHM asymmetry, NOT tide-coupled, NOT a calibrated STOFS "
        f"nowcast; observed Ike ~3-4 m at the coast  |  color scale pinned 0-{VMAX_PINNED:.1f} m"
    )
    fig.text(0.5, -0.02, caption, ha="center", va="top", fontsize=8, color="#3a3a3c",
              wrap=True)

    fig.tight_layout()
    fig.savefig(OUT / "schism_pahm_surge.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"[render] schism_pahm_surge.png peak_surge_m={peak_surge_m:.3f} "
          f"bbox_ll={bbox_ll} bbox_pad={bbox_pad} zoom={z}")
    return peak_surge_m, bbox_ll


def render_chart():
    t_s, elev_m = [], []
    for line in STAOUT.read_text().splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        t_s.append(float(parts[0]))
        elev_m.append(float(parts[1]))
    t_hr = np.asarray(t_s) / 3600.0
    elev_m = np.asarray(elev_m)

    fig, ax = plt.subplots(figsize=(6.0, 2.2))
    ax.plot(t_hr, elev_m, color="#0a84ff", lw=1.4)
    ax.axhline(0.0, color="#8e8e93", lw=0.8, ls="--")
    ax.fill_between(t_hr, elev_m, 0.0, color="#0a84ff", alpha=0.2)
    ax.set_xlabel("hours since track base (2008-09-12 06Z)", fontsize=8)
    ax.set_ylabel("gauge elevation (m)", fontsize=8)
    ax.set_title("coastal gauge surge hydrograph (SCHISM station.in, mesh centroid)",
                 fontsize=9)
    ax.tick_params(labelsize=7)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()

    trough_m = float(elev_m.min())
    peak_m = float(elev_m.max())
    caption = (
        "gauge: SCHISM station.in at the mesh centroid ~(-94.80, 29.28) -- the BLUE "
        "dot on the map, the Galveston Bay entrance / Bolivar Roads near Pier 21  |  "
        f"setdown to {trough_m:.2f} m as the vortex approaches (offshore-directed "
        f"winds on the left flank), then set-up to +{peak_m:.2f} m at/after landfall "
        "-- the classic surge setdown/setup story, distinct from the 3.18 m coastal "
        "peak read at the right-of-track Bolivar nodes on the map"
    )
    fig.text(0.5, -0.06, caption, ha="center", va="top", fontsize=6.6, color="#3a3a3c",
              wrap=True)

    fig.savefig(OUT / "schism_pahm_surge_chart.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"[render] schism_pahm_surge_chart.png trough_m={trough_m:.3f} n={len(t_hr)}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    render_map()
    render_chart()
    print("[render] done ->", OUT)
