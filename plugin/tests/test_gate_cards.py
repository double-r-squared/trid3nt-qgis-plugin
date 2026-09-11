"""Gate card parsing and the round trip over the wire.

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
from plugin.ui import gate  # noqa: E402
from stub_server import (  # noqa: E402
    PAYLOAD_WARNING_HARDCAP_ROW,
    PAYLOAD_WARNING_ROW,
    STUB_WARNING_ID,
    StubAgentServer,
)



class TestGateParsing(unittest.TestCase):
    def test_parse_payload_warning_fields(self):
        w = gate.parse_payload_warning(PAYLOAD_WARNING_ROW)
        self.assertEqual(w.warning_id, STUB_WARNING_ID)
        self.assertEqual(w.tool_name, "run_sfincs_simulation")
        self.assertEqual(w.estimated_mb, 48.0)
        self.assertEqual(w.threshold_mb, 25.0)
        self.assertTrue(w.can_proceed)
        self.assertTrue(w.can_narrow)
        self.assertEqual(w.resolution_choices, [10.0, 30.0, 60.0, 120.0])
        self.assertEqual(w.suggested_resolution_m, 30.0)
        self.assertEqual(w.alternative_args, {"grid_resolution_m": 60.0})
        self.assertIsNotNone(w.time_scale)
        # Malformed: no warning_id -> unusable
        self.assertIsNone(gate.parse_payload_warning({"tool_name": "x"}))
        self.assertIsNone(gate.parse_payload_warning("not-a-dict"))

    def test_hardcap_removes_proceed(self):
        w = gate.parse_payload_warning(PAYLOAD_WARNING_HARDCAP_ROW)
        self.assertFalse(w.can_proceed)
        self.assertTrue(w.can_narrow)
        # summary carries the honest hard-cap line
        self.assertTrue(any("Hard cap" in line for line in gate.summary_lines(w)))

    def test_summary_lines_compute_wording(self):
        # Local-cloud fingerprint fix: the "local" compute lane
        # renders plain CPU wording ("local run (8 CPU)"), never the cloud
        # "vCPU" label; any other compute label (a remote/cloud agent) keeps
        # the prior wording unchanged.
        import copy

        row = copy.deepcopy(PAYLOAD_WARNING_ROW)
        row["granularity"]["compute_class"] = "local"
        row["granularity"]["vcpus"] = 8
        joined = "\n".join(gate.summary_lines(gate.parse_payload_warning(row)))
        self.assertIn("local run (8 CPU)", joined)
        self.assertNotIn("vCPU", joined)
        # vcpus <= 1 (the fetch-resolution gate) -> bare "local run".
        row["granularity"]["vcpus"] = 1
        joined = "\n".join(gate.summary_lines(gate.parse_payload_warning(row)))
        self.assertIn("local run", joined)
        self.assertNotIn("local run (", joined)
        # A cloud/remote compute label keeps the prior cloud wording.
        row["granularity"]["compute_class"] = "standard"
        row["granularity"]["vcpus"] = 8
        joined = "\n".join(gate.summary_lines(gate.parse_payload_warning(row)))
        self.assertIn("standard (8 vCPU)", joined)

    def test_resolve_gate_decision_rules(self):
        w = gate.parse_payload_warning(PAYLOAD_WARNING_ROW)
        # unchanged rung -> proceed, revised None (web ResolutionPickerCard rule)
        d = gate.resolve_gate_decision(w, chosen_resolution_m=30.0)
        self.assertEqual((d.decision, d.revised_args), ("proceed", None))
        # changed rung -> narrow_scope with the EXACT resolution_param key
        d = gate.resolve_gate_decision(w, chosen_resolution_m=60.0)
        self.assertEqual(d.decision, "narrow_scope")
        self.assertEqual(d.revised_args, {"grid_resolution_m": 60.0})
        # changed cadence + duration merge into the SAME revised dict
        d = gate.resolve_gate_decision(
            w, chosen_resolution_m=60.0, interval_min=10.0, duration_hr=12.0
        )
        self.assertEqual(
            d.revised_args,
            {"grid_resolution_m": 60.0, "output_interval_min": 10.0, "duration_hr": 12.0},
        )
        # cancel wins
        d = gate.resolve_gate_decision(w, cancel=True, chosen_resolution_m=60.0)
        self.assertEqual((d.decision, d.revised_args), ("cancel", None))
        # interval below the deck floor is re-floored (min_interval_min=1.0)
        d = gate.resolve_gate_decision(w, interval_min=0.5)
        self.assertEqual(d.revised_args, {"output_interval_min": 1.0})

    def test_resolve_gate_decision_hardcap(self):
        w = gate.parse_payload_warning(PAYLOAD_WARNING_HARDCAP_ROW)
        # unchanged -> REFUSED (proceed not offered), honest note
        d = gate.resolve_gate_decision(w, chosen_resolution_m=30.0)
        self.assertIsNone(d.decision)
        self.assertIn("hard cap", d.note)
        # coarser rung -> narrow_scope allowed
        d = gate.resolve_gate_decision(w, chosen_resolution_m=120.0)
        self.assertEqual(d.decision, "narrow_scope")
        self.assertEqual(d.revised_args, {"grid_resolution_m": 120.0})

    def test_estimates_mirror_web_math(self):
        g = PAYLOAD_WARNING_ROW["granularity"]
        # same rung -> authoritative numbers unchanged
        self.assertEqual(gate.estimate_cells(g, 30.0), 46000)
        self.assertAlmostEqual(gate.estimate_eta_seconds(g, 30.0), 70.0)
        # coarser (60 m): cells scale by (30/60)^2 = 1/4
        self.assertEqual(gate.estimate_cells(g, 60.0), 11500)
        self.assertAlmostEqual(gate.estimate_eta_seconds(g, 60.0), 17.5)
        # frames: duration_hr*60/interval clamped to [1, max_frames]
        ts = PAYLOAD_WARNING_ROW["time_scale"]
        self.assertEqual(gate.estimate_frames(ts, 5.0, 6.0), 72)
        self.assertEqual(gate.estimate_frames(ts, 1.0, 24.0), 144)  # capped
        self.assertEqual(gate.estimate_frames(ts, 0.25, 6.0), 144)  # floored at 1 min


class TestGateRoundTrip(unittest.TestCase):
    """confirm_payload round trips against the stub's gated 'simulate' turn."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("gate test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_proceed_round_trip(self):
        self.client.send_chat("simulate a flood here")
        warning = self._await_kind("payload-warning")
        parsed = gate.parse_payload_warning(warning.data)
        self.assertEqual(parsed.warning_id, STUB_WARNING_ID)
        # sim must NOT have started: no chunk before the confirmation
        self.client.confirm_payload(parsed.warning_id, "proceed")
        chunk = self._await_kind("chunk")
        self.assertEqual(chunk.data["delta"], "Starting the run at 30 m.")
        self._await_kind("turn-complete")
        conf = self.server.confirmations[-1]
        self.assertEqual(conf["warning_id"], STUB_WARNING_ID)
        self.assertEqual(conf["decision"], "proceed")
        self.assertIsNone(conf["revised_args"])  # contract cross-rule

    def test_cancel_round_trip(self):
        self.client.send_chat("simulate a flood here")
        warning = self._await_kind("payload-warning")
        self.client.confirm_payload(warning.data["warning_id"], "cancel")
        done = self._await_kind("turn-complete")
        self.assertTrue(done.data.get("cancelled"))
        conf = self.server.confirmations[-1]
        self.assertEqual(conf["decision"], "cancel")
        self.assertIsNone(conf["revised_args"])

    def test_narrow_scope_round_trip_carries_revised_args(self):
        self.client.send_chat("simulate a flood here")
        warning = self._await_kind("payload-warning")
        parsed = gate.parse_payload_warning(warning.data)
        decision = gate.resolve_gate_decision(parsed, chosen_resolution_m=60.0)
        self.client.confirm_payload(
            parsed.warning_id, decision.decision, decision.revised_args
        )
        chunk = self._await_kind("chunk")
        self.assertEqual(chunk.data["delta"], "Starting the run at 60 m.")
        conf = self.server.confirmations[-1]
        self.assertEqual(conf["decision"], "narrow_scope")
        self.assertEqual(conf["revised_args"], {"grid_resolution_m": 60.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
