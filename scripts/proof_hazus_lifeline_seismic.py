#!/usr/bin/env python
"""proof: HAZUS earthquake lifeline-network damage-and-loss templates.

Chart-first validation class (tabular DL, no raster/mesh -> the QGIS/ESRI proof
norm is N/A, mirror the pelicun_hazus_seismic_dl_run proof style). Drives the real
pelicun DL_calculation harness in-venv over the three bundled DamageAndLossModel
Library lifeline fragility libraries; every plotted number is a pelicun output.

Three panels into docs/proof/templates/ (named after the tool):
  * ..._transportation_bridge.png -- HWB bridge repair-cost loss-exceedance curve
    over a SA(1.0) sweep (0.4 / 0.8 / 1.2 g).
  * ..._potable_water_pipe.png -- ductile-iron main leak/break damage-state split
    over a PGV/PGD sweep.
  * ..._electric_power_substation.png -- substation damage-state distribution over
    a PGA sweep (0.2 -> 1.5 g), showing the monotonic damage-state migration.
"""
from __future__ import annotations

import asyncio
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from trid3nt_server.data import TOOL_REGISTRY

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "pelicun_hazus_lifeline_seismic_dl_run"
FN = TOOL_REGISTRY["pelicun_hazus_lifeline_seismic_dl_run"].fn
N = 2000


def _run(**kw):
    return asyncio.run(FN(realizations=N, seed=7, **kw))


def proof_transportation() -> None:
    # Structural (ground-shaking) response: sweep SA(1.0); ground failure OFF so the
    # SA fragility is visible (PGD failure otherwise dominates to near-total loss).
    sas = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    ladder = ["None", "Slight", "Moderate", "Extensive", "Complete"]
    colors = ["#54a24b", "#b2df8a", "#f0a202", "#e45756", "#7b0828"]
    runs = [_run(lifeline_class="transportation", ground_failure=False,
                 sa_1_0_g=sa, sa_0_3_g=sa * 1.5) for sa in sas]
    dists = [r["damage_state_probabilities"] for r in runs]
    costs = [r["loss_summary"]["mean_repair_cost_ratio"] for r in runs]
    comp = runs[0]["auto_populated_component"]

    fig, ax1 = plt.subplots(figsize=(8.0, 5.0))
    x = np.arange(len(sas))
    bottom = np.zeros(len(sas))
    for ds, c in zip(ladder, colors):
        vals = np.array([d.get(ds, 0.0) for d in dists])
        ax1.bar(x, vals, bottom=bottom, label=ds, color=c, width=0.6)
        bottom += vals
    ax1.set_xticks(x)
    ax1.set_xticklabels([f"{sa}g" for sa in sas])
    ax1.set_xlabel("SA(1.0)")
    ax1.set_ylabel("damage-state probability")
    ax1.set_ylim(0, 1.0)
    ax2 = ax1.twinx()
    ax2.plot(x, costs, "o-", color="#1f1f1f", lw=2, label="mean repair-cost ratio")
    ax2.set_ylabel("mean repair-cost loss ratio")
    ax2.set_ylim(0, max(0.5, max(costs) * 1.2))
    ax1.set_title(f"HAZUS transportation bridge {comp} - damage + repair cost vs SA(1.0)\n"
                  "pelicun DL_calculation, ground shaking only")
    ax1.legend(loc="upper left", fontsize=8)
    ax2.legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    fig.savefig(f"{OUT}/{STEM}_transportation_bridge.png", dpi=130)
    plt.close(fig)


def proof_potable_water() -> None:
    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    sweep = [(3, 0.1), (6, 0.3), (15, 1.0)]
    x = np.arange(len(sweep))
    none_p, leak_p, break_p = [], [], []
    for pgv, pgd in sweep:
        d = _run(lifeline_class="potable_water", pgv_cmps=pgv, pgd_inch=pgd
                 )["damage_state_probabilities"]
        none_p.append(d.get("None", 0.0))
        leak_p.append(d.get("Leak", 0.0))
        break_p.append(d.get("Break", 0.0))
    ax.bar(x, none_p, label="None", color="#54a24b")
    ax.bar(x, leak_p, bottom=none_p, label="Leak (DS1)", color="#f0a202")
    ax.bar(x, break_p, bottom=np.array(none_p) + np.array(leak_p),
           label="Break (DS2)", color="#e45756")
    ax.set_xticks(x)
    ax.set_xticklabels([f"PGV={p}cm/s\nPGD={g}in" for p, g in sweep])
    ax.set_ylabel("per-segment damage-state probability")
    ax.set_ylim(0, 1.0)
    ax.set_title("HAZUS potable-water ductile-iron main (PWP.D) - leak/break split\n"
                 "pelicun DL_calculation, 20 ft segments")
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(f"{OUT}/{STEM}_potable_water_pipe.png", dpi=130)
    plt.close(fig)


def proof_electric_power() -> None:
    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    pgas = [0.2, 0.5, 1.0, 1.5]
    ladder = ["None", "Slight", "Moderate", "Extensive", "Complete"]
    colors = ["#54a24b", "#b2df8a", "#f0a202", "#e45756", "#7b0828"]
    dists = [_run(lifeline_class="electric_power", pga_g=p
                  )["damage_state_probabilities"] for p in pgas]
    x = np.arange(len(pgas))
    bottom = np.zeros(len(pgas))
    for ds, c in zip(ladder, colors):
        vals = np.array([d.get(ds, 0.0) for d in dists])
        ax.bar(x, vals, bottom=bottom, label=ds, color=c)
        bottom += vals
    ax.set_xticks(x)
    ax.set_xticklabels([f"PGA={p}g" for p in pgas])
    ax.set_ylabel("damage-state probability")
    ax.set_ylim(0, 1.0)
    ax.set_title("HAZUS electric-power substation (EP.S.L.U) - damage states vs PGA\n"
                 "pelicun DL_calculation, monotonic damage migration")
    ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5))
    fig.tight_layout()
    fig.savefig(f"{OUT}/{STEM}_electric_power_substation.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    proof_transportation()
    proof_potable_water()
    proof_electric_power()
    print("wrote proof panels to", OUT)
