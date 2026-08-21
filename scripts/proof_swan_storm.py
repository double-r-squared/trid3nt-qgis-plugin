#!/usr/bin/env python
"""row 3 proof: SWAN NONSTATIONARY storm evolution.

Two figures into docs/proof/templates/ (named after the workflow file):
  * swan_wave_field_nonstationary_storm_peak_hs.png -- the peak-Hs field over
    ESRI World Imagery (white box = AOI).
  * swan_wave_field_nonstationary_storm_frames.png -- a filmstrip of time-stamped
    nearshore Hs frames (the scrubber animation), pinned frame time + a SHARED
    color scale in the captions, showing the storm build-peak-decay.

Env (MinIO): set -a; source .env.local; set +a
Usage: venvs/agent/bin/python scripts/proof_swan_storm.py <run_id>
"""
from __future__ import annotations

import os
import sys

import boto3
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import Normalize

from _env_guard import require_local_endpoint
from matplotlib.patches import Rectangle
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proof_swan_maps import fetch_basemap, reproject_to_3857, TO_3857  # reuse helpers

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "swan_wave_field_nonstationary_storm"
RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "01KZGWMV258B65HPEFEC2F3RFF"
FRAME_HOURS = 2.0  # out_delt = sim_duration/output_frames = 129600/18 = 7200 s
TMP = "/tmp/claude-1000/-home-nate-Documents-GRACE-2/fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad"


def _dl(s3, key, dst):
    s3.download_file(os.environ["TRID3NT_RUNS_BUCKET"], f"{RUN_ID}/{key}", dst)
    return dst


def main():
    s3 = boto3.client("s3", endpoint_url=require_local_endpoint())

    # ---- (1) peak-Hs map ----------------------------------------------------
    peak_tif = _dl(s3, "swan_wave_height_peak.tif", f"{TMP}/swan_peak.tif")
    hs, (w, e, s_, n), (lw, ls, le, ln) = reproject_to_3857(peak_tif)
    hs = np.where(hs > 0.05, hs, np.nan)
    vmax = float(np.nanpercentile(hs, 99.5)) if np.isfinite(hs).any() else 6.0
    pad_x = (le - lw) * 0.15
    pad_y = (ln - ls) * 0.15
    basemap, bm_ext = fetch_basemap(lw - pad_x, ls - pad_y, le + pad_x, ln + pad_y, 11)

    fig, ax = plt.subplots(figsize=(9, 8), dpi=110)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    im = ax.imshow(hs, extent=(w, e, s_, n), origin="upper", cmap="turbo",
                   norm=Normalize(0, vmax), alpha=0.85, zorder=3)
    ax0, ay0 = TO_3857.transform(lw, ls)
    ax1, ay1 = TO_3857.transform(le, ln)
    ax.add_patch(Rectangle((ax0, ay0), ax1 - ax0, ay1 - ay0, fill=False,
                           edgecolor="white", linewidth=1.6, zorder=4))
    wx0, _ = TO_3857.transform(lw - pad_x, ls); wx1, _ = TO_3857.transform(le + pad_x, ls)
    _, wy0 = TO_3857.transform(lw, ls - pad_y); _, wy1 = TO_3857.transform(lw, ln + pad_y)
    ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1); ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("SWAN nonstationary storm: peak significant wave height", fontsize=12)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label("peak Hs (m)")
    fig.text(0.01, 0.01,
             f"Mexico Beach / Tyndall FL shelf. Peak-over-36h Hs from a time-varying storm"
             f"boundary (build to Hs=6 m offshore at hour 18, decay). max Hs "
             f"{vmax:.1f} m. run {RUN_ID}. swan_wave_field mode=nonstationary "
             f"storm_peak_hs_m (ADR 0190 row 3).",
             fontsize=7, color="0.35", wrap=True)
    fig.tight_layout()
    p1 = os.path.join(OUT, f"{STEM}_peak_hs.png")
    fig.savefig(p1, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p1, "vmax=%.2f" % vmax)

    # ---- (2) time-stamped frame filmstrip -----------------------------------
    # frames at t=0,2,...,36 h; pick 0h, 12h, 18h(peak), 24h, 30h, 36h.
    pick = [1, 7, 10, 13, 16, 19]
    grids = []
    for fi in pick:
        tif = _dl(s3, f"swan_wave_height_frame_{fi:02d}.tif", f"{TMP}/swan_f{fi:02d}.tif")
        g, ext, ll = reproject_to_3857(tif)
        grids.append((fi, np.where(g > 0.05, g, np.nan), ext))
    fmax = max(float(np.nanpercentile(g, 99.5)) for _, g, _ in grids
              if np.isfinite(g).any())
    bm2, bm2_ext = fetch_basemap(lw - pad_x, ls - pad_y, le + pad_x, ln + pad_y, 10)

    fig, axes = plt.subplots(2, 3, figsize=(11, 7.4), dpi=110)
    for ax, (fi, g, (w, e, s_, n)) in zip(axes.ravel(), grids):
        t_hr = (fi - 1) * FRAME_HOURS
        ax.imshow(bm2, extent=bm2_ext, origin="upper")
        im = ax.imshow(g, extent=(w, e, s_, n), origin="upper", cmap="turbo",
                       norm=Normalize(0, fmax), alpha=0.85, zorder=3)
        ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1)
        ax.set_xticks([]); ax.set_yticks([])
        peak_tag = " (storm peak)" if fi == 10 else ""
        ax.set_title(f"t = {t_hr:.0f} h{peak_tag}", fontsize=9)
    cb = fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.6, pad=0.02)
    cb.set_label(f"Hs (m), shared scale 0-{fmax:.1f} m")
    fig.suptitle("SWAN nonstationary storm evolution: time-stamped nearshore Hs frames",
                 fontsize=12, y=0.98)
    fig.text(0.5, 0.005,
             "swan_wave_field mode=nonstationary storm_peak_hs_m: the offshore "
             "boundary builds to 6 m at t=18 h then decays; the nearshore wave "
             "field marches with it (row 3; frames feed the scrubber "
             f"animation). run {RUN_ID}.",
             ha="center", fontsize=6.5, color="0.4")
    p2 = os.path.join(OUT, f"{STEM}_frames.png")
    fig.savefig(p2, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p2, "frame_vmax=%.2f" % fmax)
    for fi, g, _ in grids:
        print(f"  frame {fi:02d}  t={ (fi-1)*FRAME_HOURS:.0f}h  maxHs={np.nanmax(g):.3f} m")


if __name__ == "__main__":
    main()
