#!/usr/bin/env python
"""row 1 reference driver: distributed on-mesh RAINFALL vs no-rain.

Non-registered reference driver (the form). Solves the SAME river
reach + mesh TWICE through the real run_solver seam -- once with NO rain forcing
(baseline) and once with a distributed on-mesh RAIN OR EVAPORATION source term
-- then reads WATER DEPTH from both r2d_river.slf results and quantifies the
rain-driven change in inundation depth + its timing, INDEPENDENT of the inflow
hydrograph (which is byte-identical between the two decks).

This proves the ``rain_or_evap_mm_per_day`` knob is honored THROUGH the rebuilt
worker image and that it changes the water surface (with-rain vs without).

Env (MinIO): set -a; source .env.local; set +a
Usage:
  venvs/agent/bin/python workers/telemac/rainfall_forcing_compare.py \
      [--rain-mm-per-day 500] [--duration-s 3600] [--out <dir>]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import boto3
import numpy as np

from trid3nt_server.workflows.telemac import run_telemac as _rt  # noqa: F401
from trid3nt_server.data.simulation.solver.solver import run_solver, wait_for_completion
from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin


def _s3():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


# A small Snake River reach (same seed as prove_telemac_seam.py) so the two
# solves are quick and the mesh is identical between them.
def _base_reach(duration_s: float) -> dict:
    return {
        "name": "snake_river_twin_falls_rain",
        "seed_lon": -114.307,
        "seed_lat": 42.579,
        "nav_direction": "DM",
        "distance_km": 3.0,
        "channel_width_m": 60.0,
        "mesh_size_m": 16.0,
        # The mesh follows the REAL NHDPlus flowline centerline (true river
        # planform + bends). constant_ribbon offsets it to an assumed constant
        # width because this short 3 km demo reach has no NHDArea water-polygon
        # coverage (the nhd_area path raises TELEMAC_BANKS_UNAVAILABLE here). The
        # channel is NOT an offset-from-the-river fan -- it is the real digitized
        # flowline with a symmetric channel; identical between the two runs (a
        # clean rain vs no-rain A/B). Bank fidelity (variable banks + islands)
        # rides the nhd_area default on reaches with NHDArea coverage.
        "bank_source": "constant_ribbon",
        "inflow_q_m3s": 250.0,
        "init_depth_m": 2.5,
        "dye_conc_mgl": 100.0,
        "duration_s": float(duration_s),
        "time_step_s": 1.0,
        "graphic_period": 200,
    }


def _solve(reach: dict, tag: str) -> str:
    cache_bucket = os.environ["TRID3NT_CACHE_BUCKET"]
    runs_bucket = os.environ["TRID3NT_RUNS_BUCKET"]
    run_tag = f"telemac-rain-{tag}-{int(time.time())}"
    manifest = {
        "reach": reach,
        "run_id": run_tag,
        "inputs": [],
        "telemac_args": [],
        "outputs": ["r2d_river.slf", "river.slf", "river.cli",
                    "t2d_river.cas", "full_listing.log", "telemac_metrics.json"],
    }
    s3 = _s3()
    key = f"telemac/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode(),
                  ContentType="application/json")
    handle = run_solver(solver="telemac_river_dye",
                        model_setup_uri=f"s3://{cache_bucket}/{key}",
                        compute_class="medium")
    t0 = time.time()
    result = asyncio.run(wait_for_completion(handle, poll_interval_s=5, timeout_s=2400))
    comp = json.loads(s3.get_object(Bucket=runs_bucket,
                                    Key=f"{handle.run_id}/completion.json")["Body"].read())
    print(f"[{tag}] run_id={handle.run_id} status={result.status} "
          f"comp={comp.get('status')} ({time.time()-t0:.0f}s)")
    if comp.get("status") != "ok":
        raise SystemExit(f"[{tag}] solve failed: {json.dumps(comp)[:400]}")
    return handle.run_id


def _utm_epsg(run_id: str) -> int:
    runs_bucket = os.environ["TRID3NT_RUNS_BUCKET"]
    m = json.loads(_s3().get_object(
        Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")["Body"].read())
    return int(m.get("utm_epsg") or 32611)


def _download_slf(run_id: str, out: Path) -> Path:
    runs_bucket = os.environ["TRID3NT_RUNS_BUCKET"]
    dst = out / f"{run_id}_r2d_river.slf"
    _s3().download_file(runs_bucket, f"{run_id}/r2d_river.slf", str(dst))
    # also grab the .cas so the proof can show the emitted keyword lines
    try:
        _s3().download_file(runs_bucket, f"{run_id}/t2d_river.cas",
                            str(out / f"{run_id}_t2d_river.cas"))
    except Exception:
        pass
    return dst


def _depth_series(slf: Path):
    mesh = read_selafin(slf)
    dvar = "WATER DEPTH" if "WATER DEPTH" in mesh["data"] else None
    if dvar is None:
        for v in mesh["varnames"]:
            if v.strip().upper().startswith("H") or "DEPTH" in v.upper():
                dvar = v
                break
    depth = mesh["data"][dvar]  # (nframes, npoin)
    times = mesh["times"]
    wet = depth > 0.05
    mean_depth_t = np.array([depth[i][wet[i]].mean() if wet[i].any() else 0.0
                             for i in range(depth.shape[0])])
    wet_area_t = wet.sum(axis=1).astype(float)
    return {"mesh": mesh, "depth": depth, "times": times,
            "mean_depth_t": mean_depth_t, "wet_area_t": wet_area_t,
            "max_depth": float(depth.max())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rain-mm-per-day", type=float, default=500.0)
    ap.add_argument("--duration-s", type=float, default=3600.0)
    ap.add_argument("--out", default="docs/proof/templates")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"== baseline (no rain), rain ({args.rain_mm_per_day} mm/day); "
          f"duration {args.duration_s}s ==")
    base_reach = _base_reach(args.duration_s)
    rain_reach = dict(base_reach, rain_or_evap_mm_per_day=float(args.rain_mm_per_day))

    base_id = _solve(base_reach, "base")
    rain_id = _solve(rain_reach, "rain")

    base_slf = _download_slf(base_id, out)
    rain_slf = _download_slf(rain_id, out)

    b = _depth_series(base_slf)
    r = _depth_series(rain_slf)

    # Same mesh -> node-aligned comparison of the FINAL frame.
    d_base = b["depth"][-1]
    d_rain = r["depth"][-1]
    n = min(d_base.size, d_rain.size)
    ddiff = d_rain[:n] - d_base[:n]
    wet_final = (d_base[:n] > 0.05) | (d_rain[:n] > 0.05)

    summary = {
        "rain_mm_per_day": args.rain_mm_per_day,
        "duration_s": args.duration_s,
        "base_run_id": base_id,
        "rain_run_id": rain_id,
        "base_max_depth_m": b["max_depth"],
        "rain_max_depth_m": r["max_depth"],
        "final_mean_depth_base_m": float(b["mean_depth_t"][-1]),
        "final_mean_depth_rain_m": float(r["mean_depth_t"][-1]),
        "final_mean_depth_delta_m": float(r["mean_depth_t"][-1] - b["mean_depth_t"][-1]),
        "final_depth_diff_mean_m": float(ddiff[wet_final].mean()) if wet_final.any() else 0.0,
        "final_depth_diff_max_m": float(ddiff[wet_final].max()) if wet_final.any() else 0.0,
        "base_final_wet_nodes": int((d_base[:n] > 0.05).sum()),
        "rain_final_wet_nodes": int((d_rain[:n] > 0.05).sum()),
    }
    print("== SUMMARY ==")
    print(json.dumps(summary, indent=2))
    (out / "telemac_river_dye_rain_forcing_summary.json").write_text(
        json.dumps(summary, indent=2))

    # persist the mesh + diff for the proof renderer
    np.savez(out / "telemac_river_dye_rain_forcing_arrays.npz",
             x=b["mesh"]["x"], y=b["mesh"]["y"], ikle=b["mesh"]["ikle"],
             utm_epsg=_utm_epsg(base_id),
             rain_mm_per_day=args.rain_mm_per_day, duration_s=args.duration_s,
             d_base=d_base[:n], d_rain=d_rain[:n], ddiff=ddiff,
             times_base=b["times"], times_rain=r["times"],
             mean_depth_base=b["mean_depth_t"], mean_depth_rain=r["mean_depth_t"])
    print(f"== wrote arrays + summary to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
