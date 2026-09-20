"""A layer-request answered by the session: the pure client seam offline, and
the QGIS-bound open/export in a subprocess under the interpreter that carries
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
    LAYER_REQUEST_ROW,
    STUB_LAYER_REQUEST_KEY,
    StubAgentServer,
)


class TestLayerRequestRoundTrip(unittest.TestCase):
    """The borrowed-provider request surfaces as its own kind and the response
    resumes the paused turn."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("layer request test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_open_request_and_response(self):
        self.client.send_chat("please open-layer the drought monitor")
        ev = self._await_kind("layer-request")
        self.assertEqual(ev.data["key"], STUB_LAYER_REQUEST_KEY)
        self.assertEqual(ev.data["provider"], LAYER_REQUEST_ROW["provider"])
        self.assertEqual(ev.data["uri"], LAYER_REQUEST_ROW["uri"])
        self.assertEqual(ev.data["mode"], "open")
        self.client.send_layer_response(ev.data["key"])
        chunk = self._await_kind("chunk")
        self.assertEqual(chunk.data["delta"], "Layer opened on the map.")
        self._await_kind("turn-complete")
        sent = self.server.layer_responses[-1]
        self.assertEqual(sent, {"key": STUB_LAYER_REQUEST_KEY, "uri": None,
                                "error": None})

    def test_materialised_answer_carries_the_staged_object(self):
        self.client.send_chat("please open-layer the drought monitor")
        ev = self._await_kind("layer-request")
        self.client.send_layer_response(ev.data["key"], uri="s3://cache/staged.gpkg")
        chunk = self._await_kind("chunk")
        self.assertIn("s3://cache/staged.gpkg", chunk.data["delta"])
        self._await_kind("turn-complete")

    def test_error_response_resumes_the_turn_honestly(self):
        self.client.send_chat("please open-layer the drought monitor")
        ev = self._await_kind("layer-request")
        self.client.send_layer_response(ev.data["key"], error="Layer is not valid")
        chunk = self._await_kind("chunk")
        self.assertIn("Layer is not valid", chunk.data["delta"])
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
class TestLayerRequestInQgis(unittest.TestCase):
    """The QGIS-bound half: the provider layer opened, windowed, exported and
    uploaded, in a headless QgsApplication subprocess."""

    def test_harness(self):
        py = _qgis_python()
        if py is None:
            self.skipTest("no interpreter with qgis.core on this box")
        harness = os.path.join(os.path.dirname(__file__), "qt_layer_request_harness.py")
        proc = subprocess.run(
            [py, "-u", harness], capture_output=True, timeout=300, text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode, 0,
            f"layer-request harness failed (rc={proc.returncode})\n--- stdout ---\n"
            f"{proc.stdout}\n--- stderr ---\n{proc.stderr[-4000:]}",
        )
        self.assertIn("[layer-request] mode open put 'cells' on the map", proc.stdout)
        self.assertIn("[layer-request] mode materialise uploaded", proc.stdout)
        self.assertIn("[layer-request] an unopenable uri answers", proc.stdout)
        self.assertIn("[layer-request] an unknown mode is refused", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
