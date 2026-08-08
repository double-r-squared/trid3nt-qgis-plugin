"""Re-render ADR 0192 sandbox proof images from saved meshes (no re-mesh).

Reads each AOI's _runs/<aoi>/coastal_tin_mesh.npz + summary.json and regenerates
docs/proof/templates/oceanmesh_standalone_<aoi>.png. Standalone sandbox helper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

SANDBOX = Path("/home/nate/Documents/trid3nt-local/scripts/sandbox/oceanmesh")
PROOF = Path("/home/nate/Documents/trid3nt-local/docs/proof/templates")
sys.path.insert(0, str(SANDBOX))
from render_mesh import render  # noqa: E402


def main() -> int:
    for run in sorted((SANDBOX / "_runs").glob("*/summary.json")):
        s = json.loads(run.read_text())
        aoi = s["aoi"]
        npz = np.load(run.parent / "coastal_tin_mesh.npz")
        points, cells = npz["points"], npz["cells"]
        qa, cfg_bbox = s["qa"], s["bbox"]
        caption = (
            f"AOI: {s['label']}   bbox={tuple(round(v,3) for v in cfg_bbox)}\n"
            f"engine: {s['mesh_stats']['engine']}\n"
            f"sizing: feature(distance-to-shore) + wavelength(bathymetry, wl=10);"
            f" gradation g={s['mesh_stats']['grade']}\n"
            f"nodes={qa['n_vertices']}  elements={qa['n_elements']}  "
            f"inverted={qa['inverted_elements']}  closed={qa['boundary_closed']}\n"
            f"resolution: {qa['edge_min_m']:.0f}-{qa['edge_max_m']:.0f} m "
            f"(median {qa['edge_median_m']:.0f} m)   "
            f"quality qE min={qa['min_quality_qE']} median={qa['median_quality_qE']}"
        )
        out = PROOF / f"oceanmesh_standalone_{aoi}.png"
        render(points, cells, cfg_bbox, out, aoi_name=s["label"], caption=caption)
        print("rerendered", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
