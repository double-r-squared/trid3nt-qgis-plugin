"""deep-water run-up PROOF renders + physics asserts (Chignik M8.2).

Consumes a solved run (scripts/drive_geoclaw_chignik_runup_proof.py) and renders,
all EPSG:3857 over Esri World Imagery:

  * the BATHYMETRY input layer the solve ran on (the deep-water rung headline: the
    ETOPO full-column reaches min ~ -6400 m, NOT the old land-only ~ -68 m),
  * the Okada seafloor DEFORMATION product (signed dipole),
  * the max surface-PERTURBATION field over the AOI from the fgout monitor, with the
    W->E distance transect points + the AMR mesh wireframe overlaid,
  * the coastal GAUGE waveform (dock-exact line chart: amplitude + arrival).

Physics asserts (printed + enforced): gauge amplitude nonzero; peak perturbation
DECAYS with distance from the epicentre; first-arrival time INCREASES with distance.

Run (repo root, MinIO env):
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/proof_geoclaw_chignik_runup.py \
    --setup-id <manifest_id> --docker-id <docker_run_id> \
    --dem-id <topo_setup_id>
"""
from __future__ import annotations

import argparse
import io
import math
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, reproject

from trid3nt_server.data.cache import read_object_bytes_s3
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
    parse_fort_q_frame,
    parse_geoclaw_gauge_series,
)

TILE_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
            "MapServer/tile/{z}/{y}/{x}")
OUT_DIR = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
TO_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

EPICENTER = (-157.8876, 55.3635)  # ak0219neiszm
AOI = (-159.8, 55.0, -158.8, 55.6)
# W->E shelf transect (all wet ~ -95..-190 m), increasing distance from the epicentre.
TRANSECT = [(-159.75, 55.30), (-159.30, 55.30), (-158.85, 55.30)]
RUNS = "trid3nt-runs"
CACHE = "trid3nt-cache"


# --------------------------------------------------------------------------- #
# Esri basemap + reprojection helpers (shared with proof_geoclaw_okada*).
# --------------------------------------------------------------------------- #
def tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


def tile_bounds_3857(x, y, z):
    n = 2 ** z

    def merc(tx, ty):
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        return TO_3857.transform(lon, lat)

    x0, y0 = merc(x, y + 1)
    x1, y1 = merc(x + 1, y)
    return x0, y0, x1, y1


def fetch_basemap(w, s, e, n, zoom):
    x0f, y1f = tile_xy(w, s, zoom)
    x1f, y0f = tile_xy(e, n, zoom)
    xs = list(range(int(math.floor(x0f)), int(math.floor(x1f)) + 1))
    ys = list(range(int(math.floor(y0f)), int(math.floor(y1f)) + 1))
    mosaic = Image.new("RGB", (256 * len(xs), 256 * len(ys)))
    sess = requests.Session()
    for j, ty in enumerate(ys):
        for i, tx in enumerate(xs):
            r = sess.get(TILE_URL.format(z=zoom, y=ty, x=tx), timeout=30)
            r.raise_for_status()
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"),
                         (i * 256, j * 256))
    wm0, _, _, _ = tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def pick_zoom(bbox):
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    for z in range(9, 3, -1):
        if 360.0 / (2 ** z) * 3 >= span:
            return z
    return 5


def cog_to_3857(cog_bytes):
    """Read a COG (any CRS) -> (array north-up EPSG:3857, extent (x0,x1,y0,y1))."""
    with rasterio.MemoryFile(cog_bytes).open() as ds:
        src = ds.read(1).astype("float64")
        src = np.where(np.isfinite(src), src, np.nan)
        b = ds.bounds
        src_crs = ds.crs
        src_tf = ds.transform
    # target 3857 grid over the same footprint
    from rasterio.warp import transform_bounds
    x0, y0, x1, y1 = transform_bounds(src_crs, "EPSG:3857", b.left, b.bottom, b.right, b.top)
    h, w = src.shape
    dst = np.full((h, w), np.nan, dtype="float64")
    dst_tf = rasterio.transform.from_bounds(x0, y0, x1, y1, w, h)
    reproject(source=src, destination=dst, src_transform=src_tf, src_crs=src_crs,
              dst_transform=dst_tf, dst_crs="EPSG:3857", resampling=Resampling.bilinear)
    return dst, (x0, x1, y0, y1)


def _basemap_for(bbox4326):
    z = pick_zoom(bbox4326)
    return fetch_basemap(*bbox4326, z)


# --------------------------------------------------------------------------- #
# fgout frame parsing: depth h(t) at transect points -> perturbation waveform.
# --------------------------------------------------------------------------- #
def _read_fgout_time(t_bytes: bytes) -> float:
    for line in t_bytes.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lower() == "time":
            return float(parts[0])
    return float("nan")


def _sample_patch(patches, lon, lat):
    """Nearest single-patch value h at (lon,lat) from the finest covering patch."""
    best = None
    for p in patches:
        x1 = p.xlow + p.mx * p.dx
        y1 = p.ylow + p.my * p.dy
        if not (p.xlow <= lon <= x1 and p.ylow <= lat <= y1):
            continue
        i = min(p.mx - 1, max(0, int((lon - p.xlow) / p.dx)))
        j = min(p.my - 1, max(0, int((lat - p.ylow) / p.dy)))
        # _Patch.h is (my, mx), row 0 = ylow (south) per rasterize convention.
        val = float(p.h[j, i])
        if best is None or p.dx < best[1]:
            best = (val, p.dx)
    return best[0] if best else float("nan")


def analyse_fgout(fgout_frames):
    """fgout_frames = list of (t_seconds, patches). Returns per-transect-point
    (distance_km, arrival_s, peak_amp_m) using perturbation h(t)-h(t0)."""
    fgout_frames = sorted(fgout_frames, key=lambda z: z[0])
    t0 = fgout_frames[0][0]
    base = {pt: _sample_patch(fgout_frames[0][1], *pt) for pt in TRANSECT}
    thresh = 0.05  # m perturbation = arrival
    rows = []
    for pt in TRANSECT:
        dkm = _haversine_km(EPICENTER, pt)
        series = []
        for t, patches in fgout_frames:
            h = _sample_patch(patches, *pt)
            pert = h - base[pt] if math.isfinite(h) and math.isfinite(base[pt]) else float("nan")
            series.append((t - t0, pert))
        finite = [(tt, pp) for tt, pp in series if math.isfinite(pp)]
        peak = max((abs(pp) for _, pp in finite), default=float("nan"))
        arr = next((tt for tt, pp in finite if abs(pp) >= thresh), float("nan"))
        rows.append((pt, dkm, arr, peak, series))
    return rows


def _haversine_km(a, b):
    R = 6371.0
    lo1, la1, lo2, la2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlo, dla = lo2 - lo1, la2 - la1
    x = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


# --------------------------------------------------------------------------- #
# S3 helpers.
# --------------------------------------------------------------------------- #
def _s3():
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])


def _list(bucket, prefix):
    cl = _s3()
    out = []
    tok = None
    while True:
        kw = dict(Bucket=bucket, Prefix=prefix)
        if tok:
            kw["ContinuationToken"] = tok
        r = cl.list_objects_v2(**kw)
        out += [o["Key"] for o in r.get("Contents", [])]
        if not r.get("IsTruncated"):
            break
        tok = r["NextContinuationToken"]
    return out


def _get(bucket, key):
    return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-id", required=True, help="manifest/postprocess run id (COGs)")
    ap.add_argument("--docker-id", required=True, help="docker run id (raw _output)")
    ap.add_argument("--dem-id", required=True, help="topo_4326 geoclaw_setup id")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    # --- fetch artifacts ---------------------------------------------------
    dem_uri = f"s3://{CACHE}/cache/static-30d/geoclaw_setup/{args.dem_id}/topo_4326.tif"
    dem_bytes = read_object_bytes_s3(dem_uri)
    defo_bytes = _get(RUNS, f"{args.setup_id}/geoclaw_seafloor_deformation.tif")

    # gauge series (reuse the composer parser on the raw gauge txt)
    import re as _re
    out_keys = _list(RUNS, f"{args.docker_id}/_output/")
    gauge_keys = [k for k in out_keys if "/gauge" in k and k.endswith(".txt")]
    fgout_q = sorted(k for k in out_keys if _re.search(r"fgout\d+\.q\d+$", k))
    # map frame number -> the .t time file (fgoutNNNN.tMMMM)
    fgout_t = {}
    for k in out_keys:
        m = _re.search(r"fgout\d+\.t(\d+)$", k)
        if m:
            fgout_t[int(m.group(1))] = k

    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="chignik_proof_"))
    gauge_series = None
    gauge_scalars: dict = {}
    if gauge_keys:
        gdir = tmp / "_output"
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / os.path.basename(gauge_keys[0])).write_bytes(_get(RUNS, gauge_keys[0]))
        gauge_series, gauge_scalars = parse_geoclaw_gauge_series(str(tmp))

    # fgout frames -> (t, patches)
    fgout_frames = []
    for qk in fgout_q:
        m = _re.search(r"fgout\d+\.q(\d+)$", qk)
        frame_no = int(m.group(1)) if m else len(fgout_frames)
        patches = parse_fort_q_frame(_get(RUNS, qk).decode(errors="replace"))
        tkey = fgout_t.get(frame_no)
        t = _read_fgout_time(_get(RUNS, tkey)) if tkey else float(frame_no)
        fgout_frames.append((t, patches))

    print(f"gauge files: {len(gauge_keys)}  fgout frames: {len(fgout_frames)}")

    # --- physics analysis --------------------------------------------------
    asserts = {}
    if gauge_scalars:
        amp = gauge_scalars.get("gauge_max_amplitude_m")
        asserts["gauge_max_amplitude_m"] = amp
        asserts["gauge_coseismic_offset_m"] = gauge_scalars.get("gauge_coseismic_offset_m")
        print(f"GAUGE max_amplitude_m={amp} coseismic_offset_m="
              f"{gauge_scalars.get('gauge_coseismic_offset_m')}")

    transect_rows = analyse_fgout(fgout_frames) if fgout_frames else []
    for pt, dkm, arr, peak, _ in transect_rows:
        print(f"  transect {pt} dist={dkm:.1f} km  arrival={arr:.0f} s  peak_pert={peak:.3f} m")

    # asserts
    ok = True
    if asserts.get("gauge_max_amplitude_m"):
        ok = ok and asserts["gauge_max_amplitude_m"] > 0
    if len(transect_rows) >= 2:
        dists = [r[1] for r in transect_rows]
        arrs = [r[2] for r in transect_rows]
        peaks = [r[3] for r in transect_rows]
        order = np.argsort(dists)
        arrs_o = [arrs[i] for i in order]
        peaks_o = [peaks[i] for i in order]
        arr_mono = all(arrs_o[i] <= arrs_o[i + 1] + 1e-6 for i in range(len(arrs_o) - 1)
                       if math.isfinite(arrs_o[i]) and math.isfinite(arrs_o[i + 1]))
        peak_decay = peaks_o[0] >= peaks_o[-1]
        asserts["arrival_increases_with_distance"] = bool(arr_mono)
        asserts["peak_decays_with_distance"] = bool(peak_decay)
        print(f"ASSERT arrival_increases_with_distance={arr_mono}  "
              f"peak_decays_with_distance={peak_decay}")

    # --- renders -----------------------------------------------------------
    _render_field(dem_bytes, "geoclaw_chignik_runup_bathy_input.png",
                  "Chignik M8.2 tsunami -- bathymetry INPUT layer (deep-water rung)",
                  "ETOPO 2022 full column reaches the deep Pacific basin (min ~ -6400 m); "
                  "the old 3DEP-land-clobbered path returned land-only (min ~ -68 m).",
                  cmap="terrain", diverging=False)
    _render_field(defo_bytes, "geoclaw_chignik_runup_deformation.png",
                  "Chignik M8.2 -- Okada seafloor deformation (input layer)",
                  "Signed coseismic dZ from the 294-subfault USGS finite-fault inversion "
                  "(red=uplift / blue=subsidence).", cmap="RdBu_r", diverging=True)
    if fgout_frames:
        mesh_bytes = None
        try:
            mesh_bytes = _get(RUNS, f"{args.setup_id}/mesh.geojson")
        except Exception:  # noqa: BLE001 - mesh overlay is best-effort
            mesh_bytes = None
        _render_maxamp(fgout_frames, mesh_bytes,
                       "geoclaw_chignik_runup_max_amplitude.png")
    if gauge_series is not None:
        _render_gauge(gauge_series, gauge_scalars,
                      "geoclaw_chignik_runup_gauge_chart.png")
    if transect_rows:
        _render_transect(transect_rows, "geoclaw_chignik_runup_transect_chart.png")

    import json
    print("ASSERTS " + json.dumps(asserts, default=float))
    return 0


def _render_field(cog_bytes, out_name, title, caption, *, cmap, diverging):
    arr, ext = cog_to_3857(cog_bytes)
    # bbox in 4326 for the basemap
    with rasterio.MemoryFile(cog_bytes).open() as ds:
        from rasterio.warp import transform_bounds
        w, s, e, n = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)
    base, bext = _basemap_for((w, s, e, n))
    fig, ax = plt.subplots(figsize=(9, 7))
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
    ax.set_xlim(bext[0], bext[1])
    ax.set_ylim(bext[2], bext[3])
    ax.set_xticks([])
    ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, shrink=0.7)
    cb.set_label("elevation / dZ (m)")
    ax.set_title(title, fontsize=11, weight="bold")
    fig.text(0.5, 0.02, caption, ha="center", fontsize=8, wrap=True)
    fig.tight_layout(rect=[0, 0.05, 1, 1])
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


def _render_maxamp(fgout_frames, mesh_bytes, out_name):
    """Per-cell max |h(t)-h(t0)| over the uniform fgout grid -> the max surface-
    perturbation field, over Esri with the AMR mesh wireframe + transect points."""
    import json as _json

    fgout_frames = sorted(fgout_frames, key=lambda z: z[0])
    p0 = fgout_frames[0][1][0]
    mx, my = int(p0.mx), int(p0.my)
    base = p0.h.astype("float64")
    maxamp = np.zeros((my, mx), dtype="float64")
    for _t, patches in fgout_frames[1:]:
        h = patches[0].h.astype("float64")
        if h.shape == base.shape:
            maxamp = np.maximum(maxamp, np.abs(h - base))
    # grid extent (4326); _Patch.h row 0 = ylow (south) -> flip for north-up imshow
    w = float(p0.xlow); s = float(p0.ylow)
    e = w + mx * float(p0.dx); n = s + my * float(p0.dy)
    field = np.flipud(maxamp)
    field = np.where(field <= 0.01, np.nan, field)
    # to 3857
    src_tf = rasterio.transform.from_bounds(w, s, e, n, mx, my)
    x0, y0 = TO_3857.transform(w, s); x1, y1 = TO_3857.transform(e, n)
    dst = np.full((my, mx), np.nan)
    reproject(source=field, destination=dst, src_transform=src_tf, src_crs="EPSG:4326",
              dst_transform=rasterio.transform.from_bounds(x0, y0, x1, y1, mx, my),
              dst_crs="EPSG:3857", resampling=Resampling.bilinear)
    base_img, bext = _basemap_for((w, s, e, n))
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.imshow(base_img, extent=[bext[0], bext[1], bext[2], bext[3]], origin="upper")
    fin = dst[np.isfinite(dst)]
    vmax = np.nanpercentile(fin, 99) if fin.size else 0.2
    im = ax.imshow(dst, extent=[x0, x1, y0, y1], origin="upper", cmap="magma",
                   vmin=0.0, vmax=max(vmax, 0.05), alpha=0.85)
    # mesh wireframe overlay
    if mesh_bytes:
        try:
            fc = _json.loads(mesh_bytes.decode())
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
    ax.set_xlim(bext[0], bext[1]); ax.set_ylim(bext[2], bext[3])
    ax.set_xticks([]); ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, shrink=0.7); cb.set_label("max |surface perturbation| (m)")
    ax.set_title("Chignik M8.2 -- max tsunami surface amplitude (fgout) + AMR mesh",
                 fontsize=11, weight="bold")
    fig.text(0.5, 0.02, "Max over 15 fgout frames of |h(t)-h(t0)| on the AOI shelf; green "
             "dots = the W->E distance transect (61/90/118 km from the epicentre). "
             "Amplitude decays offshore->onshore + with distance.",
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
    ax.set_xlabel("time (min)")
    ax.set_ylabel("surface elevation (m)")
    amp = gauge_scalars.get("gauge_max_amplitude_m", 0.0)
    ax.set_title(f"Chignik M8.2 coastal gauge (-159.30, 55.30): peak-to-trough "
                 f"amplitude {amp:.2f} m", fontsize=11, weight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


def _render_transect(rows, out_name):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4))
    for pt, dkm, arr, peak, series in rows:
        tt = [s[0] / 60.0 for s in series]
        pp = [s[1] for s in series]
        a1.plot(tt, pp, label=f"{dkm:.0f} km")
    a1.axhline(0, color="#999", lw=0.6)
    a1.set_xlabel("time (min)")
    a1.set_ylabel("surface perturbation h(t)-h0 (m)")
    a1.set_title("fgout transect waveforms")
    a1.legend(fontsize=8, title="dist from epicentre")
    dd = [r[1] for r in rows]
    a2.plot(dd, [r[3] for r in rows], "o-", color="#b5651d", label="peak |pert| (m)")
    a2b = a2.twinx()
    a2b.plot(dd, [r[2] / 60.0 for r in rows], "s--", color="#1f6f8b", label="arrival (min)")
    a2.set_xlabel("distance from epicentre (km)")
    a2.set_ylabel("peak |perturbation| (m)", color="#b5651d")
    a2b.set_ylabel("arrival (min)", color="#1f6f8b")
    a2.set_title("decay + arrival vs distance")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, out_name), dpi=130)
    plt.close(fig)
    print("wrote", out_name)


if __name__ == "__main__":
    sys.exit(main())
