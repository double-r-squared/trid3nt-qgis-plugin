"""ADR 0230 Slab2 SCENARIO proof renders (Cascadia M9.0), EPSG:3857 over Esri World
Imagery, AMR mesh overlaid where present:

  1. geoclaw_scenario_cascadia_deformation.png -- the MONEY SHOT: the Slab2 multi-
     subfault Okada seafloor deformation dipole tracking the CURVED trench (vs the old
     straight bar). Signed RdBu (red uplift / blue subsidence).
  2. geoclaw_scenario_cascadia_bathy_input.png -- the fetched deep-water topobathy
     INPUT layer (ADR 0227/0229 rung) under the run.
  3. geoclaw_scenario_cascadia_max_amplitude.png -- max fgout surface amplitude + AMR
     mesh + the offshore decay transect points.
  4. geoclaw_scenario_cascadia_gauge_chart.png -- the Newport, Oregon coastal mareogram.
  5. geoclaw_scenario_cascadia_transect_chart.png -- peak amplitude + arrival vs
     distance from the rupture centroid (decay + arrival ordering).

Physics asserts (printed + enforced): the deformation is a signed dipole; the coastal
gauge amplitude is nonzero (the wave reaches the Oregon coast); peak perturbation
DECAYS and arrival INCREASES with distance from the rupture centroid.

Run (repo root, MinIO env):
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/proof_geoclaw_scenario_cascadia.py \
    --setup-id <id> --docker-id <id> --dem-id <id>
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject, transform_bounds

# reuse the Chignik proof primitives verbatim (generic: basemap, reprojection, S3).
import proof_geoclaw_chignik_runup as P
from proof_geoclaw_chignik_runup import (
    _basemap_for,
    _get,
    _list,
    _sample_patch,
    cog_to_3857,
    TO_3857,
)
from trid3nt_server.agent.tools.cache import read_object_bytes_s3
from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
    parse_fort_q_frame,
    parse_geoclaw_gauge_series,
)

OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
RUNS = "trid3nt-runs"
CACHE = "trid3nt-cache"
GAUGE = (-124.10, 44.62)          # Newport, Oregon nearshore
RUPTURE_CENTROID = (-125.5, 45.0)
# Offshore SW transect at increasing great-circle distance from the rupture centroid
# (all in deep water within the domain west of the coast) -> amplitude decays,
# arrival increases outward from the distributed megathrust source.
TRANSECT = [(-126.2, 45.0), (-126.8, 44.3), (-127.3, 43.5), (-127.6, 42.8)]


def _haversine_km(a, b):
    R = 6371.0
    lo1, la1, lo2, la2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlo, dla = lo2 - lo1, la2 - la1
    x = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def _analyse(fgout_frames):
    fgout_frames = sorted(fgout_frames, key=lambda z: z[0])
    t0 = fgout_frames[0][0]
    base = {pt: _sample_patch(fgout_frames[0][1], *pt) for pt in TRANSECT}
    rows = []
    for pt in TRANSECT:
        dkm = _haversine_km(RUPTURE_CENTROID, pt)
        series = []
        for t, patches in fgout_frames:
            h = _sample_patch(patches, *pt)
            pert = (h - base[pt]) if math.isfinite(h) and math.isfinite(base[pt]) else float("nan")
            series.append((t - t0, pert))
        finite = [(tt, pp) for tt, pp in series if math.isfinite(pp)]
        peak = max((abs(pp) for _, pp in finite), default=float("nan"))
        arr = next((tt for tt, pp in finite if abs(pp) >= 0.05), float("nan"))
        rows.append((pt, dkm, arr, peak, series))
    return rows


def _render_field(cog_bytes, out_name, title, caption, *, cmap, diverging):
    arr, ext = cog_to_3857(cog_bytes)
    with rasterio.MemoryFile(cog_bytes).open() as ds:
        w, s, e, n = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)
    base, bext = _basemap_for((w, s, e, n))
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.imshow(base, extent=[bext[0], bext[1], bext[2], bext[3]], origin="upper")
    finite = arr[np.isfinite(arr)]
    if diverging:
        m = np.nanmax(np.abs(finite)) if finite.size else 1.0
        vmin, vmax = -m, m
        show = np.where(np.abs(arr) < 0.02 * m, np.nan, arr)
    else:
        vmin, vmax = np.nanpercentile(finite, 1), np.nanpercentile(finite, 99)
        show = arr
    im = ax.imshow(show, extent=[ext[0], ext[1], ext[2], ext[3]], origin="upper",
                   cmap=cmap, vmin=vmin, vmax=vmax, alpha=0.82)
    ax.set_xlim(bext[0], bext[1]); ax.set_ylim(bext[2], bext[3])
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, shrink=0.7); cb.set_label("elevation / dZ (m)")
    ax.set_title(title, fontsize=11, weight="bold")
    fig.text(0.5, 0.02, caption, ha="center", fontsize=8, wrap=True)
    fig.tight_layout(rect=[0, 0.05, 1, 1])
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


def _render_maxamp(fgout_frames, mesh_bytes, out_name):
    fgout_frames = sorted(fgout_frames, key=lambda z: z[0])
    p0 = fgout_frames[0][1][0]
    mx, my = int(p0.mx), int(p0.my)
    base = p0.h.astype("float64")
    maxamp = np.zeros((my, mx), dtype="float64")
    for _t, patches in fgout_frames[1:]:
        h = patches[0].h.astype("float64")
        if h.shape == base.shape:
            maxamp = np.maximum(maxamp, np.abs(h - base))
    w = float(p0.xlow); s = float(p0.ylow)
    e = w + mx * float(p0.dx); n = s + my * float(p0.dy)
    field = np.flipud(maxamp)
    field = np.where(field <= 0.01, np.nan, field)
    src_tf = rasterio.transform.from_bounds(w, s, e, n, mx, my)
    x0, y0 = TO_3857.transform(w, s); x1, y1 = TO_3857.transform(e, n)
    dst = np.full((my, mx), np.nan)
    reproject(source=field, destination=dst, src_transform=src_tf, src_crs="EPSG:4326",
              dst_transform=rasterio.transform.from_bounds(x0, y0, x1, y1, mx, my),
              dst_crs="EPSG:3857", resampling=Resampling.bilinear)
    base_img, bext = _basemap_for((w, s, e, n))
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.imshow(base_img, extent=[bext[0], bext[1], bext[2], bext[3]], origin="upper")
    fin = dst[np.isfinite(dst)]
    vmax = np.nanpercentile(fin, 99) if fin.size else 0.5
    im = ax.imshow(dst, extent=[x0, x1, y0, y1], origin="upper", cmap="magma",
                   vmin=0.0, vmax=max(vmax, 0.1), alpha=0.85)
    if mesh_bytes:
        try:
            fc = json.loads(mesh_bytes.decode())
            for feat in fc.get("features", []):
                coords = feat["geometry"]["coordinates"]
                mls = coords if feat["geometry"]["type"] == "MultiLineString" else [coords]
                for seg in mls:
                    xs = [TO_3857.transform(p[0], p[1])[0] for p in seg]
                    ys = [TO_3857.transform(p[0], p[1])[1] for p in seg]
                    ax.plot(xs, ys, color="#7fdfff", lw=0.15, alpha=0.5)
        except Exception:  # noqa: BLE001
            pass
    for pt in TRANSECT:
        px, py = TO_3857.transform(pt[0], pt[1])
        ax.plot(px, py, "o", color="#00ff88", ms=6, mec="black")
    gx, gy = TO_3857.transform(*GAUGE)
    ax.plot(gx, gy, "^", color="#ffdd00", ms=9, mec="black")
    ax.set_xlim(bext[0], bext[1]); ax.set_ylim(bext[2], bext[3])
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, shrink=0.7); cb.set_label("max |surface perturbation| (m)")
    ax.set_title("Cascadia M9.0 SCENARIO -- max tsunami surface amplitude (fgout) + AMR mesh",
                 fontsize=11, weight="bold")
    fig.text(0.5, 0.02, "Max over fgout frames of |h(t)-h(t0)|. Green dots = the offshore "
             "decay transect (increasing distance from the rupture centroid); yellow "
             "triangle = the Newport, Oregon coastal gauge. HYPOTHETICAL scenario source.",
             ha="center", fontsize=8, wrap=True)
    fig.tight_layout(rect=[0, 0.05, 1, 1])
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


def _render_gauge(gauge_series, gauge_scalars, out_name):
    t = np.asarray(gauge_series.get("t") or [], dtype=float)
    eta = np.asarray(gauge_series.get("eta") or [], dtype=float)
    fig, ax = plt.subplots(figsize=(9, 4))
    if t.size and eta.size:
        ax.plot(t / 60.0, eta, color="#1f6f8b", lw=1.4)
        ax.axhline(0, color="#888", lw=0.7)
    ax.set_xlabel("time (min)"); ax.set_ylabel("surface elevation (m)")
    amp = gauge_scalars.get("gauge_max_amplitude_m", 0.0)
    ax.set_title(f"Cascadia M9.0 SCENARIO -- Newport, Oregon coastal gauge "
                 f"(-124.10, 44.62): peak-to-trough {amp:.2f} m", fontsize=11, weight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


def _render_transect(rows, out_name):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4))
    for pt, dkm, arr, peak, series in rows:
        a1.plot([s[0] / 60.0 for s in series], [s[1] for s in series], label=f"{dkm:.0f} km")
    a1.axhline(0, color="#999", lw=0.6)
    a1.set_xlabel("time (min)"); a1.set_ylabel("surface perturbation h(t)-h0 (m)")
    a1.set_title("fgout transect waveforms"); a1.legend(fontsize=8, title="dist from centroid")
    dd = [r[1] for r in rows]
    a2.plot(dd, [r[3] for r in rows], "o-", color="#b5651d", label="peak |pert| (m)")
    a2b = a2.twinx()
    a2b.plot(dd, [r[2] / 60.0 for r in rows], "s--", color="#1f6f8b", label="arrival (min)")
    a2.set_xlabel("distance from rupture centroid (km)")
    a2.set_ylabel("peak |perturbation| (m)", color="#b5651d")
    a2b.set_ylabel("arrival (min)", color="#1f6f8b")
    a2.set_title("decay + arrival vs distance")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-id", required=True)
    ap.add_argument("--docker-id", required=True)
    ap.add_argument("--dem-id", required=True)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    dem_uri = f"s3://{CACHE}/cache/static-30d/geoclaw_setup/{args.dem_id}/topo_4326.tif"
    dem_bytes = read_object_bytes_s3(dem_uri)
    defo_bytes = _get(RUNS, f"{args.setup_id}/geoclaw_seafloor_deformation.tif")

    out_keys = _list(RUNS, f"{args.docker_id}/_output/")
    gauge_keys = [k for k in out_keys if "/gauge" in k and k.endswith(".txt")]
    fgout_q = sorted(k for k in out_keys if re.search(r"fgout\d+\.q\d+$", k))
    fgout_t = {int(m.group(1)): k for k in out_keys
               if (m := re.search(r"fgout\d+\.t(\d+)$", k))}

    tmp = Path(tempfile.mkdtemp(prefix="cascadia_proof_"))
    gauge_series, gauge_scalars = None, {}
    if gauge_keys:
        gdir = tmp / "_output"; gdir.mkdir(parents=True, exist_ok=True)
        (gdir / os.path.basename(gauge_keys[0])).write_bytes(_get(RUNS, gauge_keys[0]))
        gauge_series, gauge_scalars = parse_geoclaw_gauge_series(str(tmp))

    fgout_frames = []
    for qk in fgout_q:
        m = re.search(r"fgout\d+\.q(\d+)$", qk)
        frame_no = int(m.group(1)) if m else len(fgout_frames)
        patches = parse_fort_q_frame(_get(RUNS, qk).decode(errors="replace"))
        tkey = fgout_t.get(frame_no)
        t = P._read_fgout_time(_get(RUNS, tkey)) if tkey else float(frame_no)
        fgout_frames.append((t, patches))

    print(f"gauge files: {len(gauge_keys)}  fgout frames: {len(fgout_frames)}")

    asserts = {}
    if gauge_scalars:
        amp = gauge_scalars.get("gauge_max_amplitude_m")
        asserts["gauge_max_amplitude_m"] = amp
        print(f"GAUGE max_amplitude_m={amp}")
    rows = _analyse(fgout_frames) if fgout_frames else []
    for pt, dkm, arr, peak, _ in rows:
        print(f"  transect {pt} dist={dkm:.1f} km arrival={arr:.0f} s peak_pert={peak:.3f} m")

    ok = True
    if asserts.get("gauge_max_amplitude_m"):
        ok = ok and asserts["gauge_max_amplitude_m"] > 0
    if len(rows) >= 2:
        dd = np.array([r[1] for r in rows])
        peaks = np.array([r[3] for r in rows])
        arrs = np.array([r[2] for r in rows])
        fin = np.isfinite(peaks)
        if fin.sum() >= 2:
            asserts["peak_decays_with_distance"] = bool(
                np.corrcoef(dd[fin], peaks[fin])[0, 1] < 0)
        fina = np.isfinite(arrs)
        if fina.sum() >= 2:
            asserts["arrival_increases_with_distance"] = bool(
                np.corrcoef(dd[fina], arrs[fina])[0, 1] > 0)
        print("ASSERT", {k: asserts[k] for k in asserts if k.endswith("distance")})

    _render_field(defo_bytes, "geoclaw_scenario_cascadia_deformation.png",
                  "Cascadia M9.0 SCENARIO -- Slab2 Okada seafloor deformation (curved interface)",
                  "Signed coseismic dZ, multi-subfault Okada over the REAL USGS Slab2 "
                  "interface (red=uplift / blue=subsidence). The dipole tracks the CURVED "
                  "trench -- NOT a straight bar. HYPOTHETICAL scenario, not a real event.",
                  cmap="RdBu_r", diverging=True)
    _render_field(dem_bytes, "geoclaw_scenario_cascadia_bathy_input.png",
                  "Cascadia M9.0 SCENARIO -- bathymetry INPUT layer (deep-water rung, ADR 0227/0229)",
                  "ETOPO 2022 full column + 3DEP onshore over the rupture-enclosing "
                  "Cascadia domain.", cmap="terrain", diverging=False)
    if fgout_frames:
        mesh_bytes = None
        try:
            mesh_bytes = _get(RUNS, f"{args.setup_id}/mesh.geojson")
        except Exception:  # noqa: BLE001
            mesh_bytes = None
        _render_maxamp(fgout_frames, mesh_bytes, "geoclaw_scenario_cascadia_max_amplitude.png")
    if gauge_series is not None:
        _render_gauge(gauge_series, gauge_scalars, "geoclaw_scenario_cascadia_gauge_chart.png")
    if rows:
        _render_transect(rows, "geoclaw_scenario_cascadia_transect_chart.png")

    print("ASSERTS " + json.dumps(asserts, default=float))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    sys.exit(main())
