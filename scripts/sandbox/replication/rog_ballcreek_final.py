"""Ball Creek RoG final runs (ADR 0204): validation + multi-peak, locked params.

Calibrated params (frozen on the Dec 2015 event): uniform CN2 = 55, AMC II,
Manning scale 1.0, initial-abstraction ratio 0.2, 24 h max-burst constant-rain
forcing. Runs the split-sample validation (Feb 2018) and the multi-peak negative
control (Dec 2015 full) with these UNCHANGED, and writes ballcreek_results.json:
per event raw + peak-aligned NSE/R2, peak/volume error, and (multi-peak) the
inter-peak-flow underestimate. Run in the agent venv. ASCII only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rog_ballcreek_live as LIVE  # noqa: E402

FORCING = Path("/tmp/rog_ballcreek/forcing")
CAL = dict(cn=55.0, manning=1.0, amc=2, ia=1)  # ia_option 1 == Ia ratio 0.2


def analyze(key: str, tag: str) -> dict:
    from trid3nt_server.agent.tools.processing.compute_skill_metrics.compute_skill_metrics import (
        nash_sutcliffe_efficiency as NSE, pearson_r2 as R2)
    f = json.loads((FORCING / f"{key}.json").read_text())
    h = json.loads((LIVE.RUNDIR / f"solve_{tag}" / "rog_outlet_hydrograph.json").read_text())
    th = np.asarray(h["t_s"], float) / 3600.0
    qc = np.asarray(h["q_m3s"], float)
    bf = float(f["baseflow_m3s"])
    obs_t = pd.to_datetime(f["obs_times"])
    obs = np.asarray(f["obs_q_m3s"], float)
    t0 = pd.to_datetime(f["t_rise"])
    rel = np.array([(t - t0).total_seconds() / 3600.0 for t in obs_t])
    comp = np.interp(rel, th, qc, left=0.0, right=qc[-1]) + bf
    nse, r2 = NSE(obs, comp), R2(obs, comp)
    shift = int(rel[np.argmax(obs)] - rel[np.argmax(comp)])
    comp_sh = np.interp(rel, rel + shift, comp, left=comp[0], right=comp[-1])
    nse_a, r2_a = NSE(obs, comp_sh), R2(obs, comp_sh)
    vol_o = float(np.trapezoid(np.clip(obs - bf, 0, None), rel) * 3600.0)
    vol_c = float(np.trapezoid(np.clip(comp - bf, 0, None), rel) * 3600.0)
    out = dict(key=key, tag=tag,
               nse=_r(nse), r2=_r(r2), nse_aligned=_r(nse_a), r2_aligned=_r(r2_a),
               peak_timing_lag_h=-shift,
               peak_obs=round(float(obs.max()), 3), peak_comp=round(float(comp.max()), 3),
               peak_err_pct=round(100 * (comp.max() - obs.max()) / obs.max(), 1),
               runoff_vol_obs_m3=round(vol_o), runoff_vol_comp_m3=round(vol_c),
               vol_err_pct=round(100 * (vol_c - vol_o) / vol_o, 1) if vol_o > 0 else None,
               baseflow_m3s=bf)
    # multi-peak: inter-peak-flow underestimate between the two observed peaks.
    if key == "dec2015_mp":
        from scipy.signal import find_peaks
        pk, _ = find_peaks(obs, height=3.0, distance=24, prominence=1.5)
        if len(pk) >= 2:
            a, b = pk[0], pk[-1]
            out["obs_peak1_h"] = round(float(rel[a]))
            out["obs_peak2_h"] = round(float(rel[b]))
            out["obs_peak2_m3s"] = round(float(obs[b]), 3)
            out["interpeak_obs_mean_m3s"] = round(float(obs[a:b].mean()), 3)
            out["interpeak_comp_mean_m3s"] = round(float(comp[a:b].mean()), 3)
            out["interpeak_underestimate_pct"] = round(
                100 * (comp[a:b].mean() - obs[a:b].mean()) / obs[a:b].mean(), 1)
            out["comp_second_peak_reproduced"] = bool(
                (comp[b - 12:b + 12].max() - comp[a + 24:b - 24].min()) > 1.0)
    return out


def _r(x):
    return None if x is None else round(x, 4)


def run(key: str) -> dict:
    f = json.loads((FORCING / f"{key}.json").read_text())
    tag = f"{key}_FINAL_cn{int(CAL['cn'])}_amc{CAL['amc']}"
    m = LIVE.phase_solve(tag=tag, intensity_mm_per_hr=f["intensity_mm_per_hr"],
                         rain_duration_hr=f["rise_hours"], sim_duration_hr=f["sim_hours"],
                         amc=CAL["amc"], uniform_cn=CAL["cn"],
                         manning_scale=CAL["manning"], ia_option=CAL["ia"])
    if m.get("status") != "ok":
        return dict(key=key, status="error", metrics=m)
    r = analyze(key, tag)
    r["wall_s"] = m.get("wall_s")
    r["continuity"] = m.get("continuity_rel_error")
    print(json.dumps(r, indent=2), flush=True)
    return r


if __name__ == "__main__":
    keys = sys.argv[1:] or ["feb2018", "dec2015_mp"]
    results = {k: run(k) for k in keys}
    out = FORCING / "ballcreek_results.json"
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing.update(results)
    out.write_text(json.dumps(existing, indent=2))
    print("saved", out)
