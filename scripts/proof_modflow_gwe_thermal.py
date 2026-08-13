"""Proof renders for the MODFLOW GWE heat-transport archetype family (ADR 0235).

Deterministic, reuses the product deck-builder (services/workers/modflow
build_modflow_deck) + the LOCAL mf6 6.7.0 binary. Two renders, QGIS-true style
(ESRI World Imagery basemap, EPSG:3857, mesh wireframe overlaid):

  1. gwe_thermal INJECTION-PLUME temperature field over ESRI at St. Paul, MN
     (a real cold-climate ATES / geothermal setting) -- the warm-water plume
     bleeding downgradient from the injection well, with the model mesh drawn.
  2. gwe_thermal ATES recovery-efficiency CHART: recovery efficiency vs cycle
     count -- the monotone rise as the aquifer thermal buffer pre-warms.

Run:
  cd /home/nate/Documents/trid3nt-local
  TRID3NT_MF6_BIN=$PWD/bin/mf6 venvs/agent/bin/python \
    scripts/proof_modflow_gwe_thermal.py
"""

from __future__ import annotations

import io
import math
import os
import subprocess
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import Transformer  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "services" / "workers" / "modflow"))
import flopy  # noqa: E402
from gwt_adapter import (  # noqa: E402
    GWE_AMBIENT_TEMPERATURE_C,
    build_modflow_deck,
)

TILE = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
MF6 = os.environ.get("TRID3NT_MF6_BIN", str(Path.cwd() / "bin" / "mf6"))
OUT = Path(__file__).parent.parent / "docs" / "proof" / "templates"
OUT.mkdir(parents=True, exist_ok=True)
STPAUL = (44.95, -93.09)
BASE = dict(spill_location_latlon=STPAUL, contaminant="temperature",
            release_rate_kg_s=1.0, aquifer_k_ms=1.0e-4, porosity=0.20)


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
            mosaic.paste(Image.open(io.BytesIO(r.content)).convert("RGB"),
                         (i * 256, j * 256))
    wm0, _, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, sm0, _, _ = _tile_bounds_3857(min(xs), max(ys), zoom)
    _, _, em1, nm1 = _tile_bounds_3857(max(xs), min(ys), zoom)
    return np.asarray(mosaic), (wm0, em1, sm0, nm1)


def _run_deck(ws: Path, **kw):
    dk = build_modflow_deck(workdir=ws, **{**BASE, **kw})
    r = subprocess.run([MF6], cwd=ws, capture_output=True, text=True)
    assert r.returncode == 0, f"mf6 rc={r.returncode}\n{r.stdout[-2000:]}"
    return dk


def render_plume_over_esri():
    ws = Path(os.environ.get("TMPDIR","/tmp")) / "gwe_proof" / "plume"
    ws.mkdir(parents=True, exist_ok=True)
    dk = _run_deck(ws, archetype="gwe_thermal", gwe_mode="injection_plume",
                   duration_days=180.0, injection_temperature_c=45.0,
                   injection_rate_m3_day=600.0)
    fp = flopy.utils.HeadFile(str(ws / dk.thermal_ucn_file), text="TEMPERATURE")
    temp = fp.get_alldata()[-1, 0]  # (nrow, ncol)

    # cell-center coordinates in the model UTM, reproject to 3857
    utm_to_3857 = Transformer.from_crs(dk.model_crs, "EPSG:3857", always_xy=True)
    xc = dk.xorigin + (np.arange(dk.ncol) + 0.5) * dk.delr
    yc = (dk.yorigin + dk.nrow * dk.delc) - (np.arange(dk.nrow) + 0.5) * dk.delc
    XX, YY = np.meshgrid(xc, yc)
    MX, MY = utm_to_3857.transform(XX, YY)

    d = 0.012
    lat, lon = STPAUL
    basemap, ext = _basemap(lon - d, lat - d, lon + d, lat + d, 15)

    fig, ax = plt.subplots(figsize=(8, 7.5), dpi=115)
    ax.imshow(basemap, extent=ext, origin="upper")
    excess = np.ma.masked_less(temp - GWE_AMBIENT_TEMPERATURE_C, 0.25)
    cf = ax.contourf(MX, MY, excess, levels=np.linspace(0.25, temp.max() -
                     GWE_AMBIENT_TEMPERATURE_C + 0.01, 12), cmap="inferno",
                     alpha=0.72, zorder=3)
    # mesh wireframe (modeled domain) -- thin grid lines every 5 cells
    for gi in range(0, dk.nrow + 1, 5):
        yy = (dk.yorigin + dk.nrow * dk.delc) - gi * dk.delc
        xa, ya = utm_to_3857.transform([dk.xorigin, dk.xorigin + dk.ncol * dk.delr],
                                       [yy, yy])
        ax.plot(xa, ya, color="white", linewidth=0.3, alpha=0.35, zorder=4)
    for gj in range(0, dk.ncol + 1, 5):
        xx = dk.xorigin + gj * dk.delr
        xa, ya = utm_to_3857.transform([xx, xx],
                                       [dk.yorigin, dk.yorigin + dk.nrow * dk.delc])
        ax.plot(xa, ya, color="white", linewidth=0.3, alpha=0.35, zorder=4)
    wx, wy = utm_to_3857.transform(dk.well_easting_m, dk.well_northing_m)
    ax.plot(wx, wy, marker="o", markersize=9, color="cyan",
            markeredgecolor="black", markeredgewidth=1.2, zorder=6)
    cb = fig.colorbar(cf, ax=ax, shrink=0.7, pad=0.02)
    cb.set_label("temperature above ambient (degC)", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    wx0, wy0 = TO3857.transform(lon - d, lat - d)
    wx1, wy1 = TO3857.transform(lon + d, lat + d)
    ax.set_xlim(wx0, wx1)
    ax.set_ylim(wy0, wy1)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("GWE thermal plume -- warm-water injection, St. Paul MN",
                 fontsize=11)
    cap = ("MODFLOW 6 GWF+GWE (heat transport), gwe_thermal / injection_plume: "
           "45 degC water injected at 600 m3/day into a 10 degC aquifer for 180 d. "
           "Colour = temperature above ambient; cyan dot = injection well; white "
           "wireframe = model mesh (every 5th cell, 50 m). Thermal properties are "
           "LOUD demo defaults (no thermal-property fetcher; ADR 0215/0235). Plume "
           f"peak = +{temp.max() - GWE_AMBIENT_TEMPERATURE_C:.1f} degC. EPSG:3857, "
           "ESRI World Imagery.")
    fig.text(0.01, 0.005, cap, fontsize=6, color="0.35", wrap=True)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    p = OUT / "modflow_gwe_thermal_injection_plume.png"
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("wrote", p, "peak_dT=", float(temp.max() - GWE_AMBIENT_TEMPERATURE_C))
    return p


def _ates_efficiency(n_cycles: int) -> float:
    ws = Path(os.environ.get("TMPDIR","/tmp")) / "gwe_proof" / f"ates{n_cycles}"
    ws.mkdir(parents=True, exist_ok=True)
    dk = _run_deck(ws, archetype="gwe_thermal", gwe_mode="ates",
                   duration_days=360.0 * n_cycles, n_cycles=n_cycles,
                   injection_temperature_c=GWE_AMBIENT_TEMPERATURE_C + 40.0,
                   injection_rate_m3_day=300.0)
    fp = flopy.utils.HeadFile(str(ws / dk.thermal_ucn_file), text="TEMPERATURE")
    kk = fp.get_kstpkper()
    last_extract = dk.n_stress_periods - 1
    prod = [fp.get_data(kstpkper=k)[0, dk.well_row, dk.well_col]
            for k in kk if k[1] == last_extract]
    amb = GWE_AMBIENT_TEMPERATURE_C
    return (float(np.mean(prod)) - amb) / (dk.injection_temperature_c - amb)


def render_ates_chart():
    cycles = [1, 2, 3, 4]
    effs = [_ates_efficiency(n) for n in cycles]
    fig, ax = plt.subplots(figsize=(6.0, 3.0), dpi=110)
    ax.plot(cycles, [e * 100 for e in effs], marker="o", color="#C0392B",
            linewidth=1.4)
    for c, e in zip(cycles, effs):
        ax.annotate(f"{e*100:.0f}%", (c, e * 100), textcoords="offset points",
                    xytext=(0, 6), fontsize=8, ha="center")
    ax.set_xlabel("ATES seasonal cycle", fontsize=9)
    ax.set_ylabel("recovery efficiency (%)", fontsize=9)
    ax.set_xticks(cycles)
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3)
    ax.set_title("ATES thermal recovery efficiency vs cycle count", fontsize=10)
    cap = ("MODFLOW 6 GWF+GWE, gwe_thermal / ates (modflow6-examples ex-gwe-ates "
           "class): 50 degC charge / recover at 300 m3/day, St. Paul MN aquifer. "
           "Recovery efficiency is < 100% and RISES with cycle count as the "
           "aquifer thermal buffer pre-warms.")
    fig.text(0.01, 0.005, cap, fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.12, 1, 1))
    p = OUT / "modflow_gwe_thermal_ates_recovery_chart.png"
    fig.savefig(p, dpi=200)
    plt.close(fig)
    print("wrote", p, "effs=", [round(e, 3) for e in effs])
    return p


if __name__ == "__main__":
    render_plume_over_esri()
    render_ates_chart()
