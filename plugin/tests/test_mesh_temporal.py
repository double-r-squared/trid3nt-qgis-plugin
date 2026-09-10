"""The layer clocks and the declared style, against REAL QGIS.

MDAL owns a mesh's time axis and QGIS owns the render, so the proof runs in a
subprocess on the interpreter with ``qgis.core`` over real SELAFINs and skips
honestly when none exists. The binding is proven on the group names MDAL reports
for a real solved result, since those fixed-width names are why it is resolved."""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

import pytest

#: A solved rain-on-grid result kept as a rendering proof. Read-only here.
_SELAFIN = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "proof", "templates",
    "rog_run_products", "coweeta_full_results.slf")

#: A solved river-dye result: the same four hydrodynamic groups plus the tracer
#: the binding proof declares.
_TRACER_SELAFIN = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "proof", "templates",
    "01KZH561BN64PFA5HWZ8EYEJPM_r2d_river.slf")


@pytest.mark.qt_harness_shim
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
        for fixture in (_SELAFIN, _TRACER_SELAFIN):
            if not os.path.exists(fixture):
                self.skipTest(f"mesh fixture missing: {fixture}")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_mesh_temporal_harness.py")
        proc = subprocess.run(
            [py, "-u", harness, os.path.abspath(_SELAFIN),
             os.path.abspath(_TRACER_SELAFIN)],
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
