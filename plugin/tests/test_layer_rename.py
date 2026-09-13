"""The retitle, against REAL QGIS: a renamed row renames the layer in place.

QGIS owns the layer name a reader sees, so the proof runs in a subprocess on the
interpreter with ``qgis.core`` and skips honestly when none exists. It is the
canvas half of a restyle carrying a title: the server puts the new name on the
layer row, and this is what the dock does with it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

import pytest


@pytest.mark.qt_harness_shim
class TestQtLayerRename(unittest.TestCase):
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

    def test_a_row_under_a_new_name_renames_the_layer_already_on_the_map(self):
        py = self._qgis_python()
        if py is None:
            self.skipTest("no interpreter with qgis.core available")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_layer_rename_harness.py")
        proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            timeout=300,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode,
            0,
            "qt layer-rename harness died (rc="
            f"{proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("QT-LAYER-RENAME-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
