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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt import install_dependencies  # noqa: E402
from trid3nt.net import trid3nt_client as tc  # noqa: E402
from trid3nt.ui import charts as charts_mod  # noqa: E402

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
        self.addCleanup(charts_mod.recheck_matplotlib)

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

    def test_recheck_busts_the_cache(self):
        with mock.patch.dict(sys.modules, _MATPLOTLIB_POISON):
            charts_mod._MATPLOTLIB_CHECKED = False
            self.assertFalse(charts_mod.matplotlib_available())
        # Outside the poisoned sys.modules, an explicit recheck must
        # re-attempt the import rather than trust the cached failure.
        with mock.patch.object(
            charts_mod, "_do_matplotlib_check",
            wraps=charts_mod._do_matplotlib_check,
        ) as spy:
            charts_mod.recheck_matplotlib()
            self.assertEqual(spy.call_count, 1)


class TestInstallCommandBuilder(unittest.TestCase):
    """Per-OS install command string builder -- pure, no subprocess. The
    executable-resolution logic itself is shared with ``install_dependencies``
    (one source of truth); these tests confirm ``charts`` delegates rather
    than re-implementing it, and that the panel's argv targets the shared
    script instead of raw pip."""

    def test_mac_derives_from_exec_prefix_bin_python3(self):
        py = charts_mod.install_python_executable(
            "darwin",
            "/Applications/QGIS.app/Contents/MacOS",
            "/Applications/QGIS.app/Contents/MacOS/QGIS",
        )
        self.assertEqual(
            py, "/Applications/QGIS.app/Contents/MacOS/bin/python3"
        )

    def test_linux_uses_running_interpreter(self):
        py = charts_mod.install_python_executable(
            "linux", "/usr", "/usr/bin/python3"
        )
        self.assertEqual(py, "/usr/bin/python3")

    def test_linux_falls_back_to_exec_prefix_when_executable_empty(self):
        py = charts_mod.install_python_executable("linux", "/usr", "")
        self.assertEqual(py, "/usr/bin/python3")

    def test_windows_derives_from_exec_prefix(self):
        # ``os.path.join`` on this (posix) test host renders a win32-style
        # exec_prefix with '/' -- the assertion below tracks whatever this
        # host's os.path.join produces, mirroring the production code path
        # rather than asserting a literal backslash it would never emit here.
        py = charts_mod.install_python_executable(
            "win32", r"C:\QGIS\apps\Python312", r"C:\QGIS\bin\qgis-bin.exe"
        )
        self.assertEqual(
            py, os.path.join(r"C:\QGIS\apps\Python312", "python.exe")
        )
        self.assertTrue(py.endswith("python.exe"))
        self.assertTrue(py.startswith(r"C:\QGIS\apps\Python312"))

    def test_argv_shape_delegates_to_shared_script(self):
        """The panel's QProcess argv must run install_dependencies.py, not
        raw pip -- one source of truth for check/install/re-verify."""
        argv = charts_mod.install_command_argv(
            "linux", "/usr", "/usr/bin/python3"
        )
        self.assertEqual(argv, ["/usr/bin/python3", install_dependencies.__file__])

    def test_command_str_unquoted_when_no_spaces(self):
        cmd = charts_mod.install_command_str("linux", "/usr", "/usr/bin/python3")
        self.assertEqual(cmd, f"/usr/bin/python3 {install_dependencies.__file__}")

    def test_command_str_quotes_path_with_spaces(self):
        cmd = charts_mod.install_command_str(
            "darwin", "/Applications/QGIS 4.app/Contents/MacOS", "irrelevant"
        )
        self.assertTrue(cmd.startswith('"/Applications/QGIS 4.app'))
        self.assertTrue(cmd.endswith(install_dependencies.__file__))

    def test_nothing_hardcoded_reflects_runtime_prefix(self):
        """Different exec_prefix inputs must produce different commands --
        proof the path is derived, not baked in."""
        cmd_a = charts_mod.install_command_str("darwin", "/opt/QGIS-A", "x")
        cmd_b = charts_mod.install_command_str("darwin", "/opt/QGIS-B", "x")
        self.assertNotEqual(cmd_a, cmd_b)
        self.assertIn("/opt/QGIS-A/bin/python3", cmd_a)
        self.assertIn("/opt/QGIS-B/bin/python3", cmd_b)


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
