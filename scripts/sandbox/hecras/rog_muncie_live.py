#!/usr/bin/env python3
"""Compose the 2068-cell carved-Muncie rain-on-grid de-risk deck (host side).

Carves Muncie to the 2068-cell de-risk block, composes a pure-2D RoG deck
(25 mm/hr x 6 h, CN 80 AMC II) with links 1-2 meteorology + SCS-CN infiltration
+ link 3 (the decoded per-area precip interpolation folder), into the
given rundir. The solve runs separately through trid3nt-local/hecras:latest via
solve_freshtopo.py. Host-only numpy/h5py; no AWS.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

FT = Path("/home/nate/Documents/trid3nt-local/workers/hecras2025/subst/crux/freshtopo")
sys.path.insert(0, str(FT))

from carve_muncie import load_muncie, carve, MUNCIE_PLAN  # noqa: E402
from hecras_deck2d import compose_pure2d_deck  # noqa: E402
import h5py  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rog_muncie")
    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < 408600.0) & (c[:, 1] > 1803025.0)
    r = carve(m, keep)
    with h5py.File(MUNCIE_PLAN, "r") as f:
        proj = f.attrs["Projection"]
        proj = proj.decode() if isinstance(proj, bytes) else str(proj)
    # apply_infiltration: False (default) = crash-free deck that reaches the engine's
    # precip readers past MetInterp; True = also author the byte-exact SCS-CN +
    # Percent Impervious layers and reach the READ_UN_HYDROLOGY2D residual.
    apply_infil = len(sys.argv) > 2 and sys.argv[2] == "--infil"
    info = compose_pure2d_deck(
        out, r.mesh, r.tables, projection_wkt=proj,
        design_storm_mm_per_hr=25.0, storm_duration_hr=6.0,
        curve_number=80.0, amc_condition="normal", apply_infiltration=apply_infil)
    print(f"[compose] rain_on_grid={info['rain_on_grid']} cells_real={info['cells_real']} "
          f"cells_total={info['cells_total']} faces={info['faces']} "
          f"storm_total_mm={info['storm_total_mm']} plan={info['paths'].plan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
