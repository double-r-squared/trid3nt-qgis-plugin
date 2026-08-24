#!/usr/bin/env python
"""Diagnostic proof render for a ``telemac_river_dye`` run: the plume, animated.

Reads the run's OWN PUBLISHED artifacts off its prefix - never a re-solve and
never a re-derivation:

  * ``r2d_river.slf``, the temporal artifact the emit-on-solve seam registers as
    the results-mesh layer, rendered frame by frame on its REAL element
    connectivity (the modeled domain, with the mesh wireframe over it);
  * ``telemac_dye_peak.tif``, the published peak COG, as the still.

Both are coloured through the product's OWN styling seam
(``publish_layer._resolve_qgis_style_params``), so the proof shows the colours
the canvas shows rather than a second palette invented here. ESRI World Imagery
is the basemap, in EPSG:3857 for both tiles and data.

Env (MinIO): set -a; source .env.local; set +a
Usage: proof_river_dye_frames.py --run-id <ULID> [--out-dir docs/proof/templates]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import PillowWriter
from matplotlib.tri import Triangulation

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "sandbox" / "oceanmesh"))

import merc_render as MR  # noqa: E402

from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin  # noqa: E402

#: The tracer variable the dye run writes. Matched loosely because SELAFIN pads
#: its variable names to 32 characters.
_DYE_VAR_TOKENS = ("DYE", "TRACER")
#: Frames per second in the GIF. Slow enough to read a 25-frame plume.
_FPS = 4
#: How much of the reach bbox to pad, so the plume is not flush to the frame.
_PAD_FRAC = 0.06
#: Render density. High enough that the ~14 m mesh elements survive, low enough
#: that a 26-frame GIF stays a few megabytes.
_DPI = 130


def _s3():
    import boto3

    return boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
                        region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _download(bucket: str, key: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    _s3().download_file(bucket, key, path)
    return path


def _read_json(bucket: str, key: str) -> dict:
    return json.loads(_s3().get_object(Bucket=bucket, Key=key)["Body"].read())


def _product_style(peak_uri: str) -> tuple[float | None, float | None, str]:
    """The vmin/vmax/colormap the PRODUCT publishes this raster with."""
    from trid3nt_server.data.publish_layer.publish_layer import (
        _parse_style_params,
        _resolve_qgis_style_params,
    )
    from trid3nt_contracts.telemac_contracts import TELEMAC_DYE_STYLE_PRESET

    params = _resolve_qgis_style_params(TELEMAC_DYE_STYLE_PRESET, peak_uri)
    vmin, vmax, cmap = _parse_style_params(params or "")
    return vmin, vmax, (cmap or "viridis")


def _dye_variable(varnames: list[str]) -> str:
    for name in varnames:
        upper = name.upper()
        if any(tok in upper for tok in _DYE_VAR_TOKENS):
            return name
    raise SystemExit(f"no dye/tracer variable among {varnames}")


def _nodes_to_lonlat(x: np.ndarray, y: np.ndarray, utm_epsg: int):
    from pyproj import Transformer

    to4326 = Transformer.from_crs(f"EPSG:{int(utm_epsg)}", "EPSG:4326", always_xy=True)
    return to4326.transform(x, y)


def _axes_with_basemap(bbox_ll, title: str):
    zoom = MR.pick_zoom(bbox_ll, max_tiles=6)
    mosaic, extent = MR.fetch_basemap(bbox_ll, zoom)
    fig, ax = plt.subplots(figsize=(10.0, 4.6), dpi=_DPI)
    ax.imshow(np.asarray(mosaic), extent=extent, origin="upper", zorder=0)
    xw, yw = MR.ll_to_merc(np.array([bbox_ll[0], bbox_ll[2]]),
                           np.array([bbox_ll[1], bbox_ll[3]]))
    padx, pady = (xw[1] - xw[0]) * _PAD_FRAC, (yw[1] - yw[0]) * _PAD_FRAC
    ax.set_xlim(xw[0] - padx, xw[1] + padx)
    ax.set_ylim(yw[0] - pady, yw[1] + pady)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=10)
    return fig, ax


def _mesh_bbox_ll(lon: np.ndarray, lat: np.ndarray):
    return (float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))


def render_animation(slf_path: str, utm_epsg: int, *, vmin, vmax, cmap: str,
                     out_path: Path, reach: str, run_id: str) -> int:
    mesh = read_selafin(slf_path)
    var = _dye_variable(mesh["varnames"])
    values = mesh["data"][var]
    lon, lat = _nodes_to_lonlat(mesh["x"], mesh["y"], utm_epsg)
    mx, my = MR.ll_to_merc(lon, lat)
    tri = Triangulation(mx, my, mesh["ikle"][:, :3])

    if vmin is None or vmax is None:
        finite = values[np.isfinite(values)]
        vmin, vmax = float(np.nanpercentile(finite, 2)), float(np.nanpercentile(finite, 98))

    fig, ax = _axes_with_basemap(_mesh_bbox_ll(lon, lat),
                                 f"TELEMAC-2D dye plume - {reach}")
    coll = ax.tripcolor(tri, values[0], shading="gouraud", cmap=cmap,
                        vmin=vmin, vmax=vmax, alpha=0.85, zorder=2)
    # The MESH is the modeled domain, drawn OVER the field: a wireframe hidden
    # under an opaque plume tells the reader nothing about what was solved. It is
    # kept light on purpose - at this reach scale the elements are ~2 px, so a
    # heavier line stops reading as a mesh and starts reading as a hatch.
    ax.triplot(tri, color="white", linewidth=0.08, alpha=0.22, zorder=3)
    cbar = fig.colorbar(coll, ax=ax, fraction=0.025, pad=0.01)
    cbar.set_label("Dye concentration (mg/L)", fontsize=8)
    cbar.ax.tick_params(labelsize=7)
    stamp = ax.text(0.012, 0.045, "", transform=ax.transAxes, fontsize=9,
                    color="white", zorder=5,
                    bbox=dict(facecolor="black", alpha=0.45, pad=3, edgecolor="none"))
    ax.text(0.012, 0.955, f"run {run_id}  |  published r2d_river.slf, "
            f"{len(mesh['times'])} frames  |  wireframe = the meshed domain  |  "
            f"ESRI World Imagery",
            transform=ax.transAxes, fontsize=6.5, color="white", va="top", zorder=5,
            bbox=dict(facecolor="black", alpha=0.4, pad=2, edgecolor="none"))

    writer = PillowWriter(fps=_FPS)
    with writer.saving(fig, str(out_path), dpi=_DPI):
        for i, t in enumerate(mesh["times"]):
            coll.set_array(values[i])
            stamp.set_text(f"t = {float(t):7.0f} s      "
                           f"max {float(np.nanmax(values[i])):.2f} mg/L")
            writer.grab_frame()
    plt.close(fig)
    return int(len(mesh["times"]))


def render_peak(cog_path: str, *, vmin, vmax, cmap: str, out_path: Path,
                reach: str, run_id: str, caption: str) -> None:
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(cog_path) as src:
        transform, w, h = calculate_default_transform(
            src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        arr = np.full((h, w), np.nan, dtype="float32")
        reproject(source=rasterio.band(src, 1), destination=arr,
                  src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs="EPSG:3857",
                  resampling=Resampling.bilinear, src_nodata=src.nodata,
                  dst_nodata=np.nan)
        left, top = transform * (0, 0)
        right, bottom = transform * (w, h)
        bounds_ll = rasterio.warp.transform_bounds(src.crs, "EPSG:4326", *src.bounds)

    fig, ax = _axes_with_basemap(bounds_ll,
                                 f"Peak dye concentration envelope - {reach}")
    masked = np.ma.masked_invalid(arr)
    im = ax.imshow(masked, extent=(left, right, bottom, top), origin="upper",
                   cmap=cmap, vmin=vmin, vmax=vmax, alpha=0.85, zorder=2)
    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cbar.set_label("Dye concentration (mg/L)", fontsize=8)
    cbar.ax.tick_params(labelsize=7)
    ax.text(0.012, 0.955, f"run {run_id}  |  published telemac_dye_peak.tif  |  "
            f"{caption}", transform=ax.transAxes, fontsize=6.5, color="white",
            va="top", zorder=5,
            bbox=dict(facecolor="black", alpha=0.4, pad=2, edgecolor="none"))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--bucket", default=os.environ.get("TRID3NT_RUNS_BUCKET",
                                                       "trid3nt-runs"))
    ap.add_argument("--out-dir", default=str(REPO / "docs" / "proof" / "templates"))
    ap.add_argument("--stem", default="telemac_river_dye")
    ns = ap.parse_args()

    out_dir = Path(ns.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id, bucket = ns.run_id, ns.bucket

    metrics = _read_json(bucket, f"{run_id}/metrics.json")
    worker = _read_json(bucket, f"{run_id}/telemac_metrics.json")
    reach = str(worker.get("reach_name") or worker.get("name") or run_id)
    peak_uri = f"s3://{bucket}/{run_id}/telemac_dye_peak.tif"
    vmin, vmax, cmap = _product_style(peak_uri)
    print(f"product style: rescale=({vmin}, {vmax}) colormap={cmap}")

    slf = _download(bucket, f"{run_id}/r2d_river.slf", ".slf")
    gif = out_dir / f"{ns.stem}_plume_animation.gif"
    frames = render_animation(slf, int(worker["utm_epsg"]), vmin=vmin, vmax=vmax,
                              cmap=cmap, out_path=gif, reach=reach, run_id=run_id)
    Path(slf).unlink(missing_ok=True)

    cog = _download(bucket, f"{run_id}/telemac_dye_peak.tif", ".tif")
    png = out_dir / f"{ns.stem}_peak_concentration.png"
    render_peak(cog, vmin=vmin, vmax=vmax, cmap=cmap, out_path=png, reach=reach,
                run_id=run_id,
                caption=(f"cmax {metrics['dye_cmax_mgl']:.3g} mg/L at "
                         f"t={metrics['dye_peak_time_s']:.0f} s, plume reach "
                         f"{metrics['plume_reach_m']:.0f} m"))
    Path(cog).unlink(missing_ok=True)

    print(json.dumps({"run_id": run_id, "frames": frames,
                      "animation": str(gif), "peak": str(png),
                      "animation_bytes": gif.stat().st_size,
                      "peak_bytes": png.stat().st_size}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
