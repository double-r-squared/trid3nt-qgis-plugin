"""Reconnect backoff and the outbound queue that survives a drop.

No QGIS required - Qt widgets are excluded.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import StubAgentServer  # noqa: E402



class TestBackoff(unittest.TestCase):
    def test_next_backoff_ladder(self):
        # deterministic rng: factor = 0.5 + 0.5*rng()
        delay, nxt = tc.next_backoff(1500, rng=lambda: 0.0)
        self.assertEqual(delay, 750)  # 0.5 x base (max jitter, earliest retry)
        self.assertEqual(nxt, 3000)  # doubles
        delay, nxt = tc.next_backoff(3000, rng=lambda: 0.999999)
        self.assertLessEqual(delay, 3000)  # never later than base
        self.assertGreater(delay, 2990)
        self.assertEqual(nxt, 5000)  # capped at RECONNECT_MAX_MS
        _, nxt = tc.next_backoff(5000, rng=lambda: 0.5)
        self.assertEqual(nxt, 5000)  # stays at the ceiling
        self.assertEqual(tc.RECONNECT_FLOOR_MS, 1500)
        self.assertEqual(tc.RECONNECT_MAX_MS, 5000)


class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("reconnect test")

    def _drain_until_closed(self, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                self.client.next_event(timeout=1.0)
            except tc.ConnectionClosed:
                return
        self.fail("socket never closed")

    def test_resume_rebinds_case_and_flushes_queue(self):
        session_id = self.client.session_id
        # Server drops the connection mid-turn.
        self.client.send_chat("please drop-connection now")
        self._drain_until_closed()
        self.assertFalse(self.client.connected)

        # Intent issued while down QUEUES instead of raising (sendOrQueue).
        self.client.send_chat("queued while offline")
        self.client.cancel(reason="queued-cancel")
        self.assertEqual(self.client.queued_outbound, 2)

        self.client.reconnect()
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.queued_outbound, 0)
        # Same session resumed; session-resume carried the ACTIVE case_id
        # (SessionResumePayload.case_id, job-CASE-AUTHORITY).
        self.assertEqual(self.client.session_id, session_id)
        self.assertEqual(self.server.connection_count, 2)
        self.assertEqual(self.server.resume_case_ids[0], None)
        self.assertEqual(self.server.resume_case_ids[1], self.client.case_id)
        # Queued frames flushed FIFO after the handshake. The flush is sent
        # before reconnect() returns but the stub INGESTS asynchronously --
        # wait for both frames to land.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if sum(1 for e in self.server.received if e["type"] == "cancel"):
                break
            time.sleep(0.05)
        types = [e["type"] for e in self.server.received]
        resume2 = [i for i, t in enumerate(types) if t == "session-resume"][1]
        flushed = types[resume2 + 1: resume2 + 3]
        self.assertEqual(flushed, ["user-message", "cancel"])
        flushed_msg = [
            e for e in self.server.received if e["type"] == "user-message"
        ][-1]
        self.assertEqual(flushed_msg["payload"]["text"], "queued while offline")

    def test_reconnect_reuses_session_and_sends_token_only_auth(self):
        """Reconnect reuses the SAME session_id and sends a token-only
        auth-token: the sticky anonymous_user_id hint was cut (every connection
        resolves to the one fixed local user server-side)."""
        first_session = self.client.session_id
        self.client.send_chat("drop-connection")
        self._drain_until_closed()
        self.client.reconnect()
        auth_frames = [e for e in self.server.received if e["type"] == "auth-token"]
        self.assertEqual(len(auth_frames), 2)
        for frame in auth_frames:
            self.assertNotIn("anonymous_user_id", frame["payload"])
            self.assertEqual(frame["session_id"], first_session)

    def test_queue_bounded_50_drops_oldest(self):
        self.client.send_chat("drop-connection")
        self._drain_until_closed()
        for i in range(55):
            self.client.send_chat(f"msg-{i}")
        self.assertEqual(self.client.queued_outbound, tc.OUTBOUND_QUEUE_MAX)
        with self.client._queue_lock:
            queued = [json.loads(raw) for raw in self.client._outbound_queue]
        # OLDEST dropped first: msg-0..msg-4 gone, msg-5 is now the head.
        self.assertEqual(queued[0]["payload"]["text"], "msg-5")
        self.assertEqual(queued[-1]["payload"]["text"], "msg-54")


if __name__ == "__main__":
    unittest.main(verbosity=2)
