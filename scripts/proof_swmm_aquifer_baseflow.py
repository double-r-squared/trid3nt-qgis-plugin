#!/usr/bin/env python
"""proof: SWMM two-zone aquifer [GROUNDWATER] baseflow-to-node.

Chart-first validation class -> charts/scalars, no raster. A pervious subcatchment
over a two-zone SWMM aquifer discharges baseflow to a drainage node; a two-storm
forcing (day 1 and day 12) makes the between-storms baseflow and the day-12
recharge bump explicit. Two panels into docs/proof/templates/:
  * ..._node_hydrograph.png  -- node inflow with groundwater (baseflow tail) vs
    surface runoff only (A1=0), both storms visible.
  * ..._baseflow_recession.png -- the between-storms baseflow recession tail on a
    log axis (linear-reservoir exponential recession) + the storm-2 recharge.
"""
from __future__ import annotations

import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from trid3nt_server.workflows.swmm.aquifer_baseflow.steps import (
    _mean_between, _peak, build_aquifer_inp, two_storm_forcing,
)
from trid3nt_server.workflows.swmm.steps.solve import _run as solve_deck  # noqa: PLC2701

OUT = "/home/nate/Documents/trid3nt-local/docs/proof/templates"
STEM = "swmm_aquifer_baseflow_to_node"
DT_MIN = 15
AREA_AC = 100.0
SIM_DAYS = 24
A1 = 0.002
B1 = 1.0
#: The deck values the template DECLARES; the proof runs the declared defaults.
COLUMN = dict(porosity=0.4637, wilting_point=0.1963, field_capacity=0.3568,
              conductivity_in_hr=0.1318)
DECK = dict(initial_water_table_ft=4.0, surface_elev_ft=10.0,
            imperviousness_pct=5.0, soil_suction_in=3.5,
            infiltration_ksat_in_hr=0.5, initial_moisture_deficit=0.30,
            aquifer_seepage_in_hr=0.002, evaporation_in_day=0.02)
STORM = dict(intensity_in_hr=0.3, storm_start_hr=6.0, storm_duration_hr=8.0,
             second_storm_day=12.0)


def main():
    rain = two_storm_forcing(dt_min=DT_MIN, sim_days=SIM_DAYS, **STORM)
    common = dict(dt_min=DT_MIN, area_ac=AREA_AC, b1=B1, sim_days=SIM_DAYS,
                  **COLUMN, **DECK)
    gw = solve_deck(build_aquifer_inp(rain, a1=A1, **common), ("J1",), (),
                    "total_inflow", "runoff", "proof-gw")
    no = solve_deck(build_aquifer_inp(rain, a1=0.0, **common), ("J1",), (),
                    "total_inflow", "runoff", "proof-no-gw")
    hrs, node_gw, cont = gw["hours"], gw["nodes"]["J1"], gw["flow_routing_error_pct"]
    node_no = no["nodes"]["J1"]

    hrs = np.array(hrs); node_gw = np.array(node_gw); node_no = np.array(node_no)
    base_gw = _mean_between(list(hrs), list(node_gw), 6, 11)
    base_no = _mean_between(list(hrs), list(node_no), 6, 11)
    peak_gw, _ = _peak(list(node_gw)); peak_no, _ = _peak(list(node_no))
    tail = [(h, q) for h, q in zip(hrs, node_gw) if 6 * 24 <= h < 11 * 24 and q > 1e-6]
    tau = (tail[-1][0] - tail[0][0]) / math.log(tail[0][1] / tail[-1][1]) if len(tail) > 1 else 0.0
    pre = _mean_between(list(hrs), list(node_gw), 11.5, 12.0)
    post = _peak([q for h, q in zip(hrs, node_gw) if 12 * 24 <= h < 14 * 24])[0]
    bump = post - pre

    # ---- (1) node hydrograph, with-GW vs no-GW --------------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.7), dpi=100)
    ax.plot(hrs / 24, node_gw, color="#1f78b4", lw=1.7, label="with groundwater (baseflow)")
    ax.plot(hrs / 24, node_no, color="#33a02c", lw=1.4, ls="--", label="surface runoff only (A1=0)")
    for d in (0, 12):
        ax.axvspan(d + 6 / 24, d + 14 / 24, color="0.85", alpha=0.5, lw=0)
    ax.set_xlabel("time (days)", fontsize=8)
    ax.set_ylabel("node inflow (cfs)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.5, frameon=False, loc="upper right")
    ax.grid(True, alpha=0.25)
    ax.set_title("Node hydrograph: two-zone aquifer baseflow vs surface runoff only", fontsize=8)
    fig.text(0.5, 0.005,
             f"swmm_aquifer_baseflow_to_node ({AREA_AC:.0f} ac, A1={A1}, B1={B1:.0f}): groundwater "
             f"sustains {base_gw:.3f} cfs baseflow between storms (surface-only {base_no:.3f}); "
             f"the day-12 storm re-recharges the aquifer (+{bump:.2f} cfs). Continuity "
             f"{cont:.2f}% (ADR 0218; EPA SWMM two-zone [AQUIFERS]/[GROUNDWATER]). Shaded = storms.",
             ha="center", fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    p1 = os.path.join(OUT, f"{STEM}_node_hydrograph.png")
    fig.savefig(p1, dpi=200); plt.close(fig)
    print("wrote", p1)
    print(f"  peak with_gw {peak_gw:.3f} cfs, no_gw {peak_no:.3f} cfs; between-storms baseflow "
          f"{base_gw:.4f} vs {base_no:.4f} cfs")

    # ---- (2) baseflow recession (log axis) ------------------------------------
    fig, ax = plt.subplots(figsize=(6.0, 2.5), dpi=100)
    mask = (hrs >= 5 * 24) & (node_gw > 1e-6)
    ax.semilogy(hrs[mask] / 24, node_gw[mask], color="#1f78b4", lw=1.7,
                label="baseflow (with groundwater)")
    ax.axvspan(12 + 6 / 24, 12 + 14 / 24, color="0.85", alpha=0.6, lw=0, label="storm 2")
    ax.set_xlabel("time (days)", fontsize=8)
    ax.set_ylabel("node inflow (cfs, log)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=6.5, frameon=False, loc="lower left")
    ax.grid(True, which="both", alpha=0.2)
    ax.set_title("Between-storms baseflow recession + storm-2 recharge", fontsize=8)
    fig.text(0.5, 0.005,
             f"Linear-reservoir (B1=1) baseflow recedes with time constant tau ~{tau:.0f} h "
             f"between storms, then the day-12 storm recharges the water table and revives the "
             f"baseflow (+{bump:.2f} cfs) -- the SWMM analogue of subsurface return flow (ADR 0218).",
             ha="center", fontsize=6, color="0.4", wrap=True)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    p2 = os.path.join(OUT, f"{STEM}_baseflow_recession.png")
    fig.savefig(p2, dpi=200); plt.close(fig)
    print("wrote", p2)
    print(f"  recession tau ~{tau:.1f} h, storm-2 recharge bump +{bump:.3f} cfs")

    # physics assertions
    assert base_gw > 0.05, "no baseflow between storms with groundwater"
    assert base_no < 1e-3, "surface-only control leaked baseflow between storms"
    assert bump > 0.1, "storm-2 recharge did not revive baseflow"
    assert cont < 5.0, "continuity error too high"
    print("  PHYSICS ASSERTIONS PASS")


if __name__ == "__main__":
    main()
