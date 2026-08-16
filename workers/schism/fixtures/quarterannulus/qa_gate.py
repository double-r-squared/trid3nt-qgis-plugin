"""QuarterAnnulus verification gate -- SCHISM's own barotropic-solver test case
(Lynch & Gray annular tidal channel; schism_verification_tests/Test_QuarterAnnulus).

Runs the M2 tidal case with the hydro-core executable under MPI (2 compute + 2
scribe), extracts the station elevation time series (staout_1), and asserts it
reproduces the bundled ANALYTICAL solution (ForPlot_ana_elev.dat) over the
spun-up window (past the 1-day tidal ramp). Mirrors the HEC-RAS Muncie in-image
gate: exits nonzero on any divergence. This is the image's build-time proof that
the baked SCHISM binary solves correctly, not just links.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

# amplitude/RMSE tolerances vs the published analytical M2 solution (measured
# green at ADR 0115: amp err 0.0027 m / RMSE 0.0155 m on a 0.44 m signal).
AMP_ERR_TOL_M = 0.010
RMSE_TOL_M = 0.030


def main() -> int:
    here = Path(__file__).resolve().parent
    exe = os.environ.get("SCHISM_HYDRO_BIN")
    if not exe or not Path(exe).exists():
        # discover the hydro-core executable on PATH / in the bin dir
        bindir = Path(os.environ.get("SCHISM_BIN_DIR", "/opt/schism/bin"))
        cands = sorted(bindir.glob("pschism_TVD-*"))
        if not cands:
            print("FAIL: hydro-core executable not found", file=sys.stderr)
            return 2
        exe = str(cands[0])

    run = Path(os.environ.get("QA_RUNDIR", "/tmp/qa_run"))
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    for f in ("hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "drag.gr3",
              "station.in", "ForPlot_ana_elev.dat"):
        shutil.copy(here / f, run / f)
    (run / "outputs").mkdir()

    cmd = ["mpirun", "--allow-run-as-root", "-np", "4", exe, "2"]
    proc = subprocess.run(cmd, cwd=run, capture_output=True, text=True, timeout=900)
    mirror = (run / "outputs" / "mirror.out").read_text() if (run / "outputs" / "mirror.out").exists() else ""
    if "Run completed successfully" not in mirror:
        print("FAIL: SCHISM did not complete\n" + proc.stdout[-2000:] + proc.stderr[-2000:],
              file=sys.stderr)
        return 3

    me = np.loadtxt(run / "outputs" / "staout_1")
    t_d = me[:, 0] / 86400.0
    z_me = me[:, 1]
    ana = np.loadtxt(here / "ForPlot_ana_elev.dat", comments="%")
    n = min(len(z_me), len(ana))
    t_d, z_me, z_ana = t_d[:n], z_me[:n], ana[:n, 1]
    mask = t_d >= 3.0  # spun-up (ramp = 1 day)
    rmse = float(np.sqrt(np.mean((z_me[mask] - z_ana[mask]) ** 2)))
    amp_me = 0.5 * (z_me[mask].max() - z_me[mask].min())
    amp_an = 0.5 * (z_ana[mask].max() - z_ana[mask].min())
    amp_err = abs(amp_me - amp_an)
    corr = float(np.corrcoef(z_me[mask], z_ana[mask])[0, 1])
    print(f"QuarterAnnulus M2 gate: RMSE={rmse:.4f} m  amp_err={amp_err:.4f} m  "
          f"amp={amp_me:.4f}/{amp_an:.4f} m  corr={corr:.5f}")
    if rmse > RMSE_TOL_M or amp_err > AMP_ERR_TOL_M:
        print(f"FAIL: outside tolerance (RMSE<={RMSE_TOL_M}, amp_err<={AMP_ERR_TOL_M})",
              file=sys.stderr)
        return 4
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
