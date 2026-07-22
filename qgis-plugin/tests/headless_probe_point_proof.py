"""Live-shaped proof: map-click point probe (2026-07-11).

Proves the FULL plugin-side flow -- POST /api/probe-point -> dock note-block
formatting -- by driving the REAL, unmodified ``probe.py``
(``post_probe_point`` / ``format_probe_result``) against a STUB HTTP server
that mirrors the agent route's real contract (services/agent
``tool_catalog_http.py`` + ``tools/probe_point.py``):

    POST /api/probe-point {"case_id","lon","lat"}
      -> 200 {"status":"ok","point":{"lon","lat"},"case_id","results":[
              {"layer_id","name","value","units"?,"note"?,"error"?} |
              {"name","series":[...],"units"?,"layer_ids"}
            ],"truncated","computed_at"}

The ONE PyQGIS-touching piece (``QgsMapToolEmitPoint`` install/restore + the
canvas-CRS -> EPSG:4326 point transform, both in ``dock.py``'s
``_toggle_probe_tool`` / ``_point_to_lonlat4326``) is NOT exercised here --
this proof covers everything downstream of "a click resolved to an EPSG:4326
point", which is where ``_ProbePointTask._run`` hands off from PyQGIS to
pure Python.

Additionally, if a REAL agent is reachable at ``TRID3NT_AGENT_HTTP`` (default
``http://127.0.0.1:8766``) AND its ``/api/probe-point`` route responds (not a
404 -- the route needs a restart to pick up this change, per the kickoff's
"do NOT restart the running agent" constraint), the SAME flow is re-run
against the live route too, so this proof becomes the live-route
verification the moment the coordinator restarts the box -- no script
changes needed, just re-run:

    python3 tests/headless_probe_point_proof.py

Run (no live agent needed):  python3 tests/headless_probe_point_proof.py
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

from trid3nt.render import probe  # noqa: E402

AGENT_HTTP = os.environ.get("TRID3NT_AGENT_HTTP", "http://127.0.0.1:8766")
# The groundwater case named in the job-0308-style kickoff's live-verify
# plan (Twin Falls MODFLOW plume) -- a real point inside its plume bbox lands
# a concentration value, not just an honest empty/absent-layer response.
CASE_ID = os.environ.get("TRID3NT_CASE_ID", "01KX80KJ8TV7YZBTVKSNRH1E2K")
PROBE_LON = float(os.environ.get("TRID3NT_PROBE_LON", "-114.35"))
PROBE_LAT = float(os.environ.get("TRID3NT_PROBE_LAT", "42.55"))

failures: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        failures.append(label)


def _run_flow(
    base_url: str, label: str, lon: float, lat: float, *, allow_route_absent: bool = False
) -> bool:
    """Drive the REAL ``probe.post_probe_point`` + ``format_probe_result``
    against ``base_url``, asserting the full request/response contract.

    Returns True iff the route responded (i.e. the flow was actually
    exercised). When ``allow_route_absent`` is True (the LIVE half only), an
    ``HTTP 404``/``HTTP 405`` failure is treated as "not deployed yet" -- a
    NOTE, not a counted failure -- since that is the EXPECTED state before
    the coordinator restarts the agent process (this session was told not
    to). Any other failure (once the route exists) is a real FAIL.
    """
    print(
        f"\n[proof] {label}: probing case={CASE_ID} at ({lon}, {lat})", flush=True
    )
    try:
        result = probe.post_probe_point(base_url, CASE_ID, lon, lat)
    except probe.ProbePointRequestError as exc:
        message = str(exc)
        route_absent = "HTTP 404" in message or "HTTP 405" in message
        if allow_route_absent and route_absent:
            print(
                f"[proof] {label}: route not deployed yet ({message}) -- "
                "EXPECTED pre-restart, not counted as a failure.",
                flush=True,
            )
            return False
        # A live agent that HAS the route but has no such case (or the case
        # has no layers) is still an honest, well-formed failure/response --
        # only a route-shape fault (not-JSON, unreachable) is a hard FAIL
        # here; a typed 404 "case not found" on an unknown CASE_ID is
        # expected when TRID3NT_CASE_ID is not overridden with a real one.
        case_not_found = "HTTP 404" in message and "not found" in message.lower()
        if label == "LIVE" and case_not_found:
            print(
                f"[proof] {label}: route is live but case {CASE_ID!r} was not "
                f"found on this agent ({message}) -- set TRID3NT_CASE_ID to a "
                "real case to exercise the full value read.",
                flush=True,
            )
            return True
        check(f"{label}: post_probe_point succeeds", False, message)
        return True

    check(f"{label}: response status == ok", result.get("status") == "ok", str(result))
    check(f"{label}: response echoes the point", result.get("point") == {"lon": lon, "lat": lat})
    check(f"{label}: response carries case_id", result.get("case_id") == CASE_ID)
    check(f"{label}: results is a list", isinstance(result.get("results"), list))

    lines = probe.format_probe_result(result)
    print(f"[proof] {label}: dock note lines -> {lines!r}", flush=True)
    check(f"{label}: format_probe_result returns >=1 line", len(lines) >= 1)
    return True


# --------------------------------------------------------------------------- #
# HALF 1: STUB route (always runs -- no live agent required)
# --------------------------------------------------------------------------- #


class _ProbeStub(http.server.BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        path, _, _qs = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""

        if path == "/api/probe-point":
            body = json.loads(raw_body) if raw_body else {}
            if not body.get("case_id") or "lon" not in body or "lat" not in body:
                self._json(400, {"error": "missing case_id/lon/lat"})
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "point": {"lon": body["lon"], "lat": body["lat"]},
                    "case_id": body["case_id"],
                    "results": [
                        {
                            "layer_id": "conc-1",
                            "name": "Plume concentration",
                            "value": 4.2,
                            "units": "mg/L",
                        },
                        {
                            "name": "flood depth",
                            "series": [
                                {"label": "step 1", "value": 0.02},
                                {"label": "step 2", "value": 0.15},
                                {"label": "step 3", "value": 0.31},
                                {"label": "step 4", "value": 0.28},
                            ],
                            "units": "m",
                            "layer_ids": ["f-1", "f-2", "f-3", "f-4"],
                        },
                    ],
                    "truncated": False,
                    "computed_at": "2026-07-11T00:00:00+00:00",
                },
            )
            return

        self._json(404, {"error": "not found"})

    def log_message(self, *args):  # silence
        pass


print("[proof] HALF 1: stub agent route (proves the plugin-side flow offline)", flush=True)
httpd = http.server.HTTPServer(("127.0.0.1", 0), _ProbeStub)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
stub_base = f"http://127.0.0.1:{httpd.server_address[1]}"
try:
    _run_flow(stub_base, "STUB", PROBE_LON, PROBE_LAT)
finally:
    httpd.shutdown()

# --------------------------------------------------------------------------- #
# HALF 2: the CURRENTLY RUNNING local agent, IF it already serves the new
# route (skipped -- not failed -- if unreachable or still pre-restart
# 404/405, per the kickoff's "do NOT restart the running agent" constraint).
# --------------------------------------------------------------------------- #

print(f"\n[proof] HALF 2: attempting the real flow against the live agent at {AGENT_HTTP} ...", flush=True)
live_route_present = False
try:
    live_route_present = _run_flow(
        AGENT_HTTP, "LIVE", PROBE_LON, PROBE_LAT, allow_route_absent=True
    )
except Exception as exc:  # noqa: BLE001 -- e.g. agent totally unreachable
    print(f"[proof] live agent unreachable at {AGENT_HTTP} ({exc}) -- skipping HALF 2", flush=True)

if not live_route_present:
    print(
        "[proof] NOTE: live agent does not yet serve /api/probe-point (404/405, "
        "or unreachable) -- this is EXPECTED before the coordinator restarts "
        "the agent process (this session was told NOT to restart it). RE-RUN "
        "THIS PROOF after that restart -- HALF 2 will then exercise the real "
        "route with ZERO script changes.",
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
