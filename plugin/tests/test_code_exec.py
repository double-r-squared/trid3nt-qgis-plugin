"""The code-exec approval gate, offline.

The parse maps the envelope's fields and answers None on a malformed one rather
than crashing; the decision resolves Run to proceed and Deny to cancel with
``revised_args`` ALWAYS None; the reply rides the EXISTING confirm envelope with
``warning_id == code_exec_id``. An approval is followed by the code itself as a
processing-request the session answers; a denial ends the turn cancelled."""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402
from plugin.ui import gate  # noqa: E402
from stub_server import (  # noqa: E402
    CODE_EXEC_REQUEST_ROW,
    STUB_CODE_EXEC_ID,
    STUB_PROCESSING_CODE_REQUEST_ID,
    StubAgentServer,
)


class TestCodeExecParsing(unittest.TestCase):
    def test_parse_fields(self):
        req = gate.parse_code_exec_request(CODE_EXEC_REQUEST_ROW)
        self.assertEqual(req.code_exec_id, STUB_CODE_EXEC_ID)
        # The EXACT code, verbatim -- never a paraphrase (contract).
        self.assertEqual(req.python_code, CODE_EXEC_REQUEST_ROW["python_code"])
        self.assertEqual(req.rationale, "Add two numbers in the session.")
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
        # rationale absent or mistyped -> an honest empty caption.
        req = gate.parse_code_exec_request(
            {
                "code_exec_id": STUB_CODE_EXEC_ID,
                "python_code": "result = 1",
                "rationale": 42,
            }
        )
        self.assertEqual(req.rationale, "")

    def test_decision_mapping(self):
        d = gate.resolve_code_exec_decision(True)
        self.assertEqual((d.decision, d.revised_args), ("proceed", None))
        d = gate.resolve_code_exec_decision(False)
        self.assertEqual((d.decision, d.revised_args), ("cancel", None))

    def test_outcome_chip_and_lines_are_honest(self):
        ok = gate.CodeExecResult(STUB_CODE_EXEC_ID, "ok", stdout_tail="hi\n",
                                 result={"value": 4})
        self.assertIn("succeeded", gate.code_exec_result_chip(ok))
        self.assertIn("Result: 4", gate.code_exec_result_lines(ok))
        self.assertIn("stdout: hi", gate.code_exec_result_lines(ok))
        failed = gate.CodeExecResult(STUB_CODE_EXEC_ID, "error",
                                     stderr_tail="NameError: x")
        self.assertIn("errored", gate.code_exec_result_chip(failed))
        self.assertIn("stderr: NameError: x", gate.code_exec_result_lines(failed))


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
        # The approved code comes back to THIS session to run, joined to the
        # card by code_exec_id; the answer resumes the turn.
        proc = self._await_kind("processing-request")
        self.assertEqual(proc.data["kind"], "code")
        self.assertEqual(proc.data["code"], CODE_EXEC_REQUEST_ROW["python_code"])
        self.assertEqual(proc.data["code_exec_id"], STUB_CODE_EXEC_ID)
        self.assertEqual(proc.data["request_id"], STUB_PROCESSING_CODE_REQUEST_ID)
        self.client.send_processing_response(
            proc.data["request_id"], "ok", result={"value": 4}, stdout=""
        )
        chunk = self._await_kind("chunk")
        self.assertEqual(chunk.data["delta"], "Code executed: result=4.")
        done = self._await_kind("turn-complete")
        self.assertFalse(done.data.get("cancelled"))
        conf = self.server.confirmations[-1]
        self.assertEqual(conf["warning_id"], STUB_CODE_EXEC_ID)
        self.assertEqual(conf["decision"], "proceed")
        self.assertIsNone(conf["revised_args"])  # contract cross-rule
        self.assertEqual(self.server.processing_responses[-1]["status"], "ok")

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
