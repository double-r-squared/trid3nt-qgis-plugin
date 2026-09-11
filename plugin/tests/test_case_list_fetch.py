"""Cold case-list fetch over the HTTP route, and the case switch it feeds.

No QGIS required except where a case names the bridge; the WS stub needs
``websockets``.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    CASE_LIST_ROWS,
    CASE_OPEN_CHAT_ROWS,
    RASTER_LAYER_ROW,
    StubAgentServer,
)



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
        # LANE PLUGIN: the stub's case-open chat_history now
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

    # -- item 1: case-open bbox extraction ---------- #

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
