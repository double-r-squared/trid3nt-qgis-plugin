"""A processing-request answered by the session: the pure client seam offline,
and the QGIS-bound run in a subprocess under the interpreter that carries
``qgis.core`` and the Processing plugin, skipped honestly when absent."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    PROCESSING_ALG_REQUEST_ROW,
    STUB_PROCESSING_ALG_REQUEST_ID,
    StubAgentServer,
)


class TestProcessingRoundTrip(unittest.TestCase):
    """The algorithm request surfaces as its own kind and the response resumes
    the paused turn with the layer summary."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("processing test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_algorithm_request_and_response(self):
        self.client.send_chat("please run-algorithm the slope")
        ev = self._await_kind("processing-request")
        self.assertEqual(ev.data["request_id"], STUB_PROCESSING_ALG_REQUEST_ID)
        self.assertEqual(ev.data["kind"], "algorithm")
        self.assertEqual(ev.data["algorithm"], PROCESSING_ALG_REQUEST_ROW["algorithm"])
        self.assertEqual(ev.data["params"], {"INPUT": "DEM", "Z_FACTOR": 1.0})
        self.client.send_processing_response(
            ev.data["request_id"], "ok",
            result={"layer_name": "Slope", "kind": "raster", "band_count": 1},
        )
        chunk = self._await_kind("chunk")
        self.assertEqual(chunk.data["delta"], "Algorithm done: Slope on the map.")
        self._await_kind("turn-complete")
        sent = self.server.processing_responses[-1]
        self.assertEqual(sent["request_id"], STUB_PROCESSING_ALG_REQUEST_ID)
        self.assertEqual(sent["status"], "ok")
        self.assertEqual(sent["result"]["layer_name"], "Slope")

    def test_error_response_resumes_the_turn_honestly(self):
        self.client.send_chat("please run-algorithm the slope")
        ev = self._await_kind("processing-request")
        self.client.send_processing_response(
            ev.data["request_id"], "error", error="no layer named DEM"
        )
        chunk = self._await_kind("chunk")
        self.assertIn("no layer named DEM", chunk.data["delta"])
        self._await_kind("turn-complete")


def _qgis_python() -> str | None:
    for py in dict.fromkeys([shutil.which("python3") or "", "/usr/bin/python3"]):
        if not py or not os.path.exists(py):
            continue
        try:
            probe = subprocess.run(
                [py, "-c", "import qgis.core, osgeo.gdal"],
                capture_output=True, timeout=60,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return py
    return None


@pytest.mark.qt_harness_shim
class TestProcessingInQgis(unittest.TestCase):
    """The QGIS-bound half: native:slope over a canvas DEM by name, and a
    snippet in the session, in a headless QgsApplication subprocess."""

    def test_harness(self):
        py = _qgis_python()
        if py is None:
            self.skipTest("no interpreter with qgis.core on this box")
        harness = os.path.join(os.path.dirname(__file__), "qt_processing_harness.py")
        proc = subprocess.run(
            [py, "-u", harness], capture_output=True, timeout=300, text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode, 0,
            f"processing harness failed (rc={proc.returncode})\n--- stdout ---\n"
            f"{proc.stdout}\n--- stderr ---\n{proc.stderr[-4000:]}",
        )
        self.assertIn("[processing] native:slope over the canvas DEM", proc.stdout)
        self.assertIn("[processing] run_pyqgis snippet read the project back", proc.stdout)
        self.assertIn("[processing] a raising snippet answers with its traceback", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
