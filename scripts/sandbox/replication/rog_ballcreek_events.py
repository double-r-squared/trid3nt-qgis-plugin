"""Ball Creek RoG event forcing + calibration/validation harness.

Builds the AORC-forced flash-flood forcing for each event, runs the TELEMAC RoG
solve (via rog_ballcreek_live.phase_solve), aligns the computed outlet hydrograph
to the observed EDI weir #9 series, and grades NSE + R2 (compute_skill_metrics).

Forcing (installed-engine constant-rain constraint): each single
storm is represented by the rain over its rising limb -- a constant intensity =
AORC(rising-limb window) / rising-limb hours, driven for rain_duration = rising
hours (native RAIN_HDUR keyword) so the modelled peak lands at the observed peak
time, then rain stops and the catchment drains (recession). Baseflow handling:
the RoG model is an event model with no subsurface return flow, so the observed
pre-event baseflow is added to the computed runoff as a constant (total-vs-total
comparison); the quickflow-vs-quickflow NSE is also reported as a sensitivity.

Run in the agent venv with .env.local sourced. ASCII only.
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

BC_BBOX = [-83.4733, 35.0281, -83.4219, 35.0601]
DISCHARGE_PARQUET = Path("/tmp/ballcreek_explore/bc9_discharge.parquet")
FORCING_DIR = Path("/tmp/rog_ballcreek/forcing")

# Analysis windows (UTC). recession_h = sim hours AFTER rain stops.
EVENTS = {
    "dec2015": dict(label="Dec 2015 (calibration, single-storm)",
                    win=("2015-12-22", "2015-12-27"), recession_h=48,
                    amc=3),  # wet December, high antecedent -> AMC III
    "feb2018": dict(label="Feb 2018 (validation, single-storm)",
                    win=("2018-02-08", "2018-02-16"), recession_h=48,
                    amc=3),  # wet antecedent (52mm 5-day, dormant) -> AMC III
    "dec2015_mp": dict(label="Dec 2015 full (multi-peak negative control)",
                       win=("2015-12-22", "2016-01-01"), recession_h=168,
                       amc=3),
}


def _aorc(a: str, b: str) -> pd.Series:
    from trid3nt_server.tools import TOOL_REGISTRY
    d = TOOL_REGISTRY["fetch_aorc_precip"].fn(bbox=BC_BBOX, start_date=a, end_date=b)
    d = d if isinstance(d, dict) else d.__dict__
    return pd.Series(d["precip_mm"], index=pd.to_datetime(d["times"])).astype(float)


def build_forcing(key: str) -> dict:
    """Compute + cache the flash-flood forcing and observed series for one event."""
    ev = EVENTS[key]
    a, b = ev["win"]
    disc = pd.read_parquet(DISCHARGE_PARQUET)["discharge_m3s"]
    p = _aorc(a, b)
    q = disc[a:b].reindex(p.index).interpolate().clip(lower=0)
    tpeak = q.idxmax()
    qpk = float(q.max())
    baseflow = float(q[:tpeak].iloc[:6].mean())
    # Rain window = the STORM_HOURS ending at the observed peak. The rain fell over
    # this multi-hour core (even though the flashy basin's discharge RESPONSE is
    # ~6 h); a constant intensity over it drives the modelled peak to the observed
    # peak time (rain stops there via RAIN_HDUR -> recession). A 6 h pulse cannot
    # establish drainage through the coarse 30-200 m mesh (it ponds), so the storm
    # core -- how long it actually rained -- is the physically-correct rain window.
    storm_h = int(EVENTS[key].get("storm_hours", 24))
    # the STORM_HOURS window of MAXIMUM rainfall = the flash-flood burst core.
    roll = p.rolling(storm_h).sum()
    t_end = roll.idxmax()
    t_rise = t_end - pd.Timedelta(hours=storm_h - 1)
    rise_h = storm_h
    rain_win = p[t_rise:t_end]
    intensity = float(rain_win.sum()) / rise_h

    # 5-day antecedent precip before the rising limb (AMC evidence).
    ant = _aorc(str((t_rise - pd.Timedelta(days=6)).date()), str(t_rise.date()))
    ant5 = float(ant[str((t_rise - pd.Timedelta(days=5))):str(t_rise)].sum())

    sim_h = rise_h + int(ev["recession_h"])
    # observed series over the sim window, hourly, from the rising-limb start.
    obs = disc[str(t_rise):].iloc[: sim_h + 1]
    forcing = dict(
        key=key, label=ev["label"], window=[a, b],
        t_rise=str(t_rise), t_peak=str(tpeak),
        obs_peak_m3s=round(qpk, 3), baseflow_m3s=round(baseflow, 4),
        rise_hours=rise_h, intensity_mm_per_hr=round(intensity, 3),
        rain_total_mm=round(float(rain_win.sum()), 1),
        window_total_precip_mm=round(float(p.sum()), 1),
        antecedent_5day_mm=round(ant5, 1), amc=int(ev["amc"]),
        sim_hours=sim_h,
        obs_times=[str(t) for t in obs.index],
        obs_q_m3s=[round(float(v), 4) for v in obs.values],
        precip_times=[str(t) for t in p.index],
        precip_mm=[round(float(v), 3) for v in p.values],
    )
    FORCING_DIR.mkdir(parents=True, exist_ok=True)
    (FORCING_DIR / f"{key}.json").write_text(json.dumps(forcing, indent=2))
    print(f"[forcing {key}] t_rise={t_rise} peak={qpk:.2f}@{tpeak} rise={rise_h}h "
          f"intensity={intensity:.2f}mm/hr baseflow={baseflow:.3f} "
          f"ant5day={ant5:.1f}mm sim={sim_h}h", flush=True)
    return forcing


def run_event(key: str, *, cn: float | None, manning_scale: float,
              amc: int | None = None, tag: str | None = None) -> dict:
    """Solve one event with given params; align to observed; return metrics+NSE/R2."""
    from trid3nt_server.tools.processing.compute_skill_metrics.compute_skill_metrics import (
        nash_sutcliffe_efficiency, pearson_r2)

    f = json.loads((FORCING_DIR / f"{key}.json").read_text())
    amc = int(amc if amc is not None else f["amc"])
    tag = tag or f"{key}_cn{cn}_mn{manning_scale}_amc{amc}".replace(".", "p")
    metrics = LIVE.phase_solve(
        tag=tag, intensity_mm_per_hr=f["intensity_mm_per_hr"],
        rain_duration_hr=f["rise_hours"], sim_duration_hr=f["sim_hours"],
        amc=amc, uniform_cn=cn, manning_scale=manning_scale,
        graphic_period_s=900.0)
    if metrics.get("status") != "ok":
        return dict(status="error", tag=tag, metrics=metrics)

    solve_dir = LIVE.RUNDIR / f"solve_{tag}"
    hyd = json.loads((solve_dir / "rog_outlet_hydrograph.json").read_text())
    t_h = np.asarray(hyd["t_s"], dtype=float) / 3600.0
    q_comp = np.asarray(hyd["q_m3s"], dtype=float)
    baseflow = float(f["baseflow_m3s"])

    # align: observed hourly from t_rise; computed sampled at each observed hour.
    obs_t = pd.to_datetime(f["obs_times"])
    obs_q = np.asarray(f["obs_q_m3s"], dtype=float)
    t_rise = pd.to_datetime(f["t_rise"])
    obs_rel_h = np.array([(t - t_rise).total_seconds() / 3600.0 for t in obs_t])
    comp_at_obs = np.interp(obs_rel_h, t_h, q_comp, left=0.0, right=q_comp[-1])
    comp_total = comp_at_obs + baseflow  # add observed pre-event baseflow (constant)

    m = obs_rel_h >= 0
    nse = nash_sutcliffe_efficiency(obs_q[m], comp_total[m])
    r2 = pearson_r2(obs_q[m], comp_total[m])
    # quickflow-vs-quickflow sensitivity (subtract baseflow from observed).
    obs_qf = np.clip(obs_q[m] - baseflow, 0, None)
    nse_qf = nash_sutcliffe_efficiency(obs_qf, comp_at_obs[m])
    r2_qf = pearson_r2(obs_qf, comp_at_obs[m])

    peak_comp = float((comp_total).max())
    vol_comp = float(np.trapz(np.clip(comp_at_obs, 0, None), obs_rel_h) * 3600.0)
    vol_obs = float(np.trapz(np.clip(obs_q - baseflow, 0, None), obs_rel_h) * 3600.0)
    out = dict(
        status="ok", key=key, tag=tag, cn=cn, manning_scale=manning_scale, amc=amc,
        nse=None if nse is None else round(nse, 4),
        r2=None if r2 is None else round(r2, 4),
        nse_quickflow=None if nse_qf is None else round(nse_qf, 4),
        r2_quickflow=None if r2_qf is None else round(r2_qf, 4),
        peak_obs_m3s=round(float(obs_q.max()), 3),
        peak_comp_m3s=round(peak_comp, 3),
        peak_err_pct=round(100 * (peak_comp - obs_q.max()) / obs_q.max(), 1),
        runoff_vol_obs_m3=round(vol_obs, 0),
        runoff_vol_comp_m3=round(vol_comp, 0),
        vol_err_pct=(round(100 * (vol_comp - vol_obs) / vol_obs, 1) if vol_obs > 0 else None),
        wall_s=metrics.get("wall_s"), continuity=metrics.get("continuity_rel_error"),
    )
    print(f"[event {key}] cn={cn} mn={manning_scale} amc={amc} -> NSE={out['nse']} "
          f"R2={out['r2']} peak {out['peak_comp_m3s']}/{out['peak_obs_m3s']} "
          f"({out['peak_err_pct']}%) vol_err={out['vol_err_pct']}%", flush=True)
    return out


def calibrate(key: str, cns: list[float], mannings: list[float], amc: int) -> list[dict]:
    """Grid-search (CN, Manning) on one event; return trials sorted by NSE desc."""
    trials = []
    for cn in cns:
        for ms in mannings:
            r = run_event(key, cn=cn, manning_scale=ms, amc=amc)
            if r.get("status") == "ok":
                trials.append(dict(cn=cn, manning=ms, amc=amc, nse=r["nse"], r2=r["r2"],
                                   peak_err=r["peak_err_pct"], vol_err=r["vol_err_pct"]))
    trials.sort(key=lambda t: (t["nse"] is None, -(t["nse"] or -1e9)))
    print("\n=== CALIBRATION TABLE (" + key + "), sorted by NSE ===")
    print(f"{'CN':>5} {'Man':>5} {'AMC':>4} {'NSE':>8} {'R2':>7} {'peak%':>7} {'vol%':>7}")
    for t in trials:
        print(f"{t['cn']:>5} {t['manning']:>5} {t['amc']:>4} {str(t['nse']):>8} "
              f"{str(t['r2']):>7} {str(t['peak_err']):>7} {str(t['vol_err']):>7}")
    (FORCING_DIR / f"calib_{key}.json").write_text(json.dumps(trials, indent=2))
    return trials


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "calibrate":
        key = sys.argv[2]
        cns = [float(x) for x in sys.argv[3].split(",")]
        mns = [float(x) for x in sys.argv[4].split(",")]
        amc = int(sys.argv[5]) if len(sys.argv) > 5 else 2
        calibrate(key, cns, mns, amc)
    elif cmd == "forcing":
        for k in (sys.argv[2:] or list(EVENTS)):
            build_forcing(k)
    elif cmd == "run":
        key = sys.argv[2]
        cn = None if sys.argv[3] == "none" else float(sys.argv[3])
        ms = float(sys.argv[4])
        amc = int(sys.argv[5]) if len(sys.argv) > 5 else None
        print(json.dumps(run_event(key, cn=cn, manning_scale=ms, amc=amc), indent=2))
