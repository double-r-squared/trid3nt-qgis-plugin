"""Proof render for landlab_normal_fault_scarp_evolution (ADR 0252).

Deterministic: downloads the staged Wasatch Range front (Provo, UT) DEM the live
run used, re-runs the worker chain (byte-identical to the published run) with the
fault ON and OFF (the discriminating pair), and renders both evolved-elevation
fields over ESRI World Imagery (EPSG:3857 tiles AND data) with the model grid
outline, plus the cumulative fault-throw footwall raster.

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) PYTHONPATH=.:contracts:. \
    venvs/agent/bin/python scripts/proof_landlab_normal_fault.py
"""

from __future__ import annotations

import io
import math
import os
from pathlib import Path

import boto3
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402
from rasterio.warp import Resampling, calculate_default_transform, reproject  # noqa: E402

from workers.landlab.component_chain import run_component_chain  # noqa: E402
from workers.landlab.entrypoint import _read_dem_for_grid  # noqa: E402

TILE = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
OUT = Path(__file__).parent.parent / "docs" / "proof" / "templates"
OUT.mkdir(parents=True, exist_ok=True)

DEM_KEY = "cache/static-30d/landlab_setup/01KZZ4DR5EJ96MSPY6Q2NV2GP0/dem.tif"
SITE = "Wasatch Range front, Provo, UT"
RES_M = 90.0
SPEC_ON = dict(
    analysis="normal_fault",
    fault_throw_rate_m_yr=1.0e-3,
    fault_dip_deg=60.0,
    fault_position_frac=0.5,
    k_bedrock=1.0e-5,
    hillslope_diffusivity_m2_yr=0.1,
    incision_run_duration_yr=5.0e5,
    incision_n_timesteps=300,
)


def _tile_xy(lon, lat, z):
    n = 2 ** z
    return (
        (lon + 180.0) / 360.0 * n,
        (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n,
    )


def _tile_bounds_3857(x, y, z):
    n = 2 ** z

    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO3857.transform(lon, lat)

    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def _basemap(bounds4326, z=13):
    w, s, e, n = bounds4326
    x0f, y1f = _tile_xy(w, n, z)
    x1f, y0f = _tile_xy(e, s, z)
    x0, x1 = int(math.floor(x0f)), int(math.floor(x1f))
    y0, y1 = int(math.floor(y1f)), int(math.floor(y0f))
    sess = requests.Session()
    tiles, exts = [], []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            url = TILE.format(z=z, x=tx, y=ty)
            r = sess.get(url, timeout=30, headers={"User-Agent": "trid3nt-proof/0.1"})
            if r.status_code != 200:
                continue
            img = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))
            tiles.append((tx, ty, img))
            exts.append(_tile_bounds_3857(tx, ty, z))
    if not tiles:
        return None, None
    minx = min(e[0] for e in exts)
    miny = min(e[1] for e in exts)
    maxx = max(e[2] for e in exts)
    maxy = max(e[3] for e in exts)
    th, tw = tiles[0][2].shape[:2]
    ncols = x1 - x0 + 1
    nrows = y1 - y0 + 1
    canvas = np.zeros((nrows * th, ncols * tw, 3), dtype=np.uint8)
    for tx, ty, img in tiles:
        cx = (tx - x0) * tw
        cy = (ty - y0) * th
        canvas[cy:cy + th, cx:cx + tw] = img
    return canvas, (minx, miny, maxx, maxy)


def _reproj_to_3857(field, src_transform, src_crs):
    h, w = field.shape
    left = src_transform.c
    top = src_transform.f
    right = left + src_transform.a * w
    bottom = top + src_transform.e * h
    src_bounds = (min(left, right), min(top, bottom), max(left, right), max(top, bottom))
    dst_crs = "EPSG:3857"
    transform, dw, dh = calculate_default_transform(
        src_crs, dst_crs, w, h, *src_bounds
    )
    dst = np.full((dh, dw), np.nan, dtype="float64")
    reproject(
        source=np.ascontiguousarray(field),
        destination=dst,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=transform,
        dst_crs=dst_crs,
        resampling=Resampling.bilinear,
        src_nodata=np.nan,
        dst_nodata=np.nan,
    )
    dleft = transform.c
    dtop = transform.f
    dright = dleft + transform.a * dw
    dbottom = dtop + transform.e * dh
    return dst, (dleft, dbottom, dright, dtop), src_bounds, src_crs


def main():
    s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
    tmp = Path("/tmp/proof_nf_dem.tif")
    s3.download_file("trid3nt-cache", DEM_KEY, str(tmp))
    dem, res, dem_transform, dem_crs = _read_dem_for_grid(str(tmp), RES_M)
    print(f"DEM {dem.shape} res={res} m crs={dem_crs}")

    spec_off = dict(SPEC_ON)
    spec_off["fault_throw_rate_m_yr"] = 0.0
    r_on = run_component_chain(dem, resolution_m=res, build_spec=SPEC_ON)
    r_off = run_component_chain(dem, resolution_m=res, build_spec=spec_off)
    print(
        "ON  total_throw=%.1f footwall_relief=%.1f n_fw_chan=%d"
        % (r_on.extra["total_throw_m"], r_on.extra["footwall_relief_m"],
           r_on.extra["n_footwall_channel_nodes"])
    )
    print(
        "OFF total_throw=%.1f footwall_relief=%.1f"
        % (r_off.extra["total_throw_m"], r_off.extra["footwall_relief_m"])
    )

    elev_on, ext_on, src_bounds, _ = _reproj_to_3857(r_on.field, dem_transform, dem_crs)
    elev_off, _, _, _ = _reproj_to_3857(r_off.field, dem_transform, dem_crs)
    throw, ext_throw, _, _ = _reproj_to_3857(
        r_on.secondary_fields["fault_throw"], dem_transform, dem_crs
    )

    wgs = Transformer.from_crs(dem_crs, "EPSG:4326", always_xy=True)
    w, s = wgs.transform(src_bounds[0], src_bounds[1])
    e, n = wgs.transform(src_bounds[2], src_bounds[3])
    base, bext = _basemap((w, s, e, n), z=13)

    vmin = float(np.nanmin([np.nanmin(elev_on), np.nanmin(elev_off)]))
    vmax = float(np.nanmax([np.nanmax(elev_on), np.nanmax(elev_off)]))

    def _grid_outline(ax, ext):
        left, bottom, right, top = ext
        ax.plot(
            [left, right, right, left, left],
            [bottom, bottom, top, top, bottom],
            color="cyan", lw=0.8, alpha=0.7,
        )

    fig, axes = plt.subplots(1, 3, figsize=(18.0, 6.2), dpi=200)
    panels = [
        (axes[0], elev_off, ext_on, f"Fault OFF (control)\nrelief {r_off.extra['footwall_relief_m']:.0f} m",
         "terrain", vmin, vmax),
        (axes[1], elev_on, ext_on,
         f"Fault ON: scarp + footwall drainage\nrelief {r_on.extra['footwall_relief_m']:.0f} m, "
         f"{r_on.extra['n_footwall_channel_nodes']} footwall channels",
         "terrain", vmin, vmax),
        (axes[2], throw, ext_throw,
         f"Cumulative fault throw (footwall)\ntotal {r_on.extra['total_throw_m']:.0f} m",
         "magma", 0.0, float(np.nanmax(throw))),
    ]
    for ax, data, ext, title, cmap, lo, hi in panels:
        if base is not None:
            ax.imshow(base, extent=[bext[0], bext[2], bext[1], bext[3]], origin="upper")
        im = ax.imshow(
            data, extent=[ext[0], ext[2], ext[1], ext[3]], origin="upper",
            cmap=cmap, vmin=lo, vmax=hi, alpha=0.72,
        )
        _grid_outline(ax, ext)
        ax.set_title(title, fontsize=10)
        ax.set_xlim(bext[0], bext[2])
        ax.set_ylim(bext[1], bext[3])
        ax.set_xticks([])
        ax.set_yticks([])
        fig.colorbar(im, ax=ax, shrink=0.72)
    fig.suptitle(
        f"Landlab normal-fault scarp evolution - {SITE} (3DEP DEM, EPSG:3857 over "
        f"ESRI World Imagery)\nNormalFault + FastscapeEroder + LinearDiffuser, "
        f"5x10^5 yr; footwall uplifts and the scarp degrades. Fault forcing is a "
        f"labeled demo; the terrain is real.",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = OUT / "landlab_normal_fault_scarp_evolution.png"
    fig.savefig(out, bbox_inches="tight")
    print("wrote", out)


if __name__ == "__main__":
    main()
