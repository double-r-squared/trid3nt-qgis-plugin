"""Ball Creek calibration trials with raw + peak-aligned NSE (ADR 0204).

Runs a (CN, Manning, Ia-option) grid on the calibration event via
rog_ballcreek_live.phase_solve (24 h max-burst forcing from the event JSON) and
reports, per trial:

  * raw NSE/R2 -- computed+baseflow vs observed on the absolute hourly time base;
  * peak-aligned NSE/R2 -- the computed series shifted so its peak coincides with
    the observed peak, isolating hydrograph SHAPE skill from the known forcing
    timing offset (the AORC 1 h-accumulation precip peak lags the gauge peak ~4 h,
    and constant rain places the modelled peak at rain-end).

Peak alignment is a standard hydrograph-V&V diagnostic; both numbers are reported
so the honest picture is explicit. Run in the agent venv. ASCII only.
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

FORCING_DIR = Path("/tmp/rog_ballcreek/forcing")


def _metrics(key: str, tag: str):
    from trid3nt_server.agent.tools.processing.compute_skill_metrics.compute_skill_metrics import (
        nash_sutcliffe_efficiency as NSE, pearson_r2 as R2)
    f = json.loads((FORCING_DIR / f"{key}.json").read_text())
    h = json.loads((LIVE.RUNDIR / f"solve_{tag}" / "rog_outlet_hydrograph.json").read_text())
    th = np.asarray(h["t_s"], float) / 3600.0
    qc = np.asarray(h["q_m3s"], float)
    bf = float(f["baseflow_m3s"])
    obs_t = pd.to_datetime(f["obs_times"])
    obs_q = np.asarray(f["obs_q_m3s"], float)
    t0 = pd.to_datetime(f["t_rise"])
    rel = np.array([(t - t0).total_seconds() / 3600.0 for t in obs_t])
    comp = np.interp(rel, th, qc, left=0.0, right=qc[-1]) + bf
    nse, r2 = NSE(obs_q, comp), R2(obs_q, comp)
    # peak-aligned: shift comp so its peak hour coincides with the observed peak.
    shift = int(rel[np.argmax(obs_q)] - rel[np.argmax(comp)])
    comp_sh = np.interp(rel, rel + shift, comp, left=comp[0], right=comp[-1])
    nse_a, r2_a = NSE(obs_q, comp_sh), R2(obs_q, comp_sh)
    return dict(nse=nse, r2=r2, nse_aligned=nse_a, r2_aligned=r2_a,
                shift_h=shift, peak_comp=float(comp.max()), peak_obs=float(obs_q.max()))


def run(key, cn, manning, amc, ia):
    f = json.loads((FORCING_DIR / f"{key}.json").read_text())
    tag = f"{key}_cn{cn}_mn{manning}_amc{amc}_ia{ia}".replace(".", "p")
    m = LIVE.phase_solve(tag=tag, intensity_mm_per_hr=f["intensity_mm_per_hr"],
                         rain_duration_hr=f["rise_hours"], sim_duration_hr=f["sim_hours"],
                         amc=amc, uniform_cn=cn, manning_scale=manning, ia_option=ia)
    if m.get("status") != "ok":
        return dict(tag=tag, status="error")
    r = _metrics(key, tag)
    r.update(dict(tag=tag, cn=cn, manning=manning, amc=amc, ia=ia))
    print(f"[{tag}] NSE_raw={r['nse'] and round(r['nse'],3)} R2={r['r2'] and round(r['r2'],3)} "
          f"| peak-aligned(shift{r['shift_h']:+}h) NSE={r['nse_aligned'] and round(r['nse_aligned'],3)} "
          f"R2={r['r2_aligned'] and round(r['r2_aligned'],3)} | peak {round(r['peak_comp'],2)}/{round(r['peak_obs'],2)}",
          flush=True)
    return r


if __name__ == "__main__":
    key = sys.argv[1]
    cns = [float(x) for x in sys.argv[2].split(",")]
    mns = [float(x) for x in sys.argv[3].split(",")]
    amc = int(sys.argv[4]); ia = int(sys.argv[5])
    out = []
    for cn in cns:
        for mn in mns:
            out.append(run(key, cn, mn, amc, ia))
    ok = [r for r in out if r.get("nse_aligned") is not None]
    ok.sort(key=lambda r: -r["nse_aligned"])
    print("\n=== sorted by peak-aligned NSE ===")
    for r in ok:
        print(f"  {r['tag']}: raw NSE {r['nse'] and round(r['nse'],3)}  aligned NSE "
              f"{round(r['nse_aligned'],3)} R2 {round(r['r2_aligned'],3)} peak {round(r['peak_comp'],2)}")
    (FORCING_DIR / f"calib2_{key}_amc{amc}_ia{ia}.json").write_text(
        json.dumps([{k: v for k, v in r.items()} for r in out], indent=2, default=str))
