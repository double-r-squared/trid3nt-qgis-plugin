"""Persistent per-case bbox (per-case-bbox 2026-07-19) -- cloud parity.

Two layers of offline coverage:

  * TransportArgs: the ``case_command`` args-threading is PURE (no Qt) -- the
    dock persists a user-edited AOI via ``case_command("set-bbox", case_id,
    {"bbox": [...]})``, and the new ``args`` slot must ride the wire while
    every existing caller (create with no args, delete) stays byte-identical.
    Checked against the StubAgentServer's recorded frames.

  * TestCaseBboxDock: the Qt dock behavior (overlay build, 4326<->canvas
    conversion, default-on-create, state clear on switch/disconnect) runs in a
    SUBPROCESS under the system interpreter that has ``qgis`` (a real
    QgsMapCanvas + QgsRubberBand), skipping honestly when absent -- the same
    convention as ``test_dock_ui``. The live QgsMapToolExtent DRAG itself is
    NOT unit-tested (it needs real mouse events on a shown canvas -- NATE
    live-verifies it on plugin reload); the harness invokes the tool's
    ``extentChanged`` handler directly, covering everything downstream.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trid3nt"))
sys.path.insert(0, os.path.dirname(__file__))

import trid3nt_client as tc  # noqa: E402
from stub_server import CASE_LIST_ROWS, StubAgentServer  # noqa: E402


class TestSetBboxTransportArgs(unittest.TestCase):
    """``AgentClient.case_command`` args threading (pure, no Qt)."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("set-bbox transport test")

    def _await_case_command(self, predicate, deadline_s=5.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            frames = [
                e
                for e in self.server.received
                if e["type"] == "case-command" and predicate(e["payload"])
            ]
            if frames:
                return frames[-1]
            time.sleep(0.05)
        self.fail("expected case-command frame never observed")

    def test_set_bbox_carries_args_and_case_id(self):
        bbox = [-82.62, 26.58, -82.50, 26.70]
        self.client.case_command("set-bbox", "CASE-1", {"bbox": bbox})
        sent = self._await_case_command(
            lambda p: p.get("command") == "set-bbox"
        )
        self.assertEqual(sent["payload"]["command"], "set-bbox")
        self.assertEqual(sent["payload"]["case_id"], "CASE-1")
        self.assertEqual(sent["payload"]["args"], {"bbox": bbox})
        self.assertEqual(sent["case_id"], "CASE-1")  # envelope-level too

    def test_create_with_bbox_args(self):
        """new_case's default-on-create path: create carrying args.bbox."""
        bbox = [-82.70, 26.50, -82.40, 26.80]
        self.client.case_command("create", args={"bbox": bbox})
        sent = self._await_case_command(
            lambda p: p.get("command") == "create" and "bbox" in p.get("args", {})
        )
        self.assertEqual(sent["payload"]["args"], {"bbox": bbox})
        self.assertNotIn("case_id", sent["payload"])  # create carries none

    def test_existing_callers_stay_empty_args(self):
        """create-with-no-args and delete must be byte-identical to before:
        args defaults to {} (the ``args=None`` -> {} coercion)."""
        self.client.case_command("create")
        create = self._await_case_command(
            lambda p: p.get("command") == "create" and p.get("args") == {}
        )
        self.assertEqual(create["payload"]["args"], {})
        target = CASE_LIST_ROWS[0]["case_id"]
        self.client.case_command("delete", target)
        delete = self._await_case_command(
            lambda p: p.get("command") == "delete"
        )
        self.assertEqual(delete["payload"]["args"], {})
        self.assertEqual(delete["payload"]["case_id"], target)

    def test_case_command_args_queue_when_disconnected(self):
        """A set-bbox tapped mid-reconnect must queue, not drop (mirrors the
        existing queue-if-closed contract)."""
        client = tc.AgentClient("ws://127.0.0.1:1/ws")  # never connected
        client.case_command("set-bbox", "C", {"bbox": [0.0, 0.0, 1.0, 1.0]})
        self.assertEqual(client.queued_outbound, 1)


def _qt_python():
    """First interpreter that can import ``qgis`` (real canvas/rubber band) --
    same probe shape as ``test_dock_ui._qt_python``."""
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
                [py, "-c", "from qgis.gui import QgsMapCanvas"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return py
    return None


class TestCaseBboxDock(unittest.TestCase):
    """Runs ``qt_case_bbox_harness.py`` in the qgis interpreter (subprocess)."""

    _proc = None

    @classmethod
    def setUpClass(cls):
        py = _qt_python()
        if py is None:
            return  # the test skips honestly
        harness = os.path.join(
            os.path.dirname(__file__), "qt_case_bbox_harness.py"
        )
        cls._proc = subprocess.run(
            [py, "-u", harness],
            capture_output=True,
            timeout=180,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )

    def test_case_bbox_dock_behaviors(self):
        if self._proc is None:
            self.skipTest("no interpreter with qgis available")
        self.assertEqual(
            self._proc.returncode,
            0,
            msg=f"harness failed:\nSTDOUT:\n{self._proc.stdout}\n"
            f"STDERR:\n{self._proc.stderr}",
        )
        self.assertIn("CASE-BBOX-OK", self._proc.stdout)


if __name__ == "__main__":
    unittest.main()
