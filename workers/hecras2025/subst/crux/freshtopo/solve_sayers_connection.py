#!/usr/bin/env python3
"""Run RasGeomPreprocess + RasUnsteady on the Sayers Dam connection deck; extract
the connection (SA/2D structure) flow + per-cell max WSE. In trid3nt-local/hecras:latest."""
import json, os, subprocess, sys
from pathlib import Path
import h5py, numpy as np

BIN = Path(os.environ.get("TRID3NT_HECRAS_BIN_DIR", "/opt/hecras/bin"))
LIBS = Path("/opt/hecras/libs")
PLAN = "BaldEagleDamBrk.p09.tmp.hdf"
GEOM = "x09"
AREA = "BaldEagleCr"
_FILL = 1e30
CONN = (f"Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/"
        f"2D Flow Areas/{AREA}/2D Hyd Conn/Sayers Dam/Structure Variables")
TIMEP = ("Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/Time")


def _env():
    e = dict(os.environ)
    e["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(LIBS), str(LIBS / "mkl"), str(LIBS / "rhel_8"), e.get("LD_LIBRARY_PATH", "")])
    e["PATH"] = os.pathsep.join([str(BIN), e.get("PATH", "")])
    return e


def _run(engine, cwd, timeout):
    p = subprocess.run([str(BIN / engine), PLAN, GEOM], cwd=str(cwd), env=_env(),
                       capture_output=True, text=True, timeout=timeout)
    fin = "Finished" in p.stdout
    print(f"  $ {engine} -> exit={p.returncode} finished={fin}", flush=True)
    if p.returncode != 0 or not fin:
        print(p.stdout[-3000:]); print(p.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"FAIL: {engine} did not finish cleanly")


def main(run):
    run = Path(run)
    _run("RasGeomPreprocess", run, 500)
    _run("RasUnsteady", run, 560)
    with h5py.File(run / PLAN, "r") as f:
        if "Results" not in f:
            raise SystemExit("FAIL: no /Results group")
        va = f["Results/Unsteady/Summary/Volume Accounting"]
        vacc = {k: (va.attrs[k].decode() if isinstance(va.attrs[k], bytes) else float(va.attrs[k]))
                for k in va.attrs}
        sv = np.asarray(f[CONN][()], float)
        sv = np.where(np.abs(sv) > _FILL, np.nan, sv)
        if TIMEP in f and f[TIMEP].shape[0] == sv.shape[0]:
            t = np.asarray(f[TIMEP][()], float)
        else:
            t = np.arange(sv.shape[0], dtype=float)
        # cols: 0 Total Flow, 1 Weir Flow, 2 Stage HW, 3 Stage TW, 4 Gate Flow (cfs/ft)
        conn = {
            "total_flow_cfs": {"min": float(np.nanmin(sv[:, 0])), "max": float(np.nanmax(sv[:, 0])),
                               "mean": float(np.nanmean(sv[:, 0]))},
            "weir_flow_cfs": {"min": float(np.nanmin(sv[:, 1])), "max": float(np.nanmax(sv[:, 1])),
                              "mean": float(np.nanmean(sv[:, 1]))},
            "gate_flow_cfs": {"max": float(np.nanmax(sv[:, 4]))},
            "stage_hw_ft": {"min": float(np.nanmin(sv[:, 2])), "max": float(np.nanmax(sv[:, 2]))},
            "stage_tw_ft": {"min": float(np.nanmin(sv[:, 3])), "max": float(np.nanmax(sv[:, 3]))},
            "n_steps": int(sv.shape[0]),
        }
        base = "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output"
        mw = np.asarray(f[f"{base}/2D Flow Areas/{AREA}/Maximum Water Surface"][()], float)
        mw = np.where(np.abs(mw) > _FILL, np.nan, mw)
        if mw.ndim == 2:
            mw = mw[0] if mw.shape[0] < mw.shape[1] else mw[:, 0]
        area = f[f"Geometry/2D Flow Areas/{AREA}"]
        cc = np.asarray(area["Cells Center Coordinate"][()], float)
        me = np.asarray(area["Cells Minimum Elevation"][()], float)
        me = np.where(np.abs(me) > _FILL, np.nan, me)
        ncell = min(mw.shape[0], cc.shape[0], me.shape[0])
        np.savez(run / "conn_arrays.npz",
                 t=t[: sv.shape[0]], sv=sv,
                 cc=cc[:ncell], me=me[:ncell], maxwse=mw[:ncell])
    out = {"volume_accounting": vacc, "connection": conn}
    (run / "conn_metrics.json").write_text(json.dumps(out, indent=2))
    print("CONN_TOTAL_FLOW peak=%.1f mean=%.1f min=%.1f cfs | gate_max=%.1f | HW[%.1f,%.1f] TW[%.1f,%.1f]"
          % (conn["total_flow_cfs"]["max"], conn["total_flow_cfs"]["mean"], conn["total_flow_cfs"]["min"],
             conn["gate_flow_cfs"]["max"], conn["stage_hw_ft"]["min"], conn["stage_hw_ft"]["max"],
             conn["stage_tw_ft"]["min"], conn["stage_tw_ft"]["max"]), flush=True)
    print("VOL err%%=%s in=%.0f out=%.0f start=%.0f end=%.0f acft"
          % (vacc.get("Error Percent"), vacc.get("Total Boundary Flux of Water In", 0),
             vacc.get("Total Boundary Flux of Water Out", 0), vacc.get("Volume Starting", 0),
             vacc.get("Volume Ending", 0)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
