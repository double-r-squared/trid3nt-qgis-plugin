"""Ball Creek RoG RE-GRADE with the CONTINUOUS SOIL-MOISTURE STORE (ADR 0213).

Lever 1 of the fidelity-ladder-II. ADR 0206 drove each event through the native
SCS-CN on the REAL AORC hyetograph (RAINDEF=3), landing aligned NSE 0.51 on the
Dec 2015 calibration but a static curve number that (a) does not transfer across
antecedent regimes (Dec CN53 vs Feb CN90) and (b) exhausts on the multi-peak
(+116 pct second peak). This driver replaces the static CN with the Michel et
al. (2005) continuous soil-moisture-accounting store: the GROSS hyetograph is
transformed to NET rainfall-excess by a production store (level V, capacity S,
recovery timescale tau) whose initial level V0 is SPUN UP from each event's real
antecedent-precipitation history -- so the antecedent wetness is dynamic STATE,
not a per-event parameter. ONE (S, tau) set is applied to BOTH events; V0 alone
differs (it is the event's antecedent state). The engine sees the net series on
a uniform CN=100 pass-through (it abstracts nothing; the store IS the
infiltration model).

Reuses the cached mesh + forcing + antecedent series
(scripts/sandbox/replication/rog_ballcreek_*, /tmp/rog_ballcreek/forcing/*).
Run in the agent env with .env.local sourced. ASCII only.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# numpy 2.x removed np.trapz -> np.trapezoid; keep the harness working on both.
_TRAPZ = getattr(np, "trapezoid", getattr(np, "trapz", None))

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "server" / "src"))
sys.path.insert(0, str(HERE))

import rog_ballcreek_live as LIVE  # noqa: E402
import rog_ballcreek_hyeto as HY  # noqa: E402

FORCING_DIR = Path("/tmp/rog_ballcreek/forcing")
TIME_STEP_S = 2.0


def spin_up_v0(key: str, *, capacity_mm: float, recovery_h: float) -> dict:
    """Initial store level V0 at the event's rising-limb start, spun up by
    running the SAME production store forward over the real antecedent AORC
    hourly precipitation (from V=0) with the SAME (S, tau). This is the
    antecedent-precipitation initialization: V0 IS the integrated antecedent
    wetness the store carries into the event."""
    ant_key = key[:-3] if key.endswith("_mp") else key  # multipeak shares base antecedent
    a = json.loads((FORCING_DIR / f"antecedent_{ant_key}.json").read_text())
    p = pd.Series(a["precip_mm"], index=pd.to_datetime(a["times"])).astype(float)
    S = float(capacity_mm)
    tau = float(recovery_h)
    V = 0.0
    for mm in p.values:
        mm = float(mm)
        fill = min(1.0, max(0.0, V / S))
        rc = 1.0 - (1.0 - fill) ** 2
        V += mm - rc * mm
        V -= V * (1.0 - math.exp(-1.0 / tau))  # hourly antecedent step
    return {"v0_mm": round(min(V, S), 4), "fill": round(min(V, S) / S, 4),
            "antecedent_total_mm": round(float(p.sum()), 1), "n_hours": len(p)}


def solve_soil(key: str, *, capacity_mm: float, recovery_h: float,
               manning_scale: float = 1.0, rundir: Path | None = None,
               tag: str | None = None, mp: bool = False) -> dict:
    """Stage + run one soil-store solve through the rebuilt image.

    ``rundir`` overrides LIVE.RUNDIR so the fine-mesh ladder rung can point at a
    different staged mesh; defaults to the calibration (coarse) mesh rundir."""
    RUN = rundir or LIVE.RUNDIR
    b = HY.build_blocks(key)
    v0 = spin_up_v0(key, capacity_mm=capacity_mm, recovery_h=recovery_h)
    facts = json.loads((RUN / "mesh_facts.json").read_text())
    tag = tag or (f"soil_{key}_S{capacity_mm:.0f}_tau{recovery_h:.0f}"
                  f"_mn{manning_scale}".replace(".", "p"))
    d = RUN / f"solve_{tag}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "watershed.slf").write_bytes((RUN / "watershed.slf").read_bytes())
    (d / "node_cn2.txt").write_bytes((RUN / "node_cn2.txt").read_bytes())
    base_manning = [float(v) for v in (RUN / "node_manning.txt").read_text().split()]
    scaled = [max(0.005, min(1.0, m * manning_scale)) for m in base_manning]
    (d / "node_manning.txt").write_text("\n".join(f"{v:.4f}" for v in scaled) + "\n")

    dt = TIME_STEP_S
    reach = {
        "name": f"ballcreek_{tag}", "mode": "rain_on_grid",
        "watershed_slf": "watershed.slf", "amc_condition": 2,
        "node_cn2_file": "node_cn2.txt", "node_manning_file": "node_manning.txt",
        "outlet_lonlat": facts["outlet_lonlat"], "initial_abstraction_option": 1,
        "n_outlet_nodes": 6, "duration_s": float(b["sim_hours"]) * 3600.0,
        "time_step_s": dt, "graphic_period": max(1, int(round(900.0 / dt))),
        "rain_hyetograph_blocks": b["blocks"],
        "soil_store": True, "soil_store_capacity_mm": float(capacity_mm),
        "soil_store_recovery_h": float(recovery_h),
        "soil_store_init_mm": float(v0["v0_mm"]),
    }
    (d / "manifest.json").write_text(json.dumps({"run_id": f"rog-bc-{tag}", "reach": reach}))
    argv = [
        "docker", "run", "--rm", "-v", f"{d}:/data",
        "-e", "TRID3NT_TELEMAC_SOLVE_TIMEOUT=86400",
        "--entrypoint", "/usr/local/bin/_entrypoint.sh", LIVE.TELEMAC_IMAGE,
        "python", "/opt/trid3nt/services/workers/telemac/entrypoint.py",
        "--data-dir", "/data", "--run-id", f"rog-bc-{tag}",
    ]
    print(f"[soil {tag}] gross={b['total_mm']}mm V0={v0['v0_mm']}mm "
          f"(fill {v0['fill']}, ant {v0['antecedent_total_mm']}mm) "
          f"S={capacity_mm} tau={recovery_h}h sim={b['sim_hours']}h ...", flush=True)
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=90000)
    mpath = d / "telemac_metrics.json"
    if not mpath.exists():
        print("[soil] NO METRICS. STDERR tail:", cp.stderr[-2000:], flush=True)
        raise SystemExit(2)
    m = json.loads(mpath.read_text())
    if m.get("status") != "ok":
        print("[soil] SOLVE ERROR:", m.get("error"), m.get("listing_tail", "")[-1200:], flush=True)
        return {"status": "error", "tag": tag, "metrics": m}
    g = grade(key, d, b, m, capacity_mm=capacity_mm, recovery_h=recovery_h,
              manning_scale=manning_scale, tag=tag, v0=v0, mp=mp)
    return g


def nash_sutcliffe_efficiency(obs, sim):
    """Standard Nash-Sutcliffe efficiency (identical definition to
    spotpy.objectivefunctions.nashsutcliffe, used by the 0204/0206 grading)."""
    obs = np.asarray(obs, dtype=float)
    sim = np.asarray(sim, dtype=float)
    denom = float(np.sum((obs - obs.mean()) ** 2))
    if denom == 0.0:
        return float("nan")
    return 1.0 - float(np.sum((obs - sim) ** 2)) / denom


def pearson_r2(obs, sim):
    """Squared Pearson correlation (spotpy rsquared definition)."""
    obs = np.asarray(obs, dtype=float)
    sim = np.asarray(sim, dtype=float)
    if obs.std() == 0.0 or sim.std() == 0.0:
        return float("nan")
    return float(np.corrcoef(obs, sim)[0, 1] ** 2)


def grade(key, solve_dir, b, metrics, *, capacity_mm, recovery_h, manning_scale,
          tag, v0, mp=False) -> dict:
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

    comp_peak_h = float(t_h[int(np.argmax(q_comp))]) if q_comp.size else 0.0
    obs_peak_h = float(obs_rel_h[int(np.argmax(obs_q))])
    lag_h = round(comp_peak_h - obs_peak_h, 1)

    shift = obs_peak_h - comp_peak_h
    comp_aligned = np.interp(obs_rel_h, t_h + shift, q_comp, left=0.0, right=q_comp[-1]) + baseflow
    nse_al = nash_sutcliffe_efficiency(obs_q[m], comp_aligned[m])
    r2_al = pearson_r2(obs_q[m], comp_aligned[m])

    peak_comp = float(comp_total.max())
    vol_comp = float(_TRAPZ(np.clip(comp_at_obs, 0, None), obs_rel_h) * 3600.0)
    vol_obs = float(_TRAPZ(np.clip(obs_q - baseflow, 0, None), obs_rel_h) * 3600.0)
    out = dict(
        status="ok", key=key, tag=tag, capacity_mm=capacity_mm,
        recovery_h=recovery_h, manning_scale=manning_scale, v0=v0,
        nse=_r(nse), r2=_r(r2), nse_aligned=_r(nse_al), r2_aligned=_r(r2_al),
        peak_obs=round(float(obs_q.max()), 3), peak_comp=round(peak_comp, 3),
        peak_err_pct=round(100 * (peak_comp - obs_q.max()) / obs_q.max(), 1),
        timing_lag_h=lag_h, comp_peak_h=round(comp_peak_h, 1), obs_peak_h=round(obs_peak_h, 1),
        vol_comp_m3=round(vol_comp, 0), vol_obs_m3=round(vol_obs, 0),
        vol_err_pct=(round(100 * (vol_comp - vol_obs) / vol_obs, 1) if vol_obs > 0 else None),
        gross_total_mm=b["total_mm"],
        soil_excess_mm=metrics.get("soil_store_excess_mm"),
        soil_drain_mm=metrics.get("soil_store_drain_mm"),
        soil_final_mm=metrics.get("soil_store_final_level_mm"),
        soil_mass_resid_mm=metrics.get("soil_store_mass_residual_mm"),
        accumulated_rainfall_m=metrics.get("accumulated_rainfall_m"),
        continuity=metrics.get("continuity_rel_error"), wall_s=metrics.get("wall_s"),
    )
    if mp:
        out.update(_multipeak_metrics(obs_rel_h, obs_q, comp_total, baseflow))
    print(f"[grade {tag}] NSE={out['nse']} aligned={out['nse_aligned']}/R2={out['r2_aligned']} "
          f"peak {out['peak_comp']}/{out['peak_obs']} ({out['peak_err_pct']}%) "
          f"lag={out['timing_lag_h']}h vol_err={out['vol_err_pct']}% "
          f"excess={out['soil_excess_mm']}mm resid={out['soil_mass_resid_mm']}mm "
          f"cont={out['continuity']} wall={out['wall_s']}s", flush=True)
    return out


def _multipeak_metrics(obs_rel_h, obs_q, comp_total, baseflow) -> dict:
    """Second-peak reproduction + over/under-shoot for the multi-peak control."""
    from scipy.signal import find_peaks
    # observed peaks
    o = obs_q.copy()
    pk, _ = find_peaks(o, prominence=0.15 * o.max(), distance=24)
    obs_peaks = sorted(pk, key=lambda i: -o[i])[:2]
    obs_peaks = sorted(obs_peaks)
    c = comp_total
    cpk, _ = find_peaks(c, prominence=0.15 * max(c.max(), 1e-6), distance=24)
    out = {"obs_peak_hours": [round(float(obs_rel_h[i]), 1) for i in obs_peaks],
           "obs_peak_q": [round(float(o[i]), 2) for i in obs_peaks],
           "n_comp_peaks": int(len(cpk))}
    if len(obs_peaks) >= 2:
        h2 = obs_rel_h[obs_peaks[1]]
        w = (obs_rel_h >= h2 - 18) & (obs_rel_h <= h2 + 18)
        comp2 = float(c[w].max()) if w.any() else 0.0
        obs2 = float(o[obs_peaks[1]])
        out["second_peak_comp"] = round(comp2, 2)
        out["second_peak_obs"] = round(obs2, 2)
        out["second_peak_err_pct"] = round(100 * (comp2 - obs2) / obs2, 1)
        out["comp_second_peak_reproduced"] = bool(comp2 > 1.5 * baseflow)
    return out


def _r(v):
    return None if v is None else round(float(v), 4)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        key = sys.argv[2]
        S = float(sys.argv[3]); tau = float(sys.argv[4])
        mn = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
        is_mp = key.endswith("_mp") or (len(sys.argv) > 6 and sys.argv[6] == "mp")
        print(json.dumps(solve_soil(key, capacity_mm=S, recovery_h=tau,
                                    manning_scale=mn, mp=is_mp), indent=2))
    elif cmd == "sweep":
        key = sys.argv[2]
        Ss = [float(x) for x in sys.argv[3].split(",")]
        tau = float(sys.argv[4])
        mn = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
        rows = []
        for S in Ss:
            r = solve_soil(key, capacity_mm=S, recovery_h=tau, manning_scale=mn)
            if r.get("status") == "ok":
                rows.append(r)
        rows.sort(key=lambda t: (t["nse_aligned"] is None, -(t["nse_aligned"] or -1e9)))
        print(f"\n=== SOIL SWEEP ({key}, tau={tau}, mn={mn}), by aligned NSE ===")
        print(f"{'S':>6} {'V0':>7} {'NSE':>8} {'alNSE':>7} {'peak%':>7} {'lag_h':>6} {'vol%':>7}")
        for t in rows:
            print(f"{t['capacity_mm']:>6} {t['v0']['v0_mm']:>7} {str(t['nse']):>8} "
                  f"{str(t['nse_aligned']):>7} {str(t['peak_err_pct']):>7} "
                  f"{str(t['timing_lag_h']):>6} {str(t['vol_err_pct']):>7}")
        (FORCING_DIR / f"soil_sweep_{key}_tau{tau:.0f}.json").write_text(json.dumps(rows, indent=2))
    else:
        print(f"unknown cmd {cmd}"); sys.exit(2)
