#!/usr/bin/env python3
"""Assemble the Bald Eagle Sayers Dam SA/2D-connection solve deck.

The connection front's matched pair, both HEC-authored (Example_Projects_6_6):

  ``baldeagle_connection/BaldEagleDamBrk.g09.hdf`` -- the 18066-cell ``BaldEagleCr``
     2D area whose sole ``Type="Connection"`` structure is Sayers Dam (weir coef
     3.1, one gate group, ``Use 2D for Overflow=1``);
  ``pure2d_reference/BaldEagleDamBrk.x09`` -- its matched ``.xNN`` preprocessor
     geometry, carrying the ``Section - Storage Area Connection Data`` weir block
     with the HW/TW cell-face pairing arrays.

partitioned the connection blocker to exactly this missing mesh; with it
seeded the deck solves end-to-end through the production 6.6 engines
(RasGeomPreprocess + RasUnsteady) with NONZERO weir flow across the connection.

Assembly: ``build_skeleton`` wraps g09's geometry (Structures KEPT -- the
connection) in a Results-typed plan HDF; ``hecras_event_conditions`` authors the
2D-BC-line forcing (an inflow hydrograph on ``Upstream Inflow``, normal-depth
outflows) that wets the mesh; ``patch_chippewa_bnn(initial_stage=...)`` seeds the
2D area's initial water surface so the reservoir impounds above the 683 ft crest
and overtops the Sayers Dam connection from t=0. The shipped x09 is used verbatim
(the original g09 mesh needs no perimeter/name patch); ``--weir-coef`` rewrites
both the x09 connection weir-coef fields and the plan-HDF ``Weir Coef`` attribute
for the weir-discharge A/B.

Solve: docker run --rm -v <out>:/run -v <freshtopo>:/ft:ro --entrypoint bash \
         trid3nt-local/hecras:latest -lc \
         '/opt/trid3nt/.venv/bin/python /ft/solve_sayers_connection.py /run'
"""
from __future__ import annotations
import argparse, shutil, sys
from pathlib import Path
import h5py
import numpy as np

_HERE = Path(__file__).resolve().parent
_CRUX = _HERE.parent
_REPO = _HERE.parents[4]
_FIX = _REPO / "workers/hecras/fixtures/baldeagle_connection"
_PURE2D = _CRUX / "pure2d_reference"
_SKEL = _REPO / "workers/hecras/fixtures"
for p in (str(_HERE), str(_CRUX), str(_SKEL)):
    if p not in sys.path:
        sys.path.insert(0, p)

from build_plan_hdf_skeleton import build_skeleton  # noqa: E402
from hecras_event_conditions import (  # noqa: E402
    write_flow_hydrograph_2d_bc, write_normal_depth_2d_bc,
    strip_1d_reach_bcs, finalize_event_conditions,
)
from hecras_pure2d_deck import patch_chippewa_bnn  # noqa: E402

AREA = "BaldEagleCr"
PLAN = "BaldEagleDamBrk.p09.tmp.hdf"
XNN = "BaldEagleDamBrk.x09"
BNN = "BaldEagleDamBrk.b09"
G09 = _FIX / "BaldEagleDamBrk.g09.hdf"
CREST_FT = 683.0  # Sayers Dam weir crest (x09 Profile Data)


def patch_x09_weir_coef(text: str, coef: float) -> str:
    """Rewrite the Sayers Dam connection weir coefficient (both 3.1 fields in the
    Storage Area Connection Data section), preserving the fixed-field width."""
    lines = text.splitlines()
    new = f"{coef:.1f}"
    if len(new) != 3:
        raise ValueError(f"weir coef {coef} must format to 3 chars (fixed field)")
    in_conn, n = False, 0
    for i, l in enumerate(lines):
        if l.startswith("Section - Storage Area Connection"):
            in_conn = True
            continue
        if in_conn and l.startswith("Section - "):
            break
        if in_conn and "3.1" in l:
            n += l.count("3.1")
            lines[i] = l.replace("3.1", new)
    if n != 2:
        raise ValueError(f"expected 2 weir-coef fields, patched {n}")
    return "\n".join(lines) + "\n"


def build(out: Path, *, g09_hdf: Path = G09, initial_stage: float = 688.0,
          inflow_cfs: float = 10000.0, window_h: float = 4.0, ds_slope: float = 0.001,
          weir_coef: float | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / PLAN
    prov = build_skeleton(
        g09_hdf, plan_path,
        geometry_filename="BaldEagleDamBrk.g09", flow_filename="BaldEagleDamBrk.u09",
        project_title="Bald Eagle Creek - Sayers Dam SA/2D connection")

    n = max(2, int(round(window_h)) + 1)
    times = [float(i) for i in range(n)]
    flows = [float(inflow_cfs)] * n
    endh = int(round(window_h))
    with h5py.File(plan_path, "r+") as f:
        n_stale = strip_1d_reach_bcs(f)
        ec = write_flow_hydrograph_2d_bc(
            f, AREA, "Upstream Inflow", times, flows,
            start_date="02Jan1900 0000", end_date=f"02Jan1900 {endh:02d}00", interval="Hour")
        write_normal_depth_2d_bc(f, AREA, "DSNormalDepth", slope=ds_slope)
        write_normal_depth_2d_bc(f, AREA, "DS2NormalD", slope=ds_slope)
        finalize_event_conditions(f)
        pi = f["Plan Data/Plan Information"]
        pi.attrs["Simulation Start Time"] = np.bytes_(b"02Jan1900 00:00:00")
        pi.attrs["Simulation End Time"] = np.bytes_(f"02Jan1900 {endh:02d}:00:00".encode())
        pi.attrs["Time Window"] = np.bytes_(
            f"02Jan1900 00:00:00 to 02Jan1900 {endh:02d}:00:00".encode())
        if weir_coef is not None:
            st = f["Geometry/Structures/Attributes"]
            rows = st[()]
            rows["Weir Coef"][:] = np.float32(weir_coef)
            st[...] = rows

    x09_text = (_PURE2D / XNN).read_text()
    if weir_coef is not None:
        x09_text = patch_x09_weir_coef(x09_text, weir_coef)
    (out / XNN).write_text(x09_text)
    (out / BNN).write_text(
        patch_chippewa_bnn(inflow_cfs, hydrograph_node=1, initial_stage=initial_stage))

    return {"structure_types": prov.get("structure_types"),
            "flow_areas": prov.get("flow_areas"), "stale_1d_ec_removed": n_stale,
            "ec_faces": ec["faces"], "ec_peak_cfs": ec["peak_cfs"],
            "initial_stage": initial_stage, "weir_coef": weir_coef,
            "window_h": window_h, "rundir": str(out)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--initial-stage", type=float, default=688.0)
    ap.add_argument("--inflow", type=float, default=10000.0)
    ap.add_argument("--window-h", type=float, default=4.0)
    ap.add_argument("--weir-coef", type=float, default=None)
    a = ap.parse_args()
    info = build(Path(a.out), initial_stage=a.initial_stage, inflow_cfs=a.inflow,
                 window_h=a.window_h, weir_coef=a.weir_coef)
    print("[build] " + "  ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
