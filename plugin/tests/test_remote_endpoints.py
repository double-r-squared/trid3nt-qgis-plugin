"""Remote-daemon endpoint derivation, in two offline layers.

The pure handshake parsing lives with the client tests. The Qt DOCK wiring - the
effective bases, the store's GDAL configuration, remote-mode isolation and the
settings token passthrough - runs its harness in a SUBPROCESS under the
interpreter that has ``qgis.PyQt``, skipping honestly when absent."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

import pytest


def _qt_python():
    """First interpreter that can import ``qgis.PyQt`` -- same probe shape as
    ``test_provider_config._qt_python`` / ``test_dock_ui._qt_python``."""
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


@pytest.mark.qt_harness_shim
class TestRemoteEndpointsDock(unittest.TestCase):
    """Runs ``qt_remote_endpoints_harness.py`` in the qgis.PyQt interpreter
    (subprocess)."""

    _proc = None

    @classmethod
    def setUpClass(cls):
        py = _qt_python()
        if py is None:
            return  # the test skips honestly
        harness = os.path.join(
            os.path.dirname(__file__), "qt_remote_endpoints_harness.py"
        )
        cls._proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            timeout=180,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )

    def test_remote_endpoints_dock_behaviors(self):
        if self._proc is None:
            self.skipTest("no interpreter with qgis.PyQt available")
        self.assertEqual(
            self._proc.returncode,
            0,
            msg=f"harness failed:\nSTDOUT:\n{self._proc.stdout}\n"
            f"STDERR:\n{self._proc.stderr}",
        )
        self.assertIn("REMOTE-ENDPOINTS-OK", self._proc.stdout)


if __name__ == "__main__":
    unittest.main()
