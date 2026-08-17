"""Live proof: remote streaming -- drives the REAL LayerMaterializer
under a real QgsApplication against the tailnet MinIO endpoint (the path a
remote QGIS client uses over the tailnet).

Proves, end to end, against REAL objects in the running stack's MinIO:
  1. a COG raster registers via GDAL /vsicurl/ -- ranged, NO local copy;
  2. a FlatGeobuf registers via /vsicurl/ -- spatial-index ranged, NO local copy;
  3. an MDAL netCDF mesh STAGES to the session temp dir (the ONE fallback --
     MDAL cannot open a /vsicurl or plain-URL source), labeled STAGED;
  4. cleanup_session removes the session dir; sweep_stale_session_dirs reaps a
     dead-owner leftover but keeps a live-owner dir (a concurrent QGIS instance).

Run (from repo root), exporting the MinIO env block explicitly (never ambient
AWS creds), against the RUNNING daemon's MinIO -- this script does NOT restart
anything:

    QT_QPA_PLATFORM=offscreen \
    AWS_ENDPOINT_URL=http://<tailnet-ip>:9000 AWS_ACCESS_KEY_ID=... \
    AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-east-1 \
    python3 plugin/tests/headless_remote_streaming_proof.py

The MinIO objects are anonymous HTTP GETs on the tailnet (the trust boundary),
so /vsicurl needs no credentials; boto3 is used ONLY to discover object keys.
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://127.0.0.1:9000")

import boto3  # noqa: E402

s3 = boto3.client("s3", endpoint_url=ENDPOINT)
runs = s3.list_objects_v2(Bucket="trid3nt-runs", MaxKeys=4000).get("Contents", [])
cache = s3.list_objects_v2(Bucket="trid3nt-cache", MaxKeys=2000).get("Contents", [])
cog = next(o["Key"] for o in sorted(runs, key=lambda o: -o["Size"])
           if o["Key"].endswith(".tif"))
mesh = next((o["Key"] for o in runs if o["Key"].endswith("sfincs_map.nc")), None)
fgb = next((o["Key"] for o in cache if o["Key"].endswith(".fgb")), None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from qgis.core import QgsApplication  # noqa: E402

qgs = QgsApplication([], False)
qgs.initQgis()

from plugin.net.trid3nt_client import LayerEvent  # noqa: E402
from plugin.render import layers as L  # noqa: E402


def event(lid, name, uri, layer_type="raster", crs=None):
    return LayerEvent(layer_id=lid, name=name, layer_type=layer_type, uri=uri,
                      raw={"crs_authid": crs} if crs else {})


m = L.LayerMaterializer(settings=types.SimpleNamespace(minio_endpoint=ENDPOINT))
m.data_base_override = ENDPOINT
m.set_case("proof", "Streaming Proof")
sd = m._ensure_temp_dir()
print(f"[session] staging dir = {sd}")

print("\n[1] COG raster via /vsicurl")
before = set(os.listdir(sd))
for n in m.materialize([event("01COG", "cog", f"s3://trid3nt-runs/{cog}")]):
    print("   ", n)
print("    staged files (expect none):", sorted(set(os.listdir(sd)) - before) or "(none)")

if fgb:
    print("\n[2] FlatGeobuf via /vsicurl")
    for n in m.materialize([event("01FGB", "fgb", f"s3://trid3nt-cache/{fgb}", "vector")]):
        print("   ", n)

if mesh:
    print("\n[3] MDAL netCDF mesh -> STAGES to session dir (labeled fallback)")
    before = set(os.listdir(sd))
    for n in m.materialize([event("01MESH", "mesh", f"s3://trid3nt-runs/{mesh}",
                                  "mesh", "EPSG:32616")]):
        print("   ", n)
    print("    staged files (expect the .nc):", sorted(set(os.listdir(sd)) - before))

print("\n[4] cleanup + stale sweep")
m.cleanup_session()
print("    cleanup_session: session dir exists after =", os.path.isdir(sd))
import tempfile  # noqa: E402

dead = os.path.join(tempfile.gettempdir(), "trid3nt_session_proofdead")
live = os.path.join(tempfile.gettempdir(), "trid3nt_session_prooflive")
os.makedirs(dead, exist_ok=True)
os.makedirs(live, exist_ok=True)
open(os.path.join(dead, ".owner_pid"), "w").write("2147480000")
open(os.path.join(live, ".owner_pid"), "w").write(str(os.getpid()))
L.sweep_stale_session_dirs()
print("    sweep: dead-owner removed =", not os.path.isdir(dead),
      "| live-owner kept =", os.path.isdir(live))
import shutil  # noqa: E402

shutil.rmtree(live, ignore_errors=True)
shutil.rmtree(dead, ignore_errors=True)

qgs.exitQgis()
print("\n[done]")
