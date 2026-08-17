#!/usr/bin/env python3
"""Assemble the CLEAN pure-2D fake-reach carve deck -- the deck that
proves the fresh carved topology solves through a genuine, dam-free pure-2D deck.

Difference from ``build_freshtopo_deck.py``: that deck keeps Muncie's
REAL White River 1D reach carrying the forcing (the 2D area rides along, dry).
THIS deck replaces the 1D side with the shipped Chippewa clean ``Fake River``/
``Fake Reach`` skeleton (``patch_chippewa_xnn`` / ``patch_chippewa_bnn``) -- a
single 2D area + a dummy fake reach, exactly HEC's 6.6 pure-2D form. It SOLVES
end-to-end (RasGeomPreprocess + RasUnsteady, vol err 0.0).

STATUS (OI-FT1): the 2D flow area stays DRY -- the fake-reach inflow routes 1D
in->out and does not spill onto the carved 2D BC line. Wetting needs the 2D BC
line enumerated as a 2D flow boundary in the plan-HDF ``/Event Conditions`` (read
by the engine's ``read_un_q2d_bc_``); no shipped file in ``Example_Projects_6_6``
exposes that schema, and authoring it blind SEGFAULTs. This script is therefore
the durable, honest building block: a fresh topology solving through the correct
pure-2D deck, stopped precisely at the unshipped 2D-BC Event-Conditions schema.

Usage: python build_chippewa_fakereach_deck.py <out_rundir> [--flow-scale F]
Solve: docker run --rm -v <out>:/run -v <freshtopo>:/ft:ro --entrypoint bash \
         trid3nt-local/hecras:latest -lc \
         '/opt/trid3nt/.venv/bin/python /ft/solve_freshtopo.py /run'
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_HECRAS2025 = _HERE.parents[2]
for p in (str(_HERE), str(_HECRAS2025)):
    if p not in sys.path:
        sys.path.insert(0, p)

from carve_muncie import load_muncie, carve  # noqa: E402
from hecras_geometry_writer import (  # noqa: E402
    write_2d_flow_area, write_boundary_condition_lines,
    BoundaryConditionLine, perimeter_face_run, PropertyTableOptions, AREA_GROUP,
)
from hecras_pure2d_deck import patch_chippewa_xnn, patch_chippewa_bnn  # noqa: E402
from build_freshtopo_deck import (  # noqa: E402
    AREA_NAME, PLAN, XNN, BNN, MUNCIE_PLAN, _STRIP_2D_COUPLING,
)


def build(out: Path, keep_mask: np.ndarray, flow_scale: float = 1.0,
          n_bc_faces: int = 14) -> dict:
    import h5py

    m = load_muncie()
    r = carve(m, keep_mask)
    mesh, tables = r.mesh, r.tables

    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / PLAN
    shutil.copy2(MUNCIE_PLAN, plan_path)
    with h5py.File(plan_path, "r+") as f:
        projection = f.attrs["Projection"]
        parent = f[AREA_GROUP]
        if AREA_NAME in parent:
            del parent[AREA_NAME]
        if "Attributes" in parent:
            del parent["Attributes"]
        write_2d_flow_area(
            f, AREA_NAME, mesh, tables, PropertyTableOptions(),
            projection_wkt=projection.decode()
            if isinstance(projection, bytes) else projection,
        )
        run = perimeter_face_run(
            mesh, min_elevation=tables.face_min_elevation, n_faces=n_bc_faces)
        bc = BoundaryConditionLine(name="Inflow", sa_2d=AREA_NAME, face_indices=run)
        prov = write_boundary_condition_lines(f, [bc], mesh)
        bc_len = prov["lines"][0]["length_ft"]
        for grp in _STRIP_2D_COUPLING:
            if grp in f["Geometry"]:
                del f["Geometry"][grp]

    n_perim = int(mesh.perimeter.shape[0])
    (out / XNN).write_text(patch_chippewa_xnn(AREA_NAME, n_perim))
    (out / BNN).write_text(patch_chippewa_bnn(100.0 * flow_scale, hydrograph_node=1))

    return {
        "real": r.n_real, "ghost": r.n_ghost, "faces": r.n_faces,
        "perimeter_pts": n_perim, "bc_faces": len(run),
        "bc_length_ft": round(bc_len, 1), "flow_scale": flow_scale,
        "deck": "chippewa-clean-fake-reach", "rundir": str(out),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--flow-scale", type=float, default=1.0)
    ap.add_argument("--xmax", type=float, default=408600.0)
    ap.add_argument("--ymin", type=float, default=1803025.0)
    args = ap.parse_args()

    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < args.xmax) & (c[:, 1] > args.ymin)
    info = build(Path(args.out), keep, flow_scale=args.flow_scale)
    print("[build] " + "  ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
