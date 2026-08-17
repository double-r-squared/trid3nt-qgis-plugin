#!/usr/bin/env python3
"""Assemble the fresh-topology deck (carve -> geometry HDF -> .xNN/.bNN) --.

Runs on the HOST (numpy/h5py). Produces a rundir the solver harness consumes:

  Fresh2D.p04.tmp.hdf -- Muncie plan HDF with the 2D area REPLACED by the carved
                         fresh-topology mesh + a Boundary Condition Line authored;
                         the 1D<->2D lateral-weir coupling groups stripped.
  Fresh2D.x04         -- Muncie's proven geometry with both lateral weirs removed
                         (``remove_lateral_weirs``), SA perimeter count patched.
  Fresh2D.b04         -- Muncie's boundary file patched (``patch_muncie_bnn``):
                         breach zeroed, a valid hydrograph location, optional
                         inflow scale.

This deck SOLVES end-to-end through the production 6.6 RasGeomPreprocess +
RasUnsteady: the fresh tessellation passes the solver's geometry +
2D-initialisation consistency checks and completes with valid volume accounting.
The White River 1D reach carries the forcing; the fresh 2D flow area is present
and solved (dry -- directing an inflow to its BC line has no combined-deck .bNN
reference; the named open item).

Usage: python build_freshtopo_deck.py <out_rundir> [--flow-scale F]
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

from carve_muncie import load_muncie, carve, MUNCIE_PLAN  # noqa: E402
from hecras_geometry_writer import (  # noqa: E402
    write_2d_flow_area, write_boundary_condition_lines,
    BoundaryConditionLine, perimeter_face_run, PropertyTableOptions, AREA_GROUP,
)
from hecras_pure2d_deck import remove_lateral_weirs, patch_muncie_bnn  # noqa: E402

AREA_NAME = "2D Interior Area"          # reuse Muncie's name (Results paths match)
PLAN = "Fresh2D.p04.tmp.hdf"
XNN = "Fresh2D.x04"
BNN = "Fresh2D.b04"
_STRIP_2D_COUPLING = ["Structures", "Reference Lines", "2D Flow Area Break Lines"]


def build(out: Path, keep_mask: np.ndarray, flow_scale: float = 1.0,
          n_bc_faces: int = 14) -> dict:
    import h5py

    m = load_muncie()
    r = carve(m, keep_mask)
    mesh, tables = r.mesh, r.tables
    wrk = MUNCIE_PLAN.parent

    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / PLAN
    shutil.copy2(MUNCIE_PLAN, plan_path)
    with h5py.File(plan_path, "r+") as f:
        projection = f.attrs["Projection"]
        parent = f[AREA_GROUP]
        # replace ONLY the area subgroup + its Attributes; keep the parent-level
        # RASMapper mesh-gen inputs (Cell Info/Points/Polygon*) that HEC expects.
        if AREA_NAME in parent:
            del parent[AREA_NAME]
        if "Attributes" in parent:
            del parent["Attributes"]
        write_2d_flow_area(
            f, AREA_NAME, mesh, tables, PropertyTableOptions(),
            projection_wkt=projection.decode() if isinstance(projection, bytes) else projection,
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
    (out / XNN).write_text(
        remove_lateral_weirs((wrk / "Muncie.x04").read_text(), n_perim))
    (out / BNN).write_text(
        patch_muncie_bnn((wrk / "Muncie.b04").read_text(), flow_scale=flow_scale))

    return {
        "real": r.n_real, "ghost": r.n_ghost, "faces": r.n_faces, "fp": r.n_fp,
        "cut_faces": r.n_cut_faces, "perimeter_pts": n_perim,
        "bc_faces": len(run), "bc_length_ft": round(bc_len, 1),
        "flow_scale": flow_scale, "rundir": str(out),
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
