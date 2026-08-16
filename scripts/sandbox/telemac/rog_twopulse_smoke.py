"""Two-pulse DISCRIMINATING test for the ADR 0206 time-varying hyetograph path.

A constant-rain design storm can only produce a SINGLE outlet-hydrograph hump;
a true time-varying hyetograph with two separated rain pulses must produce TWO
distinct outlet responses. This driver runs BOTH on the same sharp fixture (a
small steep near-impervious plane so overland travel time << the inter-pulse
gap) THROUGH the worker pipeline and asserts:

  * time-varying run: outlet hydrograph is BIMODAL (2 local maxima, clear trough);
  * constant run (same total volume): UNIMODAL (1 hump);
  * mass check: the engine's accumulated rainfall == the hyetograph integral.

Run with the worker dir mounted over the baked copy (dev) OR through a rebuilt
image (worker-image law). ASCII only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKER = "/opt/trid3nt/workers/telemac"
sys.path.insert(0, WORKER)

import numpy as np  # noqa: E402
from scipy.spatial import Delaunay  # noqa: E402

import rog_build as R  # noqa: E402
import entrypoint as E  # noqa: E402


CN = 98.0  # near-saturated / near-impervious: small SCS-CN abstraction so BOTH
           # pulses run off comparably (cumulative abstraction otherwise starves
           # the first pulse) -- isolates the RAIN STRUCTURE as the discriminator.


def build_sharp_watershed(rundir: Path) -> int:
    """Small steep plane: 150 m x 80 m, 10% slope toward +x.

    Low Manning (0.03) + CN 98 so most rain runs off and the outlet responds
    within minutes -- fast enough to resolve two rain pulses ~45 min apart as
    two distinct hydrograph peaks."""
    xs = np.arange(0.0, 150.0 + 1e-6, 7.5)
    ys = np.arange(0.0, 80.0 + 1e-6, 8.0)
    gx, gy = np.meshgrid(xs, ys)
    X = gx.ravel().astype(float)
    Y = gy.ravel().astype(float)
    bed = (150.0 - X) * 0.10 + 0.12 * np.abs(Y - 40.0) / 40.0
    tri = Delaunay(np.column_stack([X, Y]))
    ikle = tri.simplices.astype(np.int64)
    b = R.build_boundary(X, Y, ikle)
    R.write_rog_slf(str(rundir / "watershed.slf"), X, Y, b["ikle"], bed,
                    b["ipob"], b["ring"], b["nptfr"])
    cn2 = np.full(X.shape[0], CN)
    manning = np.full(X.shape[0], 0.03)
    (rundir / "node_cn2.txt").write_text("\n".join(f"{v:.3f}" for v in cn2) + "\n")
    (rundir / "node_manning.txt").write_text(
        "\n".join(f"{v:.3f}" for v in manning) + "\n")
    return int(X.shape[0])


DUR_S = 7200.0
# two 15-min 100 mm/hr pulses (25 mm each) separated by a 45-min dry gap.
BLOCKS = [[900.0, 25.0], [3600.0, 0.0], [4500.0, 25.0]]
TOTAL_MM = 50.0


def _base_reach(name: str) -> dict:
    return {
        "name": name, "mode": "rain_on_grid", "watershed_slf": "watershed.slf",
        "amc_condition": 2, "node_cn2_file": "node_cn2.txt",
        "node_manning_file": "node_manning.txt", "n_outlet_nodes": 6,
        "duration_s": DUR_S, "time_step_s": 1.0, "graphic_period": 20,
        "curve_number": CN,
    }


def _run(rundir: Path, reach: dict, tag: str) -> dict:
    d = rundir / tag
    d.mkdir(parents=True, exist_ok=True)
    for f in ("watershed.slf", "node_cn2.txt", "node_manning.txt"):
        (d / f).write_bytes((rundir / f).read_bytes())
    (d / "manifest.json").write_text(json.dumps({"run_id": tag, "reach": reach}))
    E.main(["--data-dir", str(d), "--run-id", tag])
    m = json.loads((d / "telemac_metrics.json").read_text())
    hg = json.loads((d / "rog_outlet_hydrograph.json").read_text())
    m["_q"] = hg["q_m3s"]
    m["_t"] = hg["t_s"]
    return m


def _count_peaks(q: list[float], rel_prom: float = 0.20) -> int:
    """Count significant outlet-hydrograph peaks (prominence >= rel_prom*peak).

    Prominence filters the small numerical/drainage wiggles in the recession
    tail so only genuine rain-driven responses count (scipy.signal.find_peaks)."""
    from scipy.signal import find_peaks
    q = np.asarray(q, dtype=float)
    if q.size < 3 or q.max() <= 0:
        return 0
    peaks, _ = find_peaks(q, prominence=rel_prom * float(q.max()), distance=5)
    return int(peaks.size)


def main() -> int:
    rundir = Path(sys.argv[1] if len(sys.argv) > 1 else "/data")
    rundir.mkdir(parents=True, exist_ok=True)
    npoin = build_sharp_watershed(rundir)
    print(f"[2pulse] sharp watershed npoin={npoin}", flush=True)

    tv = dict(_base_reach("twopulse_tv"))
    tv["rain_hyetograph_blocks"] = BLOCKS
    m_tv = _run(rundir, tv, "tv")

    # constant control: same TOTAL volume over the same 0..4500 s storm span.
    cst = dict(_base_reach("twopulse_const"))
    cst["rain_intensity_mm_per_hr"] = TOTAL_MM / (4500.0 / 3600.0)
    cst["rain_duration_s"] = 4500.0
    m_cst = _run(rundir, cst, "const")

    n_tv = _count_peaks(m_tv["_q"])
    n_cst = _count_peaks(m_cst["_q"])
    acc = m_tv.get("accumulated_rainfall_m")
    hint = m_tv.get("hyetograph_total_mm")
    print(f"[2pulse] TIME-VARYING: runoff_path={m_tv.get('runoff_path')} "
          f"peaks={n_tv} peakQ={m_tv.get('peak_discharge_m3s')} "
          f"hyeto_total_mm={hint} continuity={m_tv.get('continuity_rel_error')}", flush=True)
    print(f"[2pulse]   q(t)={[round(v,4) for v in m_tv['_q']]}", flush=True)
    print(f"[2pulse] CONSTANT: peaks={n_cst} peakQ={m_cst.get('peak_discharge_m3s')}", flush=True)
    print(f"[2pulse]   q(t)={[round(v,4) for v in m_cst['_q']]}", flush=True)

    ok = True
    if not (m_tv.get("correct_end") and m_cst.get("correct_end")):
        print("[2pulse] FAIL: a run did not reach CORRECT END"); ok = False
    if n_tv < 2:
        print(f"[2pulse] FAIL: time-varying run is not bimodal (peaks={n_tv})"); ok = False
    if n_cst != 1:
        print(f"[2pulse] WARN: constant control peaks={n_cst} (expected 1)")
    # mass check: gross accumulated rainfall == hyetograph integral (mm).
    if acc is not None:
        acc_mm = acc * 1000.0
        err = abs(acc_mm - TOTAL_MM)
        print(f"[2pulse] MASS: engine accumulated {acc_mm:.3f} mm vs "
              f"hyetograph {TOTAL_MM:.3f} mm (err {err:.4f} mm)", flush=True)
        if err > 0.5:
            print("[2pulse] FAIL: mass mismatch"); ok = False
    print("[2pulse]", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
