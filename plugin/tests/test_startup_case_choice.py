"""Startup case choice: resume before select-newest before create.

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
# Startup case reuse (live-feedback 2026-07-09): never mint a fresh
# "QGIS session ..." case while the user already has one
# --------------------------------------------------------------------------- #


class TestChooseStartupCase(unittest.TestCase):
    """The pure connect-flow decision ladder (``choose_startup_case``):
    resume > select-newest > create."""

    @staticmethod
    def _case(case_id, updated_at="", status="active"):
        return tc.CaseInfo(
            case_id=case_id, title=case_id, status=status, updated_at=updated_at
        )

    def test_resumed_case_wins_over_list(self):
        cases = [self._case("01NEWEST", "2026-07-08T00:00:00Z")]
        self.assertEqual(
            tc.choose_startup_case("01RESUMED", cases), ("resume", "01RESUMED")
        )

    def test_newest_live_case_selected(self):
        cases = [
            self._case("01OLD", "2026-06-01T00:00:00Z"),
            self._case("01NEW", "2026-07-08T00:00:00Z"),
            self._case("01MID", "2026-07-01T00:00:00Z"),
        ]
        self.assertEqual(tc.choose_startup_case(None, cases), ("select", "01NEW"))

    def test_tombstones_and_malformed_rows_skipped(self):
        cases = [
            self._case("01ARCHIVED", "2026-07-09T00:00:00Z", status="archived"),
            self._case("01DELETED", "2026-07-09T00:00:00Z", status="deleted"),
            self._case("", "2026-07-09T00:00:00Z"),  # no case_id -- dropped
            self._case("01LIVE", "2026-06-15T00:00:00Z"),
        ]
        self.assertEqual(tc.choose_startup_case(None, cases), ("select", "01LIVE"))

    def test_missing_updated_at_sorts_oldest(self):
        cases = [
            self._case("01NODATE", ""),
            self._case("01DATED", "2026-07-01T00:00:00Z"),
        ]
        self.assertEqual(tc.choose_startup_case(None, cases), ("select", "01DATED"))

    def test_zero_cases_creates(self):
        self.assertEqual(tc.choose_startup_case(None, []), ("create", None))
        self.assertEqual(tc.choose_startup_case("", None), ("create", None))

    def test_all_tombstoned_creates(self):
        cases = [self._case("01GONE", "2026-07-01T00:00:00Z", status="archived")]
        self.assertEqual(tc.choose_startup_case(None, cases), ("create", None))

    def test_stub_rows_pick_the_active_newest(self):
        # The stub's canonical rows: Asheville (active) + Tampa (archived).
        cases = tc.parse_case_list({"cases": CASE_LIST_ROWS})
        self.assertEqual(
            tc.choose_startup_case(None, cases),
            ("select", "01STUBCASELISTAAAAAAAAAAAA"),
        )


class TestStartupCaseReuse(unittest.TestCase):
    """The client half of the connect-flow reuse.

    The handshake stashes the case list and adopts a server-rebound case, and the
    reuse ladder ends in a full case-open rehydration."""

    def _client(self, server, **kwargs):
        client = tc.AgentClient(server.url, **kwargs)
        self.addCleanup(client.close)
        return client

    def _await_kind(self, client, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_handshake_stashes_case_list(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        # The stub interleaves case-list BEFORE session-state -- the
        # handshake drain must stash it, not drop it.
        self.assertIsNotNone(client.last_case_list)
        self.assertEqual(
            [c.case_id for c in client.last_case_list],
            [r["case_id"] for r in CASE_LIST_ROWS],
        )

    def test_bare_resume_adopts_server_rebound_case(self):
        server = StubAgentServer()
        server.resume_rebind_case_id = CASE_LIST_ROWS[0]["case_id"]
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        # Rule 1: the persisted active case the resume rebound is adopted.
        self.assertEqual(client.case_id, CASE_LIST_ROWS[0]["case_id"])

    def test_client_stamp_beats_server_rebind(self):
        server = StubAgentServer()
        server.resume_rebind_case_id = CASE_LIST_ROWS[0]["case_id"]
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.case_id = "01CLIENTSTAMPAAAAAAAAAAAAA"  # reconnect posture
        client.connect()
        # job-CASE-AUTHORITY: the client's own stamp is never overwritten.
        self.assertEqual(client.case_id, "01CLIENTSTAMPAAAAAAAAAAAAA")

    def test_no_rebind_no_adoption(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        self.assertIsNone(client.case_id)

    def test_reuse_ladder_selects_newest_and_rehydrates(self):
        """The worker's local-mode connect flow, minus Qt: connect ->
        choose_startup_case -> select -> the case-open rehydration carries
        the authoritative title + layers (the dock's rebind input)."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        action, target = tc.choose_startup_case(
            client.case_id, client.last_case_list or []
        )
        self.assertEqual((action, target), ("select", CASE_LIST_ROWS[0]["case_id"]))
        client.select_case(target)
        self.assertEqual(client.case_id, target)  # bound -- never caseless
        ev = self._await_kind(client, "case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)
        self.assertEqual(info.case_id, target)
        self.assertEqual(info.title, "Asheville flood")
        self.assertEqual(len(info.layers), 1)  # persisted layers replayed
        # No create ever hit the wire -- the whole point of the reuse ladder.
        creates = [
            e
            for e in server.received
            if e["type"] == "case-command"
            and (e["payload"] or {}).get("command") == "create"
        ]
        self.assertEqual(creates, [])

    def test_reuse_ladder_resume_wins(self):
        server = StubAgentServer()
        server.resume_rebind_case_id = CASE_LIST_ROWS[0]["case_id"]
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        action, target = tc.choose_startup_case(
            client.case_id, client.last_case_list or []
        )
        self.assertEqual(
            (action, target), ("resume", CASE_LIST_ROWS[0]["case_id"])
        )

    def test_event_pump_stashes_case_list_too(self):
        """The live server emits case-list AFTER session-state -- the event
        pump path must stash it just like the handshake drain does."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        client.last_case_list = None  # wipe the drain stash
        self.assertTrue(client.request_case_list_refresh())
        self._await_kind(client, "case-list")
        self.assertIsNotNone(client.last_case_list)
        self.assertEqual(len(client.last_case_list), 2)
