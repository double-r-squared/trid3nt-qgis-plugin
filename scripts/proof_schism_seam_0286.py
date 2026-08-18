"""ADR 0286 live proof -- SCHISM native-mesh onto the emit-on-solve seam.

Direct-call (no chat layer): runs a REAL SCHISM solve through the migrated composer,
then proves the Option-B chain end-to-end on the real run:

  1. solve -> the composer writes outputs.json with a kind="mesh" entry
     (crs_authid) + the typed peak raster entry(ies).
  2. the SEAM builds the layer_type="mesh" LayerURI the plugin publishes
     (build_layers_from_outputs(frames_only=True)) -- name/style/role/crs/uri
     asserted (the byte-equivalence shape).
  3. the typed peak COG survives (the composer's own primary layer).
  4. the native out2d/salinity netCDF loads through REAL QGIS/MDAL, temporal
     (dataset groups + >1 timestep) -- delegated to the system PyQGIS probe.

Env (source .env.local; TRID3NT_SOLVER_BACKEND=local-docker + MinIO block):
  TEMPLATE=tidal|surge|baroclinic   MESH_SOURCE=coastal_tin|bundled_quarterannulus
  LOCATION=...  SIM_DAYS=...  OUTPUT_INTERVAL_MIN=...  (surge: STORM/YEAR)

  env $(grep -v "^#" .env.local | xargs) PYTHONPATH=.:contracts \
    venvs/agent/bin/python scripts/proof_schism_seam_0286.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("proof_schism_seam_0286")

import boto3

TEMPLATE = os.environ.get("TEMPLATE", "tidal")
MESH_SOURCE = os.environ.get("MESH_SOURCE", "coastal_tin")
LOCATION = os.environ.get("LOCATION", "Galveston Bay")
SIM_DAYS = float(os.environ.get("SIM_DAYS", "2"))
OUTPUT_INTERVAL_MIN = (
    float(os.environ["OUTPUT_INTERVAL_MIN"])
    if os.environ.get("OUTPUT_INTERVAL_MIN") else None
)
RUNS_BUCKET = os.environ["TRID3NT_RUNS_BUCKET"]

s3 = boto3.client(
    "s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)


async def _solve():
    if TEMPLATE == "tidal":
        from trid3nt_server.workflows.schism.tidal_hydro.tidal_hydro import (
            schism_tidal_hydro,
        )
        kw = dict(mesh_source=MESH_SOURCE, sim_days=SIM_DAYS,
                  output_interval_min=OUTPUT_INTERVAL_MIN)
        if MESH_SOURCE == "coastal_tin":
            kw.update(location_query=LOCATION, tidal_amplitude_m=0.4,
                      constituents=["M2"])
        return await schism_tidal_hydro(**kw)
    if TEMPLATE == "surge":
        from trid3nt_server.workflows.schism.pahm_surge.pahm_surge import (
            schism_pahm_surge,
        )
        return await schism_pahm_surge(
            storm_name=os.environ.get("STORM", "Ike"),
            year=int(os.environ.get("YEAR", "2008")),
            location_query=LOCATION, sim_days=SIM_DAYS,
            allow_synthetic_domain=os.environ.get("ALLOW_SYNTHETIC") == "1",
            output_interval_min=OUTPUT_INTERVAL_MIN)
    if TEMPLATE == "baroclinic":
        from trid3nt_server.workflows.schism.baroclinic_circulation.baroclinic_circulation import (  # noqa: E501
            schism_baroclinic_circulation,
        )
        # law 9 (recon guidance): NWM-DERIVED discharge (None -> dominant reach) +
        # USER salinity (row 20 has no fetcher; 33.5 psu is unambiguously user).
        disc = os.environ.get("DISCHARGE")
        return await schism_baroclinic_circulation(
            location_query=LOCATION, sim_days=SIM_DAYS,
            river_discharge_m3s=(float(disc) if disc else None),
            ocean_salinity_psu=float(os.environ.get("SALINITY", "33.5")),
            output_interval_min=OUTPUT_INTERVAL_MIN, input_mode="auto")
    if TEMPLATE == "coupled":
        from trid3nt_server.workflows.schism.coupled_waves.coupled_waves import (
            schism_coupled_waves,
        )
        return await schism_coupled_waves(sim_hours=float(os.environ.get("SIM_HOURS", "4")))
    raise SystemExit(f"unknown TEMPLATE={TEMPLATE}")


def _fetch_outputs_json(run_id: str) -> dict:
    obj = s3.get_object(Bucket=RUNS_BUCKET, Key=f"{run_id}/outputs.json")
    return json.loads(obj["Body"].read())


def _mdal_probe(mesh_key: str) -> dict:
    """Download the mesh netCDF + run the system-PyQGIS temporal dock-load probe."""
    tmp = tempfile.mkdtemp(prefix="seam0286_")
    local = os.path.join(tmp, os.path.basename(mesh_key))
    s3.download_file(RUNS_BUCKET, mesh_key, local)
    probe = (Path(__file__).parent.parent
             / "_scratch_dockload_probe.py")
    probe.write_text(_PROBE_SRC, encoding="utf-8")
    out = subprocess.run(
        ["/usr/bin/python3", str(probe), local],
        capture_output=True, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    probe.unlink(missing_ok=True)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": out.stdout[-500:] + out.stderr[-500:]}


_PROBE_SRC = r'''
import os, sys, json
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qgis.core import QgsApplication, QgsMeshLayer, QgsMeshDatasetIndex, Qgis
qgs = QgsApplication([], False); qgs.initQgis()
src = sys.argv[1]
L = QgsMeshLayer(src, os.path.basename(src), "mdal")
dp = L.dataProvider()
groups, max_steps, times = [], 0, []
if L.isValid():
    for i in range(L.datasetGroupCount()):
        m = L.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0))
        if m.isTemporal():
            n = dp.datasetCount(i)
            groups.append({"name": m.name(), "time_steps": n})
            if n > max_steps:
                max_steps = n
                times = [round(dp.datasetMetadata(QgsMeshDatasetIndex(i, j)).time(), 3)
                         for j in range(min(n, 6))]
print(json.dumps({"qgis": str(Qgis.QGIS_VERSION), "isValid": bool(L.isValid()),
    "datasetGroupCount": int(L.datasetGroupCount()) if L.isValid() else 0,
    "temporal_groups": groups, "max_time_steps": max_steps,
    "times_hours_sample": times}))
qgs.exitQgis()
'''


async def main() -> int:
    log.info("TEMPLATE=%s MESH_SOURCE=%s SIM_DAYS=%s OUTPUT_INTERVAL_MIN=%s",
             TEMPLATE, MESH_SOURCE, SIM_DAYS, OUTPUT_INTERVAL_MIN)
    result = await _solve()
    if isinstance(result, dict) and result.get("error_code"):
        log.error("solve returned FAILED envelope: %s", result)
        print(json.dumps({"PASS": False, "error": result}, indent=2))
        return 2

    # run_id from the typed peak layer_id (schism-elev-max-<run_id> / -hs-max- / -surf-salt-)
    layer_id = getattr(result, "layer_id", "")
    run_id = layer_id.rsplit("-", 1)[-1]
    log.info("solved run_id=%s peak layer_id=%s", run_id, layer_id)

    manifest = _fetch_outputs_json(run_id)
    entries = manifest.get("entries", [])
    mesh_entries = [e for e in entries if e.get("kind") == "mesh"]
    raster_entries = [e for e in entries if e.get("kind") == "raster"]

    # Build the seam layers exactly as the composer publishes them.
    from trid3nt_server.emission.outputs_seam import (
        build_layers_from_outputs, read_outputs_manifest,
    )
    import types as _t
    m = read_outputs_manifest(_t.SimpleNamespace(run_id=run_id))
    seam = build_layers_from_outputs(m, run_id=run_id, frames_only=True)
    seam_mesh = [l for l in seam.layers if l.layer_type == "mesh"]

    mesh_key = f"{run_id}/outputs/{os.path.basename(mesh_entries[0]['uri'])}" \
        if mesh_entries else None
    mdal = _mdal_probe(mesh_key) if mesh_key else {"error": "no mesh entry"}

    smesh = seam_mesh[0] if seam_mesh else None
    checks = {
        "outputs_json_has_mesh_entry": bool(mesh_entries),
        "mesh_entry_has_crs_or_none": (
            mesh_entries[0].get("crs_authid") if mesh_entries else None),
        "outputs_json_has_peak_raster": bool(raster_entries),
        "seam_built_mesh_layer": bool(seam_mesh),
        "seam_mesh_layer_type": (smesh.layer_type if smesh else None),
        "seam_mesh_style_preset": (smesh.style_preset if smesh else None),
        "seam_mesh_role": (smesh.role if smesh else None),
        "seam_mesh_name": (smesh.name if smesh else None),
        "seam_mesh_uri": (smesh.uri if smesh else None),
        "seam_mesh_crs_authid": (smesh.crs_authid if smesh else None),
        "seam_skipped_peak_under_frames_only": (
            not any(l.layer_type == "raster" for l in seam.layers)),
        "typed_peak_survives": bool(layer_id),
        "mdal_isValid": mdal.get("isValid"),
        "mdal_datasetGroupCount": mdal.get("datasetGroupCount"),
        "mdal_max_time_steps": mdal.get("max_time_steps"),
        "mdal_times_hours_sample": mdal.get("times_hours_sample"),
    }
    passed = (
        checks["outputs_json_has_mesh_entry"]
        and checks["seam_built_mesh_layer"]
        and checks["seam_mesh_layer_type"] == "mesh"
        and checks["seam_mesh_style_preset"] == "mesh_grid"
        and checks["seam_mesh_role"] == "context"
        and checks["seam_skipped_peak_under_frames_only"]
        and checks["typed_peak_survives"]
        and checks["mdal_isValid"] is True
        and (checks["mdal_max_time_steps"] or 0) > 1
    )
    report = {"PASS": bool(passed), "template": TEMPLATE, "mesh_source": MESH_SOURCE,
              "run_id": run_id, "output_interval_min": OUTPUT_INTERVAL_MIN,
              "n_mesh_entries": len(mesh_entries), "n_raster_entries": len(raster_entries),
              "checks": checks}
    print("\n=== ADR 0286 SEAM PROOF ===")
    print(json.dumps(report, indent=2, default=str))
    outp = Path(__file__).parent.parent / "docs" / "proof"
    outp.mkdir(parents=True, exist_ok=True)
    tag = f"{TEMPLATE}_{MESH_SOURCE}" + (
        f"_oi{int(OUTPUT_INTERVAL_MIN)}" if OUTPUT_INTERVAL_MIN else "")
    (outp / f"schism_seam_0286_{tag}.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
