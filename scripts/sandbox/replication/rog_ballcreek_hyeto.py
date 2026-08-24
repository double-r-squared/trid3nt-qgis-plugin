"""Ball Creek RoG RE-GRADE with the TRUE time-varying AORC hyetograph.

drove each event as a CONSTANT design-storm intensity (the installed
RAINDEF=1 limit) and landed raw NSE -1.41 with a +11 h peak-timing lag. This
driver replaces the constant pulse with the REAL AORC hourly hyetograph via the
new native time-varying path (rain_hyetograph_blocks -> RAINDEF=3 FORTRAN FILE),
re-calibrates CN, and re-grades NSE / R2 / peak / timing lag on the SAME mesh
and gauge, then re-runs the Feb 2018 validation + the multi-peak control with
the calibrated params.

Reuses the cached mesh + forcing (scripts/sandbox/replication/rog_ballcreek_*).
Run in the agent venv with .env.local sourced. ASCII only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import rog_ballcreek_live as LIVE  # noqa: E402

FORCING_DIR = Path("/tmp/rog_ballcreek/forcing")
TIME_STEP_S = 2.0


def build_blocks(key: str) -> dict:
    """Block hyetograph (t_end_s, gross_mm) from the cached AORC hourly series,
    aligned so t=0 is the rising-limb start, spanning the sim window."""
    f = json.loads((FORCING_DIR / f"{key}.json").read_text())
    t_rise = pd.to_datetime(f["t_rise"])
    sim_h = int(f["sim_hours"])
    p = pd.Series(f["precip_mm"], index=pd.to_datetime(f["precip_times"])).astype(float)
    # hour i covers [t_rise + i h, t_rise + (i+1) h]; AORC value labelled at the
    # hour start is that interval's gross rainfall (mm).
    blocks = []
    total = 0.0
    for i in range(sim_h):
        t0 = t_rise + pd.Timedelta(hours=i)
        mm = float(p.get(t0, 0.0))
        blocks.append([float((i + 1) * 3600), round(max(0.0, mm), 5)])
        total += max(0.0, mm)
    return {"blocks": blocks, "sim_hours": sim_h, "total_mm": round(total, 2),
            "t_rise": f["t_rise"], "baseflow": float(f["baseflow_m3s"]),
            "obs_times": f["obs_times"], "obs_q": f["obs_q_m3s"],
            "obs_peak": float(f["obs_peak_m3s"]), "t_peak": f["t_peak"]}


def solve_hyeto(key: str, *, cn, amc: int, manning_scale: float,
                ia_option: int = 1, tag: str | None = None) -> dict:
    """Stage + run one time-varying-hyetograph solve through the rebuilt image."""
    b = build_blocks(key)
    facts = json.loads((LIVE.RUNDIR / "mesh_facts.json").read_text())
    tag = tag or f"hy_{key}_cn{cn}_amc{amc}_mn{manning_scale}".replace(".", "p")
    d = LIVE.RUNDIR / f"solve_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "watershed.slf").write_bytes((LIVE.RUNDIR / "watershed.slf").read_bytes())
    (d / "node_cn2.txt").write_bytes((LIVE.RUNDIR / "node_cn2.txt").read_bytes())
    base_manning = [float(v) for v in (LIVE.RUNDIR / "node_manning.txt").read_text().split()]
    scaled = [max(0.005, min(1.0, m * manning_scale)) for m in base_manning]
    (d / "node_manning.txt").write_text("\n".join(f"{v:.4f}" for v in scaled) + "\n")

    dt = TIME_STEP_S
    reach = {
        "name": f"ballcreek_{tag}", "mode": "rain_on_grid",
        "watershed_slf": "watershed.slf", "amc_condition": int(amc),
        "node_cn2_file": "node_cn2.txt", "node_manning_file": "node_manning.txt",
        "outlet_lonlat": facts["outlet_lonlat"], "initial_abstraction_option": int(ia_option),
        "n_outlet_nodes": 6, "duration_s": float(b["sim_hours"]) * 3600.0,
        "time_step_s": dt, "graphic_period": max(1, int(round(900.0 / dt))),
        "rain_hyetograph_blocks": b["blocks"],
    }
    if cn is not None:
        reach["curve_number"] = float(cn)
    (d / "manifest.json").write_text(json.dumps({"run_id": f"rog-bc-{tag}", "reach": reach}))
    argv = [
        "docker", "run", "--rm", "-v", f"{d}:/data",
        "-e", "TRID3NT_TELEMAC_SOLVE_TIMEOUT=86400",
        "--entrypoint", "/usr/local/bin/_entrypoint.sh", LIVE.TELEMAC_IMAGE,
        "python", "/opt/trid3nt/workers/telemac/entrypoint.py",
        "--data-dir", "/data", "--run-id", f"rog-bc-{tag}",
    ]
    print(f"[hyeto {tag}] blocks={len(b['blocks'])} total_rain={b['total_mm']}mm "
          f"cn={cn} amc={amc} mn={manning_scale} sim={b['sim_hours']}h ...", flush=True)
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=90000)
    mpath = d / "telemac_metrics.json"
    if not mpath.exists():
        print("[hyeto] NO METRICS. STDERR tail:", cp.stderr[-2000:], flush=True)
        raise SystemExit(2)
    m = json.loads(mpath.read_text())
    if m.get("status") != "ok":
        print("[hyeto] SOLVE ERROR:", m.get("error"), m.get("listing_tail", "")[-800:], flush=True)
        return {"status": "error", "tag": tag, "metrics": m}
    return grade(key, d, b, m, cn=cn, amc=amc, manning_scale=manning_scale, tag=tag)


def grade(key, solve_dir, b, metrics, *, cn, amc, manning_scale, tag) -> dict:
    """Align computed outlet hydrograph to observed; NSE/R2/peak/timing lag."""
    from trid3nt_server.tools.processing.compute_skill_metrics.compute_skill_metrics import (
        nash_sutcliffe_efficiency, pearson_r2)

    hyd = json.loads((solve_dir / "rog_outlet_hydrograph.json").read_text())
    t_h = np.asarray(hyd["t_s"], dtype=float) / 3600.0
    q_comp = np.asarray(hyd["q_m3s"], dtype=float)
    baseflow = float(b["baseflow"])

    obs_t = pd.to_datetime(b["obs_times"])
    obs_q = np.asarray(b["obs_q"], dtype=float)
    t_rise = pd.to_datetime(b["t_rise"])
    obs_rel_h = np.array([(t - t_rise).total_seconds() / 3600.0 for t in obs_t])
    comp_at_obs = np.interp(obs_rel_h, t_h, q_comp, left=0.0, right=q_comp[-1])
    comp_total = comp_at_obs + baseflow

    m = obs_rel_h >= 0
    nse = nash_sutcliffe_efficiency(obs_q[m], comp_total[m])
    r2 = pearson_r2(obs_q[m], comp_total[m])

    # timing lag: computed peak hour minus observed peak hour (rel to t_rise).
    comp_peak_h = float(t_h[int(np.argmax(q_comp))]) if q_comp.size else 0.0
    obs_peak_h = float(obs_rel_h[int(np.argmax(obs_q))])
    lag_h = round(comp_peak_h - obs_peak_h, 1)

    # peak-aligned NSE (shift computed so its peak coincides with observed peak).
    shift = obs_peak_h - comp_peak_h
    comp_aligned = np.interp(obs_rel_h, t_h + shift, q_comp, left=0.0, right=q_comp[-1]) + baseflow
    nse_al = nash_sutcliffe_efficiency(obs_q[m], comp_aligned[m])
    r2_al = pearson_r2(obs_q[m], comp_aligned[m])

    peak_comp = float(comp_total.max())
    vol_comp = float(np.trapz(np.clip(comp_at_obs, 0, None), obs_rel_h) * 3600.0)
    vol_obs = float(np.trapz(np.clip(obs_q - baseflow, 0, None), obs_rel_h) * 3600.0)
    out = dict(
        status="ok", key=key, tag=tag, cn=cn, amc=amc, manning_scale=manning_scale,
        nse=_r(nse), r2=_r(r2), nse_aligned=_r(nse_al), r2_aligned=_r(r2_al),
        peak_obs=round(float(obs_q.max()), 3), peak_comp=round(peak_comp, 3),
        peak_err_pct=round(100 * (peak_comp - obs_q.max()) / obs_q.max(), 1),
        timing_lag_h=lag_h, comp_peak_h=round(comp_peak_h, 1), obs_peak_h=round(obs_peak_h, 1),
        vol_comp_m3=round(vol_comp, 0), vol_obs_m3=round(vol_obs, 0),
        vol_err_pct=(round(100 * (vol_comp - vol_obs) / vol_obs, 1) if vol_obs > 0 else None),
        rain_total_mm=b["total_mm"], continuity=metrics.get("continuity_rel_error"),
        wall_s=metrics.get("wall_s"),
    )
    print(f"[grade {tag}] NSE={out['nse']} R2={out['r2']} aligned NSE={out['nse_aligned']}/"
          f"R2={out['r2_aligned']} peak {out['peak_comp']}/{out['peak_obs']} "
          f"({out['peak_err_pct']}%) lag={out['timing_lag_h']}h vol_err={out['vol_err_pct']}% "
          f"wall={out['wall_s']}s", flush=True)
    return out


def _r(v):
    return None if v is None else round(float(v), 4)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        key = sys.argv[2]
        cn = None if sys.argv[3] == "none" else float(sys.argv[3])
        amc = int(sys.argv[4])
        mn = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
        print(json.dumps(solve_hyeto(key, cn=cn, amc=amc, manning_scale=mn), indent=2))
    elif cmd == "sweep":
        key = sys.argv[2]
        cns = [None if x == "none" else float(x) for x in sys.argv[3].split(",")]
        amc = int(sys.argv[4])
        mn = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
        rows = []
        for cn in cns:
            r = solve_hyeto(key, cn=cn, amc=amc, manning_scale=mn)
            if r.get("status") == "ok":
                rows.append(r)
        rows.sort(key=lambda t: (t["nse"] is None, -(t["nse"] or -1e9)))
        print("\n=== HYETO SWEEP (" + key + f", amc={amc}, mn={mn}), by NSE ===")
        print(f"{'CN':>5} {'NSE':>8} {'R2':>7} {'alNSE':>7} {'peak%':>7} {'lag_h':>6} {'vol%':>7}")
        for t in rows:
            print(f"{str(t['cn']):>5} {str(t['nse']):>8} {str(t['r2']):>7} "
                  f"{str(t['nse_aligned']):>7} {str(t['peak_err_pct']):>7} "
                  f"{str(t['timing_lag_h']):>6} {str(t['vol_err_pct']):>7}")
        (FORCING_DIR / f"hyeto_sweep_{key}_amc{amc}.json").write_text(json.dumps(rows, indent=2))
    else:
        print(f"unknown cmd {cmd}"); sys.exit(2)
