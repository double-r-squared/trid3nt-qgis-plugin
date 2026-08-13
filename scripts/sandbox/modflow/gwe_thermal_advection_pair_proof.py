"""Discriminating-pair proof render for GWE thermal transport (proof-norm #9).

NATE's critique (recorded): the prior canonical render
(docs/proof/templates/modflow_gwe_thermal_injection_plume.png) reads as a
uniform box around the well -- it cannot visually distinguish working
advection-dispersion from a broken zero-flow deck. This script fixes that by
rendering the DISCRIMINATING PAIR, side by side, with settings chosen so the
signature is unmistakable:

  LEFT  -- conduction-only (no ambient regional flow): a warm-water injection
           well with NO regional head gradient. Heat spreads by conduction +
           the well's own radial injection flow only -> a roughly SYMMETRIC
           halo around the well.
  RIGHT -- the IDENTICAL source (same well, same injection rate/temperature,
           same grid, same duration) PLUS an explicit regional head gradient
           (west high -> east low CHD boundaries) -> the plume is advected
           downgradient and ELONGATES visibly, with a pronounced tail.

The only difference between the two panels is the presence of ambient flow.
A broken/zero-flow transport deck could produce the left panel by accident
(diffusion alone) but could NOT produce the right panel's elongation --
that requires the GWF6-GWE6 exchange + advection (TVD) scheme to actually be
wired and working.

Physical settings (LOUD, stated here and on the figure):
  - Regional hydraulic gradient = 0.005 (m/m), the upper end of a realistic
    urban/coastal-plain aquifer gradient range (0.001-0.005); chosen (not the
    demo-deck default of 0.002) specifically so the advective signature is
    visually unmistakable at proof scale, and stated LOUD rather than hidden.
  - Horizon = 4 years, a defensible district-heating / ATES-style multi-year
    injection horizon (within the 2-5 yr range instructed).
  - Grid: 71x71 cells @ 25 m (finer than the 40x40 @ 50 m production demo
    grid) so the multi-year advective shift resolves as several whole cells,
    not sub-cell blur.
  - Thermal retardation is real: with n=0.20, water heat capacity 4184
    J/kg/degC (1000 kg/m3) vs grain heat capacity 800 J/kg/degC (2650 kg/m3),
    R = 1 + (1-n)*rho_s*cps / (n*rho_w*cpw) ~= 3.0, so the thermal front
    travels ~3x slower than the water itself -- the numbers below already
    reflect that physics, not an idealized solute front.

Run:
  cd /home/nate/Documents/trid3nt-local
  TRID3NT_MF6_BIN=$PWD/bin/mf6 venvs/agent/bin/python \
    scripts/sandbox/modflow/gwe_thermal_advection_pair_proof.py
"""

from __future__ import annotations

import io
import math
import os
import shutil
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import flopy  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from matplotlib.colors import Normalize  # noqa: E402
from PIL import Image  # noqa: E402
from pyproj import CRS, Transformer  # noqa: E402

MF6 = os.environ.get("TRID3NT_MF6_BIN", str(Path.cwd() / "bin" / "mf6"))
WORK = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
            "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/gwe_pair")
OUT = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "proof" / "templates"

# --- domain / physics constants (SI), tuned for VISIBLE advection --------- #
NROW = NCOL = 71
DELR = DELC = 25.0                 # m  (finer than the 40x40@50m demo grid)
TOP, BOTM = 0.0, -20.0             # 20 m saturated thickness
K_MS = 1.0e-4                      # hydraulic conductivity, m/s (typical sand)
POROSITY = 0.20
AMBIENT_T = 10.0                   # degC, undisturbed aquifer temperature
CPW, RHOW = 4184.0, 1000.0         # water heat capacity J/kg/degC, density kg/m3
CPS, RHOS = 800.0, 2650.0          # grain heat capacity, density
KTW, KTS = 0.56, 2.5               # thermal conductivity water / solid, W/m/degC
ALH = 10.0                          # longitudinal thermal dispersivity, m (matches
                                     # the production gwe_thermal deck value)
CENTER = NROW // 2                  # well cell index (row and col), grid center
DAY = 86400.0
YEAR = 365.0 * DAY

REGIONAL_GRADIENT = 0.005           # m/m -- upper end of the realistic urban
                                     # aquifer range (0.001-0.005); LOUD choice,
                                     # not the 0.002 demo-deck default, made
                                     # specifically for proof visibility.
DURATION_S = 4.0 * YEAR             # 4-year district-heating injection horizon
N_STEPS = 180
TSMULT = 1.06
Q_INJECT = 3.0e-3                   # m3/s warm-water injection (~259 m3/day)
INJECT_DT = 25.0                    # degC above ambient (district-heating scale)
WARM_THRESHOLD_DT = 0.5             # degC above ambient counted as "plume"

# retardation factor (informational, printed): R = 1 + (1-n)*rho_s*cps/(n*rho_w*cpw)
R_THERMAL = 1.0 + (1.0 - POROSITY) * RHOS * CPS / (POROSITY * RHOW * CPW)

STPAUL = (44.95, -93.09)           # cold-climate ATES/geothermal setting (matches
                                     # the production render's location)
UTM_CRS = CRS.from_epsg(32615)     # UTM zone 15N (covers -93 deg lon)
TO_UTM = Transformer.from_crs("EPSG:4326", UTM_CRS, always_xy=True)
UTM_TO_3857 = Transformer.from_crs(UTM_CRS, "EPSG:3857", always_xy=True)
TO3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

TILE = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}")

# grid origin: center the domain on STPAUL
_cx, _cy = TO_UTM.transform(STPAUL[1], STPAUL[0])
XORIGIN = _cx - (NCOL / 2.0) * DELR
YORIGIN = _cy - (NROW / 2.0) * DELC


def _sim(ws: Path, perioddata):
    ws.mkdir(parents=True, exist_ok=True)
    sim = flopy.mf6.MFSimulation(sim_name="gwe", sim_ws=str(ws),
                                 exe_name=MF6, version="mf6")
    flopy.mf6.ModflowTdis(sim, time_units="SECONDS", nper=len(perioddata),
                          perioddata=perioddata)
    return sim


def _gwf(sim, *, regional_gradient, wel_spd):
    name = "flow"
    flopy.mf6.ModflowIms(sim, filename=f"{name}.ims", complexity="SIMPLE",
                         outer_dvclose=1e-6, inner_dvclose=1e-6,
                         linear_acceleration="CG")
    gwf = flopy.mf6.ModflowGwf(sim, modelname=name, save_flows=True)
    sim.register_ims_package(sim.get_package(f"{name}.ims"), [name])
    flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=NROW, ncol=NCOL, delr=DELR,
                            delc=DELC, top=TOP, botm=BOTM,
                            xorigin=XORIGIN, yorigin=YORIGIN)
    gwf.modelgrid.set_coord_info(xoff=XORIGIN, yoff=YORIGIN, crs=UTM_CRS.to_epsg())
    domain_width_m = NCOL * DELR
    head_west = TOP + regional_gradient * domain_width_m
    head_east = TOP
    flopy.mf6.ModflowGwfic(gwf, strt=head_west)
    flopy.mf6.ModflowGwfnpf(gwf, save_flows=True, save_specific_discharge=True,
                            icelltype=0, k=K_MS)
    chd = []
    for r in range(NROW):
        chd.append([(0, r, 0), head_west])
        chd.append([(0, r, NCOL - 1), head_east])
    flopy.mf6.ModflowGwfchd(gwf, stress_period_data={0: chd})
    flopy.mf6.ModflowGwfwel(gwf, auxiliary=["TEMPERATURE"],
                            stress_period_data=wel_spd, pname="wel")
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord=f"{name}.hds",
                           budget_filerecord=f"{name}.cbc",
                           saverecord=[("HEAD", "LAST"), ("BUDGET", "LAST")])
    return gwf


def _gwe(sim):
    name = "energy"
    flopy.mf6.ModflowIms(sim, filename=f"{name}.ims", complexity="MODERATE",
                         outer_dvclose=1e-6, inner_dvclose=1e-6,
                         linear_acceleration="BICGSTAB")
    gwe = flopy.mf6.ModflowGwe(sim, modelname=name, save_flows=True)
    sim.register_ims_package(sim.get_package(f"{name}.ims"), [name])
    flopy.mf6.ModflowGwedis(gwe, nlay=1, nrow=NROW, ncol=NCOL, delr=DELR,
                            delc=DELC, top=TOP, botm=BOTM,
                            xorigin=XORIGIN, yorigin=YORIGIN)
    flopy.mf6.ModflowGweic(gwe, strt=AMBIENT_T)
    flopy.mf6.ModflowGweadv(gwe, scheme="TVD")
    flopy.mf6.ModflowGwecnd(gwe, alh=ALH, ath1=ALH * 0.1, ktw=KTW, kts=KTS)
    flopy.mf6.ModflowGweest(gwe, porosity=POROSITY, heat_capacity_water=CPW,
                            density_water=RHOW, heat_capacity_solid=CPS,
                            density_solid=RHOS)
    flopy.mf6.ModflowGwessm(gwe, sources=[["wel", "AUX", "TEMPERATURE"]])
    flopy.mf6.ModflowGweoc(gwe, temperature_filerecord=f"{name}.ucn",
                           budget_filerecord=f"{name}.cbc",
                           saverecord=[("TEMPERATURE", "LAST"), ("BUDGET", "LAST")])
    flopy.mf6.ModflowGwfgwe(sim, exgtype="GWF6-GWE6", exgmnamea="flow",
                            exgmnameb="energy")
    return gwe


def _run(sim, ws: Path):
    sim.write_simulation()
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        raise RuntimeError(f"mf6 failed in {ws}:\n" + "\n".join(buff[-30:]))


def build_run(tag: str, regional_gradient: float) -> np.ndarray:
    ws = WORK / tag
    if ws.exists():
        shutil.rmtree(ws)
    perioddata = [(1.0, 1, 1.0), (DURATION_S, N_STEPS, TSMULT)]
    wel_spd = {0: [], 1: [[(0, CENTER, CENTER), Q_INJECT, AMBIENT_T + INJECT_DT]]}
    sim = _sim(ws, perioddata)
    _gwf(sim, regional_gradient=regional_gradient, wel_spd=wel_spd)
    _gwe(sim)
    _run(sim, ws)
    fp = flopy.utils.HeadFile(str(ws / "energy.ucn"), text="TEMPERATURE")
    return fp.get_alldata()[-1, 0]  # (nrow, ncol), final time


def centroid_col(temp: np.ndarray) -> float:
    w = np.clip(temp - AMBIENT_T, 0, None)
    cols = np.arange(NCOL)[None, :]
    return float((w * cols).sum() / w.sum())


def extent_ratio(temp: np.ndarray) -> float:
    warm = temp > (AMBIENT_T + WARM_THRESHOLD_DT)
    down = int(warm[:, CENTER + 1:].sum())   # east of the well (downgradient)
    up = int(warm[:, :CENTER].sum())         # west of the well (upgradient)
    return down / max(up, 1)


# --- basemap plumbing (self-contained; mirrors scripts/proof_modflow_gwe_thermal.py) #
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


def _mesh_lines(ax):
    for gi in range(0, NROW + 1, 5):
        yy = (YORIGIN + NROW * DELC) - gi * DELC
        xa, ya = UTM_TO_3857.transform([XORIGIN, XORIGIN + NCOL * DELR], [yy, yy])
        ax.plot(xa, ya, color="white", linewidth=0.5, alpha=0.5, zorder=4)
    for gj in range(0, NCOL + 1, 5):
        xx = XORIGIN + gj * DELR
        xa, ya = UTM_TO_3857.transform([xx, xx], [YORIGIN, YORIGIN + NROW * DELC])
        ax.plot(xa, ya, color="white", linewidth=0.5, alpha=0.5, zorder=4)


def _panel(ax, temp, basemap, ext, xlim, ylim, xc3857, yc3857, wx, wy,
           vmax, title, note, show_arrow):
    ax.imshow(basemap, extent=ext, origin="upper")
    excess = np.ma.masked_less(temp - AMBIENT_T, WARM_THRESHOLD_DT)
    cf = ax.contourf(xc3857, yc3857, excess,
                     levels=np.linspace(WARM_THRESHOLD_DT, vmax, 14),
                     cmap="inferno", alpha=0.75, zorder=3)
    _mesh_lines(ax)
    ax.plot(wx, wy, marker="o", markersize=9, color="cyan",
            markeredgecolor="black", markeredgewidth=1.2, zorder=6)
    if show_arrow:
        dx = 0.22 * (xlim[1] - xlim[0])
        ax.annotate("", xy=(wx + dx, wy), xytext=(wx - dx * 0.4, wy),
                    arrowprops=dict(arrowstyle="-|>", color="lime", lw=2.5),
                    zorder=7)
        ax.text(wx + dx * 0.15, wy + 0.03 * (ylim[1] - ylim[0]),
                "regional flow (west -> east)", color="lime", fontsize=8,
                fontweight="bold", zorder=7)
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title, fontsize=10)
    ax.text(0.02, 0.02, note, transform=ax.transAxes, fontsize=8.5,
            color="white", va="bottom", ha="left", zorder=8,
            bbox=dict(boxstyle="round", facecolor="black", alpha=0.55, pad=0.35))
    return cf


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    print(f"mf6 = {MF6}")
    print(f"grid = {NROW}x{NCOL} @ {DELR:.0f} m, duration = "
          f"{DURATION_S / YEAR:.1f} yr, regional_gradient(advective) = "
          f"{REGIONAL_GRADIENT}")
    print(f"thermal retardation factor R = {R_THERMAL:.2f} "
          "(front travels ~1/R of the water velocity)")

    temp_cond = build_run("conduction_only", 0.0)
    temp_adv = build_run("advective", REGIONAL_GRADIENT)

    shift_cond = centroid_col(temp_cond) - CENTER
    shift_adv = centroid_col(temp_adv) - CENTER
    ratio_cond = extent_ratio(temp_cond)
    ratio_adv = extent_ratio(temp_adv)
    shift_adv_m = shift_adv * DELR

    print(f"\ncentroid col-shift  conduction-only = {shift_cond:+.2f} cells "
          f"({shift_cond * DELR:+.1f} m)")
    print(f"centroid col-shift  advective        = {shift_adv:+.2f} cells "
          f"({shift_adv_m:+.1f} m)")
    print(f"downgrad/upgrad extent ratio  conduction-only = {ratio_cond:.2f}x")
    print(f"downgrad/upgrad extent ratio  advective        = {ratio_adv:.2f}x")

    assert abs(shift_cond) < 0.5, (
        f"conduction-only centroid shift should be ~0, got {shift_cond:+.2f} cells")
    assert shift_adv > 2.0, (
        f"advective centroid shift must exceed 2 cells, got {shift_adv:+.2f}")
    assert ratio_adv > 1.5, (
        f"downgradient/upgradient extent ratio must exceed 1.5x, got {ratio_adv:.2f}")
    print("\n[ASSERT] conduction-only shift ~ 0: PASS")
    print(f"[ASSERT] advective centroid shift > 2 cells: PASS ({shift_adv:.2f})")
    print(f"[ASSERT] downgrad/upgrad extent ratio > 1.5x: PASS ({ratio_adv:.2f})")

    # --- render the pair --------------------------------------------------- #
    xc = XORIGIN + (np.arange(NCOL) + 0.5) * DELR
    yc = (YORIGIN + NROW * DELC) - (np.arange(NROW) + 0.5) * DELC
    XX, YY = np.meshgrid(xc, yc)
    XC3857, YC3857 = UTM_TO_3857.transform(XX, YY)
    wx, wy = UTM_TO_3857.transform(XORIGIN + (CENTER + 0.5) * DELR,
                                   (YORIGIN + NROW * DELC) - (CENTER + 0.5) * DELC)

    d = 0.014
    lat, lon = STPAUL
    basemap, ext = _basemap(lon - d, lat - d, lon + d, lat + d, 15)
    wx0, wy0 = TO3857.transform(lon - d, lat - d)
    wx1, wy1 = TO3857.transform(lon + d, lat + d)
    xlim, ylim = (wx0, wx1), (wy0, wy1)

    vmax = max(float(temp_cond.max()), float(temp_adv.max())) - AMBIENT_T

    fig = plt.figure(figsize=(16.5, 7.6), dpi=115)
    gs = fig.add_gridspec(1, 3, width_ratios=[1.0, 1.0, 0.04], wspace=0.06)
    ax_l = fig.add_subplot(gs[0, 0])
    ax_r = fig.add_subplot(gs[0, 1])
    cax = fig.add_subplot(gs[0, 2])
    note_l = (f"NO regional gradient -- conduction + radial injection only.\n"
              f"centroid shift = {shift_cond:+.2f} cells "
              f"({shift_cond * DELR:+.1f} m)")
    note_r = (f"regional gradient = {REGIONAL_GRADIENT} m/m (advective).\n"
              f"centroid shift = {shift_adv:+.2f} cells ({shift_adv_m:+.1f} m); "
              f"downgrad/upgrad extent = {ratio_adv:.2f}x")
    _panel(ax_l, temp_cond, basemap, ext, xlim, ylim, XC3857, YC3857, wx, wy,
          vmax, "LEFT: conduction-only (no ambient flow) -- symmetric halo",
          note_l, show_arrow=False)
    cf = _panel(ax_r, temp_adv, basemap, ext, xlim, ylim, XC3857, YC3857, wx, wy,
               vmax, "RIGHT: same source + regional gradient -- advected plume",
               note_r, show_arrow=True)

    cb = fig.colorbar(cf, cax=cax)
    cb.set_label("temperature above ambient (degC)", fontsize=9)

    cap = (
        "MODFLOW 6 GWF+GWE heat transport, IDENTICAL warm-water injection source "
        f"(+{INJECT_DT:.0f} degC, {Q_INJECT * DAY:.0f} m3/day) in both panels over a "
        f"{DURATION_S / YEAR:.0f}-year district-heating horizon on a {NROW}x{NCOL} "
        f"@ {DELR:.0f} m grid. The ONLY difference is ambient flow: LEFT has no "
        "regional head gradient, RIGHT has an explicit west-to-east regional "
        f"gradient of {REGIONAL_GRADIENT} m/m (realistic urban-aquifer range "
        "0.001-0.005 m/m, chosen at the upper end for proof visibility). A broken "
        "or zero-flow transport deck could produce the left panel by diffusion "
        "alone but could NOT produce the right panel's downgradient elongation -- "
        "that requires a working GWF6-GWE6 exchange and advection scheme. Thermal "
        f"retardation factor R = {R_THERMAL:.2f} (grain heat storage slows the "
        "front vs the water itself) is baked into both simulations, not idealized "
        "away. White wireframe = model mesh (every 5th cell). Cyan dot = well. "
        "EPSG:3857, ESRI World Imagery."
    )
    fig.text(0.01, 0.005, cap, fontsize=7, color="0.35", wrap=True)
    fig.suptitle("GWE thermal transport -- discriminating pair "
                "(conduction-only vs advective), St. Paul MN", fontsize=12.5)
    fig.subplots_adjust(left=0.02, right=0.97, top=0.90, bottom=0.10)

    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "modflow_gwe_thermal_advection_pair.png"
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
