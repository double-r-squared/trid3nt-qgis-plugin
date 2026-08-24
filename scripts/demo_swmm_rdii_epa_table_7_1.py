#!/usr/bin/env python
"""DEMO - the EPA SWMM 5 Ch.7 Table 7-1 RDII worked example, as a saved invocation.

This is a DEMONSTRATION, not workflow code. It exists so the canonical EPA
numbers - the sewershed, the published hyetograph, the R/T/K split and the
published node flows - live in ONE place that is a declared invocation of
``swmm_rdii_rtk_unit_hydrograph`` rather than constants inside the template. The
template ships no demo values of its own; running this script is what makes it
reproduce the published case.

Source: EPA SWMM 5 Hydrology Manual Ch.7 RDII worked example (Table 7-1 / Figures
7-8 and 7-10), via the CHI markdown re-publication at swmm5.org. The setup - a
10-acre sewershed at node N1, the first storm's hourly rainfall, and RTK unit
hydrographs whose R values sum to 0.36 - is exactly as published. The EXACT
per-UH R/T/K appear only in Figure 7-8, which is not machine-accessible here, so
``UHS`` below is a REPRESENTATIVE split that honors the published sum; NATE
supplying the figure's numbers is what would make the replication bit-exact.
What IS exact is the method and the native-SWMM cross-check the tool runs.

Usage: scripts/demo_swmm_rdii_epa_table_7_1.py
Env (MinIO): set -a; source .env.local; set +a
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

#: Sewershed area at node N1 (acres), as published.
AREA_AC = 10.0

#: Hourly rainfall (inches) at hours 0..6 - the FIRST storm. The 27-30 h second
#: storm is outside the demonstration window.
RAINFALL_IN_PER_HR = [0.0, 0.25, 0.5, 0.8, 0.4, 0.1, 0.0]

#: The published sum of the three R values.
SUM_R = 0.36

#: A representative (R, T, K) split summing to ``SUM_R``. See the module
#: docstring: the published per-UH numbers live in Figure 7-8 only.
UHS = [(0.12, 1.0, 2.0), (0.15, 3.0, 3.0), (0.09, 8.0, 3.0)]

#: Published node RDII flows (cfs) read off Figure 7-10, keyed by clock label -
#: the replication TARGET, not an input.
PUBLISHED_RDII_CFS = {
    "01:15": 0.204195, "02:00": 0.554604, "03:00": 1.021479,
    "04:00": 1.001312, "05:00": 0.703842,
}

#: The same published flows keyed by elapsed hours, for plotting against a time
#: axis (what the proof renderer wants).
PUBLISHED_RDII_BY_HOUR = {1.5: 0.204195, 2.0: 0.554604, 3.0: 1.021479,
                          4.0: 1.001312, 5.0: 0.703842}

#: The invocation itself: every value above, as tool arguments.
ARGS = {
    "R1": UHS[0][0], "T1": UHS[0][1], "K1": UHS[0][2],
    "R2": UHS[1][0], "T2": UHS[1][1], "K2": UHS[1][2],
    "R3": UHS[2][0], "T3": UHS[2][1], "K3": UHS[2][2],
    "sewershed_area_ac": AREA_AC,
    "rainfall_series_in_per_hr": RAINFALL_IN_PER_HR,
}


def main() -> int:
    from trid3nt_server.workflows.swmm.rdii_rtk.rdii_rtk import (
        swmm_rdii_rtk_unit_hydrograph,
    )

    result = asyncio.run(swmm_rdii_rtk_unit_hydrograph(**ARGS))
    print(json.dumps({k: v for k, v in result.items() if k != "curves"},
                     indent=2, default=str))
    if result.get("status") != "ok":
        return 1
    published_peak = max(PUBLISHED_RDII_CFS.values())
    print(f"\npublished Figure 7-10 peak: {published_peak:.4f} cfs")
    print(f"closed-form peak:           {result['rdii_peak_cfs']:.4f} cfs "
          f"({abs(result['rdii_peak_cfs'] - published_peak) / published_peak * 100:.1f}% off)")
    print(f"native SWMM / closed form:  "
          f"{result['swmm_vs_closed_form_peak_ratio']:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
