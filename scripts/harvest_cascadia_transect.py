"""Sample the offshore decay/arrival transect from the FULL-domain fort.q frames of
the completed Cascadia M9 solve (the fgout monitor was placed over the coastal AOI
only, so the deep-offshore transect must come from fort.q, whose coarse base patch
spans the whole propagation domain). Streams each fort.q frame one at a time and
deletes it after parsing (peak disk ~700 MB), samples the 4 transect points + the
coastal gauge point, tracks peak |perturbation| + first-arrival per point, prints
the decay/arrival correlations, and re-renders the transect chart with real data."""
from __future__ import annotations

import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import numpy as np
from proof_geoclaw_chignik_runup import _get, _list, _sample_patch, _read_fgout_time
from proof_geoclaw_scenario_cascadia import (
    RUPTURE_CENTROID, TRANSECT, _haversine_km, _render_transect,
)
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import parse_fort_q_frame

RUNS = "trid3nt-runs"
DOCKER = "01KZW4N9RDHKECRF8C1JHP9T3C"


def main() -> int:
    keys = _list(RUNS, f"{DOCKER}/_output/")
    fq = sorted(k for k in keys if re.search(r"fort\.q\d+$", k))
    ft = {int(m.group(1)): k for k in keys if (m := re.search(r"fort\.t(\d+)$", k))}
    print(f"fort.q frames: {len(fq)}")

    # base (t0) sample per point + running peak/arrival
    base: dict = {}
    series: dict = {pt: [] for pt in TRANSECT}
    for qk in fq:
        m = re.search(r"fort\.q(\d+)$", qk)
        fno = int(m.group(1))
        raw = _get(RUNS, qk).decode(errors="replace")
        patches = parse_fort_q_frame(raw)
        del raw
        tkey = ft.get(fno)
        t = _read_fgout_time(_get(RUNS, tkey)) if tkey else float(fno)
        for pt in TRANSECT:
            h = _sample_patch(patches, *pt)
            if fno == 0:
                base[pt] = h
            b = base.get(pt, float("nan"))
            pert = (h - b) if (math.isfinite(h) and math.isfinite(b)) else float("nan")
            series[pt].append((t, pert))
        del patches

    t0 = series[TRANSECT[0]][0][0]
    rows = []
    for pt in TRANSECT:
        dkm = _haversine_km(RUPTURE_CENTROID, pt)
        s = [(tt - t0, pp) for tt, pp in series[pt]]
        finite = [(tt, pp) for tt, pp in s if math.isfinite(pp)]
        peak = max((abs(pp) for _, pp in finite), default=float("nan"))
        arr = next((tt for tt, pp in finite if abs(pp) >= 0.05), float("nan"))
        rows.append((pt, dkm, arr, peak, s))
        print(f"  transect {pt} dist={dkm:.1f} km arrival={arr:.0f} s peak_pert={peak:.3f} m")

    dd = np.array([r[1] for r in rows])
    peaks = np.array([r[3] for r in rows])
    arrs = np.array([r[2] for r in rows])
    fin = np.isfinite(peaks)
    fina = np.isfinite(arrs)
    if fin.sum() >= 2:
        print("peak_decays_with_distance:", bool(np.corrcoef(dd[fin], peaks[fin])[0, 1] < 0),
              "corr=", float(np.corrcoef(dd[fin], peaks[fin])[0, 1]))
    if fina.sum() >= 2:
        print("arrival_increases_with_distance:", bool(np.corrcoef(dd[fina], arrs[fina])[0, 1] > 0),
              "corr=", float(np.corrcoef(dd[fina], arrs[fina])[0, 1]))

    _render_transect(rows, "geoclaw_scenario_cascadia_transect_chart.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
