"""Live proof: MDAL mesh integration phase 1 (2026-07-11).

Proves BOTH halves of the mesh feature against REAL data:

  AGENT HALF   POSTs ``/api/export-qgis`` on the CURRENTLY RUNNING local
               agent (``ws://127.0.0.1:8765`` -> HTTP ``:8766``) for a REAL
               case that has a SFINCS flood-depth layer, and reports whether
               the response already carries the new additive ``"mesh"``
               list. The running agent process predates this change (it was
               NOT restarted by this proof -- restarting the box is NOT this
               script's job), so this half is expected to print a NOTE
               (mesh absent) rather than a hard failure -- it documents
               exactly what to re-check after the next agent restart/deploy.

  PLUGIN HALF  Drives the REAL plugin code (``case_export.py`` +
               ``layers.py``, unmodified from what ships) against a mesh
               export entry built from values INDEPENDENTLY VERIFIED against
               the real agent-side ``export_case_to_qgis`` code path (run
               directly, offline, against the real case's persisted layers
               and the real MinIO runs bucket -- see the session's
               evidence): the real ``s3_uri``, the real ``crs_authid``
               (``EPSG:32616``, read off the mesh file's own CRS variable),
               and the real dataset-group set (19 groups, including the
               ``maximum_water_depth_timemax:*`` series).

               ``case_export.download_mesh_file`` fetches the REAL
               ``sfincs_map.nc`` from the REAL MinIO endpoint (anonymous
               path-style HTTP GET -- proven reachable), then
               ``LayerMaterializer.materialize_export`` loads it as a
               NATIVE ``QgsMeshLayer`` inside a REAL offscreen QGIS
               (``QT_QPA_PLATFORM=offscreen``), asserting: the mesh is
               valid, its CRS was set explicitly (MDAL reports an empty one
               on its own -- proven live 2026-07-10 in this same
               investigation), the active scalar dataset group is the
               ``maximum_water_depth_timemax`` group with the LARGEST time
               suffix (the true peak -- NOT just "the last one MDAL
               enumerates", which is a smaller value here), and the layer
               lands inside the plugin's own "TRID3NT export ..." group
               (never floating loose in the project).

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_mesh_proof.py

Env overrides: TRID3NT_AGENT_HTTP (default http://127.0.0.1:8766),
TRID3NT_CASE_ID / TRID3NT_RUN_ID (default the real dev case+run this proof
was authored against), TRID3NT_MINIO_ENDPOINT (default
http://100.92.163.46:9000).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

AGENT_HTTP = os.environ.get("TRID3NT_AGENT_HTTP", "http://127.0.0.1:8766")
CASE_ID = os.environ.get("TRID3NT_CASE_ID", "01KWRSGHJV4Q5R6SWDGNRZDYJS")
RUN_ID = os.environ.get("TRID3NT_RUN_ID", "01KWRSKE771W6XVDJRSQDXZYSY")
MINIO_ENDPOINT = os.environ.get("TRID3NT_MINIO_ENDPOINT", "http://100.92.163.46:9000")

# Independently verified against the real agent-side export_case_to_qgis
# code path + the real MinIO object (see the session's evidence -- run
# directly via the agent's .venv against the server's updated
# tools/export_case_to_qgis.py, NOT through the (unrestarted) live agent
# process): s3_uri, crs_authid, and name are the EXACT values the live
# route will return once the box is restarted/redeployed with this change.
KNOWN_MESH_ENTRY = {
    "kind": "mesh",
    "format": "sfincs_map_netcdf",
    "s3_uri": f"s3://trid3nt-runs/{RUN_ID}/sfincs_map.nc",
    "crs_authid": "EPSG:32616",
    "name": f"SFINCS mesh ({RUN_ID[:8]})",
}

failures: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        failures.append(label)


# --------------------------------------------------------------------------- #
# AGENT HALF: does the CURRENTLY RUNNING agent's export route serve mesh yet?
# --------------------------------------------------------------------------- #

print(f"[proof] AGENT HALF: POST {AGENT_HTTP}/api/export-qgis case_id={CASE_ID}", flush=True)
agent_result = None
agent_reachable = False
try:
    body = json.dumps({"case_id": CASE_ID}).encode("utf-8")
    req = urllib.request.Request(
        f"{AGENT_HTTP}/api/export-qgis",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        agent_result = json.loads(resp.read().decode("utf-8"))
    agent_reachable = True
except (urllib.error.URLError, OSError, TimeoutError) as exc:
    print(f"[proof] agent unreachable at {AGENT_HTTP} ({exc}) -- skipping agent half", flush=True)

if agent_reachable:
    check("live agent export route responds", agent_result is not None and agent_result.get("status") in ("ok", "partial"))
    live_mesh = (agent_result or {}).get("mesh")
    if live_mesh:
        print(
            f"[proof] NOTE: live agent ALREADY serves mesh (post-restart) -- {live_mesh}",
            flush=True,
        )
        KNOWN_MESH_ENTRY.update(live_mesh[0])
    else:
        print(
            "[proof] NOTE: live agent export result has NO 'mesh' key/empty list -- "
            "the running agent process predates this change and needs a restart/"
            "redeploy to pick it up. RE-RUN THIS PROOF after that restart to "
            "confirm the live route itself (not just the values below, which were "
            "independently verified offline against the same real case+MinIO data).",
            flush=True,
        )
else:
    print("[proof] NOTE: agent half skipped entirely -- plugin half still runs on known-real values.", flush=True)

# --------------------------------------------------------------------------- #
# PLUGIN HALF: real case_export.py + layers.py, real MinIO fetch, real QGIS
# --------------------------------------------------------------------------- #

from trid3nt.case import case_export  # noqa: E402

print(f"[proof] PLUGIN HALF: downloading {KNOWN_MESH_ENTRY['s3_uri']} via MinIO http form", flush=True)
mesh_dir = tempfile.mkdtemp(prefix="trid3nt_mesh_proof_")
try:
    local_nc = case_export.download_mesh_file(
        MINIO_ENDPOINT, KNOWN_MESH_ENTRY["s3_uri"], mesh_dir, timeout=60
    )
    check("mesh .nc downloaded from real MinIO", os.path.isfile(local_nc), local_nc)
    check(
        "downloaded file is non-trivially sized",
        os.path.getsize(local_nc) > 100_000,
        f"{os.path.getsize(local_nc)} bytes",
    )
except case_export.ExportRequestError as exc:
    check("mesh .nc downloaded from real MinIO", False, str(exc))
    local_nc = None

result = {
    "status": "ok",
    "mesh": [dict(KNOWN_MESH_ENTRY, local_path=local_nc)],
}
plan = case_export.plan_export_layers(result)
check(
    "plan_export_layers parsed the mesh entry",
    len(plan.mesh_entries) == 1 and plan.mesh_entries[0]["local_path"] == local_nc,
    str(plan.mesh_entries),
)
check(
    "plan_export_layers resolved crs_authid",
    plan.mesh_entries[0].get("crs_authid") == "EPSG:32616" if plan.mesh_entries else False,
)

from qgis.core import QgsApplication  # noqa: E402

qgs = QgsApplication([], False)
qgs.initQgis()

from trid3nt.render.layers import LayerMaterializer  # noqa: E402
from trid3nt.plugin_settings import PluginSettings  # noqa: E402

materializer = LayerMaterializer(settings=PluginSettings())
notes = materializer.materialize_export(plan, group_label=CASE_ID[:8])
print("[proof] materialize_export notes:", flush=True)
for n in notes:
    print(f"         - {n}", flush=True)

mesh_layers = [
    l for l in materializer.last_added_layers if l.__class__.__name__ == "QgsMeshLayer"
]
check("exactly one QgsMeshLayer materialized", len(mesh_layers) == 1, str(len(mesh_layers)))

if mesh_layers:
    mesh_layer = mesh_layers[0]
    check("mesh layer is valid (MDAL opened it)", mesh_layer.isValid())
    check(
        "mesh layer CRS was set explicitly",
        mesh_layer.crs().isValid() and mesh_layer.crs().authid() == "EPSG:32616",
        mesh_layer.crs().authid(),
    )
    check(
        "mesh carries 19 dataset groups (bed_level..water_level)",
        mesh_layer.datasetGroupCount() == 19,
        str(mesh_layer.datasetGroupCount()),
    )

    from qgis.core import QgsMeshDatasetIndex

    active = mesh_layer.rendererSettings().activeScalarDatasetGroup()
    active_name = (
        mesh_layer.datasetGroupMetadata(QgsMeshDatasetIndex(active, 0)).name()
        if active is not None and active >= 0
        else None
    )
    print(f"[proof] active scalar dataset group: index={active} name={active_name!r}", flush=True)
    check(
        "active dataset group is the LARGEST maximum_water_depth_timemax (peak depth)",
        active_name == "maximum_water_depth_timemax:3600",
        f"got {active_name!r}",
    )

    from qgis.core import QgsProject

    export_group = (
        QgsProject.instance().layerTreeRoot().findGroup(f"TRID3NT export {CASE_ID[:8]}")
    )
    check("export group exists", export_group is not None)
    if export_group is not None:
        direct_layer_ids = {
            c.layer().id() for c in export_group.children() if hasattr(c, "layer")
        }
        check(
            "mesh layer sits inside the case's TRID3NT export group",
            mesh_layer.id() in direct_layer_ids,
        )
else:
    check("mesh layer CRS was set explicitly", False, "no mesh layer materialized")
    check("mesh carries 19 dataset groups (bed_level..water_level)", False)
    check("active dataset group is the LARGEST maximum_water_depth_timemax (peak depth)", False)
    check("export group exists", False)
    check("mesh layer sits inside the case's TRID3NT export group", False)

qgs.exitQgis()

print(
    f"\n[proof] AGENT HALF: {'confirmed live post-restart' if agent_reachable and (agent_result or {}).get('mesh') else 'NEEDS a post-restart/redeploy re-run to confirm the live route'}",
    flush=True,
)

if failures:
    print(f"\n[proof] DONE -- {len(failures)} FAILURE(S): {failures}", flush=True)
    sys.exit(1)
print("\n[proof] DONE -- ALL CHECKS PASSED", flush=True)
sys.exit(0)
