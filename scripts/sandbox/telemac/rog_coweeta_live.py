"""Live Coweeta Creek rain-on-grid proof (ADR 0196 C4) -- direct driver.

Runs the REAL pieces the registered telemac_rain_on_grid template composes,
end-to-end, on the Coweeta Creek NC catchment (pour point -83.40402 35.05746,
ADR 0193 site), THROUGH the rebuilt trid3nt-local/telemac:latest image:

  phase "mesh"  -- acquire_watershed_mesh (pysheds delineation + NHD river +
                   3DEP DEM + OceanMesh2D TIN, projected to UTM) -> watershed.slf;
                   fetch_landcover (NLCD 2021) sampled at the mesh nodes ->
                   per-node CN2 + Manning fields staged next to the mesh.
  phase "solve" -- stage the manifest (mode=rain_on_grid, constant design-storm
                   intensity, AMC knob) + run the worker container; extract the
                   outlet hydrograph + max fields + mass balance.
  phase "render"-- max-depth COG over an ESRI basemap + catchment boundary
                   (EPSG:3857), the dock-exact hydrograph chart (AMC II vs AMC I
                   overlay), and the mesh wireframe -- all to docs/proof/templates/.

This is a TEMPLATE SMOKE, not the replication experiment: a CONSTANT design-storm
intensity drives the native SCS-CN path (the installed v9.0.0 build hardcodes
RAINDEF=1, so a true time-varying MRMS hyetograph cannot drive the compiled
solver without recompiling user_rain.f -- documented in rog_build). Run in the
agent venv with .env.local sourced (set -a; source .env.local; set +a).

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
sys.path.insert(0, str(REPO / "server" / "src"))

POUR_POINT = (-83.40402, 35.05746)
# bbox covering the Coweeta Creek catchment upstream of the pour point.
BBOX = (-83.47, 35.02, -83.36, 35.10)
RUNDIR = Path(os.environ.get("ROG_RUNDIR", "/tmp/rog_coweeta"))
TELEMAC_IMAGE = "trid3nt-local/telemac:latest"
WORKER_DIR = str(REPO / "services" / "workers" / "telemac")

# a Coweeta flash-flood design storm: ~25 mm/hr sustained (TS-Fred-remnants class
# total ~150 mm over ~6 h). Constant intensity = the native SCS-CN path input.
DESIGN_INTENSITY_MM_PER_HR = 25.0
SIM_DURATION_S = 21600.0   # 6 h
TIME_STEP_S = 3.0
GRAPHIC_PERIOD = 200


def phase_mesh() -> None:
    from trid3nt_server.agent.workflows.telemac.rain_on_grid import mesh_acquisition as MA
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
        _sample_raster_at_nodes)
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    RUNDIR.mkdir(parents=True, exist_ok=True)
    print(f"[mesh] acquiring Coweeta watershed mesh at {POUR_POINT} ...", flush=True)
    mesh = MA.acquire_watershed_mesh(
        pour_point=POUR_POINT, bbox=BBOX, output_dir=str(RUNDIR),
        min_edge_length_m=40.0, max_edge_length_m=300.0, grade=0.2)
    print(f"[mesh] nodes={mesh.points_utm.shape[0]} cells={mesh.cells.shape[0]} "
          f"epsg={mesh.utm_epsg} area_km2={mesh.area_km2:.2f} "
          f"outlet={mesh.outlet_lonlat}", flush=True)

    points_ll = np.asarray(mesh.meta["points_lonlat"], dtype=float)

    # NLCD 2021 sampled at the mesh nodes -> per-node land cover.
    print("[mesh] fetch_landcover (NLCD 2021) ...", flush=True)
    lc = TOOL_REGISTRY["fetch_landcover"].fn(bbox=list(BBOX), dataset="nlcd_2021",
                                             resolution_m=30)
    lc_uri = lc["uri"] if isinstance(lc, dict) else getattr(lc, "uri")
    from trid3nt_server.agent.tools.cache import read_object_bytes_s3
    lc_path = RUNDIR / "nlcd.tif"
    lc_path.write_bytes(
        read_object_bytes_s3(lc_uri) if str(lc_uri).startswith("s3://")
        else Path(lc_uri).read_bytes())
    nlcd_vals = _sample_raster_at_nodes(lc_path, points_ll)
    node_nlcd = [int(round(v)) for v in nlcd_vals]

    cn2, manning = MA.assemble_node_fields(
        node_nlcd=node_nlcd, uniform_cn=None, slopes_m_per_m=None,
        steep_slope_correction=False)

    (RUNDIR / "node_cn2.txt").write_text("\n".join(f"{v:.3f}" for v in cn2) + "\n")
    (RUNDIR / "node_manning.txt").write_text(
        "\n".join(f"{v:.3f}" for v in manning) + "\n")
    # persist facts for the solve/render phases.
    (RUNDIR / "mesh_facts.json").write_text(json.dumps({
        "npoin": int(mesh.points_utm.shape[0]),
        "nelem": int(mesh.cells.shape[0]),
        "utm_epsg": int(mesh.utm_epsg),
        "area_km2": float(mesh.area_km2),
        "outlet_lonlat": list(mesh.outlet_lonlat),
        "catchment_geojson": mesh.catchment_geojson,
        "cn2_min": float(np.min(cn2)), "cn2_max": float(np.max(cn2)),
        "manning_min": float(np.min(manning)), "manning_max": float(np.max(manning)),
        "nlcd_classes": sorted(set(node_nlcd)),
    }, indent=2))
    print(f"[mesh] staged watershed.slf + node fields; CN2 {min(cn2):.0f}-{max(cn2):.0f} "
          f"Manning {min(manning):.3f}-{max(manning):.3f} NLCD {sorted(set(node_nlcd))}",
          flush=True)


def phase_solve(amc: int, tag: str) -> dict:
    facts = json.loads((RUNDIR / "mesh_facts.json").read_text())
    solve_dir = RUNDIR / f"solve_{tag}"
    solve_dir.mkdir(parents=True, exist_ok=True)
    # stage the mesh + node fields into the per-solve dir.
    for name in ("watershed.slf", "node_cn2.txt", "node_manning.txt"):
        (solve_dir / name).write_bytes((RUNDIR / name).read_bytes())
    manifest = {
        "run_id": f"rog-coweeta-{tag}",
        "reach": {
            "name": f"coweeta_creek_amc{amc}",
            "mode": "rain_on_grid",
            "watershed_slf": "watershed.slf",
            "runoff_path": "native",
            "amc_condition": amc,
            "rain_intensity_mm_per_hr": DESIGN_INTENSITY_MM_PER_HR,
            "node_cn2_file": "node_cn2.txt",
            "node_manning_file": "node_manning.txt",
            "outlet_lonlat": facts["outlet_lonlat"],
            "n_outlet_nodes": 8,
            "duration_s": SIM_DURATION_S,
            "time_step_s": TIME_STEP_S,
            "graphic_period": GRAPHIC_PERIOD,
        },
    }
    (solve_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"[solve amc={amc}] running worker container (hours-class) ...", flush=True)
    argv = [
        "docker", "run", "--rm",
        "-v", f"{solve_dir}:/data",
        "-e", "TRID3NT_TELEMAC_SOLVE_TIMEOUT=86400",
        "--entrypoint", "/usr/local/bin/_entrypoint.sh", TELEMAC_IMAGE,
        "python", "/opt/trid3nt/services/workers/telemac/entrypoint.py",
        "--data-dir", "/data", "--run-id", f"rog-coweeta-{tag}",
    ]
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=90000)
    metrics = json.loads((solve_dir / "telemac_metrics.json").read_text())
    print(f"[solve amc={amc}] status={metrics.get('status')} "
          f"peakQ={metrics.get('peak_discharge_m3s')} m3/s "
          f"vol={metrics.get('outflow_volume_m3')} m3 "
          f"maxH={metrics.get('max_depth_peak_m')} m "
          f"continuity={metrics.get('continuity_rel_error')} "
          f"wall_s={metrics.get('wall_s')}", flush=True)
    if metrics.get("status") != "ok":
        print("[solve] STDERR tail:", cp.stderr[-1500:], flush=True)
    return metrics


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "mesh"
    if phase == "mesh":
        phase_mesh()
    elif phase == "solve":
        amc = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        tag = sys.argv[3] if len(sys.argv) > 3 else f"amc{amc}"
        phase_solve(amc, tag)
    else:
        print(f"unknown phase: {phase}")
        sys.exit(2)
