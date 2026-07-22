"""Tests for ``probe.py`` -- the map-click point probe's pure (no PyQGIS)
half: request builder, response parser, and the dock note-block formatter.

No QGIS required (mirrors ``test_push_layer.py``'s pattern): a small
``http.server.BaseHTTPRequestHandler`` stub mirrors the agent's real
``POST /api/probe-point`` route semantics (services/agent
``tool_catalog_http.py`` + ``tools/probe_point.py``), and the tests drive
``probe.post_probe_point`` / ``probe.format_probe_result`` against it. The
ONE PyQGIS-touching piece (the ``QgsMapToolEmitPoint`` install/restore) is
NOT exercised here -- see ``tests/headless_probe_point_proof.py`` for the
plugin-side flow proof.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.render import probe  # noqa: E402


# ---------------------------------------------------------------------------
# Probe-route stub -- the REAL route's semantics, in miniature
# ---------------------------------------------------------------------------


class _ProbeStub(http.server.BaseHTTPRequestHandler):
    """POST /api/probe-point with the agent's guard ladder (400 missing
    fields, 404 case not found, 200 otherwise)."""

    result: dict = {}
    status: int = 200
    last_body: dict = {}

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
            _ProbeStub.last_body = body
            self._json(self.status, self.result)
            return

        self._json(404, {"error": "not found"})

    def log_message(self, *args):  # silence
        pass


class _ProbeStubBase(unittest.TestCase):
    def setUp(self):
        _ProbeStub.result = {
            "status": "ok",
            "point": {"lon": -85.42, "lat": 29.95},
            "case_id": "01CASE",
            "results": [],
            "truncated": False,
            "computed_at": "2026-07-11T00:00:00+00:00",
        }
        _ProbeStub.status = 200
        _ProbeStub.last_body = {}
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _ProbeStub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"


# ---------------------------------------------------------------------------
# post_probe_point
# ---------------------------------------------------------------------------


class TestPostProbePoint(_ProbeStubBase):
    def test_happy_path_sends_case_lon_lat(self):
        result = probe.post_probe_point(self.base, "01CASE", -85.42, 29.95)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(_ProbeStub.last_body["case_id"], "01CASE")
        self.assertEqual(_ProbeStub.last_body["lon"], -85.42)
        self.assertEqual(_ProbeStub.last_body["lat"], 29.95)

    def test_case_not_found_404_surfaced(self):
        _ProbeStub.status = 404
        _ProbeStub.result = {"error": "case '01GONE' not found."}
        with self.assertRaises(probe.ProbePointRequestError) as ctx:
            probe.post_probe_point(self.base, "01GONE", -85.42, 29.95)
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_bad_request_400_surfaced(self):
        with self.assertRaises(probe.ProbePointRequestError) as ctx:
            probe.post_probe_point(self.base, "", -85.42, 29.95)
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_unreachable_host(self):
        with self.assertRaises(probe.ProbePointRequestError) as ctx:
            probe.post_probe_point(
                "http://127.0.0.1:1", "01CASE", -85.42, 29.95, timeout=1.0
            )
        self.assertIn("unreachable", str(ctx.exception))


# ---------------------------------------------------------------------------
# format_probe_result
# ---------------------------------------------------------------------------


class TestFormatProbeResult(unittest.TestCase):
    def test_no_results_honest_line(self):
        lines = probe.format_probe_result({"results": []})
        self.assertEqual(lines, ["No layers to probe at this point."])

    def test_single_layer_with_units(self):
        lines = probe.format_probe_result(
            {"results": [{"layer_id": "l-1", "name": "Elevation", "value": 12.345, "units": "m"}]}
        )
        self.assertEqual(lines, ["Elevation: 12.3 m"])

    def test_single_layer_no_units(self):
        lines = probe.format_probe_result(
            {"results": [{"layer_id": "l-1", "name": "Score", "value": 0.5}]}
        )
        self.assertEqual(lines, ["Score: 0.5"])

    def test_single_layer_outside_bounds_note(self):
        lines = probe.format_probe_result(
            {
                "results": [
                    {
                        "layer_id": "l-1",
                        "name": "Elevation",
                        "value": None,
                        "note": "point outside the layer extent",
                    }
                ]
            }
        )
        self.assertEqual(lines, ["Elevation: point outside the layer extent"])

    def test_single_layer_error(self):
        lines = probe.format_probe_result(
            {
                "results": [
                    {
                        "layer_id": "l-1",
                        "name": "Broken",
                        "value": None,
                        "error": "FileNotFoundError: gone.tif",
                    }
                ]
            }
        )
        self.assertEqual(lines, ["Broken: FileNotFoundError: gone.tif"])

    def test_series_entry_chain_and_peak(self):
        lines = probe.format_probe_result(
            {
                "results": [
                    {
                        "name": "flood depth",
                        "units": "m",
                        "series": [
                            {"label": "step 1", "value": 0.02},
                            {"label": "step 2", "value": 0.15},
                            {"label": "step 3", "value": 0.31},
                            {"label": "step 4", "value": 0.28},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(
            lines,
            ["flood depth: 0.02 -> 0.15 -> 0.31 -> 0.28 m (4 steps, peak 0.31)"],
        )

    def test_series_entry_with_gap_shows_dashes(self):
        lines = probe.format_probe_result(
            {
                "results": [
                    {
                        "name": "flood depth",
                        "units": "m",
                        "series": [
                            {"label": "step 1", "value": 0.1},
                            {"label": "step 2", "value": None, "note": "nodata at this point"},
                            {"label": "step 3", "value": 0.3},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(
            lines, ["flood depth: 0.1 -> -- -> 0.3 m (3 steps, peak 0.3)"]
        )

    def test_series_entry_all_null_no_data(self):
        lines = probe.format_probe_result(
            {
                "results": [
                    {
                        "name": "flood depth",
                        "series": [
                            {"label": "step 1", "value": None},
                            {"label": "step 2", "value": None},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(lines, ["flood depth: no data (2 steps)"])

    def test_mixed_single_and_series(self):
        lines = probe.format_probe_result(
            {
                "results": [
                    {"layer_id": "dem-1", "name": "Elevation", "value": 12.0, "units": "m"},
                    {
                        "name": "flood depth",
                        "units": "m",
                        "series": [
                            {"label": "step 1", "value": 0.1},
                            {"label": "step 2", "value": 0.2},
                        ],
                    },
                ]
            }
        )
        self.assertEqual(
            lines,
            [
                "Elevation: 12 m",
                "flood depth: 0.1 -> 0.2 m (2 steps, peak 0.2)",
            ],
        )

    def test_truncated_appends_honest_line(self):
        lines = probe.format_probe_result(
            {
                "results": [{"layer_id": "l-1", "name": "A", "value": 1.0}],
                "truncated": True,
            }
        )
        self.assertEqual(lines[-1], "(case has more raster layers than this probe samples -- some were skipped)")


class TestProbeLocationLabel(unittest.TestCase):
    def test_formats_five_decimals(self):
        self.assertEqual(probe.probe_location_label(-85.42, 29.95), "(-85.42000, 29.95000)")


if __name__ == "__main__":
    unittest.main()
