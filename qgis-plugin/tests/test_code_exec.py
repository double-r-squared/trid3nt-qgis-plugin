"""Code-exec approval gate tests (live-feedback 2026-07-21).

The agent's ``code-exec-request`` envelope (contracts
``sandbox_contracts.py``) previously had ZERO plugin handling: it fell
through the client's classifier as kind="raw", the dock dropped it, and the
agent blocked on its confirm-gate future forever ("it just stopped").

Coverage here (offline -- pure parse/decision logic + stub_server round
trips; the Qt card itself is covered at the qt harness level, see
``qt_dock_ui_harness.py``):

* ``gate.parse_code_exec_request`` field mapping + malformed-envelope
  honesty (no code_exec_id / no python_code -> None, never a crash).
* ``gate.resolve_code_exec_decision``: Run -> ("proceed", None), Deny ->
  ("cancel", None) -- revised_args ALWAYS None (contract cross-rule; the
  server fail-closes narrow_scope for code, so it is never offered).
* the wire round trip against the stub: a "run-code" turn pauses behind the
  ``code-exec-request``; the reply rides the EXISTING
  ``tool-payload-confirmation`` envelope with ``warning_id ==
  code_exec_id`` (the server's shared confirm seam -- no new client verb);
  proceed resumes the turn, cancel ends it honestly.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402
from trid3nt.ui import gate  # noqa: E402
from stub_server import (  # noqa: E402
    CODE_EXEC_REQUEST_ROW,
    STUB_CODE_EXEC_ID,
    StubAgentServer,
)


class TestCodeExecParsing(unittest.TestCase):
    def test_parse_fields(self):
        req = gate.parse_code_exec_request(CODE_EXEC_REQUEST_ROW)
        self.assertEqual(req.code_exec_id, STUB_CODE_EXEC_ID)
        # The EXACT code, verbatim -- never a paraphrase (contract).
        self.assertEqual(req.python_code, CODE_EXEC_REQUEST_ROW["python_code"])
        self.assertEqual(
            req.layer_refs, {"depth": "s3://trid3nt-runs/flood/depth.tif"}
        )
        self.assertEqual(
            req.rationale,
            "Compute the 95th-percentile flood depth over the AOI.",
        )
        self.assertIs(req.raw, CODE_EXEC_REQUEST_ROW)

    def test_parse_malformed_is_none(self):
        # No code_exec_id -> nothing to confirm against.
        self.assertIsNone(
            gate.parse_code_exec_request({"python_code": "result = 1"})
        )
        # No / blank python_code -> approving unseen code is refused.
        self.assertIsNone(
            gate.parse_code_exec_request({"code_exec_id": STUB_CODE_EXEC_ID})
        )
        self.assertIsNone(
            gate.parse_code_exec_request(
                {"code_exec_id": STUB_CODE_EXEC_ID, "python_code": "   \n"}
            )
        )
        self.assertIsNone(gate.parse_code_exec_request("not-a-dict"))
        self.assertIsNone(gate.parse_code_exec_request(None))

    def test_parse_defensive_optional_fields(self):
        # rationale / layer_refs absent or mistyped -> honest defaults.
        req = gate.parse_code_exec_request(
            {
                "code_exec_id": STUB_CODE_EXEC_ID,
                "python_code": "result = 1",
                "layer_refs": "not-a-dict",
                "rationale": 42,
            }
        )
        self.assertEqual(req.layer_refs, {})
        self.assertEqual(req.rationale, "")

    def test_decision_mapping(self):
        d = gate.resolve_code_exec_decision(True)
        self.assertEqual((d.decision, d.revised_args), ("proceed", None))
        d = gate.resolve_code_exec_decision(False)
        self.assertEqual((d.decision, d.revised_args), ("cancel", None))

    def test_layer_lines(self):
        req = gate.parse_code_exec_request(
            {
                "code_exec_id": STUB_CODE_EXEC_ID,
                "python_code": "result = 1",
                # ADDITIVE multi-frame extension: a list value is an ordered
                # frame set -- the line honestly reads "N frames".
                "layer_refs": {
                    "depth": "s3://trid3nt-runs/flood/depth.tif",
                    "frames": ["s3://a.tif", "s3://b.tif", "s3://c.tif"],
                },
            }
        )
        lines = gate.code_exec_layer_lines(req)
        self.assertIn("depth: s3://trid3nt-runs/flood/depth.tif", lines)
        self.assertIn("frames: 3 frames", lines)


class TestCodeExecRoundTrip(unittest.TestCase):
    """The wire round trip against the stub's gated 'run-code' turn --
    mirrors TestGateRoundTrip (test_milestone2) for the code-exec seam."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("code exec test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_request_classified_not_raw(self):
        # The regression itself: the envelope must surface as its own kind,
        # never the dropped-on-the-floor "raw" fallthrough.
        self.client.send_chat("please run-code the depth analysis")
        ev = self._await_kind("code-exec-request")
        self.assertEqual(ev.data["code_exec_id"], STUB_CODE_EXEC_ID)
        self.assertEqual(
            ev.data["python_code"], CODE_EXEC_REQUEST_ROW["python_code"]
        )

    def test_approve_round_trip(self):
        self.client.send_chat("please run-code the depth analysis")
        ev = self._await_kind("code-exec-request")
        req = gate.parse_code_exec_request(ev.data)
        self.assertEqual(req.code_exec_id, STUB_CODE_EXEC_ID)
        decision = gate.resolve_code_exec_decision(True)
        # The reply is the EXISTING confirm verb: warning_id == code_exec_id.
        self.client.confirm_payload(
            req.code_exec_id, decision.decision, decision.revised_args
        )
        chunk = self._await_kind("chunk")
        self.assertEqual(chunk.data["delta"], "Code executed.")
        done = self._await_kind("turn-complete")
        self.assertFalse(done.data.get("cancelled"))
        conf = self.server.confirmations[-1]
        self.assertEqual(conf["warning_id"], STUB_CODE_EXEC_ID)
        self.assertEqual(conf["decision"], "proceed")
        self.assertIsNone(conf["revised_args"])  # contract cross-rule

    def test_deny_round_trip(self):
        self.client.send_chat("please run-code the depth analysis")
        ev = self._await_kind("code-exec-request")
        decision = gate.resolve_code_exec_decision(False)
        self.client.confirm_payload(
            ev.data["code_exec_id"], decision.decision, decision.revised_args
        )
        done = self._await_kind("turn-complete")
        self.assertTrue(done.data.get("cancelled"))
        conf = self.server.confirmations[-1]
        self.assertEqual(conf["warning_id"], STUB_CODE_EXEC_ID)
        self.assertEqual(conf["decision"], "cancel")
        self.assertIsNone(conf["revised_args"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
