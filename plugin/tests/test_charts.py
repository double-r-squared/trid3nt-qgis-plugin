"""Charts surface tests (OpenQuake result parity, live-feedback 2026-07-13).

Two halves, mirroring the repo convention:

* PURE-PYTHON (this venv, no Qt): ``trid3nt_client.parse_charts`` -- the
  defensive ``session_state.charts`` replay parser -- plus the case-open
  carrier (``CaseOpenInfo.charts``) and the live ``chart-emission`` ->
  ``AgentEvent("chart", ...)`` dispatch.
* QT SUBPROCESS: ``qt_charts_harness.py`` under the system interpreter (the
  one with ``qgis.PyQt`` + matplotlib -- the ``test_dock_ui`` convention),
  covering the ChartsWindow rendering (log-log hazard curve, dashed rule,
  bars, paging, de-dupe, clear), its interactivity (nearest-vertex click
  inspect, locate-on-map enablement + callback) and the dock wiring (lazy
  bottom window + "Charts (N)" button + one pointer note, never a chart
  widget in the chat message list).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin import install_dependencies  # noqa: E402
from plugin.net import trid3nt_client as tc  # noqa: E402
from plugin.ui import charts as charts_mod  # noqa: E402

CHART_ROW = {
    "envelope_type": "chart-emission",
    "chart_id": "01CHARTAAAAAAAAAAAAAAAAAAA",
    "title": "Seismic hazard curve - PGA",
    "caption": "19 IML points",
    "vega_lite_spec": {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "layer": [],
    },
}


class TestParseCharts(unittest.TestCase):
    def test_valid_row_passes_through_whole(self):
        out = tc.parse_charts({"charts": [CHART_ROW]})
        self.assertEqual(out, [CHART_ROW])

    def test_missing_or_non_list_charts(self):
        self.assertEqual(tc.parse_charts({}), [])
        self.assertEqual(tc.parse_charts({"charts": None}), [])
        self.assertEqual(tc.parse_charts({"charts": "nope"}), [])

    def test_bad_rows_skipped_never_raised(self):
        rows = [
            "junk",
            {"chart_id": "", "vega_lite_spec": {"mark": "line"}},
            {"chart_id": None, "vega_lite_spec": {"mark": "line"}},
            {"chart_id": "01OK", "vega_lite_spec": "not-a-dict"},
            {"chart_id": "01OK", "vega_lite_spec": {}},
            CHART_ROW,
        ]
        out = tc.parse_charts({"charts": rows})
        self.assertEqual(out, [CHART_ROW])

    def test_order_preserved(self):
        second = dict(CHART_ROW, chart_id="01CHARTBBBBBBBBBBBBBBBBBBB")
        out = tc.parse_charts({"charts": [CHART_ROW, second]})
        self.assertEqual(
            [c["chart_id"] for c in out],
            [CHART_ROW["chart_id"], second["chart_id"]],
        )


class TestCaseOpenCarriesCharts(unittest.TestCase):
    def test_parse_case_open_includes_charts(self):
        info = tc.parse_case_open(
            {
                "session_state": {
                    "case": {"case_id": "01CASEAAAAAAAAAAAAAAAAAAAA", "title": "PSHA"},
                    "loaded_layers": [],
                    "chat_history": [],
                    "charts": [CHART_ROW],
                }
            }
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.charts, [CHART_ROW])

    def test_parse_case_open_chartless_default(self):
        info = tc.parse_case_open(
            {"session_state": {"case": {"case_id": "01CASEAAAAAAAAAAAAAAAAAAAA"}}}
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.charts, [])


class TestChartEventDispatch(unittest.TestCase):
    def test_chart_emission_dispatches_as_chart(self):
        client = tc.AgentClient("ws://127.0.0.1:1")  # never connected
        env = {
            "type": "chart-emission",
            "session_id": "01SESSIONAAAAAAAAAAAAAAAAA",
            "payload": CHART_ROW,
        }
        client._recv = lambda timeout: json.dumps(env)  # type: ignore[assignment]
        ev = client.next_event(timeout=0.1)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.kind, "chart")
        self.assertEqual(ev.data, CHART_ROW)


# sys.modules poisoning that forces the NEXT ``import matplotlib...`` to
# fail regardless of whether this venv already imported it earlier (once a
# submodule is cached in sys.modules, a bare ``from matplotlib.figure import
# Figure`` short-circuits straight to the cache and never re-checks the
# parent -- every entry point the guard's try/except can hit must be poisoned
# too, or the mock is a no-op).
_MATPLOTLIB_POISON = {
    k: None for k in [
        "matplotlib", "matplotlib.figure",
        "matplotlib.backends.backend_qtagg",
        "matplotlib.backends.backend_qt5agg",
    ]
}


class TestMatplotlibGuard(unittest.TestCase):
    """The import guard itself (``charts.py`` has no Qt imports at module
    level -- these run in the pure-python venv, no ``qgis.PyQt`` needed)."""

    def setUp(self):
        charts_mod._MATPLOTLIB_CHECKED = False
        self.addCleanup(setattr, charts_mod, "_MATPLOTLIB_CHECKED", False)

    def test_missing_module_reports_unavailable_with_reason(self):
        with mock.patch.dict(sys.modules, _MATPLOTLIB_POISON):
            charts_mod._MATPLOTLIB_CHECKED = False
            available = charts_mod.matplotlib_available()
        self.assertFalse(available)
        self.assertIsNone(charts_mod.Figure)
        self.assertIsNone(charts_mod.FigureCanvasQTAgg)
        reason = charts_mod.matplotlib_error()
        self.assertIsNotNone(reason)
        self.assertIn("matplotlib", reason.lower())

    def test_cached_after_first_check(self):
        """A second call must not re-attempt the import (cheap+cached)."""
        with mock.patch.dict(sys.modules, _MATPLOTLIB_POISON):
            charts_mod._MATPLOTLIB_CHECKED = False
            with mock.patch.object(
                charts_mod, "_do_matplotlib_check",
                wraps=charts_mod._do_matplotlib_check,
            ) as spy:
                charts_mod.matplotlib_available()
                charts_mod.matplotlib_available()
                charts_mod.matplotlib_error()
                self.assertEqual(spy.call_count, 1)

def _fake_isfile(true_for):
    real = os.path.isfile

    def _f(path):
        return path in true_for or real(path)

    return _f


def _fake_access(true_for):
    real = os.access

    def _f(path, mode):
        return path in true_for or real(path, mode)

    return _f


class TestInstallCommandBuilder(unittest.TestCase):
    """Per-OS install command builders -- pure, no subprocess. ``charts``
    delegates the Windows/macOS derivations to ``install_dependencies``
    (one source of truth; full probe-order coverage lives in
    ``test_install_dependencies``); these tests confirm the delegation and
    the Linux literal."""

    def test_linux_is_a_plain_literal_no_derivation(self):
        cmd = charts_mod.linux_install_command()
        self.assertEqual(cmd, "python3 -m pip install matplotlib")

    def test_linux_honors_extra_pip_names(self):
        cmd = charts_mod.linux_install_command(["matplotlib", "otherpkg"])
        self.assertEqual(cmd, "python3 -m pip install matplotlib otherpkg")

    def test_windows_delegates_to_shared_probe(self):
        exec_prefix = r"C:\QGIS\apps\Python312"
        executable = r"C:\QGIS\bin\qgis-bin.exe"
        expected = os.path.join(exec_prefix, "python.exe")
        with mock.patch(
            "plugin.install_dependencies.os.path.isfile", _fake_isfile({expected})
        ), mock.patch(
            "plugin.install_dependencies.os.access", _fake_access({expected})
        ):
            cmd = charts_mod.windows_install_command(
                exec_prefix=exec_prefix, executable=executable
            )
        self.assertEqual(cmd, f"{expected} -m pip install matplotlib")

    def test_windows_honest_fallback_when_nothing_found(self):
        exec_prefix = r"C:\QGIS-ghost\apps\Python312"
        with mock.patch(
            "plugin.install_dependencies.os.path.isfile", lambda p: False
        ), mock.patch(
            "plugin.install_dependencies.os.access", lambda p, m: False
        ):
            cmd = charts_mod.windows_install_command(
                exec_prefix=exec_prefix, executable=exec_prefix + r"\QGIS"
            )
        self.assertTrue(cmd.startswith("could not locate the QGIS python.exe"))
        self.assertNotIn("pip install", cmd)

    def test_mac_wheel_recipe_delegates_to_shared_recipe(self):
        recipe = charts_mod.mac_wheel_recipe(
            python_version="3.12",
            platform_tag="macosx_11_0_arm64",
            profile_python="/profile/python",
        )
        self.assertEqual(
            recipe,
            install_dependencies.mac_wheel_recipe(
                ("matplotlib",), "3.12", "macosx_11_0_arm64", "/profile/python"
            ),
        )
        self.assertIn("pip download matplotlib", recipe)
        self.assertIn("-d /tmp/qgis_mpl", recipe)
        self.assertIn('"/profile/python"', recipe)


def _qt_python() -> str | None:
    """First interpreter that can import qgis.PyQt AND matplotlib (the
    charts harness asserts the real renderer, not the text fallback)."""
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
                [py, "-c",
                 "from qgis.PyQt.QtCore import QCoreApplication; "
                 "import matplotlib"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return py
    return None


class TestChartsWindow(unittest.TestCase):
    def test_charts_harness(self):
        py = _qt_python()
        if py is None:
            self.skipTest("no interpreter with qgis.PyQt + matplotlib")
        harness = os.path.join(os.path.dirname(__file__), "qt_charts_harness.py")
        proc = subprocess.run(
            [py, harness],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode, 0,
            f"charts harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("CHARTS-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
