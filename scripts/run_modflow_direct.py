"""Direct MODFLOW sustainable_yield invocation for trid3nt-local proof.

Bypasses the LLM/agent layer entirely:
  1. Builds a sustainable_yield GWF-only deck for Fresno CA using flopy
  2. Runs mf6 locally (GRACE2_MF6_BIN)
  3. Postprocesses drawdown -> COG in MinIO (trid3nt-runs bucket)
  4. Publishes TiTiler tile URL if GRACE2_TILE_SERVER_BASE is set
  5. Prints the DrawdownLayerURI JSON + the MinIO object path

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v '^#' .env.local | xargs) \
    GRACE2_MODFLOW_LOCAL=1 \
    PYTHONPATH=vendor/services/agent/src:vendor/packages/contracts/src \
    venvs/agent/bin/python scripts/run_modflow_direct.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("run_modflow_direct")

# ---------------------------------------------------------------------------
# Validate environment
# ---------------------------------------------------------------------------

MF6 = os.environ.get("GRACE2_MF6_BIN", "mf6")
if not Path(MF6).is_file():
    # Try PATH
    import shutil
    found = shutil.which(MF6)
    if not found:
        log.error("mf6 binary not found: GRACE2_MF6_BIN=%s", MF6)
        sys.exit(1)
    MF6 = found
log.info("mf6 binary: %s", MF6)

# Fresno CA center
FRESNO_LAT = 36.7468
FRESNO_LON = -119.7726

# Small default grid -- archetype defaults apply (20x20 cells, small bbox)
WELL_LAT = FRESNO_LAT
WELL_LON = FRESNO_LON

# ---------------------------------------------------------------------------
# Build deck with flopy
# ---------------------------------------------------------------------------

log.info("importing gwt_adapter (flopy) ...")
try:
    from grace2_agent.workflows.run_modflow import build_modflow_deck
except ImportError as exc:
    log.error("import failed -- is PYTHONPATH set? %s", exc)
    sys.exit(1)

workdir = Path(tempfile.mkdtemp(prefix="modflow_fresno_"))
log.info("workdir: %s", workdir)

deck = build_modflow_deck(
    spill_location_latlon=(FRESNO_LAT, FRESNO_LON),
    contaminant="n/a",            # not a plume run -- archetype ignores this
    release_rate_kg_s=1.0,        # ignored by archetype but required field
    duration_days=1.0,            # ignored by archetype but required field
    aquifer_k_ms=1e-4,
    porosity=0.3,
    workdir=workdir,
    archetype="sustainable_yield",
    well_location_latlon=(WELL_LAT, WELL_LON),
    pumping_rate_m3_day=2000.0,   # positive magnitude (adapter applies WEL sign)
    sim_years=5,
)
log.info("deck built: archetype=%s transient=%s gwt_present=%s crs=%s",
         deck.archetype, deck.transient, deck.gwt_present, deck.model_crs)

# ---------------------------------------------------------------------------
# Run mf6
# ---------------------------------------------------------------------------

log.info("running mf6 ...")
stdout_p = workdir / "mf6.stdout"
stderr_p = workdir / "mf6.stderr"
with open(stdout_p, "wb") as out, open(stderr_p, "wb") as err:
    proc = subprocess.run([MF6], cwd=str(workdir), stdout=out, stderr=err, check=False)

rc = proc.returncode
lst = workdir / "mfsim.lst"
if lst.exists():
    lst_text = lst.read_text(errors="replace")
    converged = "Normal termination of simulation" in lst_text
    log.info("mf6 exit=%d converged=%s listing=%s", rc, converged, lst)
    if not converged:
        log.error("mf6 did not converge -- last 30 lines of listing:")
        print("\n".join(lst_text.splitlines()[-30:]))
        sys.exit(1)
else:
    log.error("mfsim.lst absent -- mf6 may have crashed (exit=%d)", rc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Postprocess drawdown
# ---------------------------------------------------------------------------

log.info("postprocessing drawdown ...")
from unittest.mock import patch

# We need boto3 pointing at MinIO.  The env vars set:
#   AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, GRACE2_RUNS_BUCKET
# boto3 honors AWS_ENDPOINT_URL natively -- no patching needed.
from grace2_agent.workflows import postprocess_modflow as pp

# For MinIO we need the bucket to exist; create it via boto3 if missing.
runs_bucket = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
try:
    s3.head_bucket(Bucket=runs_bucket)
    log.info("bucket %s exists", runs_bucket)
except Exception:
    log.info("creating bucket %s ...", runs_bucket)
    try:
        s3.create_bucket(Bucket=runs_bucket)
    except Exception as exc:
        log.warning("create_bucket failed (may already exist): %s", exc)

run_id = f"fresno-sy-{Path(workdir).name[-8:]}"
log.info("run_id: %s", run_id)

# Skip publish_layer (TiTiler wms_url step) -- we'll verify with a direct tile URL
layer = pp.postprocess_drawdown(
    str(workdir),          # run_outputs_uri = local file path
    run_id=run_id,
    model_crs=deck.model_crs,
    deck_dir=str(workdir),
    runs_bucket=runs_bucket,
    publish=False,         # skip publish_layer WMS registration
)

log.info("DrawdownLayerURI: %s", layer)
log.info("max_drawdown_m: %.4f", layer.max_drawdown_m)
log.info("cog_uri: %s", layer.uri)

# ---------------------------------------------------------------------------
# Upload COG to MinIO
# ---------------------------------------------------------------------------

cog_local = layer.uri.replace("file://", "")
cog_key = f"{run_id}/drawdown.tif"
log.info("uploading COG to s3://%s/%s ...", runs_bucket, cog_key)

if Path(cog_local).exists():
    with open(cog_local, "rb") as fh:
        s3.put_object(Bucket=runs_bucket, Key=cog_key, Body=fh.read())
    log.info("upload OK -- s3://%s/%s", runs_bucket, cog_key)
    cog_s3_uri = f"s3://{runs_bucket}/{cog_key}"
else:
    log.warning("local COG not found at %s -- checking workdir", cog_local)
    tifs = list(workdir.glob("*.tif"))
    if tifs:
        cog_local = str(tifs[0])
        with open(cog_local, "rb") as fh:
            s3.put_object(Bucket=runs_bucket, Key=cog_key, Body=fh.read())
        log.info("upload OK (fallback) -- s3://%s/%s", runs_bucket, cog_key)
        cog_s3_uri = f"s3://{runs_bucket}/{cog_key}"
    else:
        cog_s3_uri = "NOT_FOUND"
        log.error("no COG tif found in workdir %s", workdir)

# ---------------------------------------------------------------------------
# Write result summary
# ---------------------------------------------------------------------------

PROOF_DIR = Path(__file__).parent.parent / "docs" / "proof"
PROOF_DIR.mkdir(parents=True, exist_ok=True)

result = {
    "run_id": run_id,
    "archetype": "sustainable_yield",
    "location": {"lat": FRESNO_LAT, "lon": FRESNO_LON},
    "mf6_binary": MF6,
    "workdir": str(workdir),
    "listing_file": str(lst),
    "converged": True,
    "max_drawdown_m": layer.max_drawdown_m,
    "model_crs": deck.model_crs,
    "cog_s3_uri": cog_s3_uri,
    "cog_local": cog_local,
    "tile_server_base": os.environ.get("GRACE2_TILE_SERVER_BASE", "http://127.0.0.1:8080"),
}

artifacts_path = PROOF_DIR / "artifacts.txt"
with open(artifacts_path, "w") as fh:
    json.dump(result, fh, indent=2)
    fh.write("\n\n--- MODFLOW listing tail (last 20 lines) ---\n")
    fh.write("\n".join(lst.read_text(errors="replace").splitlines()[-20:]))

log.info("artifacts written to %s", artifacts_path)
print("\n=== MODFLOW direct run COMPLETE ===")
print(json.dumps(result, indent=2))
