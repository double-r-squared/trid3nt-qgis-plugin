"""Render the Mexico Beach surge inundation: first / peak / last flood_depth frames.

Produces a 3-panel figure (first wet frame, peak frame, last frame) of the SFINCS
flood-depth animation over the AOI, on a SHARED viridis depth scale with nodata
transparent, so the water is visibly climbing sea->land into town. Optionally also
a "rise above baseline" panel (peak depth minus the first wet frame depth) that
isolates the marching wet front.

Usage:
  python render_mexbeach_inundation.py --run-id <RUN_ID> \\
      --out /tmp/mexbeach_inundation_first_peak_last.png

Reads the per-frame flood_depth_frame_NN.tif + flood_depth_peak.tif COGs from
s3://$GRACE2_RUNS_BUCKET/<run-id>/ (boto3 / vsis3, creds from the env).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys


def _vsi(uri: str) -> str:
    return "/vsis3/" + uri[len("s3://"):] if uri.startswith("s3://") else uri


def _rio_env():
    import rasterio
    return rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.nc",
        AWS_REGION=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2",
    )


def _read(uri: str):
    import numpy as np
    import rasterio
    with _rio_env():
        with rasterio.open(_vsi(uri)) as ds:
            arr = ds.read(1).astype("float64")
            nd = ds.nodata
            if nd is not None and not (isinstance(nd, float) and math.isnan(nd)):
                arr = np.where(arr == nd, np.nan, arr)
            b = ds.bounds
            return arr, (b.left, b.right, b.bottom, b.top)


def _list_frames(bucket: str, run_id: str) -> list[str]:
    import boto3
    s3 = boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2",
    )
    pat = re.compile(r"flood_depth_frame_(\d+)\.tif$")
    found: list[tuple[int, str]] = []
    pag = s3.get_paginator("list_objects_v2")
    for page in pag.paginate(Bucket=bucket, Prefix=f"{run_id}/"):
        for o in page.get("Contents", []) or []:
            m = pat.search(o["Key"])
            if m:
                found.append((int(m.group(1)), f"s3://{bucket}/{o['Key']}"))
    found.sort(key=lambda t: t[0])
    return [u for _, u in found]


def main(argv: list[str]) -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib import colors as mcolors

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default="/tmp/mexbeach_inundation_first_peak_last.png")
    ap.add_argument("--rise-out", default="/tmp/mexbeach_rise_above_baseline.png")
    ap.add_argument("--wet-depth", type=float, default=0.05)
    args = ap.parse_args(argv)

    bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    if not bucket:
        print("GRACE2_RUNS_BUCKET must be set")
        return 2

    frame_uris = _list_frames(bucket, args.run_id)
    print(f"found {len(frame_uris)} frame COGs for run {args.run_id}")
    if not frame_uris:
        print("no frames -- cannot render")
        return 1

    frames = []
    extent = None
    for u in frame_uris:
        a, ext = _read(u)
        frames.append(a)
        extent = ext

    # first WET frame (any cell > wet-depth), peak frame (max wet area), last frame.
    def wet_count(a):
        return int(np.nansum(np.asarray(a) > args.wet_depth))

    counts = [wet_count(a) for a in frames]
    first_idx = next((i for i, c in enumerate(counts) if c > 0), 0)
    peak_idx = int(np.argmax(counts))
    last_idx = len(frames) - 1
    print(f"first_wet_frame={first_idx} (n={counts[first_idx]})  "
          f"peak_frame={peak_idx} (n={counts[peak_idx]})  last_frame={last_idx} (n={counts[last_idx]})")

    sel = [(first_idx, "first wet frame"), (peak_idx, "peak frame"), (last_idx, "last frame")]

    # shared depth scale across the three panels (mask <= wet-depth to transparent).
    all_wet = np.concatenate(
        [np.asarray(frames[i])[np.asarray(frames[i]) > args.wet_depth].ravel() for i, _ in sel]
        + [np.array([args.wet_depth])]
    )
    vmax = float(np.nanpercentile(all_wet, 99.0)) if all_wet.size else 1.0
    vmax = max(vmax, args.wet_depth * 4)
    norm = mcolors.Normalize(vmin=0.0, vmax=vmax)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(alpha=0.0)

    fig, axes = plt.subplots(1, 3, figsize=(15, 6), constrained_layout=True)
    im = None
    for ax, (idx, label) in zip(axes, sel):
        a = np.asarray(frames[idx], dtype="float64")
        masked = np.ma.masked_where(~(a > args.wet_depth), a)
        ax.imshow(np.zeros_like(a), extent=extent, cmap="gray", vmin=0, vmax=1,
                  alpha=0.12, origin="upper")  # faint land backdrop
        im = ax.imshow(masked, extent=extent, cmap=cmap, norm=norm, origin="upper")
        ax.set_title(f"{label}\n(frame {idx}, wet cells={counts[idx]})", fontsize=11)
        ax.set_xlabel("lon"); ax.set_ylabel("lat")
        ax.tick_params(labelsize=8)
    cb = fig.colorbar(im, ax=axes, shrink=0.85, location="right")
    cb.set_label("flood depth (m)")
    fig.suptitle(
        f"Mexico Beach surge inundation -- water climbing sea -> town\n"
        f"run {args.run_id}  (first / peak / last flood_depth frames, shared scale)",
        fontsize=12,
    )
    fig.savefig(args.out, dpi=130, bbox_inches="tight")
    print(f"wrote {args.out}")

    # --- rise-above-baseline panel: peak depth minus first-wet-frame depth ----
    try:
        base = np.asarray(frames[first_idx], dtype="float64")
        peak = np.asarray(frames[peak_idx], dtype="float64")
        base0 = np.where(np.isfinite(base) & (base > args.wet_depth), base, 0.0)
        peak0 = np.where(np.isfinite(peak) & (peak > args.wet_depth), peak, 0.0)
        rise = peak0 - base0
        rise_m = np.ma.masked_where(~(rise > args.wet_depth), rise)
        fig2, ax2 = plt.subplots(figsize=(7, 6), constrained_layout=True)
        ax2.imshow(np.zeros_like(rise), extent=extent, cmap="gray", vmin=0, vmax=1,
                   alpha=0.12, origin="upper")
        im2 = ax2.imshow(rise_m, extent=extent, cmap="magma", origin="upper",
                         vmin=0.0, vmax=max(vmax, float(np.nanpercentile(rise[rise > 0], 99)) if np.any(rise > 0) else 1.0))
        ax2.set_title(
            f"Rise above first-wet baseline (peak frame {peak_idx} - first frame {first_idx})\n"
            "the surge-driven wet front that marched inland", fontsize=11)
        ax2.set_xlabel("lon"); ax2.set_ylabel("lat")
        cb2 = fig2.colorbar(im2, ax=ax2, shrink=0.85)
        cb2.set_label("depth increase (m)")
        fig2.savefig(args.rise_out, dpi=130, bbox_inches="tight")
        print(f"wrote {args.rise_out}")
    except Exception as exc:  # noqa: BLE001
        print(f"(rise panel skipped: {type(exc).__name__}: {exc})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
