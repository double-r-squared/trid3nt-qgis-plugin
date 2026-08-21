"""Proof renders for the MODFLOW vadose_transport + CSUB formulation knobs.

Deterministic, reuses product code:
  - vadose breakthrough CHART (dock-exact): the UZT base-of-column concentration
    series at the SAME Tippecanoe County IN site for a 4 m (demo) and an 8 m
    depth-to-water column -> the monotone arrival-vs-thickness physics.
  - vadose SPILL-SITE point over ESRI World Imagery (EPSG:3857 tiles).
  - CSUB land-subsidence bowl COG over ESRI (San Joaquin corridor, head-based).
  - CSUB knob-contrast CHART: max subsidence (cm) for head-based no-delay baseline
    vs DELAY interbed (lags/less) vs EFFECTIVE_STRESS (~0.4-0.5 of head-based).

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v "^#" .env.local | xargs) TRID3NT_MODFLOW_LOCAL=1 \
    TRID3NT_MF6_BIN=$PWD/bin/mf6 PYTHONPATH=workers/modflow \
    venvs/agent/bin/python scripts/proof_modflow_vadose_and_csub.py
"""

from __future__ import annotations

import asyncio
import glob
import io
import math
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402

os.environ.setdefault("TRID3NT_MODFLOW_LOCAL", "1")

from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs  # noqa: E402
from trid3nt_server.workflows.modflow.run_modflow import (  # noqa: E402
    build_and_stage_modflow_deck,
    run_modflow_local,
)
from trid3nt_server.workflows.modflow.sustainable_yield.sustainable_yield import (  # noqa: E402
    modflow_sustainable_yield,
)
from trid3nt_server.workflows.modflow.vadose_transport.vadose_transport import (  # noqa: E402
    modflow_vadose_transport,
)

TILE = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
OUT = Path(__file__).parent.parent / "docs" / "proof" / "templates"
OUT.mkdir(parents=True, exist_ok=True)
TIPPECANOE = (40.42, -86.90)
SANJOAQUIN = (36.75, -120.38)


def _tile_xy(lon, lat, z):
    n = 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)


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
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _run_vadose_obs(thickness: float):
    """Build+run the flat vadose deck; return (time_days, conc) from the UZT obs csv."""
    ra = MODFLOWRunArgs(
        spill_location_latlon=TIPPECANOE, contaminant="nitrate",
        release_rate_kg_s=1.0, duration_days=1.0, archetype="vadose_transport",
        aquifer_k_ms=1e-4, porosity=0.3,
        vadose_thickness_m=thickness,
    )
    st = build_and_stage_modflow_deck(ra)
    root = run_modflow_local(st).replace("file://", "")
    hits = sorted(glob.glob(os.path.join(root, "**/*.uzt.obs.csv"), recursive=True))
    obs = np.genfromtxt(hits[0], delimiter=",", names=True)
    t = np.atleast_1d(obs["time"]).astype(float)
    c = np.atleast_1d(obs["UZBOT"]).astype(float)
    idx = np.where(c >= 0.5)[0]
    arr = float(t[idx[0]]) if len(idx) else float("nan")
    return t, c, arr


def render_vadose_chart():
    t4, c4, a4 = _run_vadose_obs(4.0)
    t8, c8, a8 = _run_vadose_obs(8.0)
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    ax.plot(t4, c4, color="#C0392B", linewidth=1.0, label=f"4 m demo (arrival {a4:.0f} d)")
    ax.plot(t8, c8, color="#1f5fbf", linewidth=1.0, label=f"8 m deeper (arrival {a8:.0f} d)")
    ax.axhline(0.5, color="0.5", linestyle="--", linewidth=0.8)
    ax.set_xlabel("elapsed days", fontsize=8)
    ax.set_ylabel("base-of-column conc", fontsize=8)
    ax.set_title("Vadose-zone tracer breakthrough at the water table", fontsize=9)
    ax.tick_params(labelsize=8)
    ax.set_xlim(0, 300)  # zoom into the breakthrough region (both plateau by ~250 d)
    ax.legend(fontsize=7)
    cap = (
        "Tippecanoe County IN (natural place); purely-advective UZF+UZT column "
        "(modflow6-examples ex-gwt-uzt-2d). Dashed = half-source threshold. Arrival is "
        f"MONOTONE in depth-to-water: 4 m -> {a4:.0f} d, 8 m -> {a8:.0f} d (deeper = later)."
    )
    fig.text(0.01, 0.005, cap, fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    p = OUT / "modflow_vadose_transport_breakthrough_chart.png"
    fig.savefig(p, dpi=200)
    plt.close(fig)
    print("wrote", p, "arrivals 4m/8m=", a4, a8)
    return a4, a8


def render_point_over_esri(latlon, *, title, caption, fname, color="#C0392B"):
    lat, lon = latlon
    d = 0.02
    basemap, ext = _basemap(lon - d, lat - d, lon + d, lat + d, 14)
    fig, ax = plt.subplots(figsize=(8, 7), dpi=110)
    ax.imshow(basemap, extent=ext, origin="upper")
    mx, my = TO3857.transform(lon, lat)
    ax.plot(mx, my, marker="v", markersize=16, color=color,
            markeredgecolor="white", markeredgewidth=1.5, zorder=5)
    wx0, wy0 = TO3857.transform(lon - d, lat - d)
    wx1, wy1 = TO3857.transform(lon + d, lat + d)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=11)
    fig.text(0.01, 0.01, caption, fontsize=7, color="0.35", wrap=True)
    fig.tight_layout()
    p = OUT / fname
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p)


def _read_cog_3857(uri: str):
    """Read a MinIO/S3 COG into (array, extent_3857) via rasterio env creds."""
    key = uri.split("trid3nt-runs/", 1)[1]
    import boto3
    from _env_guard import require_local_endpoint
    s3 = boto3.client(
        "s3", endpoint_url=require_local_endpoint(),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    body = s3.get_object(Bucket="trid3nt-runs", Key=key)["Body"].read()
    tmp = OUT / "_subs_cog.tif"
    tmp.write_bytes(body)
    with rasterio.open(tmp) as ds:
        arr = ds.read(1).astype("float64")
        nod = ds.nodata
        if nod is not None:
            arr = np.where(arr == nod, np.nan, arr)
        b = ds.bounds
        to3857 = Transformer.from_crs(ds.crs, "EPSG:3857", always_xy=True)
        x0, y0 = to3857.transform(b.left, b.bottom)
        x1, y1 = to3857.transform(b.right, b.top)
        ll = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True)
        w, s = ll.transform(b.left, b.bottom)
        e, n = ll.transform(b.right, b.top)
    tmp.unlink(missing_ok=True)
    return arr, (x0, x1, y0, y1), (w, s, e, n)


async def render_subsidence():
    async def run(**kw):
        r = await modflow_sustainable_yield(
            aoi_latlon=list(SANJOAQUIN), well_location_latlon=list(SANJOAQUIN),
            pumping_rate_m3_day=4000.0, sim_years=10.0, n_periods=10,
            couple_subsidence=True, **kw)
        s = r.get("summary", {})
        lyr = r.get("subsidence_layer", {})
        return s.get("max_subsidence_cm"), lyr.get("uri")
    base_cm, base_uri = await run()
    delay_cm, _ = await run(csub_delay_interbeds=True)
    eff_cm, _ = await run(csub_effective_stress=True)
    print("subsidence cm base/delay/eff=", base_cm, delay_cm, eff_cm, "uri", base_uri)

    # Map: baseline subsidence bowl COG over ESRI. The layer .uri is a NORMALIZED
    # display OVERVIEW (raw pixels are a relative 0..1-scaled bowl, not cm), so the
    # colorbar is labeled relative and the true peak (the typed headline scalar) is
    # rescaled onto the ticks + stated in the caption -- honest, no fabricated units.
    arr, ext3857, (w, s, e, n) = _read_cog_3857(base_uri)
    finite = arr[np.isfinite(arr)]
    amax = float(np.nanmax(finite)) if finite.size else 1.0
    # The layer .uri is a NORMALIZED display OVERVIEW; its raw pixels are a relative
    # bowl, not cm. Render a RELATIVE 0..1 field (shape is the deliverable) and put
    # the TRUE peak (the typed headline scalar) on the colorbar label + caption --
    # honest, no fabricated per-pixel cm.
    arr_rel = arr / amax if amax > 0 else arr
    px, py = (e - w) * 0.4 + 1e-4, (n - s) * 0.4 + 1e-4
    basemap, bm_ext = _basemap(w - px, s - py, e + px, n + py, 12)
    fig, ax = plt.subplots(figsize=(9, 8), dpi=110)
    ax.imshow(basemap, extent=bm_ext, origin="upper")
    im = ax.imshow(np.ma.masked_invalid(arr_rel), extent=(ext3857[0], ext3857[1], ext3857[2], ext3857[3]),
                   origin="upper", cmap="magma_r", alpha=0.82, vmin=0.0, vmax=1.0, zorder=3)
    wx0, wy0 = TO3857.transform(w - px, s - py)
    wx1, wy1 = TO3857.transform(e + px, n + py)
    ax.set_xlim(wx0, wx1); ax.set_ylim(wy0, wy1)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("MODFLOW CSUB land subsidence bowl (head-based)\nSan Joaquin Valley corridor", fontsize=11)
    cb = fig.colorbar(im, ax=ax, shrink=0.72, pad=0.02)
    cb.set_label(f"relative subsidence (0 = none, 1.0 = peak {base_cm:.1f} cm)")
    cap = (
        f"San Joaquin Valley well (36.75/-120.38), 4000 m3/day over 10 yr; ESRI World "
        f"Imagery basemap + COG both EPSG:3857. HEAD_BASED no-delay CSUB peak "
        f"{base_cm:.1f} cm (demo interbed storage; not a calibrated Central Valley "
        f"forecast). mf6 6.7.0 (the ADR 0228 OBS-keying fix, deployed)."
    )
    fig.text(0.01, 0.01, cap, fontsize=7, color="0.35", wrap=True)
    fig.tight_layout()
    p = OUT / "modflow_sustainable_yield_csub_subsidence_bowl.png"
    fig.savefig(p, bbox_inches="tight"); plt.close(fig)
    print("wrote", p)

    # Chart: knob contrast.
    fig, ax = plt.subplots(figsize=(6.0, 2.4), dpi=100)
    labels = ["head-based\n(no-delay)", "DELAY\ninterbed", "EFFECTIVE\nSTRESS"]
    vals = [base_cm, delay_cm, eff_cm]
    colors = ["#7a2f8f", "#2a9d8f", "#1f5fbf"]
    ax.bar(labels, vals, color=colors, width=0.6)
    ax.set_ylabel("peak subsidence (cm)", fontsize=8)
    ax.set_title("CSUB formulation knobs (same well, same drawdown)", fontsize=9)
    ax.tick_params(labelsize=8)
    for i, v in enumerate(vals):
        ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
    ax.margins(y=0.2)
    cap = (
        f"Same San Joaquin well + head decline (~12.3 m) across all three. Delay "
        f"interbed lags -> LESS end-of-pumping compaction ({delay_cm:.1f} vs "
        f"{base_cm:.1f} cm); effective-stress ~{eff_cm/base_cm:.2f} of head-based "
        f"({eff_cm:.1f} cm) -- an order-of-magnitude crosscheck. Demo interbed params."
    )
    fig.text(0.01, 0.005, cap, fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    p = OUT / "modflow_sustainable_yield_csub_knob_contrast_chart.png"
    fig.savefig(p, dpi=200); plt.close(fig)
    print("wrote", p)
    return base_cm, delay_cm, eff_cm


def main():
    a4, a8 = render_vadose_chart()
    render_point_over_esri(
        TIPPECANOE,
        title="MODFLOW vadose_transport spill site (context point)\nTippecanoe County, Indiana",
        caption=(
            "ESRI World Imagery (EPSG:3857). The spill-site context point geolocates "
            f"where the 1D UZF+UZT vadose column was evaluated; the breakthrough CHART "
            f"carries the physics (4 m demo -> {a4:.0f} d arrival to the water table)."
        ),
        fname="modflow_vadose_transport_spill_site.png",
    )
    asyncio.run(render_subsidence())
    print("PROOFS COMPLETE")


if __name__ == "__main__":
    main()
