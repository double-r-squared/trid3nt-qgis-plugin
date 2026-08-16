"""Proof renders for the Landlab groundwater templates (ADR 0214).

Deterministic: downloads the staged Panola Mountain (GA) DEM the live runs used,
re-runs both chains via the worker (byte-identical to the published run), and
renders:
  - depth-to-water raster over ESRI World Imagery (EPSG:3857 tiles AND data)
  - groundwater seepage raster over ESRI (steady return-flow)
  - peak-seepage raster over ESRI (storm)
  - dock-exact charts (Figure 6.0x2.2 dpi=200): steady baseflow partition +
    storm baseflow hydrograph.

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) PYTHONPATH=.:contracts/src:. \
    venvs/agent/bin/python scripts/proof_landlab_groundwater.py
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

STEADY_DEM_KEY = "cache/static-30d/landlab_setup/01KZPN1ZS5ZKEV7CPYJADYHMGB/dem.tif"
STORM_DEM_KEY = "cache/static-30d/landlab_setup/01KZPN3V7VKRFVW4DH4JG5DSHD/dem.tif"
SITE = "Panola Mountain Research Watershed, GA"


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


def _basemap(w, s, e, n, zoom):
    x0f, y1f = _tile_xy(w, s, zoom)
    x1f, y0f = _tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(
                Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256)
            )
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _download_dem(key: str) -> Path:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    body = s3.get_object(Bucket="trid3nt-cache", Key=key)["Body"].read()
    p = OUT / ("_dem_" + key.split("/")[-2] + ".tif")
    p.write_bytes(body)
    return p


def _field_to_3857(field, transform, crs):
    """Reproject a metric-CRS field (H,W) to EPSG:3857; return (arr, extent)."""
    H, W = field.shape
    left = transform.c
    top = transform.f
    right = left + transform.a * W
    bottom = top + transform.e * H
    dtr, dw, dh = calculate_default_transform(
        crs, "EPSG:3857", W, H, left, bottom, right, top
    )
    dst = np.full((dh, dw), np.nan, dtype="float64")
    reproject(
        source=np.ascontiguousarray(field),
        destination=dst,
        src_transform=transform,
        src_crs=crs,
        src_nodata=float("nan"),
        dst_transform=dtr,
        dst_crs="EPSG:3857",
        dst_nodata=float("nan"),
        resampling=Resampling.bilinear,
    )
    ext = (dtr.c, dtr.c + dtr.a * dw, dtr.f + dtr.e * dh, dtr.f)  # (w,e,s,n)
    return dst, ext


def _ll_bounds(transform, crs, shape):
    H, W = shape
    left, top = transform.c, transform.f
    right, bottom = left + transform.a * W, top + transform.e * H
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = to_ll.transform([left, right, left, right], [top, bottom, bottom, top])
    return min(lons), min(lats), max(lons), max(lats)


def render_map(field, transform, crs, *, title, caption, cmap, label, fname, vmax=None):
    w, s, e, n = _ll_bounds(transform, crs, field.shape)
    px, py = (e - w) * 0.35 + 1e-4, (n - s) * 0.35 + 1e-4
    basemap, bm_ext = _basemap(w - px, s - py, e + px, n + py, 14)
    arr, ext = _field_to_3857(field, transform, crs)
    if vmax is None:
        finite = arr[np.isfinite(arr)]
        vmax = float(np.nanpercentile(finite, 98)) if finite.size else 1.0
    vmax = max(vmax, 1e-9)

    fig, ax = plt.subplots(figsize=(9, 8), dpi=110)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    masked = np.ma.masked_invalid(arr)
    im = ax.imshow(
        masked, extent=(ext[0], ext[1], ext[2], ext[3]), origin="upper",
        cmap=cmap, alpha=0.82, vmin=0.0, vmax=vmax, zorder=3,
    )
    wx0, wy0 = TO3857.transform(w - px, s - py)
    wx1, wy1 = TO3857.transform(e + px, n + py)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=11)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label(label)
    fig.text(0.01, 0.01, caption, fontsize=7, color="0.35", wrap=True)
    fig.tight_layout()
    p = OUT / fname
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p, "vmax=%.4g" % vmax)


def render_partition_chart(underflow, seepage, fname, caption):
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    bars = ["groundwater\nunderflow", "surface\nseepage"]
    vals = [underflow, seepage]
    ax.bar(bars, vals, color=["#1f5fbf", "#2a9d8f"], width=0.6)
    ax.set_ylabel("steady discharge (m3/s)", fontsize=8)
    ax.set_title("Steady baseflow partition", fontsize=9)
    ax.tick_params(labelsize=8)
    for i, v in enumerate(vals):
        ax.text(i, v, f"{v:.3f}", ha="center", va="bottom", fontsize=8)
    ax.margins(y=0.18)
    fig.text(0.01, 0.005, caption, fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    p = OUT / fname
    fig.savefig(p, dpi=200)
    plt.close(fig)
    print("wrote", p)


def render_hydrograph_chart(hydro, tau, peak, fname, caption):
    t = [h["time_days"] for h in hydro]
    q = [h["discharge_m3s"] for h in hydro]
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    ax.plot(t, q, color="#1f5fbf", linewidth=0.9)
    ax.set_xlabel("time (days)", fontsize=8)
    ax.set_ylabel("baseflow (m3/s)", fontsize=8)
    ax.set_title(f"Groundwater baseflow hydrograph (recession tau={tau:.1f} d)", fontsize=9)
    ax.tick_params(labelsize=8)
    ax.margins(x=0.01)
    fig.text(0.01, 0.005, caption, fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    p = OUT / fname
    fig.savefig(p, dpi=200)
    plt.close(fig)
    print("wrote", p)


def main():
    # --- STEADY ---
    dem_p = _download_dem(STEADY_DEM_KEY)
    dem, res, transform, crs = _read_dem_for_grid(dem_p, 30.0)
    r = run_component_chain(
        dem, resolution_m=res,
        build_spec={"analysis": "groundwater_steady", "gw_recharge_mm_yr": 250.0,
                    "gw_aquifer_thickness_m": 15.0},
    )
    e = r.extra
    dtw = np.asarray(r.field)
    seep = np.asarray(r.secondary_fields["seepage_specific_discharge"])
    cap_s = (
        f"{SITE} (EPSG:{crs.split(':')[-1] if isinstance(crs,str) else crs}); ESRI World "
        f"Imagery basemap + data both EPSG:3857. Steady GroundwaterDupuitPercolator, "
        f"250 mm/yr recharge, 15 m aquifer (demo K=1e-4 m/s). Mean depth-to-water "
        f"{e['mean_depth_to_water_m']:.1f} m, baseflow {e['baseflow_discharge_m3s']:.3f} "
        f"m3/s, mass-balance rel err {e['mass_balance_rel_error']:.1e} (V&V < 1%). "
        f"Scale 0-{max(np.nanpercentile(dtw[np.isfinite(dtw)],98),1):.0f} m."
    )
    render_map(
        dtw, transform, crs,
        title="Landlab groundwater: steady depth to water table\n" + SITE,
        caption=cap_s, cmap="YlGnBu", label="depth to water table (m)",
        fname="landlab_groundwater_water_table.png",
    )
    smax = float(np.nanmax(seep)) if np.any(np.isfinite(seep)) else 1e-6
    render_map(
        seep, transform, crs,
        title="Landlab groundwater: steady seepage (return flow to surface)\n" + SITE,
        caption=(
            f"{SITE}; ESRI basemap + data EPSG:3857. Steady groundwater return-flow "
            f"(surface-water specific discharge) where the water table meets the "
            f"surface -- the baseflow-generating cells. Seeping-area fraction "
            f"{e['seeping_area_fraction']:.3f}; the return flow a surface-only "
            f"rain-on-grid run omits. Scale 0-{smax:.1e} m/s."
        ),
        cmap="viridis", label="seepage specific discharge (m/s)",
        fname="landlab_groundwater_water_table_seepage.png", vmax=smax,
    )
    render_partition_chart(
        e["groundwater_underflow_m3s"], e["surface_seepage_m3s"],
        "landlab_groundwater_water_table_chart.png",
        caption=(
            f"{SITE}: steady catchment baseflow splits into subsurface groundwater "
            f"underflow vs surface seepage. Total {e['baseflow_discharge_m3s']:.3f} "
            f"m3/s (= recharge in {e.get('recharge_in_m3s',0):.3f}, conserved)."
        ),
    )

    # --- STORM ---
    dem_p2 = _download_dem(STORM_DEM_KEY)
    dem2, res2, transform2, crs2 = _read_dem_for_grid(dem_p2, 30.0)
    r2 = run_component_chain(
        dem2, resolution_m=res2,
        build_spec={"analysis": "groundwater_storm", "gw_storm_aquifer_thickness_m": 6.0,
                    "gw_storm_mean_depth_mm": 22.0, "gw_storm_total_days": 120.0},
    )
    e2 = r2.extra
    pseep = np.asarray(r2.field)
    pmax = float(np.nanmax(pseep)) if np.any(np.isfinite(pseep)) else 1e-6
    render_map(
        pseep, transform2, crs2,
        title="Landlab groundwater: peak storm seepage (return-flow emergence)\n" + SITE,
        caption=(
            f"{SITE}; ESRI basemap + data EPSG:3857. Per-cell PEAK groundwater seepage "
            f"over a {e2['total_days']:.0f}-day Poisson storm sequence ({e2['n_storms']} "
            f"storms, 6 m aquifer). Seeping-area fraction {e2['seeping_area_fraction']:.3f}; "
            f"peak baseflow {e2['peak_baseflow_m3s']:.2f} m3/s; mass-balance rel err "
            f"{e2['mass_balance_rel_error']:.1e} (V&V < 1%). Scale 0-{pmax:.1e} m/s."
        ),
        cmap="viridis", label="peak seepage specific discharge (m/s)",
        fname="landlab_groundwater_storm_recession.png", vmax=pmax,
    )
    render_hydrograph_chart(
        e2["hydrograph"], e2["recession_timescale_days"], e2["peak_baseflow_m3s"],
        "landlab_groundwater_storm_recession_chart.png",
        caption=(
            f"{SITE}: total groundwater + seepage discharge at the catchment boundary "
            f"through {e2['n_storms']} storms. Peak {e2['peak_baseflow_m3s']:.2f} m3/s, "
            f"final {e2['final_baseflow_m3s']:.2f} m3/s; first-limb recession "
            f"tau {e2['recession_timescale_days']:.1f} d."
        ),
    )

    for p in (dem_p, dem_p2):
        try:
            p.unlink()
        except OSError:
            pass
    print("PROOFS COMPLETE")


if __name__ == "__main__":
    main()
