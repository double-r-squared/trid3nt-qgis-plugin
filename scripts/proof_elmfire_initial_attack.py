#!/usr/bin/env python
"""ADR 0190 row 2 proof: ELMFIRE Hirsch initial-attack POC (closed form).

Closed-form validation class -> charts/scalars, NO georeferenced raster. Two
panels into docs/proof/templates/ (named after the tool):
  * ..._poc_delay_chart.png  -- POC vs attack delay, one curve per head-fire
    intensity + the 0.5 containment threshold. Figure(6.0,2.2) dpi=100
    savefig dpi=200 (plugin render_spec geometry).
  * ..._poc_surface.png -- the Hirsch POC(size, intensity) parameter surface
    (a parameter-space heatmap, not a geographic raster).
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from trid3nt_server.workflows.elmfire.initial_attack.initial_attack import (
    hirsch_poc, poc_vs_delay, _critical_delay_min,
)

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "elmfire_initial_attack_containment_probability"
LEVELS = [1000.0, 2500.0, 4000.0, 6000.0]
COLORS = {1000.0: "#1a9850", 2500.0: "#66bd63", 4000.0: "#f46d43", 6000.0: "#a50026"}


def main():
    delays = list(np.linspace(0, 120, 49))
    curves = {lvl: poc_vs_delay(lvl, 0.1, 1.5, 2.5, 4.0, delays) for lvl in LEVELS}

    # ---- (1) POC vs delay chart (Figure(6.0,2.2)) ---------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.2), dpi=100)
    for lvl in LEVELS:
        d = [c["delay_min"] for c in curves[lvl]]
        p = [c["poc"] for c in curves[lvl]]
        ax.plot(d, p, color=COLORS[lvl], lw=1.6, label=f"{lvl:.0f} kW/m")
    ax.axhline(0.5, color="0.5", ls="--", lw=1.0)
    ax.set_xlabel("attack delay / get-away time (min)", fontsize=8)
    ax.set_ylabel("probability of\ncontainment", fontsize=8)
    ax.set_ylim(0, 1)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.5, title="head-fire intensity", title_fontsize=6.5,
              loc="upper right", frameon=False, ncol=2)
    ax.grid(True, alpha=0.25)
    ax.set_title("Hirsch (1998) initial-attack POC vs attack delay", fontsize=8)
    fig.text(0.5, 0.02,
             "elmfire_initial_attack_containment_probability: a faster-spreading, more\n"
             "intense fire loses containability sooner as response slows (ADR 0190 row 2;\n"
             "exact published elmfire.io Hirsch coefficients)",
             ha="center", va="bottom", fontsize=6, color="0.4")
    fig.tight_layout(rect=(0, 0.20, 1, 1))
    p1 = os.path.join(OUT, f"{STEM}_poc_delay_chart.png")
    fig.savefig(p1, dpi=200)
    plt.close(fig)

    # ---- (2) POC(size, intensity) parameter surface -------------------------
    sizes = np.logspace(np.log10(0.05), np.log10(50), 120)   # ha
    intens = np.linspace(200, 7000, 120)                     # kW/m
    Z = np.array([[hirsch_poc(s, i) for s in sizes] for i in intens])
    fig, ax = plt.subplots(figsize=(6.0, 3.4), dpi=100)
    im = ax.pcolormesh(sizes, intens, Z, cmap="RdYlGn", vmin=0, vmax=1, shading="auto")
    cs = ax.contour(sizes, intens, Z, levels=[0.25, 0.5, 0.75], colors="k",
                    linewidths=0.8)
    ax.clabel(cs, fmt="%.2f", fontsize=6)
    ax.set_xscale("log")
    ax.set_xlabel("fire size at attack (ha, log)", fontsize=8)
    ax.set_ylabel("head-fire intensity (kW/m)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.set_title("Hirsch (1998) probability-of-containment surface", fontsize=9)
    cb = fig.colorbar(im, ax=ax, shrink=0.85, pad=0.02)
    cb.set_label("POC", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    fig.text(0.5, 0.005,
             "elmfire_initial_attack_containment_probability: POC logistic in fire "
             "size, head-fire intensity + interaction (ADR 0190 row 2)",
             ha="center", fontsize=6, color="0.4")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    p2 = os.path.join(OUT, f"{STEM}_poc_surface.png")
    fig.savefig(p2, dpi=200)
    plt.close(fig)

    # ---- scalars ------------------------------------------------------------
    print("wrote", p1)
    print("wrote", p2)
    for lvl in LEVELS:
        cd = _critical_delay_min(curves[lvl])
        print(f"  I={lvl:.0f} kW/m  POC@30min={hirsch_poc(poc_vs_delay(lvl,0.1,1.5,2.5,4.0,[30])[0]['size_ha'], lvl):.3f}  "
              f"critical_delay(POC=0.5)={'n/a' if cd is None else f'{cd:.1f} min'}")


if __name__ == "__main__":
    main()
