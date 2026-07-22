"""Charts surface tests (OpenQuake result parity, live-feedback 2026-07-13).

Two halves, mirroring the repo convention:

* PURE-PYTHON (this venv, no Qt): ``trid3nt_client.parse_charts`` -- the
  defensive ``session_state.charts`` replay parser -- plus the case-open
  carrier (``CaseOpenInfo.charts``) and the live ``chart-emission`` ->
  ``AgentEvent("chart", ...)`` dispatch.
* QT SUBPROCESS: ``qt_charts_harness.py`` under the system interpreter (the
  one with ``qgis.PyQt`` + matplotlib -- the ``test_dock_ui`` convention),
  covering the ChartsPanel rendering (log-log hazard curve, dashed rule,
  bars, paging, de-dupe, clear) and the dock wiring (panel + one pointer
  note, never a chart widget in the chat message list).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402

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


class TestChartsPanel(unittest.TestCase):
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
