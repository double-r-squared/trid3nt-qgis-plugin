"""Ball Creek rain-on-grid replication -- live direct driver.

Executes the Godara et al. (2024) rain-on-grid protocol on the Ball Creek fork of
the Coweeta basin, gauged by EDI weir house #9 (knb-lter-cwt.3037/19, hourly
discharge in m3/s, 2014-2019). The domain is re-cut to the Ball Creek catchment
(spatial caveat); forcing is the NOAA AORC hyetograph (fetch_aorc_precip)
over the catchment; observed discharge is the EDI record (fetch_lter_records).

INSTALLED-ENGINE CONSTRAINT: TELEMAC v9.0.0 hardcodes RAINDEF=1
(constant rain intensity; a time-varying hyetograph needs a user_rain.f recompile).
So each event is driven as a constant-intensity design storm = AORC storm-core
total / core duration, for a rain window (DURATION OF RAIN OR EVAPORATION IN HOURS,
the native RAIN_HDUR keyword) after which rain stops and the catchment drains (the
recession limb). Mass balance caps constant-rain outlet runoff at excess_rate*area,
so flashy sub-daily peaks are structurally under-represented -- a documented
screening-grade limitation quantified in the results.

Phases:
  mesh   -- generate_catchment_mesh at the Ball Creek pour point + NLCD node fields.
  solve  -- stage a rain_on_grid manifest (constant intensity + rain window + AMC +
            uniform CN override + scaled Manning) and run the worker container.

Run in the agent venv with .env.local sourced (set -a; source .env.local; set +a).
ASCII only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

# Ball Creek weir #9 pour point (delineation): a channel cell +12 up the
# Ball Creek fork so the mesh 8-cell max-accumulation snap resolves to the fork
# outlet (~-83.4297, 35.0592) without jumping to the Coweeta Creek merged stem.
POUR_POINT = (-83.43131, 35.05701)
# bbox covering the whole Ball Creek catchment upstream of the pour point.
BBOX = (-83.480, 35.020, -83.418, 35.065)
# AOI-mean AORC hyetograph bbox = the Ball Creek catchment extent.
AORC_BBOX = [-83.4733, 35.0281, -83.4219, 35.0601]
RUNDIR = Path(os.environ.get("ROG_RUNDIR", "/tmp/rog_ballcreek"))
TELEMAC_IMAGE = "trid3nt-local/telemac:latest"

MIN_EDGE_M = 30.0
MAX_EDGE_M = 200.0
GRADE = 0.2
TIME_STEP_S = 2.0


def phase_mesh() -> None:
    from trid3nt_server.workflows.mesh import watershed as W
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        landcover_cn_manning, node_curve_numbers)
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.cache import read_object_bytes_s3

    RUNDIR.mkdir(parents=True, exist_ok=True)
    print(f"[mesh] acquiring Ball Creek watershed mesh at {POUR_POINT} ...", flush=True)
    mesh = W.generate_catchment_mesh(
        pour_point=POUR_POINT, bbox=BBOX, slug="watershed", output_dir=str(RUNDIR),
        bed_dem=TOOL_REGISTRY["fetch_dem"].fn(bbox=list(BBOX), source="3dep",
                                              resolution_m=10),
        rivers=TOOL_REGISTRY["fetch_river_geometry"].fn(bbox=list(BBOX),
                                                        source="nhdplus_hr"),
        min_edge_length_m=MIN_EDGE_M, max_edge_length_m=MAX_EDGE_M, grade=GRADE)
    print(f"[mesh] nodes={mesh.points_utm.shape[0]} cells={mesh.cells.shape[0]} "
          f"epsg={mesh.utm_epsg} area_km2={mesh.area_km2:.2f} "
          f"outlet={mesh.outlet_lonlat}", flush=True)

    points_ll = np.asarray(mesh.points_lonlat, dtype=float)
    print("[mesh] fetch_landcover (NLCD 2021) ...", flush=True)
    lc = TOOL_REGISTRY["fetch_landcover"].fn(bbox=list(BBOX), dataset="nlcd_2021",
                                             resolution_m=30)
    lc_uri = lc["uri"] if isinstance(lc, dict) else getattr(lc, "uri")
    lc_path = RUNDIR / "nlcd.tif"
    lc_path.write_bytes(
        read_object_bytes_s3(lc_uri) if str(lc_uri).startswith("s3://")
        else Path(lc_uri).read_bytes())
    nlcd_vals = W.sample_raster_at_nodes(lc_path, points_ll)
    node_nlcd = [int(round(v)) for v in nlcd_vals]

    manning = [landcover_cn_manning(c)[1] for c in node_nlcd]
    cn2 = node_curve_numbers(node_nlcd, uniform_cn=None)

    (RUNDIR / "node_cn2.txt").write_text("\n".join(f"{v:.3f}" for v in cn2) + "\n")
    (RUNDIR / "node_manning.txt").write_text(
        "\n".join(f"{v:.3f}" for v in manning) + "\n")
    (RUNDIR / "mesh_facts.json").write_text(json.dumps({
        "npoin": int(mesh.points_utm.shape[0]),
        "nelem": int(mesh.cells.shape[0]),
        "utm_epsg": int(mesh.utm_epsg),
        "area_km2": float(mesh.area_km2),
        "outlet_lonlat": list(mesh.outlet_lonlat),
        "pour_point_lonlat": list(POUR_POINT),
        "catchment_geojson": mesh.catchment_geojson,
        "cn2_min": float(np.min(cn2)), "cn2_max": float(np.max(cn2)),
        "manning_min": float(np.min(manning)), "manning_max": float(np.max(manning)),
        "nlcd_classes": sorted(set(node_nlcd)),
    }, indent=2))
    print(f"[mesh] staged watershed.slf + node fields; CN2 {min(cn2):.0f}-{max(cn2):.0f} "
          f"Manning {min(manning):.3f}-{max(manning):.3f} NLCD {sorted(set(node_nlcd))}",
          flush=True)


def phase_solve(*, tag: str, intensity_mm_per_hr: float, rain_duration_hr: float,
                sim_duration_hr: float, amc: int, uniform_cn: float | None,
                manning_scale: float, ia_option: int = 1,
                graphic_period_s: float = 900.0) -> dict:
    """Stage + run one Ball Creek RoG solve; return the worker metrics + hydrograph."""
    facts = json.loads((RUNDIR / "mesh_facts.json").read_text())
    solve_dir = RUNDIR / f"solve_{tag}"
    solve_dir.mkdir(parents=True, exist_ok=True)
    (solve_dir / "watershed.slf").write_bytes((RUNDIR / "watershed.slf").read_bytes())
    (solve_dir / "node_cn2.txt").write_bytes((RUNDIR / "node_cn2.txt").read_bytes())
    # scaled Manning field (calibration lever).
    base_manning = [float(v) for v in
                    (RUNDIR / "node_manning.txt").read_text().split()]
    scaled = [max(0.005, min(1.0, m * manning_scale)) for m in base_manning]
    (solve_dir / "node_manning.txt").write_text(
        "\n".join(f"{v:.4f}" for v in scaled) + "\n")

    dt = TIME_STEP_S
    gp_steps = max(1, int(round(graphic_period_s / dt)))
    reach = {
        "name": f"ballcreek_{tag}",
        "mode": "rain_on_grid",
        "watershed_slf": "watershed.slf",
        "runoff_path": "native",
        "amc_condition": int(amc),
        "rain_intensity_mm_per_hr": float(intensity_mm_per_hr),
        "rain_duration_s": float(rain_duration_hr) * 3600.0,
        "node_cn2_file": "node_cn2.txt",
        "node_manning_file": "node_manning.txt",
        "outlet_lonlat": facts["outlet_lonlat"],
        "initial_abstraction_option": int(ia_option),
        "n_outlet_nodes": 6,
        "duration_s": float(sim_duration_hr) * 3600.0,
        "time_step_s": dt,
        "graphic_period": gp_steps,
    }
    if uniform_cn is not None:
        reach["curve_number"] = float(uniform_cn)
    manifest = {"run_id": f"rog-bc-{tag}", "reach": reach}
    (solve_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[solve {tag}] intensity={intensity_mm_per_hr:.2f}mm/hr rain_dur={rain_duration_hr}h "
          f"sim={sim_duration_hr}h amc={amc} cn={uniform_cn} manning_x={manning_scale} ...",
          flush=True)
    argv = [
        "docker", "run", "--rm",
        "-v", f"{solve_dir}:/data",
        "-e", "TRID3NT_TELEMAC_SOLVE_TIMEOUT=86400",
        "--entrypoint", "/usr/local/bin/_entrypoint.sh", TELEMAC_IMAGE,
        "python", "/opt/trid3nt/workers/telemac/entrypoint.py",
        "--data-dir", "/data", "--run-id", f"rog-bc-{tag}",
    ]
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=90000)
    mpath = solve_dir / "telemac_metrics.json"
    if not mpath.exists():
        print("[solve] NO METRICS. STDERR tail:", cp.stderr[-2000:], flush=True)
        raise SystemExit(2)
    metrics = json.loads(mpath.read_text())
    print(f"[solve {tag}] status={metrics.get('status')} peakQ={metrics.get('peak_discharge_m3s')} "
          f"m3/s vol={metrics.get('outflow_volume_m3')} m3 maxH={metrics.get('max_depth_peak_m')} "
          f"continuity={metrics.get('continuity_rel_error')} wall_s={metrics.get('wall_s')}",
          flush=True)
    if metrics.get("status") != "ok":
        print("[solve] STDERR tail:", cp.stderr[-1500:], flush=True)
    return metrics


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "mesh"
    if phase == "mesh":
        phase_mesh()
    elif phase == "solve":
        # ad-hoc: solve tag inten raindur simdur amc cn manning
        phase_solve(tag=sys.argv[2], intensity_mm_per_hr=float(sys.argv[3]),
                    rain_duration_hr=float(sys.argv[4]), sim_duration_hr=float(sys.argv[5]),
                    amc=int(sys.argv[6]),
                    uniform_cn=(None if sys.argv[7] == "none" else float(sys.argv[7])),
                    manning_scale=float(sys.argv[8]))
    else:
        print(f"unknown phase: {phase}")
        sys.exit(2)
