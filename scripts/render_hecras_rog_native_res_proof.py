#!/usr/bin/env python3
"""Render hecras_flood_2d_rog_depth_native.png (2026-08-11 fidelity-first drive).

Reads docs/proof/hecras_rog_coweeta_native_result.json (layer uri/legend from
the direct TOOL_REGISTRY drive) and renders max depth over ESRI World Imagery,
EPSG:3857 both, pinned scale bar, caption on the 10 m 3DEP source vs the 20 m
HEC-RAS 2025 mesh-generator solver floor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_fidelity_proof_generic import render  # noqa: E402

RESULT = Path("/home/nate/Documents/trid3nt-local/docs/proof/hecras_rog_coweeta_native_result.json")
OUT = Path("/home/nate/Documents/trid3nt-local/docs/proof/templates/hecras_flood_2d_rog_depth_native.png")
WALL_S = 202.2


def main():
    d = json.loads(RESULT.read_text())
    depth_max = d["depth_max_ft"]
    depth_mean = d["depth_mean_ft"]
    wet_cells = d["wet_cell_count"]
    uri = d["uri"]
    title = ("HEC-RAS 2025 rain-on-grid: peak depth, Coweeta Creek NC outlet reach\n"
              "resolution_m=20 (SOLVER FLOOR, basis=user) over 3DEP 10 m NATIVE terrain")
    caption = (f"25 mm/hr x 6 h design storm, DWE rain-only. Solver run at hecras_flood_2d's "
               f"declared 20 m mesh-generator MINIMUM (_MIN_RES_M); source terrain is 3DEP 10 m "
               f"native (2x finer than the solved mesh -- the DEM is coarsened to the 20 m subgrid, "
               f"not upsampled). Peak depth {depth_max:.2f} ft, mean wet-cell depth {depth_mean:.2f} ft, "
               f"{wet_cells} wet cells. Wall clock {WALL_S:.0f} s.")
    render(
        uri=uri,
        out_png=str(OUT),
        title=title,
        caption=caption,
        cmap="YlGnBu",
        units_label="water depth (ft)",
        vmin=0.0,
        vmax=depth_max,
        zoom=16,
    )
    print(str(OUT))


if __name__ == "__main__":
    main()
