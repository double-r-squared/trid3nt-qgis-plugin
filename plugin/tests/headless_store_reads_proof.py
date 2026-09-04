"""Live proof: a template run's layers reach the canvas through ``s3://``.

Drives the REAL ``LayerMaterializer`` under a real ``QgsApplication`` against a
REAL case's persisted ``loaded_layer_summaries`` -- the same rows the dock
replays on case-open -- so nothing here is a re-implementation of the render
path. One store, one scheme, end to end:

  1. every layer reference in the run is an ``s3://`` uri (no second face);
  2. rasters and vectors register through GDAL ``/vsis3`` and stage NOTHING;
  3. the mesh takes the ONE cache hop (MDAL has no /vsi layer) and its cost is
     MEASURED and printed, not assumed;
  4. the store is private: an unsigned HTTP GET of the same object is refused,
     which is what makes the signed read the only path in.

Run (from repo root), against the RUNNING stack -- this script restarts
nothing:

    QT_QPA_PLATFORM=offscreen python3 plugin/tests/headless_store_reads_proof.py [CASE_ID]

Defaults to the most recently updated case that carries raster, vector AND
mesh rows. Exits nonzero on any failed check.
"""
import json
import os
import sys
import time
import types
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://127.0.0.1:9000")
ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID", "trid3nt")
SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "trid3nt-local-dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")
PERSISTENCE = os.environ.get(
    "TRID3NT_DEV_PERSISTENCE_DIR", os.path.join(REPO, "data", "persistence")
)

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"    {'PASS' if ok else 'FAIL'}  {message}")
    if not ok:
        failures.append(message)


def pick_case() -> tuple[str, list[dict]]:
    with open(os.path.join(PERSISTENCE, "trid3nt_dev", "projects.json")) as f:
        projects = json.load(f)
    if len(sys.argv) > 1:
        case = projects[sys.argv[1]]
        return sys.argv[1], case["loaded_layer_summaries"]
    best = None
    for case_id, case in projects.items():
        rows = case.get("loaded_layer_summaries") or []
        kinds = {r.get("layer_type") for r in rows}
        if {"raster", "vector", "mesh"} <= kinds:
            key = case.get("updated_at") or ""
            if best is None or key > best[0]:
                best = (key, case_id, rows)
    if best is None:
        raise SystemExit("no case carries raster + vector + mesh rows")
    return best[1], best[2]


case_id, rows = pick_case()
print(f"[case] {case_id} -- {len(rows)} layer rows")

print("\n[1] every layer reference is an s3:// uri")
for row in rows:
    check(
        str(row.get("uri", "")).startswith("s3://"),
        f"{row['layer_type']:6} {row['layer_id'][:44]:44} {row.get('uri', '')[:60]}",
    )

from qgis.core import QgsApplication  # noqa: E402

qgs = QgsApplication([], False)
qgs.initQgis()

from plugin.net.trid3nt_client import parse_layer_events  # noqa: E402
from plugin.render import layers as L  # noqa: E402

print("\n[2] the store is configured once, then GDAL reads natively")
note = L.configure_store_access(ENDPOINT, ACCESS_KEY, SECRET_KEY, REGION)
check(note is None, f"configure_store_access({ENDPOINT}) -> {note or 'ok'}")

materializer = L.LayerMaterializer(settings=types.SimpleNamespace())
materializer.set_case(case_id, "Store Reads Proof")
staging = materializer._ensure_temp_dir()
before = set(os.listdir(staging))

events = parse_layer_events({"loaded_layers": rows})
started = time.monotonic()
notes = materializer.materialize(events)
elapsed = time.monotonic() - started
for line in notes:
    print(f"    {line}")

kinds = {e.layer_id: e.layer_type for e in events}
loaded = {layer.customProperty("trid3nt/layer_id") for layer in materializer.last_added_layers}
check(
    len(materializer.last_added_layers) == len(events),
    f"{len(materializer.last_added_layers)}/{len(events)} rows reached the canvas "
    f"in {elapsed:.1f}s",
)
for event in events:
    check(event.layer_id in loaded, f"{event.layer_type:6} {event.layer_id[:52]} loaded")

print("\n[3] rasters and vectors stream; only the mesh takes the cache hop")
staged = sorted(set(os.listdir(staging)) - before - {".owner_pid"})
mesh_ids = [e.layer_id for e in events if e.layer_type in ("mesh", "ugrid")]
check(
    len(staged) == len(mesh_ids),
    f"staged files = {staged or '(none)'} for mesh rows {len(mesh_ids)}",
)
check(
    all("streamed via /vsis3" in n for n in notes if "raster '" in n or "vector '" in n),
    "every raster/vector note says streamed via /vsis3, no local copy",
)

for mesh_id in mesh_ids:
    uri = next(e.uri for e in events if e.layer_id == mesh_id)
    started = time.monotonic()
    copied = materializer._stage_s3_to_session(uri, "cache_hop_measure" + os.path.splitext(uri)[1])
    hop = time.monotonic() - started
    size = os.path.getsize(copied) if copied else 0
    check(
        copied is not None,
        f"cache hop {uri.rsplit('/', 1)[-1]}: {size / 1e6:.1f} MB in {hop:.2f}s",
    )

print("\n[4] the store is private -- the signed read is the only way in")
sample = next(e.uri for e in events if e.layer_type == "raster")
http = f"{ENDPOINT.rstrip('/')}/{sample[len('s3://'):]}"
try:
    with urllib.request.urlopen(http, timeout=20) as response:
        check(False, f"unsigned GET returned {response.status} -- the store is public")
except urllib.error.HTTPError as exc:
    check(exc.code == 403, f"unsigned GET of the same object -> {exc.code}")

materializer.cleanup_session()
check(not os.path.isdir(staging), "cleanup_session removed the staging dir")

qgs.exitQgis()
print(f"\n[done] {len(failures)} failed check(s)")
raise SystemExit(1 if failures else 0)
