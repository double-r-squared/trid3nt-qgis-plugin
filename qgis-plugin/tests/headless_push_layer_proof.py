"""Live-shaped proof: bidirectional layer push (2026-07-11).

Proves the FULL plugin-side flow -- temp export -> upload POST ->
ingest POST -> success note -- by driving the REAL, unmodified
``push_layer.py`` (``push_exported_file`` / ``format_push_note``) against a
STUB HTTP server that mirrors the two agent routes' real contracts
(``server/src/trid3nt_server/tool_catalog_http.py``):

    POST /api/ingest-layer-file?filename=<name>  (raw octet-stream body)
      -> 200 {"s3_uri": "s3://..."}
    POST /api/ingest-layer {"case_id","name","kind","s3_uri",
      "crs_authid"?,"make_aoi"?}
      -> 200 {"status":"ok","layer_id",...,"aoi_pinned","feature_count"}

The "temp export" step (the ONE QGIS-touching piece,
``export_active_layer_to_tempfile``) is SIMULATED here by writing a small
GeoPackage-shaped file directly to disk -- proving that half requires a real
QGIS session (see ``headless_mesh_proof.py`` for that heavier pattern); this
proof covers everything downstream of "a file exists on disk", which is
where ``_PushLayerTask._run`` hands off from PyQGIS to pure Python.

Additionally, if a REAL agent is reachable at ``TRID3NT_AGENT_HTTP`` (default
``http://127.0.0.1:8766``) AND its ``/api/ingest-layer-file`` route responds
(not a 404 -- the route needs a restart to pick up this change, per the
kickoff's "do NOT restart the running agent" constraint), the SAME flow is
re-run against the live routes too, so this proof becomes the live-route
verification the moment the coordinator restarts the box -- no script
changes needed, just re-run:

    python3 tests/headless_push_layer_proof.py

Run (no live agent needed):  python3 tests/headless_push_layer_proof.py
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

from trid3nt.case import push_layer  # noqa: E402

AGENT_HTTP = os.environ.get("TRID3NT_AGENT_HTTP", "http://127.0.0.1:8766")
CASE_ID = os.environ.get("TRID3NT_CASE_ID", "01KWRSGHJV4Q5R6SWDGNRZDYJS")

failures: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        failures.append(label)


def _run_flow(base_url: str, label: str, *, allow_route_absent: bool = False) -> bool:
    """Drive the REAL ``push_layer.push_exported_file`` + ``format_push_note``
    against ``base_url``, asserting the full request/response contract.

    Returns True iff the route responded (i.e. the flow was actually
    exercised). When ``allow_route_absent`` is True (the LIVE half only), an
    ``HTTP 404``/``HTTP 405`` failure is treated as "not deployed yet" -- a
    NOTE, not a counted failure -- since that is the EXPECTED state before
    the coordinator restarts the agent process (this session was told not
    to). Any other failure (once the route exists) is a real FAIL.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".gpkg", prefix="trid3nt_push_proof_")
    os.close(fd)
    os.unlink(tmp_path)
    # A REAL 1-feature GeoPackage: the live route validates content (a fake
    # byte string draws an honest 400), so build genuine bytes via ogr.
    from osgeo import ogr, osr

    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(tmp_path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ogr_lyr = ds.CreateLayer("proof", srs, ogr.wkbPolygon)
    feat = ogr.Feature(ogr_lyr.GetLayerDefn())
    feat.SetGeometry(ogr.CreateGeometryFromWkt(
        "POLYGON((-122.5 45.5,-122.4 45.5,-122.4 45.6,-122.5 45.6,-122.5 45.5))"
    ))
    ogr_lyr.CreateFeature(feat)
    ds = None

    print(f"\n[proof] {label}: pushing a simulated {tmp_path} to case={CASE_ID}", flush=True)
    try:
        result = push_layer.push_exported_file(
            base_url,
            CASE_ID,
            tmp_path,
            "vector",
            "Proof AOI polygon",
            crs_authid="EPSG:4326",
            make_aoi=True,
        )
    except push_layer.PushLayerRequestError as exc:
        message = str(exc)
        route_absent = "HTTP 404" in message or "HTTP 405" in message
        if allow_route_absent and route_absent:
            print(
                f"[proof] {label}: route not deployed yet ({message}) -- "
                "EXPECTED pre-restart, not counted as a failure.",
                flush=True,
            )
            check(f"{label}: temp file removed after the attempt", not os.path.exists(tmp_path))
            return False
        check(f"{label}: push_exported_file succeeds", False, message)
        check(f"{label}: temp file removed after the attempt", not os.path.exists(tmp_path))
        return True

    check(f"{label}: push_exported_file returns status=ok", result.get("status") == "ok", str(result))
    check(f"{label}: temp file removed after upload+ingest", not os.path.exists(tmp_path))
    check(f"{label}: response carries a layer_id", bool(result.get("layer_id")))
    check(f"{label}: response layer_type == vector", result.get("layer_type") == "vector")

    note = push_layer.format_push_note("Proof AOI polygon", result)
    print(f"[proof] {label}: dock note -> {note!r}", flush=True)
    check(f"{label}: note names the layer", note.startswith("'Proof AOI polygon' pushed to case"))
    return True


# --------------------------------------------------------------------------- #
# HALF 1: STUB routes (always runs -- no live agent required)
# --------------------------------------------------------------------------- #


class _IngestStub(http.server.BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""

        if path == "/api/ingest-layer-file":
            import urllib.parse as _up

            params = _up.parse_qs(qs)
            filename = (params.get("filename") or [""])[0]
            if not filename or not raw_body:
                self._json(400, {"error": "missing filename or body"})
                return
            self._json(
                200, {"s3_uri": f"s3://cache/user-uploads/01PROOFULID/{filename}"}
            )
            return

        if path == "/api/ingest-layer":
            body = json.loads(raw_body) if raw_body else {}
            if not body.get("case_id") or not body.get("s3_uri"):
                self._json(400, {"error": "missing case_id or s3_uri"})
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "layer_id": "user-01PROOF",
                    "name": body.get("name"),
                    "layer_type": body.get("kind"),
                    "uri": "s3://runs/case-data/{}/user-01PROOF.fgb".format(
                        body.get("case_id")
                    ),
                    "bbox": [-122.5, 45.5, -122.4, 45.6],
                    "aoi_pinned": bool(body.get("make_aoi")),
                    "feature_count": 1,
                },
            )
            return

        self._json(404, {"error": "not found"})

    def log_message(self, *args):  # silence
        pass


print("[proof] HALF 1: stub agent routes (proves the plugin-side flow offline)", flush=True)
httpd = http.server.HTTPServer(("127.0.0.1", 0), _IngestStub)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
stub_base = f"http://127.0.0.1:{httpd.server_address[1]}"
try:
    _run_flow(stub_base, "STUB")
finally:
    httpd.shutdown()

# --------------------------------------------------------------------------- #
# HALF 2: the CURRENTLY RUNNING local agent, IF it already serves the new
# routes (skipped -- not failed -- if unreachable or still pre-restart
# 404/405, per the kickoff's "do NOT restart the running agent" constraint).
# --------------------------------------------------------------------------- #

print(f"\n[proof] HALF 2: attempting the real flow against the live agent at {AGENT_HTTP} ...", flush=True)
live_route_present = False
try:
    live_route_present = _run_flow(AGENT_HTTP, "LIVE", allow_route_absent=True)
except Exception as exc:  # noqa: BLE001 -- e.g. agent totally unreachable
    print(f"[proof] live agent unreachable at {AGENT_HTTP} ({exc}) -- skipping HALF 2", flush=True)

if not live_route_present:
    print(
        "[proof] NOTE: live agent does not yet serve /api/ingest-layer(-file) "
        "(404/405, or unreachable) -- this is EXPECTED before the coordinator "
        "restarts the agent process (this session was told NOT to restart "
        "it). RE-RUN THIS PROOF after that restart -- HALF 2 will then "
        "exercise the real routes with ZERO script changes.",
        flush=True,
    )

print(
    f"\n[proof] LIVE-ROUTE VERIFICATION: {'confirmed against the running agent' if live_route_present else 'PENDING -- re-run this script after the agent restart'}",
    flush=True,
)

if failures:
    print(f"\n[proof] DONE -- {len(failures)} FAILURE(S): {failures}", flush=True)
    sys.exit(1)
print("\n[proof] DONE -- ALL CHECKS PASSED", flush=True)
sys.exit(0)
