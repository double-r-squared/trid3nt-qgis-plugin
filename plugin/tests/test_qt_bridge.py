"""The real Qt bridge wiring, under a real QCoreApplication."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from stub_server import StubAgentServer  # noqa: E402

# --------------------------------------------------------------------------- #
# REAL Qt bridge wiring (subprocess -- the layer the stdlib tests bypass)
# --------------------------------------------------------------------------- #


@pytest.mark.qt_harness_shim
class TestQtBridgeStart(unittest.TestCase):
    """Exercises ``AgentBridge.start`` under a REAL QCoreApplication.

    A pyqtSignal named for a C++ virtual makes the first delivered QEvent abort the
    host process, which only a real Qt object tree catches; runs in a subprocess."""

    @staticmethod
    def _qt_python() -> str | None:
        candidates = []
        which = shutil.which("python3")
        if which:
            candidates.append(which)
        candidates.append("/usr/bin/python3")
        for py in dict.fromkeys(candidates):
            if not os.path.exists(py):
                continue
            try:
                probe = subprocess.run(
                    [py, "-c", "from qgis.PyQt.QtCore import QCoreApplication"],
                    capture_output=True,
                    timeout=60,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return py
        return None

    def test_bridge_start_survives_real_qt_event_delivery(self):
        py = self._qt_python()
        if py is None:
            self.skipTest("no interpreter with qgis.PyQt available")
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        harness = os.path.join(os.path.dirname(__file__), "qt_bridge_harness.py")
        proc = subprocess.run(
            [py, "-u", harness, server.url],
            capture_output=True,
            timeout=120,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode,
            0,
            "qt bridge harness died (rc="
            f"{proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("QT-BRIDGE-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
