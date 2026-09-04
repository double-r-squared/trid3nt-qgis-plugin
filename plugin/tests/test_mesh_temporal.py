"""The layer clocks and the declared style, against REAL QGIS.

The collapse this covers only pays off if the platform really does the work:
MDAL owns a mesh's time axis, QGIS owns the render, and this side only states
facts. So the proof runs in a subprocess on the system interpreter (the one
with ``qgis.core``) over a real SELAFIN, and skips honestly when no such
interpreter exists -- the same tier as ``TestQtBridgeStart``.

The harness itself carries what is asserted; this module owns finding an
interpreter and reading its verdict.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

#: A solved rain-on-grid result kept as a rendering proof. Read-only here.
_SELAFIN = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "proof", "templates",
    "rog_run_products", "coweeta_full_results.slf")


class TestQtMeshTemporalAndDeclaredStyle(unittest.TestCase):
    @staticmethod
    def _qgis_python():
        candidates = [p for p in (shutil.which("python3"), "/usr/bin/python3") if p]
        for py in dict.fromkeys(candidates):
            if not os.path.exists(py):
                continue
            try:
                probe = subprocess.run(
                    [py, "-c", "import qgis.core"],
                    capture_output=True,
                    timeout=60,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return py
        return None

    def test_the_run_states_its_clock_and_the_preset_states_its_render(self):
        py = self._qgis_python()
        if py is None:
            self.skipTest("no interpreter with qgis.core available")
        if not os.path.exists(_SELAFIN):
            self.skipTest(f"mesh fixture missing: {_SELAFIN}")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_mesh_temporal_harness.py")
        proc = subprocess.run(
            [py, "-u", harness, os.path.abspath(_SELAFIN)],
            capture_output=True,
            timeout=300,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode,
            0,
            "qt mesh-temporal harness died (rc="
            f"{proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("QT-MESH-TEMPORAL-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
