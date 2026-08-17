"""GAIA erodible-bed scour proof render: the SIGNED bed-evolution COG over ESRI
World Imagery (EPSG:3857 tiles AND data, per the lesson;
merc_render.py is the shared mercator module). Overwrites
docs/proof/templates/telemac_erodible_bed_scour_proof.png in place. A second
file, the _chart.png, carries the along-channel bed-change profile (dock-exact
panel size, 6.0x2.2in @ dpi200) kept separate so the map panel stays full-bleed
over the basemap.

Inputs: the scratchpad artifacts a run_erodible_scour_direct.py solve leaves -
the signed bed-evolution COG, the mesh-preview geojson (wireframe + centerline)
and the run metrics json (npoin/nelem). Update the paths + RUN_ID below to the
run being proved.
"""
from __future__ import annotations

import json
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
    "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad"
)
OUT = REPO / "docs" / "proof" / "templates"
COG = SCRATCH / "bed_evolution.tif"
MESH_GEOJSON = SCRATCH / "mesh_preview.geojson"
METRICS = SCRATCH / "metrics_new.json"

RUN_ID = "01KZPV98F6WPXYRT8XDD0QX5Z6"
MOFAC = 5.0
D50_UM = 300.0


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


def _mesh_lines_merc():
    fc = json.loads(MESH_GEOJSON.read_text())
    lines = []
    for feat in fc["features"]:
        g = feat["geometry"]
        if g["type"] != "MultiLineString":
            continue
        for part in g["coordinates"]:
            arr = np.asarray(part, dtype=float)
            mx, my = MR.ll_to_merc(arr[:, 0], arr[:, 1])
            lines.append(np.column_stack([mx, my]))
    return lines


def _centerline_ll():
    """Longest MultiLineString part = the along-channel centerline (lon/lat)."""
    fc = json.loads(MESH_GEOJSON.read_text())
    best = None
    best_len = -1
    for feat in fc["features"]:
        g = feat["geometry"]
        if g["type"] != "MultiLineString":
            continue
        for part in g["coordinates"]:
            arr = np.asarray(part, dtype=float)
            if arr.shape[0] > best_len:
                best_len = arr.shape[0]
                best = arr
    return best


def render_map(metrics):
    arr, extent, bounds_ll = _reproject_to_merc(COG)
    bbox_ll = (bounds_ll.left, bounds_ll.bottom, bounds_ll.right, bounds_ll.top)
    pad = 0.004
    bbox_pad = (bbox_ll[0] - pad, bbox_ll[1] - pad, bbox_ll[2] + pad, bbox_ll[3] + pad)

    finite = arr[np.isfinite(arr)]
    robust = float(np.percentile(np.abs(finite), 99)) if finite.size else 1.0
    max_dep = float(finite[finite > 0].max()) if (finite > 0).any() else 0.0
    max_scour = float(-finite[finite < 0].min()) if (finite < 0).any() else 0.0
    vext = round(max(min(max(max_dep, max_scour), max(robust, 1e-3)), 1e-3), 1)

    fig, ax = plt.subplots(figsize=(9, 8))
    z = MR.pick_zoom(bbox_pad, max_tiles=8)
    img, tile_extent = MR.fetch_basemap(bbox_pad, z)
    ax.imshow(img, extent=tile_extent, origin="upper", zorder=0)

    im = ax.imshow(
        arr, extent=extent, origin="upper", cmap="RdBu_r",
        vmin=-vext, vmax=vext, alpha=0.82, zorder=2,
    )

    for line in _mesh_lines_merc():
        ax.plot(line[:, 0], line[:, 1], color="#8e8e93", lw=0.35, alpha=0.75, zorder=3)

    # white AOI box only (no fill) - the raster's own bounds
    bx0, by0 = MR.ll_to_merc(bbox_ll[0], bbox_ll[1])
    bx1, by1 = MR.ll_to_merc(bbox_ll[2], bbox_ll[3])
    ax.plot([bx0, bx1, bx1, bx0, bx0], [by0, by0, by1, by1, by0],
            color="white", lw=1.6, zorder=4)

    ax.set_xlim(tile_extent[0], tile_extent[1])
    ax.set_ylim(tile_extent[2], tile_extent[3])
    ax.set_xticks([]); ax.set_yticks([])

    cb = fig.colorbar(im, ax=ax, shrink=0.62, pad=0.02)
    cb.set_label(f"bed evolution (mm)  --  scour < 0 < deposition  --  pinned +/-{vext:.0f} mm (P99 |change|)")

    ax.set_title(
        "telemac_river_dye (erodible-bed GAIA v2)  --  Snake River near Twin Falls, ID\n"
        f"{metrics['npoin']} nodes / {metrics['nelem']} elements, MOFAC={MOFAC:.0f}, d50={D50_UM:.0f} um",
        fontsize=10,
    )
    ax.text(
        0.01, 0.01,
        "EPSG:3857 -- ESRI World Imagery -- grey = mesh wireframe -- white box = AOI",
        transform=ax.transAxes, fontsize=7, color="w",
        bbox=dict(fc="k", alpha=0.45, pad=1.5), zorder=5,
    )

    deepest_scour_mm = round(max_scour, 1)
    peak_dep_mm = round(max_dep, 1)
    caption = (
        f"workflow: telemac_river_dye (substance=scour, erodible_bed=True)  |  "
        f"run {RUN_ID}  |  deepest scour {deepest_scour_mm:.0f} mm  |  "
        f"peak deposition {peak_dep_mm:.0f} mm  |  MOFAC {MOFAC:.0f}  |  d50 {D50_UM:.0f} um  |  "
        f"scale capped +/-{vext:.0f} mm (P99 |bed change|, per ADR 0216 boundary-pileup cap)"
    )
    fig.text(0.5, -0.01, caption, ha="center", va="top", fontsize=8, color="#3a3a3c",
              wrap=True)

    fig.tight_layout()
    fig.savefig(OUT / "telemac_erodible_bed_scour_proof.png", dpi=200,
                bbox_inches="tight")
    plt.close(fig)
    print("[render] telemac_erodible_bed_scour_proof.png "
          f"vext={vext} max_dep={max_dep:.1f} max_scour={max_scour:.1f}")
    return deepest_scour_mm, peak_dep_mm, vext


def render_chart(metrics):
    with rasterio.open(COG) as ds:
        band = ds.read(1)
        transform = ds.transform

    line = _centerline_ll()
    # cumulative distance along the centerline (metres, equirect approx near 42.6N)
    lat0 = float(np.mean(line[:, 1]))
    coslat = np.cos(np.radians(lat0))
    dx = (line[1:, 0] - line[:-1, 0]) * 111320.0 * coslat
    dy = (line[1:, 1] - line[:-1, 1]) * 110540.0
    seglen = np.hypot(dx, dy)
    dist_m = np.concatenate([[0.0], np.cumsum(seglen)])

    inv = ~transform
    vals = []
    for lon, lat in line:
        col, row = inv * (lon, lat)
        r, c = int(round(row)), int(round(col))
        if 0 <= r < band.shape[0] and 0 <= c < band.shape[1]:
            v = band[r, c]
            vals.append(v if np.isfinite(v) else np.nan)
        else:
            vals.append(np.nan)
    vals = np.asarray(vals)

    fig, ax = plt.subplots(figsize=(6.0, 2.2))
    ax.plot(dist_m, vals, color="#3a3a3c", lw=0.9)
    ax.axhline(0.0, color="#8e8e93", lw=0.8, ls="--")
    # colors match the map panel's RdBu_r convention: scour (negative) = blue,
    # deposition (positive) = red.
    ax.fill_between(dist_m, vals, 0.0, where=np.nan_to_num(vals) < 0,
                     color="#0a84ff", alpha=0.25, interpolate=True, label="scour")
    ax.fill_between(dist_m, vals, 0.0, where=np.nan_to_num(vals) > 0,
                     color="#ff453a", alpha=0.25, interpolate=True, label="deposition")
    ax.set_xlabel("distance downstream (m)", fontsize=8)
    ax.set_ylabel("bed change (mm)", fontsize=8)
    ax.set_title("along-channel bed-evolution profile (final frame)", fontsize=9)
    ax.tick_params(labelsize=7)
    ax.legend(loc="upper right", fontsize=6, framealpha=0.6)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "telemac_erodible_bed_scour_proof_chart.png", dpi=200,
                bbox_inches="tight")
    plt.close(fig)
    print("[render] telemac_erodible_bed_scour_proof_chart.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    metrics = json.loads(METRICS.read_text())
    render_map(metrics)
    render_chart(metrics)
    print("[render] done ->", OUT)
