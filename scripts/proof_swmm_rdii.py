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

import asyncio
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from demo_swmm_rdii_epa_table_7_1 import (
    ARGS as EPA_ARGS,
    AREA_AC as AREA,
    PUBLISHED_RDII_BY_HOUR as _PUBLISHED,
    RAINFALL_IN_PER_HR as EPA_RAINFALL_IN_PER_HR,
    UHS,
)
from trid3nt_server.workflows.swmm.rdii_rtk.rdii_rtk import (
    swmm_rdii_rtk_unit_hydrograph,
)
from trid3nt_server.workflows.swmm.rdii_rtk.steps import rtk_unit_hydrograph

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "swmm_rdii_rtk_unit_hydrograph"
DT_MIN = 15
C_RUNOFF = 0.30


def main():
    dt_hr = DT_MIN / 60.0
    # ONE declared invocation of the tool on the EPA Table 7-1 case - the proof
    # cites the product's own curves rather than re-implementing the method.
    res = asyncio.run(swmm_rdii_rtk_unit_hydrograph(
        **EPA_ARGS, direct_runoff_coeff=C_RUNOFF, dt_min=DT_MIN))
    assert res["status"] == "ok", res

    curves = res["curves"]
    t = np.array(curves["times_hr"])
    rdii = np.array(curves["rdii_cfs"])
    runoff = np.array(curves["runoff_cfs"])
    runoff_peak = res["direct_runoff_peak_cfs"]
    DEPTH = res["rainfall_depth_in"]
    peak_ratio = res["swmm_vs_closed_form_peak_ratio"]
    rdii_frac = res["rdii_fraction_of_total"]
    identity = res["rtk_volume_identity_ratio"]
    # the native-engine series is a SCALAR on the product (its peak); plot it at
    # the closed form's peak time so the two are visibly the same number.
    sw = np.array([res["swmm_rdii_peak_cfs"]])
    tsw = np.array([float(t[int(np.argmax(rdii))])])

    # ---- (1) RDII vs runoff + SWMM overlay ---------------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.6), dpi=100)
    ax.plot(t, rdii, color="#1f78b4", lw=1.8, label="RDII (RTK closed form)")
    ax.plot(tsw, sw, color="#e31a1c", lw=0.0, marker="o", ms=2.4,
            label="RDII peak (native SWMM 5)")
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
             f"example ({AREA:.0f} ac, sum R={res['sum_R']:.2f}, published "
             f"hourly rainfall). Volume identity {identity:.4f}; closed form matches "
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
    print(f"  volume identity {identity:.5f}; RDII fraction of node peak {rdii_frac:.3f}")

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
