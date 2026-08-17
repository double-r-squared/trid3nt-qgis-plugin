"""Milestone 3 tests -- cold case-list fetch, case switching, resume
refresh + debounce, selection-bbox AOI math, token-expiry classification,
chat-history parsing.

No QGIS required (Qt widgets excluded, as in milestones 1-2). The WS stub
needs ``websockets`` (trid3nt-local agent venv).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.case import aoi  # noqa: E402
from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    CASE_LIST_ROWS,
    CASE_OPEN_CHAT_ROWS,
    EXPIRED_TOKEN,
    RASTER_LAYER_ROW,
    StubAgentServer,
)


def _make_gpkg(path: str, tables: list) -> None:
    """Minimal gpkg_contents so the pure sqlite3 listing works."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    for name, data_type in tables:
        conn.execute("INSERT INTO gpkg_contents VALUES (?, ?)", (name, data_type))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Remote export API stub -- the REAL route's semantics, in miniature
# --------------------------------------------------------------------------- #


class _CaseListStub(http.server.BaseHTTPRequestHandler):
    """Mirrors the agent's real ``GET /api/case-list`` route in miniature
    (the server's ``catalog_http.py``): 200 ``{"cases": [...]}`` on
    success, or a configurable status + ``{"error": ...}`` body."""

    status: int = 200
    body: dict = {"cases": []}

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path != "/api/case-list":
            self._json(404, {"error": "not found"})
            return
        self._json(self.status, self.body)

    def log_message(self, *args):  # silence
        pass


class _CaseListStubBase(unittest.TestCase):
    def _start(self, status: int, body: dict) -> str:
        _CaseListStub.status = status
        _CaseListStub.body = body
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _CaseListStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"


class TestFetchCaseList(_CaseListStubBase):
    def test_happy_path_two_cases(self):
        base = self._start(
            200,
            {
                "cases": [
                    {
                        "case_id": "01STUBCASELISTAAAAAAAAAAAA",
                        "title": "Asheville flood",
                        "updated_at": "2026-07-06T12:00:00Z",
                        "bbox": [-82.6, 35.55, -82.5, 35.65],
                    },
                    {
                        "case_id": "01STUBCASELISTBBBBBBBBBBBB",
                        "title": "Tampa surge",
                        "updated_at": "2026-06-21T09:30:00Z",
                        "bbox": None,
                    },
                ]
            },
        )
        cases = tc.fetch_case_list(base, timeout=10)
        self.assertEqual(len(cases), 2)
        self.assertTrue(all(isinstance(c, tc.CaseInfo) for c in cases))
        self.assertEqual(
            [c.case_id for c in cases],
            ["01STUBCASELISTAAAAAAAAAAAA", "01STUBCASELISTBBBBBBBBBBBB"],
        )
        self.assertEqual(cases[0].title, "Asheville flood")
        self.assertEqual(cases[0].bbox, [-82.6, 35.55, -82.5, 35.65])
        self.assertIsNone(cases[1].bbox)

    def test_malformed_rows_are_skipped(self):
        base = self._start(
            200,
            {
                "cases": [
                    {"case_id": "01GOOD0000000000000000000", "title": "Good"},
                    {"title": "No case_id -- dropped"},
                    "not-a-dict",
                    {"case_id": "01GOOD2000000000000000000", "title": "Also good"},
                ]
            },
        )
        cases = tc.fetch_case_list(base, timeout=10)
        self.assertEqual(
            [c.case_id for c in cases],
            ["01GOOD0000000000000000000", "01GOOD2000000000000000000"],
        )

    def test_empty_list_is_ok(self):
        base = self._start(200, {"cases": []})
        self.assertEqual(tc.fetch_case_list(base, timeout=10), [])

    def test_persistence_unavailable_503_raises_honest_error(self):
        base = self._start(503, {"error": "persistence unavailable"})
        with self.assertRaises(tc.CaseListRequestError) as ctx:
            tc.fetch_case_list(base, timeout=10)
        self.assertIn("persistence unavailable", str(ctx.exception))

    def test_route_absent_404_raises(self):
        base = self._start(404, {"error": "not found"})
        with self.assertRaises(tc.CaseListRequestError):
            tc.fetch_case_list(base, timeout=10)

    def test_unreachable_agent_raises_honest_error(self):
        with self.assertRaises(tc.CaseListRequestError) as ctx:
            tc.fetch_case_list("http://127.0.0.1:1", timeout=2)
        self.assertIn("unreachable", str(ctx.exception))

    def test_non_json_body_raises(self):
        class _BadJsonStub(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                raw = b"not json"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), _BadJsonStub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with self.assertRaises(tc.CaseListRequestError) as ctx:
            tc.fetch_case_list(base, timeout=10)
        self.assertIn("non-JSON", str(ctx.exception))


class TestCaseSelect(unittest.TestCase):
    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("select test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_select_sends_command_and_rebinds(self):
        target = CASE_LIST_ROWS[0]["case_id"]
        self.client.select_case(target)
        # web mirror: the local stamp updates AT SEND TIME
        self.assertEqual(self.client.case_id, target)
        ev = self._await_kind("case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)
        self.assertEqual(info.case_id, target)
        self.assertEqual(info.title, "Asheville flood")
        # the rehydration replays the persisted layers
        self.assertEqual(
            [l.layer_id for l in info.layers], [RASTER_LAYER_ROW["layer_id"]]
        )
        # the wire frame carried the select shape
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.server.selects:
            time.sleep(0.05)
        self.assertEqual(self.server.selects, [target])
        sel = [e for e in self.server.received if e["type"] == "case-command"][-1]
        self.assertEqual(sel["payload"]["command"], "select")
        self.assertEqual(sel["payload"]["case_id"], target)
        self.assertEqual(sel["payload"]["args"], {})
        # the NEXT session-resume re-asserts the selected case (rebind proof)
        self.client._send("session-resume", {"case_id": self.client.case_id})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(self.server.resume_case_ids) < 2:
            time.sleep(0.05)
        self.assertEqual(self.server.resume_case_ids[-1], target)

    def test_select_rehydration_surfaces_persisted_thinking(self):
        # LANE PLUGIN (2026-07-22): the stub's case-open chat_history now
        # carries a thinking-carrying agent row (Lane CORE "thinking" field)
        # plus a PLAIN agent row -- the parsed chat_messages must surface
        # the field on the former and None-default it on the latter.
        self.client.select_case(CASE_LIST_ROWS[0]["case_id"])
        info = tc.parse_case_open(self._await_kind("case-open").data)
        self.assertIsNotNone(info)
        agent_rows = [r for r in info.chat_messages if r["role"] == "agent"]
        self.assertEqual(len(agent_rows), 2)
        self.assertIsNone(agent_rows[0]["thinking"])  # plain row unchanged
        self.assertEqual(
            agent_rows[1]["thinking"], CASE_OPEN_CHAT_ROWS[3]["thinking"]
        )
        self.assertEqual(
            agent_rows[1]["content"], CASE_OPEN_CHAT_ROWS[3]["content"]
        )

    def test_select_unknown_case_yields_null_rehydration(self):
        self.client.select_case("01NOSUCHCASEAAAAAAAAAAAAAA")
        ev = self._await_kind("case-open")
        self.assertIsNone(tc.parse_case_open(ev.data))

    def test_parse_case_open_defensive(self):
        self.assertIsNone(tc.parse_case_open("not-a-dict"))
        self.assertIsNone(tc.parse_case_open({}))
        self.assertIsNone(tc.parse_case_open({"session_state": None}))
        self.assertIsNone(tc.parse_case_open({"session_state": {"case": None}}))
        self.assertIsNone(
            tc.parse_case_open({"session_state": {"case": {"case_id": ""}}})
        )
        info = tc.parse_case_open(
            {"session_state": {"case": {"case_id": "01OK"}, "loaded_layers": []}}
        )
        self.assertEqual(info.case_id, "01OK")
        self.assertEqual(info.title, "01OK")  # falls back to the id
        self.assertEqual(info.layers, [])
        self.assertIsNone(info.bbox)  # no bbox on the row -> honest None

    # -- item 1 (live-feedback 2026-07-09): case-open bbox extraction ---------- #

    def test_parse_case_open_bbox_present(self):
        info = tc.parse_case_open(
            {
                "session_state": {
                    "case": {
                        "case_id": "01OK",
                        "title": "Asheville flood",
                        "bbox": [-82.6, 35.55, -82.5, 35.65],
                    },
                    "loaded_layers": [],
                }
            }
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.bbox, (-82.6, 35.55, -82.5, 35.65))
        # every element is a float regardless of int/float mix on the wire
        self.assertTrue(all(isinstance(v, float) for v in info.bbox))

    def test_parse_case_open_bbox_absent(self):
        info = tc.parse_case_open(
            {"session_state": {"case": {"case_id": "01OK"}, "loaded_layers": []}}
        )
        self.assertIsNotNone(info)
        self.assertIsNone(info.bbox)

    def test_parse_case_open_bbox_malformed(self):
        # Wrong length, non-numeric elements, and a non-list value all yield
        # an honest None on the field -- never a raise, never a fabricated
        # bbox.
        for bad_bbox in (
            [-82.6, 35.55, -82.5],  # only 3 elements
            [-82.6, 35.55, -82.5, "not-a-number"],
            "not-a-list",
            42,
            None,
        ):
            info = tc.parse_case_open(
                {
                    "session_state": {
                        "case": {"case_id": "01OK", "bbox": bad_bbox},
                        "loaded_layers": [],
                    }
                }
            )
            self.assertIsNotNone(info)
            self.assertIsNone(info.bbox, f"bbox={bad_bbox!r} should parse to None")

    def test_parse_case_open_bbox_int_elements_coerced_to_float(self):
        info = tc.parse_case_open(
            {
                "session_state": {
                    "case": {"case_id": "01OK", "bbox": [-83, 35, -82, 36]},
                    "loaded_layers": [],
                }
            }
        )
        self.assertEqual(info.bbox, (-83.0, 35.0, -82.0, 36.0))


# --------------------------------------------------------------------------- #
# ITEM B (live-feedback 2026-07-10): chat-history replay extraction --
# ``session_state.chat_history`` (contracts ``case.py`` CaseChatMessage) ->
# plain role/content rows for the dock's case-open chat replay.
# --------------------------------------------------------------------------- #


class TestParseChatHistory(unittest.TestCase):
    def test_present_user_and_agent_rows_survive(self):
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    {"role": "user", "content": "how deep does it flood?"},
                    {"role": "agent", "content": "up to 1.2 m near the river"},
                ]
            }
        )
        # LANE PLUGIN (2026-07-22): an agent row with no persisted thinking
        # surfaces an honest thinking=None (plain rows replay unchanged).
        self.assertEqual(
            rows,
            [
                {"role": "user", "content": "how deep does it flood?"},
                {
                    "role": "agent",
                    "content": "up to 1.2 m near the river",
                    "thinking": None,
                },
            ],
        )

    def test_agent_row_thinking_surfaces(self):
        # LANE PLUGIN (2026-07-22): the persisted "thinking" field (Lane
        # CORE row-model addition) rides through on agent rows so the dock
        # replays the collapsed thinking fold on case reopen.
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    {
                        "role": "agent",
                        "content": "the answer",
                        "thinking": "reasoning about the depth raster",
                    },
                ]
            }
        )
        self.assertEqual(
            rows,
            [
                {
                    "role": "agent",
                    "content": "the answer",
                    "thinking": "reasoning about the depth raster",
                }
            ],
        )

    def test_agent_row_thinking_defensive_defaults_to_none(self):
        # Absent, non-string, and blank thinking values all default to an
        # honest None -- never raised on, never a fabricated fold. A user
        # row never carries the key (thinking is an agent-row field).
        for bad in ({}, {"thinking": None}, {"thinking": 42},
                    {"thinking": ["not", "a", "string"]}, {"thinking": "   "}):
            rows = tc.parse_chat_history(
                {"chat_history": [{"role": "agent", "content": "hi", **bad}]}
            )
            self.assertEqual(
                rows,
                [{"role": "agent", "content": "hi", "thinking": None}],
                f"thinking variant {bad!r} did not default to None",
            )
        rows = tc.parse_chat_history(
            {"chat_history": [{"role": "user", "content": "hi",
                               "thinking": "never on user rows"}]}
        )
        self.assertEqual(rows, [{"role": "user", "content": "hi"}])

    def test_absent_chat_history_yields_empty_list(self):
        self.assertEqual(tc.parse_chat_history({}), [])
        self.assertEqual(tc.parse_chat_history({"chat_history": None}), [])
        self.assertEqual(tc.parse_chat_history({"chat_history": "not-a-list"}), [])

    def test_malformed_rows_are_skipped_not_raised(self):
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    "not-a-dict",
                    {"role": "user"},  # no content
                    {"content": "no role"},
                    {"role": "user", "content": 42},  # non-string content
                    {"role": "user", "content": ""},  # empty content
                    {"role": "system", "content": "tool bookkeeping"},
                    {"role": "tool"},  # tool row with NO tool_card and NO content
                    {"role": "tool", "content": "{...}"},
                    {"role": "bogus", "content": "hi"},
                    {"role": "user", "content": "the one good row"},
                ]
            }
        )
        # Item H (qgis-ux-batch 2026-07-19): a tool row with a usable content
        # twin SURFACES (tool_card rides along, None here); a tool row with
        # NEITHER a tool_card dict NOR content is skipped like every other
        # malformed row -- never raised on.
        self.assertEqual(
            rows,
            [
                {"role": "tool", "tool_card": None, "content": "{...}"},
                {"role": "user", "content": "the one good row"},
            ],
        )

    def test_capped_at_replay_max_keeping_the_tail(self):
        many = [
            {"role": "user" if i % 2 == 0 else "agent", "content": f"msg {i}"}
            for i in range(tc.CHAT_HISTORY_REPLAY_MAX + 10)
        ]
        rows = tc.parse_chat_history({"chat_history": many})
        self.assertEqual(len(rows), tc.CHAT_HISTORY_REPLAY_MAX)
        # the TAIL survives (most recent conversation), not the head
        self.assertEqual(rows[0]["content"], "msg 10")
        self.assertEqual(rows[-1]["content"], f"msg {tc.CHAT_HISTORY_REPLAY_MAX + 9}")

    def test_parse_case_open_surfaces_chat_messages(self):
        info = tc.parse_case_open(
            {
                "session_state": {
                    "case": {"case_id": "01OK", "title": "Asheville flood"},
                    "loaded_layers": [],
                    "chat_history": [
                        {"role": "user", "content": "start a flood sim"},
                        {
                            "role": "tool",
                            "tool_card": {"name": "run_flood_sim", "state": "ok"},
                            "content": "{tool_card}",
                        },
                        {"role": "agent", "content": "here is the result"},
                    ],
                }
            }
        )
        self.assertIsNotNone(info)
        # Item H (qgis-ux-batch 2026-07-19): tool rows are SURFACED with their
        # typed tool_card dict (tool-call chain replay on reopen), in order,
        # inline between the user and agent bubbles.
        self.assertEqual(
            info.chat_messages,
            [
                {"role": "user", "content": "start a flood sim"},
                {
                    "role": "tool",
                    "tool_card": {"name": "run_flood_sim", "state": "ok"},
                    "content": "{tool_card}",
                },
                {"role": "agent", "content": "here is the result",
                 "thinking": None},
            ],
        )

    def test_parse_case_open_without_chat_history_is_empty(self):
        info = tc.parse_case_open(
            {"session_state": {"case": {"case_id": "01OK"}, "loaded_layers": []}}
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.chat_messages, [])


# --------------------------------------------------------------------------- #
# ITEM D (live-feedback 2026-07-10): auto-focus fallback bbox scan --
# ``find_fallback_bbox`` covers a case-open payload OUTSIDE the primary
# session_state.case.bbox carrier ``parse_case_open`` already extracts.
# --------------------------------------------------------------------------- #


class TestFindFallbackBbox(unittest.TestCase):
    def test_top_level_payload_bbox(self):
        bbox = tc.find_fallback_bbox({"bbox": [-83, 35, -82, 36]})
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_session_state_level_bbox(self):
        bbox = tc.find_fallback_bbox(
            {"session_state": {"bbox": [-83, 35, -82, 36]}}
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_session_state_case_level_bbox(self):
        bbox = tc.find_fallback_bbox(
            {"session_state": {"case": {"bbox": [-83, 35, -82, 36]}}}
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_precedence_top_level_wins(self):
        # top-level payload.bbox is checked first, even when a DIFFERENT
        # bbox also sits deeper in the payload.
        bbox = tc.find_fallback_bbox(
            {
                "bbox": [-83, 35, -82, 36],
                "session_state": {"case": {"bbox": [-70, 40, -69, 41]}},
            }
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_absent_or_malformed_yields_none(self):
        for payload in (
            {},
            {"bbox": None},
            {"bbox": [-83, 35, -82]},  # only 3 elements
            {"bbox": "not-a-list"},
            {"session_state": None},
            {"session_state": {"case": "not-a-dict"}},
            "not-a-dict",
            None,
        ):
            self.assertIsNone(tc.find_fallback_bbox(payload), f"payload={payload!r}")

    def test_int_elements_coerced_to_float(self):
        bbox = tc.find_fallback_bbox({"bbox": [-83, 35, -82, 36]})
        self.assertTrue(all(isinstance(v, float) for v in bbox))


# --------------------------------------------------------------------------- #
# Generic case-command (create/delete) -- item 2/3 (live-feedback 2026-07-09)
# --------------------------------------------------------------------------- #


class TestCaseCommandCreateDelete(unittest.TestCase):
    """``AgentClient.case_command`` -- the New/Delete case plumbing.

    Unlike ``create_case`` (blocking, used only during the initial connect
    handshake), ``case_command`` sends without waiting: the reply flows
    through the normal ``next_event`` pump like ``select_case``'s does.
    """

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
        """F34: the pump must ADOPT the case-open rebind into client.case_id.

        Pre-fix, case_command("create") never updated the stamp, so the next
        user-message carried the PREVIOUS case_id and the turn ran/persisted
        into the wrong case (live-proven 2026-07-10: a fresh flood case ended
        up empty while its layers landed in the startup case).
        """
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
    """The client half of the connect-flow reuse: the handshake stashes the
    case-list + adopts a server-rebound case, and the reuse ladder ends in a
    full case-open rehydration (the worker's ``_bind_startup_case`` path,
    minus Qt)."""

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


# --------------------------------------------------------------------------- #
# Selection AOI (pure math)
# --------------------------------------------------------------------------- #


class TestSelectionAoi(unittest.TestCase):
    def test_choose_aoi_precedence(self):
        sel = (-82.60, 35.55, -82.55, 35.60)
        canvas = (-83.0, 35.0, -82.0, 36.0)
        # selection wins when preferred and resolved
        self.assertEqual(aoi.choose_aoi(sel, canvas, True), (sel, "selection"))
        # no selection resolved -> canvas
        self.assertEqual(aoi.choose_aoi(None, canvas, True), (canvas, "canvas"))
        # selection present but toggle off -> canvas
        self.assertEqual(aoi.choose_aoi(sel, canvas, False), (canvas, "canvas"))
        # nothing -> honest (None, None)
        self.assertEqual(aoi.choose_aoi(None, None, True), (None, None))
        # a TOO-LARGE selection is still chosen (the guard rejects it later
        # with an honest "selection ... too large" -- never a silent canvas
        # fallback the user did not ask for)
        wide_sel = (-90.0, 30.0, -80.0, 40.0)
        chosen, source = aoi.choose_aoi(wide_sel, canvas, True)
        self.assertEqual((chosen, source), (wide_sel, "selection"))
        self.assertFalse(aoi.bbox_within_guard(chosen))

    def test_selection_status_text(self):
        sel = (-82.62, 35.55, -82.50, 35.64)
        self.assertEqual(
            aoi.aoi_status_text(sel, True, source="selection"),
            "AOI: selection 0.12 x 0.09 deg",
        )
        wide = (-85.0, 35.0, -80.0, 36.0)
        status = aoi.aoi_status_text(wide, True, source="selection")
        self.assertIn("selection", status)
        self.assertIn("too large", status)
        # (2026-07-22): the per-message in-text context line is GONE
        # -- the AOI rides the structured ``aoi_bbox`` user-message payload
        # field for every source (see test_client structured-AOI tests).
        self.assertFalse(hasattr(aoi, "attach_aoi_to_text"))

    def test_selection_bbox_transform_reuses_extent_math(self):
        # a 3857 selection rect (what boundingBoxOfSelected returns for a
        # web-mercator layer) inverts through the same pure math
        bbox = aoi.extent_to_bbox4326(
            -9196000.0, 4238000.0, -9185000.0, 4249000.0, "EPSG:3857"
        )
        self.assertIsNotNone(bbox)
        self.assertTrue(aoi.bbox_within_guard(bbox))
        # a degenerate rect (single-point selection) is an honest None
        self.assertIsNone(
            aoi.extent_to_bbox4326(-82.5, 35.5, -82.5, 35.5, "EPSG:4326")
        )


# --------------------------------------------------------------------------- #
# Token-expiry classification
# --------------------------------------------------------------------------- #


class TestAuthFailureClassification(unittest.TestCase):
    def test_auth_failures_classify_true(self):
        f = tc.is_auth_failure
        # the broker's pre-upgrade rejection (?st= token dead)
        self.assertTrue(f("HandshakeFailed: upgrade rejected: HTTP/1.1 401 Unauthorized"))
        self.assertTrue(f("HandshakeFailed: upgrade rejected: HTTP/1.1 403 Forbidden"))
        # the in-band agent rejection (error envelope folded into the text)
        self.assertTrue(f("ConnectionClosed: connection closed (code=1008 reason='auth required') [AUTH_REQUIRED token expired or invalid]"))
        self.assertTrue(f("something something TOKEN EXPIRED"))

    def test_transport_failures_classify_false(self):
        f = tc.is_auth_failure
        self.assertFalse(f(""))
        self.assertFalse(f("ConnectionClosed: connection closed (code=1011 reason='stub drop')"))
        self.assertFalse(f("OSError: [Errno 111] Connection refused"))
        self.assertFalse(f("ConnectionClosed: read timeout"))
        self.assertFalse(f("HandshakeFailed: upgrade rejected: HTTP/1.1 502 Bad Gateway"))
        # 401/403 appearing OUTSIDE an upgrade rejection does not classify
        self.assertFalse(f("fetched 403 rows from the catalog"))

    def test_expired_token_connect_classifies_as_auth(self):
        """Full stub round trip: dead token -> error envelope + 1008 close ->
        the combined failure text classifies as auth (ladder must stop)."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url, token=EXPIRED_TOKEN)
        self.addCleanup(client.close)
        with self.assertRaises((tc.ConnectionClosed, tc.HandshakeFailed)) as ctx:
            client.connect()
        # the drained error envelope was stashed for classification
        self.assertIsNotNone(client.last_handshake_error)
        self.assertEqual(
            client.last_handshake_error.get("error_code"), "AUTH_REQUIRED"
        )
        combined = (
            f"{type(ctx.exception).__name__}: {ctx.exception} "
            f"[{client.last_handshake_error.get('error_code')} "
            f"{client.last_handshake_error.get('message')}]"
        )
        self.assertTrue(tc.is_auth_failure(combined))

    def test_good_token_still_connects(self):
        """The rejection path must not break the normal token handshake."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url, token="live-token")
        self.addCleanup(client.close)
        client.connect()
        self.assertTrue(client.connected)
        self.assertIsNone(client.last_handshake_error)


# --------------------------------------------------------------------------- #
# REAL Qt bridge wiring (subprocess -- the layer the stdlib tests bypass)
# --------------------------------------------------------------------------- #


class TestQtBridgeStart(unittest.TestCase):
    """Exercises AgentBridge.start under a REAL QCoreApplication.

    Regression for the QObject.event() shadowing crash (a pyqtSignal named
    ``event`` made the first delivered QEvent qFatal the whole QGIS process
    -- "TypeError: native Qt signal is not callable"). The stdlib stub tests
    never build a Qt object tree, which is exactly why milestones 1-2 shipped
    with it; this test runs the wiring in a subprocess using the SYSTEM
    interpreter (the one with qgis.PyQt) and skips honestly when absent.
    """

    @staticmethod
    def _qt_python() -> str | None:
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
                    [py, "-c", "from qgis.PyQt.QtCore import QCoreApplication"],
                    capture_output=True,
                    timeout=60,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return py
        return None

    def test_bridge_start_survives_real_qt_event_delivery(self):
        py = self._qt_python()
        if py is None:
            self.skipTest("no interpreter with qgis.PyQt available")
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        harness = os.path.join(os.path.dirname(__file__), "qt_bridge_harness.py")
        proc = subprocess.run(
            [py, "-u", harness, server.url],
            capture_output=True,
            timeout=120,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        self.assertEqual(
            proc.returncode,
            0,
            "qt bridge harness died (rc="
            f"{proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("QT-BRIDGE-OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestShowThinkingSettings(unittest.TestCase):
    """F9 (live-feedback 2026-07-09): show_thinking preference in plugin_settings."""

    def _make_settings(self, stored: dict = None):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = stored or {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            return importlib.import_module("plugin.plugin_settings").PluginSettings()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_default_is_true(self):
        s = self._make_settings()
        self.assertTrue(s.show_thinking, "show_thinking must default to True")

    def test_explicit_false_stored(self):
        s = self._make_settings({"trid3nt/show_thinking": "false"})
        self.assertFalse(s.show_thinking)

    def test_explicit_true_stored(self):
        s = self._make_settings({"trid3nt/show_thinking": "true"})
        self.assertTrue(s.show_thinking)

    def test_setter_persists(self):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            ps = importlib.import_module("plugin.plugin_settings")
            s = ps.PluginSettings()
            s.show_thinking = False
            self.assertEqual(FakeQSettings.store.get("trid3nt/show_thinking"), "false")
            s.show_thinking = True
            self.assertEqual(FakeQSettings.store.get("trid3nt/show_thinking"), "true")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class TestAutoBasemapSettings(unittest.TestCase):
    """Item 4 (live-feedback 2026-07-09): auto_basemap preference in
    plugin_settings -- same shape as TestShowThinkingSettings above."""

    def _make_settings(self, stored: dict = None):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = stored or {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            return importlib.import_module("plugin.plugin_settings").PluginSettings()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_default_is_true(self):
        s = self._make_settings()
        self.assertTrue(s.auto_basemap, "auto_basemap must default to True")

    def test_explicit_false_stored(self):
        s = self._make_settings({"trid3nt/auto_basemap": "false"})
        self.assertFalse(s.auto_basemap)

    def test_explicit_true_stored(self):
        s = self._make_settings({"trid3nt/auto_basemap": "true"})
        self.assertTrue(s.auto_basemap)

    def test_setter_persists(self):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            ps = importlib.import_module("plugin.plugin_settings")
            s = ps.PluginSettings()
            s.auto_basemap = False
            self.assertEqual(FakeQSettings.store.get("trid3nt/auto_basemap"), "false")
            s.auto_basemap = True
            self.assertEqual(FakeQSettings.store.get("trid3nt/auto_basemap"), "true")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class TestProviderModelSettings(unittest.TestCase):
    """OpenRouter model-extensibility (design 2026-07-19): provider / model_id
    / openrouter_api_key round-trip through QSettings -- same FakeQSettings
    idiom as TestShowThinkingSettings / TestAutoBasemapSettings above."""

    def _make_settings(self, stored: dict = None):
        import types
        import importlib

        class FakeQSettings:
            store: dict = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        FakeQSettings.store = stored or {}
        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis_mod, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin.plugin_settings", None)
            return importlib.import_module("plugin.plugin_settings").PluginSettings(), FakeQSettings
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_provider_default_is_local_ollama(self):
        s, _ = self._make_settings()
        self.assertEqual(s.provider, "local-ollama")

    def test_provider_stored_value(self):
        s, _ = self._make_settings({"trid3nt/provider": "openrouter-paid"})
        self.assertEqual(s.provider, "openrouter-paid")

    def test_provider_setter_persists(self):
        s, store = self._make_settings()
        s.provider = "groq"
        self.assertEqual(store.store.get("trid3nt/provider"), "groq")

    def test_provider_blank_falls_back_to_default(self):
        s, store = self._make_settings()
        s.provider = "   "
        self.assertEqual(store.store.get("trid3nt/provider"), "local-ollama")

    def test_model_id_default_is_empty(self):
        s, _ = self._make_settings()
        self.assertEqual(s.model_id, "")

    def test_model_id_stored_value(self):
        s, _ = self._make_settings({"trid3nt/model_id": "deepseek/deepseek-chat"})
        self.assertEqual(s.model_id, "deepseek/deepseek-chat")

    def test_model_id_setter_persists_stripped(self):
        s, store = self._make_settings()
        s.model_id = "  meta-llama/llama-3.3-70b-instruct  "
        self.assertEqual(
            store.store.get("trid3nt/model_id"),
            "meta-llama/llama-3.3-70b-instruct",
        )

    def test_api_key_default_is_empty(self):
        s, _ = self._make_settings()
        self.assertEqual(s.openrouter_api_key, "")

    def test_api_key_round_trip(self):
        s, store = self._make_settings()
        s.openrouter_api_key = "sk-or-v1-SECRET"
        self.assertEqual(store.store.get("trid3nt/openrouter_api_key"), "sk-or-v1-SECRET")
        s2, _ = self._make_settings({"trid3nt/openrouter_api_key": "sk-or-v1-SECRET"})
        self.assertEqual(s2.openrouter_api_key, "sk-or-v1-SECRET")
