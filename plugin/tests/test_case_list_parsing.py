"""Case list parsing.

No QGIS required - Qt widgets are excluded.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    CASE_LIST_ROWS,
    StubAgentServer,
)

# --------------------------------------------------------------------------- #
# Case list
# --------------------------------------------------------------------------- #


class TestCaseList(unittest.TestCase):
    def test_parse_case_list(self):
        cases = tc.parse_case_list({"cases": CASE_LIST_ROWS})
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0].case_id, "01STUBCASELISTAAAAAAAAAAAA")
        self.assertEqual(cases[0].title, "Asheville flood")
        self.assertEqual(cases[0].status, "active")
        self.assertEqual(cases[0].bbox, [-82.6, 35.55, -82.5, 35.65])
        self.assertEqual(cases[1].status, "archived")
        self.assertIsNone(cases[1].bbox)
        # defensive: malformed rows skipped, never raised on
        cases = tc.parse_case_list(
            {"cases": [None, {"title": "no id"}, {"case_id": ""}, 42,
                       {"case_id": "OK1", "bbox": [1, 2, 3]}]}
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].case_id, "OK1")
        self.assertIsNone(cases[0].bbox)  # wrong-length bbox dropped
        self.assertEqual(tc.parse_case_list({"cases": "nope"}), [])

    def test_case_list_event_surfaces_on_connect(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url)
        self.addCleanup(client.close)
        client.connect()
        client.create_case("case-list test")
        # Trigger a fresh emission cycle: the stub replies to session-resume
        # with case-list first; ask for one more resume round.
        client._send("session-resume", {"case_id": client.case_id})
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            ev = client.next_event(timeout=1.0)
            if ev is not None and ev.kind == "case-list":
                cases = ev.data["cases"]
                self.assertEqual(len(cases), 2)
                self.assertIsInstance(cases[0], tc.CaseInfo)
                self.assertEqual(cases[1].title, "Tampa surge")
                return
        self.fail("no case-list event surfaced")


if __name__ == "__main__":
    unittest.main(verbosity=2)
