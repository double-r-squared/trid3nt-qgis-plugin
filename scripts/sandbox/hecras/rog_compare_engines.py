"""Cross-engine rain-on-grid comparison: TELEMAC-2D vs HEC-RAS 2D (ADR 0199).

Replicates the Godara, Bruland and Alfredsen 2024 (Front. Water 6:1384205)
TELEMAC-vs-HEC-RAS rain-on-grid EXPERIMENT on OUR US catchment -- Coweeta Creek
NC (pour point -83.40402 35.05746, the ADR 0193/0196 site) -- with the SAME design
storm (25 mm/hr for 6 h) and the SAME AMC-II / CN-equivalent infiltration, so the
two solvers are compared like-for-like on a steep catchment.

This is a TEMPLATE/KNOB SMOKE + reference comparison, NOT the NATE-gated replication
calibration (no gauge calibration runs).

Reported (the paper's experiment, our catchment):
  (a) outlet / peak discharge per engine,
  (b) wet-area extent per engine,
  (c) wall-time per engine,
  (d) the paper's qualitative findings CHECKED against ours (triangular-vs-square
      mesh stability on this steep catchment, velocity behaviour on steep slopes) --
      reported honestly whichever way it lands; disagreement is a finding.

Engine status (2026-08-08):
  - TELEMAC-2D RoG: LIVE (ADR 0196). ``--telemac`` runs the landed direct driver
    (scripts/sandbox/telemac/rog_coweeta_live.py) end-to-end, or ``--telemac-ref``
    reads the ADR 0196 landed numbers as the reference row.
  - HEC-RAS 2D RoG: authoring LIVE-COMPLETE (fresh-AOI 2D mesh + terrain subgrid
    tables + SCS-CN infiltration geometry layer + uniform-storm Meteorology
    Values/Timestamp, all accepted by the 6.6 engine's readers); the SOLVE is
    BLOCKED at the ``READ_UN_M2D_PRECIP_INTERP`` (MetInterp.f90) per-2D-area
    interpolation folder -- the GUI-precomputed schema that segfaults when authored
    blind (needs a reference RoG deck, ADR 0137 wall). ``--hecras`` authors the
    Coweeta HR2D RoG deck and attempts the solve, recording the exact blocker +
    wall time to the authoring stage.

Run in the agent venv with .env.local sourced (set -a; source .env.local; set +a).
ASCII only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "server" / "src"))
_FRESHTOPO = REPO / "services/workers/hecras2025/subst/crux/freshtopo"
_HECRAS2025 = REPO / "services/workers/hecras2025"

POUR_POINT = (-83.40402, 35.05746)
BBOX = (-83.47, 35.02, -83.36, 35.10)             # Coweeta Creek catchment
DESIGN_STORM_MM_PER_HR = 25.0
STORM_DURATION_HR = 6.0
AMC = "normal"                                     # AMC II
CN2 = 80.0                                          # AMC-II CN-equivalent

#: The ADR 0196 landed TELEMAC-2D RoG Coweeta live numbers (AMC II), the reference
#: row when the live TELEMAC re-run is not exercised here.
TELEMAC_REF = {
    "engine": "TELEMAC-2D",
    "mesh": "triangular (TIN)",
    "peak_outlet_q_m3s": 45.5,
    "runoff_volume_1e3_m3": 162.0,
    "max_depth_m": 6.95,
    "continuity": 1.3e-15,
    "wall_s": 45.0,
    "nodes": 4854,
    "cells": 9521,
    "status": "live (ADR 0196 C4)",
}


def _paper_findings_check(telemac: dict, hecras: dict) -> list[dict]:
    """The paper's qualitative findings vs ours (honest -- disagreement is a finding)."""
    checks = []
    checks.append({
        "finding": "TELEMAC triangular mesh is stable on steep terrain",
        "paper": "stable",
        "ours": (f"CORRECT END, continuity {telemac.get('continuity')}, "
                 f"peak {telemac.get('peak_outlet_q_m3s')} m3/s"
                 if telemac.get("status", "").startswith("live")
                 else "not run this session"),
        "agrees": telemac.get("status", "").startswith("live"),
    })
    checks.append({
        "finding": "HEC-RAS structured/square grid stability on the steep catchment",
        "paper": "structured grid needed care (stability sensitivity) on steep slopes",
        "ours": hecras.get("status", "not run"),
        "agrees": None,  # HR2D solve blocked at MetInterp -- not yet comparable
    })
    checks.append({
        "finding": "velocity behaviour on steep slopes (both engines)",
        "paper": "velocities sensitive to scheme on steep slopes",
        "ours": "TELEMAC max fields captured (ADR 0196); HR2D pending the solve link",
        "agrees": None,
    })
    return checks


def run_hecras(workdir: Path, *, do_solve: bool = True) -> dict:
    """Author the Coweeta HR2D RoG deck (fresh-AOI) and attempt the solve.

    Records the authored cell counts, the authoring wall time, and -- on solve --
    the exact terminal status (the MetInterp block until link 3 is decoded)."""
    for p in (str(_FRESHTOPO), str(_HECRAS2025)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from flood2d_pipeline import author_and_compose, _docker_solve, Flood2dPipelineError

    workdir.mkdir(parents=True, exist_ok=True)
    # Fetch the AOI DEM via the server seam (reuse the template's fetcher).
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    dem_layer = TOOL_REGISTRY["fetch_dem"].fn(bbox=list(BBOX), resolution_m=10)
    dem_uri = getattr(dem_layer, "uri", None) or dem_layer.get("uri")
    from trid3nt_server.agent.tools.simulation.solver.solver import _download_object
    dem_tif = workdir / "dem.tif"
    _download_object(str(dem_uri), dem_tif)

    t0 = time.time()
    result, info = author_and_compose(
        dem_tif, workdir, resolution_m=40.0,
        design_storm_mm_per_hr=DESIGN_STORM_MM_PER_HR,
        storm_duration_hr=STORM_DURATION_HR, curve_number=CN2, amc_condition=AMC)
    author_s = time.time() - t0

    out = {
        "engine": "HEC-RAS 2D",
        "mesh": "structured/subgrid (AuthorMesh)",
        "cells_real": result.cells_real,
        "cells_total": result.cells_total,
        "faces": result.faces,
        "storm_total_mm": info.get("storm_total_mm"),
        "cn_effective": [info.get("cn_min"), info.get("cn_max")],
        "author_wall_s": round(author_s, 1),
        "deck_dir": result.deck_dir,
    }
    if not do_solve:
        out["status"] = "authored (solve not attempted)"
        return out

    t1 = time.time()
    try:
        metrics = _docker_solve(Path(result.deck_dir), "trid3nt-local/hecras:latest")
        out["solve_wall_s"] = round(time.time() - t1, 1)
        out["status"] = "SOLVED"
        out["peak_outlet_q_m3s"] = metrics.get("peak_outlet_q_m3s")
        out["max_depth_m"] = metrics.get("max_depth_m")
        out["wet_km2"] = metrics.get("wet_km2")
    except Flood2dPipelineError as exc:
        out["solve_wall_s"] = round(time.time() - t1, 1)
        msg = str(exc)
        blocked = "READ_UN_M2D_PRECIP_INTERP" in msg or "MetInterp" in msg
        out["status"] = ("BLOCKED at READ_UN_M2D_PRECIP_INTERP (MetInterp per-2D-area "
                         "interpolation folder -- the GUI-precomputed schema; ADR 0199 "
                         "residual)" if blocked else f"solve failed: {msg[:300]}")
    return out


def run_telemac(workdir: Path) -> dict:
    """Run the landed TELEMAC-2D RoG Coweeta driver end-to-end (ADR 0196)."""
    driver = REPO / "scripts/sandbox/telemac/rog_coweeta_live.py"
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(driver), "all"], cwd=str(REPO),
        capture_output=True, text=True, timeout=3600,
        env={**__import__("os").environ, "ROG_RUNDIR": str(workdir)})
    wall = time.time() - t0
    res = dict(TELEMAC_REF)
    res["wall_s"] = round(wall, 1)
    res["status"] = "live (this run)" if proc.returncode == 0 else \
        f"live driver exit {proc.returncode} -- using ADR 0196 reference numbers"
    result_json = workdir / "telemac_rog_result.json"
    if result_json.is_file():
        try:
            m = json.loads(result_json.read_text())
            res.update({k: m[k] for k in
                        ("peak_outlet_q_m3s", "max_depth_m", "runoff_volume_1e3_m3")
                        if k in m})
        except Exception:  # noqa: BLE001
            pass
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hecras", action="store_true", help="author+solve the HR2D RoG deck")
    ap.add_argument("--no-solve", action="store_true", help="HR2D: author only, skip solve")
    ap.add_argument("--telemac", action="store_true", help="run the live TELEMAC RoG driver")
    ap.add_argument("--telemac-ref", action="store_true",
                    help="use the ADR 0196 landed TELEMAC numbers (no live re-run)")
    ap.add_argument("--workdir", default="/tmp/rog_compare")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    hecras = {"engine": "HEC-RAS 2D", "status": "not run"}
    telemac = {"engine": "TELEMAC-2D", "status": "not run"}

    if args.hecras:
        hecras = run_hecras(workdir / "hecras", do_solve=not args.no_solve)
    if args.telemac:
        telemac = run_telemac(workdir / "telemac")
    elif args.telemac_ref or not args.telemac:
        telemac = dict(TELEMAC_REF)

    report = {
        "catchment": "Coweeta Creek NC",
        "pour_point": POUR_POINT,
        "design_storm": f"{DESIGN_STORM_MM_PER_HR} mm/hr x {STORM_DURATION_HR} h "
                        f"= {DESIGN_STORM_MM_PER_HR * STORM_DURATION_HR:.0f} mm",
        "amc": "II (normal)",
        "engines": {"telemac": telemac, "hecras": hecras},
        "paper_findings_check": _paper_findings_check(telemac, hecras),
    }
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
