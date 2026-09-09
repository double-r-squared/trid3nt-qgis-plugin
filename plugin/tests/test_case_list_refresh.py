"""Case-list refresh as a resume round trip, and its debounce.

No QGIS required except where a case names the bridge; the WS stub needs
``websockets``.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import StubAgentServer  # noqa: E402

# --------------------------------------------------------------------------- #
# Case-list refresh (resume round trip) + debounce
# --------------------------------------------------------------------------- #


class TestRefresh(unittest.TestCase):
    def test_refresh_is_a_resume_round_trip(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url)
        self.addCleanup(client.close)
        client.connect()
        client.create_case("refresh test")
        self.assertTrue(client.request_case_list_refresh())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            ev = client.next_event(timeout=1.0)
            if ev is not None and ev.kind == "case-list":
                self.assertEqual(len(ev.data["cases"]), 2)
                break
        else:
            self.fail("no case-list arrived from the refresh resume")
        # the resume carried the ACTIVE case_id (server re-binds, no reset)
        self.assertEqual(server.resume_case_ids[-1], client.case_id)

    def test_refresh_refused_when_disconnected(self):
        client = tc.AgentClient("ws://127.0.0.1:1/ws")
        self.assertFalse(client.request_case_list_refresh())

    def test_debouncer_min_interval(self):
        clock = {"t": 100.0}
        d = tc.Debouncer(interval_s=2.0, clock=lambda: clock["t"])
        self.assertTrue(d.allow())      # first fire
        self.assertFalse(d.allow())     # immediate repeat suppressed
        clock["t"] += 1.9
        self.assertFalse(d.allow())     # still inside the window
        clock["t"] += 0.2
        self.assertTrue(d.allow())      # window elapsed -> fires (re-stamps)
        self.assertFalse(d.allow())
        self.assertEqual(tc.REFRESH_DEBOUNCE_S, 2.0)
