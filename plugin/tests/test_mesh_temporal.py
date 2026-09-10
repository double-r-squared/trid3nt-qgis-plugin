"""The layer clocks and the declared style, against REAL QGIS.

MDAL owns a mesh's time axis and QGIS owns the render, so the proof runs in a
subprocess on the interpreter with ``qgis.core`` over real SELAFINs and skips
honestly when none exists. The binding is proven on the group names MDAL reports
for a real solved result, since those fixed-width names are why it is resolved."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import unittest

import pytest

#: Where the solver leaves a run directory on THIS MACHINE. A solved result is a
#: run product, and a run product is transient: git carries none, so the fixtures
#: are found here and the proof skips honestly when the machine has made no run.
_RUNS_DIR = os.environ.get("TRID3NT_RUNS_DIR") or os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "runs")

#: The rain-on-grid result: the four hydrodynamic groups on a time axis.
_ROG_RESULT = "r2d_rog.slf"
#: The river result: those same groups plus the DYE tracer the binding proof
#: declares. Every river run advects dye, so the basename is the whole selector.
_TRACER_RESULT = "r2d_river.slf"


def _newest(result: str) -> str | None:
    """The newest local run's ``result`` file, or ``None`` when there is none."""
    found = glob.glob(os.path.join(_RUNS_DIR, "*", result))
    return max(found, key=os.path.getmtime) if found else None


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
        fixtures = [_newest(name) for name in (_ROG_RESULT, _TRACER_RESULT)]
        if not all(fixtures):
            self.skipTest(
                f"no solved {_ROG_RESULT} and {_TRACER_RESULT} under {_RUNS_DIR}; "
                "drive the rain-on-grid and river-dye canaries first")
        harness = os.path.join(
            os.path.dirname(__file__), "qt_mesh_temporal_harness.py")
        proc = subprocess.run(
            [py, "-u", harness, *(os.path.abspath(f) for f in fixtures)],
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
