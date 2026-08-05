#!/usr/bin/env python3
"""Assemble the WETTING pure-2D fake-reach carve deck (ADR 0138 / OI-FT1).

This extends ``build_chippewa_fakereach_deck.py`` (ADR 0137, which SOLVES but
stays DRY) with the ONE missing forcing link the chain named: the plan-HDF
``/Event Conditions`` 2D-BC-line flow-hydrograph enumeration read by the engine's
``read_un_q2d_bc_``. That schema was decoded (schema facts only) from shipped
HEC-RAS 6.6 pure-2D plan HDFs and is authored here by ``hecras_event_conditions``
against OUR carved ``Inflow`` BC line -- directing moving water onto the carved
2D area so it WETS.

The deck otherwise matches ADR 0137: the fresh NW-quadrant Muncie carve, the
Chippewa clean fake reach (``.x04``/``.b04``, an inert required-1D placeholder),
solved by production 6.6 ``RasGeomPreprocess`` + ``RasUnsteady``.

Usage: python build_chippewa_wetting_deck.py <out_rundir> [--peak-cfs F]
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
from hecras_event_conditions import (  # noqa: E402
    write_flow_hydrograph_2d_bc, write_normal_depth_2d_bc,
    strip_1d_reach_bcs, finalize_event_conditions,
)
from build_freshtopo_deck import (  # noqa: E402
    AREA_NAME, PLAN, XNN, BNN, MUNCIE_PLAN, _STRIP_2D_COUPLING,
)

# Mirror the copied Muncie plan's computational window (Interval=Days, 25 hourly
# ordinates over one day) so the EC hydrograph is consistent with Plan Data.
_START_DATE = "01Jan1900 2400"
_END_DATE = "02Jan1900 2400"
_N_ORD = 25


def _hydrograph(peak_cfs: float, base_cfs: float = 200.0):
    """A 25-ordinate ramp (base -> peak over the first ~6 hrs, then hold)."""
    t = np.linspace(0.0, 1.0, _N_ORD)                       # days
    ramp = np.clip(t / (6.0 / 24.0), 0.0, 1.0)              # full by ~6 hrs
    q = base_cfs + (peak_cfs - base_cfs) * ramp
    return t, q


def build(out: Path, keep_mask: np.ndarray, peak_cfs: float = 2000.0,
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
        inflow_run = perimeter_face_run(
            mesh, min_elevation=tables.face_min_elevation, n_faces=n_bc_faces)
        ds_run = perimeter_face_run(mesh, edge="s", n_faces=n_bc_faces)
        if set(inflow_run) & set(ds_run):
            raise ValueError("Inflow and DS perimeter runs overlap")
        lines = [
            BoundaryConditionLine(name="Inflow", sa_2d=AREA_NAME, face_indices=inflow_run),
            BoundaryConditionLine(name="DS", sa_2d=AREA_NAME, face_indices=ds_run),
        ]
        prov = write_boundary_condition_lines(f, lines, mesh)
        bc_len = prov["lines"][0]["length_ft"]
        for grp in _STRIP_2D_COUPLING:
            if grp in f["Geometry"]:
                del f["Geometry"][grp]

        # --- OI-FT1: the 2D-BC-line Event-Conditions forcing (the wetting link) ---
        # Inflow = a 2D-BC flow hydrograph (the wetting inflow); DS = a 2D-BC
        # normal depth (the outlet, so the domain drains instead of filling).
        n_stale = strip_1d_reach_bcs(f)
        t, q = _hydrograph(peak_cfs)
        ec = write_flow_hydrograph_2d_bc(
            f, AREA_NAME, "Inflow", t, q,
            start_date=_START_DATE, end_date=_END_DATE, interval="Days")
        write_normal_depth_2d_bc(f, AREA_NAME, "DS", slope=0.001)
        finalize_event_conditions(f)

    n_perim = int(mesh.perimeter.shape[0])
    (out / XNN).write_text(patch_chippewa_xnn(AREA_NAME, n_perim))
    # The .b04 fake reach is an inert required-1D placeholder; the REAL 2D forcing
    # lives in Event Conditions. Keep a modest constant hold on the fake reach.
    (out / BNN).write_text(patch_chippewa_bnn(100.0, hydrograph_node=1))

    return {
        "real": r.n_real, "ghost": r.n_ghost, "faces": r.n_faces,
        "perimeter_pts": n_perim, "bc_faces": ec["faces"],
        "bc_length_ft": round(bc_len, 1), "ec_peak_cfs": ec["peak_cfs"],
        "ec_ordinates": ec["n_ordinates"], "stale_1d_ec_removed": n_stale,
        "deck": "chippewa-wetting-2dbc-ec", "rundir": str(out),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--peak-cfs", type=float, default=2000.0)
    ap.add_argument("--xmax", type=float, default=408600.0)
    ap.add_argument("--ymin", type=float, default=1803025.0)
    args = ap.parse_args()

    m = load_muncie()
    c = m.cell_center[:m.nc_real]
    keep = (c[:, 0] < args.xmax) & (c[:, 1] > args.ymin)
    info = build(Path(args.out), keep, peak_cfs=args.peak_cfs)
    print("[build] " + "  ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
