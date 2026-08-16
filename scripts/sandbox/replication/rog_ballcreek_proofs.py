"""Ball Creek RoG replication proof charts (ADR 0204) -> docs/proof/templates/.

Dock-exact computed-vs-observed outlet-discharge overlays (6.0 x 2.2 in, dpi 200,
caption strip, quantitative axes, no annotation boxes):

  telemac_rain_on_grid_replication_chart.png -- the VALIDATION event (Feb 2018,
      split-sample, calibrated params unchanged): computed vs observed + NSE/R2.
  telemac_rain_on_grid_multipeak_chart.png   -- the NEGATIVE CONTROL (Dec 2015
      multi-peak): the inter-peak gap where the RoG model, having no subsurface
      return flow, drains to near-zero while the gauge stays baseflow-supported.
  telemac_rain_on_grid_calibration_chart.png -- the calibration event (Dec 2015).

Reads each event's forcing JSON + the chosen solve's outlet hydrograph, aligns the
computed series (plus the observed pre-event baseflow) to the observed hourly
gauge, and renders. Run in the agent venv. ASCII only.
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

COMP_C = "#0a84ff"
OBS_C = "#1c1c1e"


def _align(key: str, tag: str):
    f = json.loads((FORCING_DIR / f"{key}.json").read_text())
    hyd = json.loads((SOLVE_ROOT / f"solve_{tag}" / "rog_outlet_hydrograph.json").read_text())
    t_h = np.asarray(hyd["t_s"], float) / 3600.0
    q_comp = np.asarray(hyd["q_m3s"], float)
    baseflow = float(f["baseflow_m3s"])
    obs_t = pd.to_datetime(f["obs_times"])
    obs_q = np.asarray(f["obs_q_m3s"], float)
    t_rise = pd.to_datetime(f["t_rise"])
    rel_h = np.array([(t - t_rise).total_seconds() / 3600.0 for t in obs_t])
    comp = np.interp(rel_h, t_h, q_comp, left=0.0, right=q_comp[-1]) + baseflow
    return f, rel_h, obs_q, comp, baseflow


def render(key: str, tag: str, out_name: str, title: str, cause: str,
           second_tag: str | None = None, second_label: str = "",
           computed_label: str = "computed (TELEMAC RoG)") -> None:
    from trid3nt_server.data.processing.compute_skill_metrics.compute_skill_metrics import (
        nash_sutcliffe_efficiency, pearson_r2)
    f, rel_h, obs_q, comp, baseflow = _align(key, tag)
    nse = nash_sutcliffe_efficiency(obs_q, comp)
    r2 = pearson_r2(obs_q, comp)

    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=200)
    ax.plot(rel_h, obs_q, color=OBS_C, lw=1.4, label="observed (EDI weir #9)")
    ax.plot(rel_h, comp, color=COMP_C, lw=1.6, label=computed_label)
    if second_tag:
        _f2, r2h, _o2, comp2, _b2 = _align(key, second_tag)
        ax.plot(r2h, comp2, color="#ff9f0a", lw=1.6, ls="--", label=second_label)
    ax.axhline(baseflow, color="#8e8e93", lw=0.6, ls=":")
    ax.set_xlabel("elapsed hours from storm onset", fontsize=7)
    ax.set_ylabel("outlet Q (m3/s)", fontsize=7)
    ax.tick_params(labelsize=6.5)
    ax.set_xlim(rel_h.min(), rel_h.max())
    ax.set_ylim(0, max(obs_q.max(), comp.max()) * 1.12)
    ax.grid(True, alpha=0.25, lw=0.5)
    ax.legend(loc="upper right", fontsize=6.2, frameon=False)
    ax.set_title(title, fontsize=8, pad=4)
    nse_s = "n/a" if nse is None else f"{nse:.2f}"
    r2_s = "n/a" if r2 is None else f"{r2:.2f}"
    cap = (f"telemac_rain_on_grid  |  Ball Creek weir #9, Coweeta NC (7.24 km2)  |  "
           f"NSE {nse_s}  R2 {r2_s}  |  {cause}")
    fig.text(0.5, 0.015, cap, ha="center", va="bottom", fontsize=5.6, color="#3a3a3c")
    fig.subplots_adjust(left=0.085, right=0.985, top=0.86, bottom=0.28)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / out_name)
    plt.close(fig)
    print(f"[proof] {out_name}  NSE={nse_s} R2={r2_s}")


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    # args: key tag out_name "title" "cause" [second_tag second_label computed_label]
    kw = {}
    if len(sys.argv) > 6:
        kw = dict(second_tag=sys.argv[6], second_label=sys.argv[7],
                  computed_label=sys.argv[8])
    render(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], **kw)
