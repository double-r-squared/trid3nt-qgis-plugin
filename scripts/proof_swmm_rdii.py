#!/usr/bin/env python
"""row 4 proof: SWMM RTK unit-hydrograph RDII (closed form + native SWMM).

Closed-form validation class -> charts/scalars, no raster. Two panels into
docs/proof/templates/ (named after the tool):
  * ..._rdii_vs_runoff.png -- RDII hydrograph (RTK closed form) with the native
    SWMM 5 node inflow overlaid + the direct-runoff hydrograph, at the node.
    Figure(6.0,2.6) dpi=100 savefig dpi=200.
  * ..._unit_hydrographs.png -- the three RTK triangular unit hydrographs.
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from trid3nt_server.workflows.swmm.rdii_rtk.rdii_rtk import (
    rtk_unit_hydrograph, rdii_hydrograph, build_rtk_rdii_inp,
    _solve_swmm_node_rdii, _rdii_volume_cf, _rtk_expected_volume_cf,
    EPA_TABLE_7_1_RAINFALL_IN_PER_HR, EPA_TABLE_7_1_PUBLISHED_RDII_CFS,
)

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "swmm_rdii_rtk_unit_hydrograph"
# EPA SWMM 5 Ch.7 Table 7-1 worked example: 10 ac, R sum 0.36, representative
# per-UH R/T/K (exact split lives in Fig 7-8, not text).
UHS = [(0.12, 1.0, 2.0), (0.15, 3.0, 3.0), (0.09, 8.0, 3.0)]
AREA = 10.0
DT_MIN = 15
C_RUNOFF = 0.30
_PUBLISHED = {1.5: 0.204195, 2.0: 0.554604, 3.0: 1.021479, 4.0: 1.001312, 5.0: 0.703842}


def main():
    dt_hr = DT_MIN / 60.0
    steps_per_hr = int(round(60 / DT_MIN))
    # expand the EPA hourly rainfall to 15-min steps (intensity = hourly depth)
    rain_int = []
    for hourly in EPA_TABLE_7_1_RAINFALL_IN_PER_HR:
        rain_int += [hourly] * steps_per_hr
    rain = [i * dt_hr for i in rain_int]
    DEPTH = sum(EPA_TABLE_7_1_RAINFALL_IN_PER_HR)
    rdii = np.array(rdii_hydrograph(UHS, rain, dt_hr, AREA))
    t = np.arange(len(rdii)) * dt_hr

    # native SWMM cross-check
    inp = build_rtk_rdii_inp(UHS, rain_int, DT_MIN, AREA, float(t[-1]))
    sw = np.array(_solve_swmm_node_rdii(inp))
    tsw = np.arange(len(sw)) * dt_hr

    # direct runoff (rational: Q=C*i*A per step)
    runoff = np.array([C_RUNOFF * rain_int[i] * AREA if i < len(rain_int) else 0.0
                       for i in range(len(rdii))])
    runoff_peak = float(runoff.max())

    vol = _rdii_volume_cf(list(rdii), dt_hr)
    exp = _rtk_expected_volume_cf(UHS, DEPTH, AREA)
    peak_ratio = float(sw.max()) / float(rdii.max())
    rdii_frac = float(rdii.max()) / (float(rdii.max()) + runoff_peak)

    # ---- (1) RDII vs runoff + SWMM overlay ---------------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.6), dpi=100)
    ax.plot(t, rdii, color="#1f78b4", lw=1.8, label="RDII (RTK closed form)")
    ax.plot(tsw, sw, color="#e31a1c", lw=0.0, marker="o", ms=2.4,
            label="RDII (native SWMM 5)")
    ax.plot(t, runoff, color="#33a02c", lw=1.4, ls="--", label="direct runoff")
    # published EPA Figure 7-10 node RDII flows (the Table 7-1 replication target)
    px = list(_PUBLISHED.keys()); py = list(_PUBLISHED.values())
    ax.plot(px, py, color="k", lw=0.0, marker="x", ms=6, mew=1.4,
            label="EPA Fig 7-10 (published)")
    ax.set_xlabel("time (hr)", fontsize=8)
    ax.set_ylabel("node inflow (cfs)", fontsize=8)
    ax.set_xlim(0, min(float(t[-1]), 12))
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.5, frameon=False, loc="upper right")
    ax.grid(True, alpha=0.25)
    ax.set_title("EPA Table 7-1 RTK RDII: closed form vs native SWMM vs published",
                 fontsize=8)
    pub_peak = max(_PUBLISHED.values())
    fig.text(0.5, 0.005,
             f"swmm_rdii_rtk_unit_hydrograph on the EPA SWMM 5 Ch.7 Table 7-1 "
             f"example (10 ac, sum R={sum(R for R,_,_ in UHS):.2f}, published "
             f"hourly rainfall). Volume identity {vol/exp:.4f}; closed form matches "
             f"native SWMM to {abs(1-peak_ratio)*100:.1f}%; peak {rdii.max():.3f} cfs "
             f"vs published {pub_peak:.3f} cfs ({abs(rdii.max()-pub_peak)/pub_peak*100:.0f}% "
             f"- exact per-UH R/T/K in Fig 7-8 only). RDII is {rdii_frac*100:.0f}% of "
             f"the node peak vs direct runoff (ADR 0190 row 4).",
             ha="center", fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    p1 = os.path.join(OUT, f"{STEM}_rdii_vs_runoff.png")
    fig.savefig(p1, dpi=200)
    plt.close(fig)
    print("wrote", p1)
    print(f"  closed-form peak {rdii.max():.4f} cfs, SWMM peak {sw.max():.4f} cfs, "
          f"ratio {peak_ratio:.4f}")
    print(f"  volume identity {vol/exp:.5f}; RDII fraction of node peak {rdii_frac:.3f}")

    # ---- (2) the three unit hydrographs ------------------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.4), dpi=100)
    labels = ["short (R=%.2f,T=%.0f,K=%.0f)" % UHS[0],
              "medium (R=%.2f,T=%.0f,K=%.0f)" % UHS[1],
              "long (R=%.2f,T=%.0f,K=%.0f)" % UHS[2]]
    colors = ["#1b9e77", "#d95f02", "#7570b3"]
    for (R, T, K), lab, col in zip(UHS, labels, colors):
        q = rtk_unit_hydrograph(R, T, K, dt_hr, AREA)
        tt = np.arange(len(q)) * dt_hr
        ax.plot(tt, q, color=col, lw=1.6, label=lab)
    ax.set_xlabel("time (hr)", fontsize=8)
    ax.set_ylabel("UH ordinate\n(cfs per inch)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.5, frameon=False)
    ax.grid(True, alpha=0.25)
    ax.set_title("RTK triangular unit hydrographs (short / medium / long)", fontsize=8)
    fig.text(0.5, 0.005,
             "swmm_rdii_rtk_unit_hydrograph: each UH area = R x rainfall x area "
             "(the RTK volume identity); base = T(1+K) (row 4)",
             ha="center", fontsize=6, color="0.4")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    p2 = os.path.join(OUT, f"{STEM}_unit_hydrographs.png")
    fig.savefig(p2, dpi=200)
    plt.close(fig)
    print("wrote", p2)


if __name__ == "__main__":
    main()
