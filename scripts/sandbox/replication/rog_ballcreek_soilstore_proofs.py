"""Ball Creek RoG SOIL-STORE proof charts -> docs/proof/templates/.

Regenerates the three dock-exact computed-vs-observed overlays IN PLACE so they
show the fidelity-ladder progression (0206 static-CN hyetograph -> 0213
continuous soil-moisture store), plus a NEW ladder-table chart. Dock-exact
(6.0 x 2.2 in / dpi 200, caption strip, quantitative axes, no annotation boxes):

  telemac_rain_on_grid_calibration_chart.png -- Dec 2015 calibration: observed +
      0206 static-CN + 0213 store (the store sharpens shape + timing).
  telemac_rain_on_grid_multipeak_chart.png   -- Dec 2015 multi-peak: the store's
      between-storm recovery fixes the second-peak overshoot the static CN
      exhausts on (+116% -> -11%).
  telemac_rain_on_grid_replication_chart.png -- Feb 2018 split-sample: the store,
      like the static CN, does NOT transfer (honest negative -- seasonal wetness).
  telemac_rain_on_grid_fidelity_ladder_chart.png -- the ladder table.

Local NSE/R2 (standard defs, no spotpy). Run in the agent env. ASCII only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
FORCING_DIR = Path("/tmp/rog_ballcreek/forcing")
SOLVE_ROOT = Path("/tmp/rog_ballcreek")
OUT = REPO / "docs" / "proof" / "templates"

OBS_C = "#1c1c1e"
STORE_C = "#0a84ff"
CN_C = "#ff9f0a"


def _nse(o, s):
    o = np.asarray(o, float); s = np.asarray(s, float)
    d = float(np.sum((o - o.mean()) ** 2))
    return None if d == 0 else 1.0 - float(np.sum((o - s) ** 2)) / d


def _r2(o, s):
    o = np.asarray(o, float); s = np.asarray(s, float)
    if o.std() == 0 or s.std() == 0:
        return None
    return float(np.corrcoef(o, s)[0, 1] ** 2)


def _align(key: str, tag: str, root: Path = SOLVE_ROOT):
    f = json.loads((FORCING_DIR / f"{key}.json").read_text())
    hyd = json.loads((root / f"solve_{tag}" / "rog_outlet_hydrograph.json").read_text())
    t_h = np.asarray(hyd["t_s"], float) / 3600.0
    q_comp = np.asarray(hyd["q_m3s"], float)
    baseflow = float(f["baseflow_m3s"])
    obs_t = pd.to_datetime(f["obs_times"])
    obs_q = np.asarray(f["obs_q_m3s"], float)
    t_rise = pd.to_datetime(f["t_rise"])
    rel_h = np.array([(t - t_rise).total_seconds() / 3600.0 for t in obs_t])
    comp = np.interp(rel_h, t_h, q_comp, left=0.0, right=q_comp[-1]) + baseflow
    return f, rel_h, obs_q, comp, baseflow


def overlay(key, store_tag, cn_tag, out_name, title, cause):
    f, rel_h, obs_q, store, baseflow = _align(key, store_tag)
    _f, _r, _o, cn, _b = _align(key, cn_tag)
    nse = _nse(obs_q, store); r2 = _r2(obs_q, store)
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=200)
    ax.plot(rel_h, obs_q, color=OBS_C, lw=1.4, label="observed (EDI weir #9)")
    ax.plot(_r, cn, color=CN_C, lw=1.3, ls="--", label="0206 static CN (hyetograph)")
    ax.plot(rel_h, store, color=STORE_C, lw=1.7, label="0213 soil-moisture store")
    ax.axhline(baseflow, color="#8e8e93", lw=0.6, ls=":")
    ax.set_xlabel("elapsed hours from storm onset", fontsize=7)
    ax.set_ylabel("outlet Q (m3/s)", fontsize=7)
    ax.tick_params(labelsize=6.5)
    ax.set_xlim(rel_h.min(), rel_h.max())
    ax.set_ylim(0, max(obs_q.max(), store.max(), cn.max()) * 1.12)
    ax.grid(True, alpha=0.25, lw=0.5)
    ax.legend(loc="upper right", fontsize=6.0, frameon=False)
    ax.set_title(title, fontsize=8, pad=4)
    nse_s = "n/a" if nse is None else f"{nse:.2f}"
    r2_s = "n/a" if r2 is None else f"{r2:.2f}"
    cap = (f"telemac_rain_on_grid | Ball Creek weir #9, Coweeta NC (7.24 km2) | "
           f"store NSE {nse_s} R2 {r2_s} | {cause}")
    fig.text(0.5, 0.015, cap, ha="center", va="bottom", fontsize=5.0, color="#3a3a3c")
    fig.subplots_adjust(left=0.085, right=0.985, top=0.86, bottom=0.28)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / out_name)
    plt.close(fig)
    print(f"[proof] {out_name}  store NSE={nse_s} R2={r2_s}")


def ladder_chart():
    """Dock-exact ladder table: the four rungs x the headline metrics, for the
    Dec 2015 calibration event + the multi-peak second-peak control."""
    cols = ["0204\nconstant", "0206\nhyetograph", "0213\nsoil store", "0213 store\n+fine mesh"]
    rows = ["aligned NSE (Dec)", "peak err % (Dec)", "timing lag h (Dec)",
            "vol err % (Dec)", "2nd-peak err % (multi-peak)"]
    # values from the graded solves (this ADR) / tables.
    data = [
        ["+0.04", "+0.51", "+0.75", "-108*"],
        ["-1.7", "+5.4", "+21", "-54"],
        ["+11.0", "+10.8", "+7.8", "+3.8"],
        ["-52", "-19", "-22", "-87"],
        ["not repro.", "+116 / +210", "+21 / -11", "--"],
    ]
    fig, ax = plt.subplots(figsize=(6.4, 2.5), dpi=200)
    ax.axis("off")
    tbl = ax.table(cellText=data, rowLabels=rows, colLabels=cols,
                   cellLoc="center", rowLoc="left", loc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(6.6)
    tbl.scale(1.0, 1.35)
    # tint the store column (the fidelity gain) and the 2nd-peak win cell.
    for r in range(len(rows) + 1):
        c = tbl[r, 2]
        c.set_facecolor("#e6f0ff" if r > 0 else "#0a84ff")
        if r == 0:
            c.set_text_props(color="white", weight="bold")
    for r in range(len(cols)):
        tbl[0, r].set_text_props(weight="bold")
    ax.set_title("telemac_rain_on_grid fidelity ladder -- Ball Creek weir #9, Coweeta NC",
                 fontsize=8, pad=8)
    cap = ("2nd-peak err = comp-peak-1 vs obs-peak-1 / comp-2nd vs obs-2nd.  "
           "* fine mesh halves the routing lag but the finer TIN holds more of the\n"
           "thin overland sheet (peak/vol drop).  continuity O(1e-15) throughout.")
    fig.text(0.5, 0.02, cap, ha="center", va="bottom", fontsize=5.2, color="#3a3a3c")
    fig.subplots_adjust(left=0.30, right=0.985, top=0.84, bottom=0.16)
    fig.savefig(OUT / "telemac_rain_on_grid_fidelity_ladder_chart.png")
    plt.close(fig)
    print("[proof] telemac_rain_on_grid_fidelity_ladder_chart.png")


if __name__ == "__main__":
    overlay("dec2015", "soil_dec2015_S1000_tau120_mn1p0",
            "hy_dec2015_cn53p0_amc2_mn1p0",
            "telemac_rain_on_grid_calibration_chart.png",
            "Dec 2015 calibration -- soil store sharpens shape + timing",
            "aligned NSE 0.51->0.75, lag 10.8->7.8 h; peak over-predicts")
    overlay("dec2015_mp", "soil_dec2015_mp_S1000_tau120_mn1p0",
            "hy_dec2015_mp_cn53p0_amc2_mn1p0",
            "telemac_rain_on_grid_multipeak_chart.png",
            "Dec 2015 multi-peak -- between-storm recovery fixes the 2nd-peak overshoot",
            "static CN 2nd peak +210% vs obs-2nd; store 2nd peak -11% (recovery between storms)")
    overlay("feb2018", "soil_feb2018_S1000_tau120_mn1p0",
            "hy_feb2018_cn53p0_amc2_mn1p0",
            "telemac_rain_on_grid_replication_chart.png",
            "Feb 2018 split-sample -- the store, like the static CN, does NOT transfer",
            "rainfall-only antecedent is inverted vs the true (seasonal) wetness; both ponds")
    ladder_chart()
