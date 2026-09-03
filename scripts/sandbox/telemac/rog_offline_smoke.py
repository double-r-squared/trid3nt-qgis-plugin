"""Offline RoG smoke: synthetic watershed TIN -> full worker pipeline THROUGH
the telemac image, asserting CORRECT END OF RUN + sane outlet hydrograph.

De-risks the C1 steering file (constant rain + SCS-CN infiltration + FORMATTED
DATA FILE 2 CN map + distributed Manning zones + free-exit outlet) on a tiny
tilted-plane catchment BEFORE the hours-class Coweeta live run and BEFORE the
image rebuild -- run with the worker dir mounted over the baked copy:

  docker run --rm \
    -v <repo>/workers/telemac:/opt/trid3nt/workers/telemac \
    -v <rundir>:/data --entrypoint python trid3nt-local/telemac:latest \
    /opt/trid3nt/workers/telemac/../../../scripts/sandbox/telemac/rog_offline_smoke.py

(the driver below mounts itself; see the sibling runner shell one-liner in the
build session). ASCII only.
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


def build_synthetic_watershed(rundir: Path) -> int:
    """A tilted-plane catchment: 600 m x 300 m, 2% slope toward the +x outlet.

    Writes watershed.slf (BOTTOM = bed), node_cn2.txt (two CN bands) and
    node_manning.txt (two roughness bands) aligned to the mesh node order."""
    xs = np.arange(0.0, 600.0 + 1e-6, 20.0)
    ys = np.arange(0.0, 300.0 + 1e-6, 20.0)
    gx, gy = np.meshgrid(xs, ys)
    X = gx.ravel().astype(float)
    Y = gy.ravel().astype(float)
    # bed slopes DOWN toward x=600 (the outlet edge); + a shallow central swale
    # so flow concentrates. positive-up metres.
    bed = (600.0 - X) * 0.02 + 0.3 * np.abs(Y - 150.0) / 150.0
    tri = Delaunay(np.column_stack([X, Y]))
    ikle = tri.simplices.astype(np.int64)

    b = R.build_boundary(X, Y, ikle)
    R.write_rog_slf(str(rundir / "watershed.slf"), X, Y, b["ikle"], bed,
                    b["ipob"], b["ring"], b["nptfr"])

    # two CN bands (upper catchment forest CN=70, lower open CN=85) + two Manning
    # bands (forest 0.20 vs open 0.05) -> exercises the CN map + >=2 friction zones.
    cn2 = np.where(Y > 150.0, 70.0, 85.0)
    manning = np.where(Y > 150.0, 0.20, 0.05)
    (rundir / "node_cn2.txt").write_text("\n".join(f"{v:.3f}" for v in cn2) + "\n")
    (rundir / "node_manning.txt").write_text(
        "\n".join(f"{v:.3f}" for v in manning) + "\n")
    return int(X.shape[0])


def main() -> int:
    rundir = Path(sys.argv[1] if len(sys.argv) > 1 else "/data")
    rundir.mkdir(parents=True, exist_ok=True)
    npoin = build_synthetic_watershed(rundir)
    manifest = {
        "run_id": "rog-offline-smoke",
        "reach": {
            "name": "synthetic_tilted_catchment",
            "mode": "rain_on_grid",
            "watershed_slf": "watershed.slf",
            "runoff_path": "native",
            "amc_condition": 2,
            "rain_intensity_mm_per_hr": 300.0,
            "node_cn2_file": "node_cn2.txt",
            "node_manning_file": "node_manning.txt",
            "n_outlet_nodes": 6,
            "duration_s": 1800.0,
            "time_step_s": 2.0,
            "graphic_period": 100,
        },
    }
    (rundir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[smoke] synthetic watershed npoin={npoin} staged at {rundir}")

    rc = E.main(["--data-dir", str(rundir), "--run-id", "rog-offline-smoke"])
    metrics = json.loads((rundir / "telemac_metrics.json").read_text())
    print("[smoke] EXIT", rc, "STATUS", metrics.get("status"),
          "CORRECT_END", metrics.get("correct_end"))
    print("[smoke] peak_Q_m3s=", metrics.get("peak_discharge_m3s"),
          "vol_m3=", metrics.get("outflow_volume_m3"),
          "maxH_m=", metrics.get("max_depth_peak_m"),
          "maxV_ms=", metrics.get("max_velocity_peak_ms"),
          "frames=", metrics.get("n_frames"),
          "zones=", metrics.get("friction_zones"),
          "continuity=", metrics.get("continuity_rel_error"))
    if metrics.get("correct_end") and float(metrics.get("peak_discharge_m3s") or 0) > 0:
        print("[smoke] PASS")
        return 0
    print("[smoke] FAIL:", metrics.get("error"), metrics.get("listing_tail", ""))
    return 1


if __name__ == "__main__":
    sys.exit(main())
