"""The New / Delete case command plumbing.

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
from stub_server import (  # noqa: E402
    CASE_LIST_ROWS,
    StubAgentServer,
)

# --------------------------------------------------------------------------- #
# Generic case-command (create/delete) -- item 2/3
# --------------------------------------------------------------------------- #


class TestCaseCommandCreateDelete(unittest.TestCase):
    """``AgentClient.case_command`` - the New and Delete case plumbing.

    Unlike the blocking ``create_case``, it sends without waiting: the reply flows
    through the normal event pump."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("case-command test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_create_sends_no_case_id_and_yields_case_open(self):
        self.client.case_command("create")
        ev = self._await_kind("case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)  # the stub's create branch always rehydrates
        create_frames = [
            e
            for e in self.server.received
            if e["type"] == "case-command" and e["payload"].get("command") == "create"
        ]
        # one from setUp's create_case, one from this test's case_command
        self.assertEqual(len(create_frames), 2)
        sent = create_frames[-1]
        self.assertNotIn("case_id", sent["payload"])
        self.assertEqual(sent["payload"]["args"], {})
        self.assertIsNone(sent["case_id"])  # envelope-level case_id too

    def test_create_reply_updates_wire_stamp(self):
        """The pump must ADOPT the case-open rebind into ``client.case_id``.

        Without it the next user-message carries the PREVIOUS id and the turn runs and
        persists into the wrong case."""
        before = self.client.case_id
        self.client.case_command("create")
        ev = self._await_kind("case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)
        self.assertEqual(self.client.case_id, info.case_id)
        if info.case_id != before:
            self.assertNotEqual(self.client.case_id, before)
        # and the very next chat frame is stamped with the OPENED case
        self.client.send_chat("hello after new case")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            chats = [
                e for e in self.server.received if e["type"] == "user-message"
            ]
            if chats:
                break
            time.sleep(0.05)
        else:
            self.fail("no user-message observed by the stub server")
        self.assertEqual(chats[-1]["case_id"], info.case_id)

    def test_delete_sends_case_id(self):
        target = CASE_LIST_ROWS[0]["case_id"]
        self.client.case_command("delete", target)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            frames = [
                e
                for e in self.server.received
                if e["type"] == "case-command"
                and e["payload"].get("command") == "delete"
            ]
            if frames:
                break
            time.sleep(0.05)
        else:
            self.fail("no delete case-command observed by the stub server")
        sent = frames[-1]
        self.assertEqual(sent["payload"]["case_id"], target)
        self.assertEqual(sent["payload"]["args"], {})
        self.assertEqual(sent["case_id"], target)  # envelope-level case_id set

    def test_case_command_queues_when_disconnected(self):
        """Mirrors select_case's queue-if-closed: a command tapped mid-
        reconnect must not be silently dropped."""
        client = tc.AgentClient("ws://127.0.0.1:1/ws")  # never connected
        client.case_command("create")
        self.assertEqual(client.queued_outbound, 1)
