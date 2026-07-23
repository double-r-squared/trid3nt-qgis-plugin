"""LANE A envelope-gap coverage (2026-07-23).

Eight server-emitted envelope kinds previously fell through the plugin
client's classifier as kind="raw" and were dropped by the dock. Two of them
are GATE-WAITS -- the server PAUSES the turn awaiting a reply, so an unhandled
envelope hung the turn forever (the exact bug class the code-exec gate was):

  * region-choice-request  (state-bbox-fallback narrowing; reply
    region-choice-provided) -- CRITICAL gate-WAIT.
  * spatial-input-request  (the agent needs a picked geometry; reply
    spatial-input-response) -- CRITICAL gate-WAIT.

The lighter six are fire-and-forget side effects the dock now renders:

  * code-exec-result  (the run outcome after an approved code-exec-request).
  * secrets-list      (the per-user/per-Case secret roster).
  * impact-envelope   (Pelicun portfolio damage/loss aggregates).
  * lesson-added      (the LESSONS LOOP ack).
  * (chart-emission + solve-progress were already classified/rendered by a
    prior lane -- covered in test_charts / the SimCard harness -- so this file
    does not re-cover them.)

Coverage here (offline -- pure parse/resolve logic + stub_server round trips;
the Qt cards themselves are exercised at the qt harness level):

* ``gate.parse_*`` field mapping + malformed-envelope honesty (a missing
  correlation id -> None, never a crash -- a hung turn is the failure this
  guards against).
* ``gate.resolve_*`` decision -> wire mapping (the exact reply the server
  consumes).
* the wire round trip against the stub: the gate-WAITs pause the turn and
  resume ONLY on the matching reply; the side effects surface as their own
  kind, never the dropped-on-the-floor "raw" fallthrough.
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
    IMPACT_ENVELOPE_ROW,
    LESSON_ADDED_ROW,
    REGION_CHOICE_REQUEST_ROW,
    SPATIAL_INPUT_BBOX_ROW,
    SPATIAL_INPUT_POINT_ROW,
    SPATIAL_INPUT_VECTOR_ROW,
    STUB_CODE_EXEC_ID,
    STUB_REGION_CHOICE_REQUEST_ID,
    STUB_REGION_ID,
    STUB_SPATIAL_POINT_REQUEST_ID,
    StubAgentServer,
)


# =========================================================================== #
# Region-choice (CRITICAL gate-WAIT) -- pure logic
# =========================================================================== #


class TestRegionChoiceParsing(unittest.TestCase):
    def test_parse_fields(self):
        req = gate.parse_region_choice(REGION_CHOICE_REQUEST_ROW)
        self.assertEqual(req.request_id, STUB_REGION_CHOICE_REQUEST_ID)
        self.assertEqual(req.state_name, "Florida")
        self.assertEqual(req.state_code, "FL")
        self.assertEqual(req.state_label, "Florida (FL)")
        self.assertEqual(len(req.candidates), 2)
        self.assertEqual(req.candidates[0].region_id, STUB_REGION_ID)
        self.assertEqual(req.candidates[0].name, "Lee County")
        self.assertEqual(req.candidates[0].admin_level, "county")

    def test_parse_malformed_is_none(self):
        # No request_id -> nothing to correlate; the paused turn would hang.
        self.assertIsNone(gate.parse_region_choice({"state_name": "Florida"}))
        self.assertIsNone(gate.parse_region_choice("not-a-dict"))
        self.assertIsNone(gate.parse_region_choice(None))

    def test_parse_skips_bad_candidates(self):
        req = gate.parse_region_choice(
            {
                "request_id": "r1",
                "candidates": [
                    {"region_id": "a", "name": "A", "bbox": [-1, -1, 1, 1]},
                    {"name": "no-id", "bbox": [-1, -1, 1, 1]},  # skipped
                    {"region_id": "b", "name": "B", "bbox": "bad"},  # skipped
                    "not-a-dict",  # skipped
                ],
            }
        )
        self.assertEqual([c.region_id for c in req.candidates], ["a"])

    def test_resolve_region_pick(self):
        req = gate.parse_region_choice(REGION_CHOICE_REQUEST_ROW)
        wire = gate.resolve_region_choice(req, STUB_REGION_ID)
        self.assertEqual(wire["choice"], "region")
        self.assertEqual(wire["selected_region_id"], STUB_REGION_ID)
        self.assertEqual(wire["selected_bbox"], [-82.32, 26.32, -81.56, 26.79])

    def test_resolve_whole_state_is_decline_path(self):
        req = gate.parse_region_choice(REGION_CHOICE_REQUEST_ROW)
        # None (or an unknown id) -> the honest already-resolved default.
        for sel in (None, "unknown-county"):
            wire = gate.resolve_region_choice(req, sel)
            self.assertEqual(wire["choice"], "whole_state")
            self.assertIsNone(wire["selected_region_id"])
            self.assertIsNone(wire["selected_bbox"])


# =========================================================================== #
# Spatial-input (CRITICAL gate-WAIT) -- pure logic
# =========================================================================== #


class TestSpatialInputParsing(unittest.TestCase):
    def test_parse_point_bbox_vector(self):
        p = gate.parse_spatial_input_request(SPATIAL_INPUT_POINT_ROW)
        self.assertEqual(p.mode, "point")
        self.assertTrue(p.supported)
        b = gate.parse_spatial_input_request(SPATIAL_INPUT_BBOX_ROW)
        self.assertEqual(b.mode, "bbox")
        self.assertTrue(b.supported)
        v = gate.parse_spatial_input_request(SPATIAL_INPUT_VECTOR_ROW)
        self.assertEqual(v.mode, "vector_draw")
        self.assertEqual(v.purpose, "barrier")
        # vector_draw is the web terra-draw surface -- the plugin degrades
        # honestly (Cancel closes the gate), so it is NOT "supported".
        self.assertFalse(v.supported)

    def test_parse_malformed_is_none(self):
        self.assertIsNone(gate.parse_spatial_input_request({"mode": "point"}))
        self.assertIsNone(
            gate.parse_spatial_input_request({"request_id": "r1", "mode": "blob"})
        )
        self.assertIsNone(gate.parse_spatial_input_request("not-a-dict"))

    def test_resolve_point(self):
        wire = gate.resolve_spatial_input_point("r1", -82.55, 27.9)
        self.assertEqual(wire["geometry_type"], "point")
        self.assertEqual(wire["coordinates"], [-82.55, 27.9])
        self.assertIsNone(wire["features"])
        self.assertFalse(wire["cancelled"])

    def test_resolve_bbox(self):
        wire = gate.resolve_spatial_input_bbox("r1", [-82.6, 27.8, -82.5, 27.95])
        self.assertEqual(wire["geometry_type"], "bbox")
        self.assertEqual(wire["coordinates"], [-82.6, 27.8, -82.5, 27.95])

    def test_resolve_cancel(self):
        wire = gate.resolve_spatial_input_cancel("r1")
        self.assertTrue(wire["cancelled"])
        self.assertIsNone(wire["geometry_type"])
        self.assertIsNone(wire["coordinates"])


# =========================================================================== #
# code-exec-result -- pure logic
# =========================================================================== #


class TestCodeExecResultParsing(unittest.TestCase):
    def test_parse_fields(self):
        res = gate.parse_code_exec_result(
            {
                "code_exec_id": STUB_CODE_EXEC_ID,
                "status": "ok",
                "stdout_tail": "done",
                "result": {"kind": "scalar", "value": 0.31},
                "duration_s": 1.4,
            }
        )
        self.assertEqual(res.code_exec_id, STUB_CODE_EXEC_ID)
        self.assertTrue(res.ok)
        self.assertEqual(res.result["kind"], "scalar")
        self.assertEqual(res.duration_s, 1.4)

    def test_parse_malformed_is_none(self):
        self.assertIsNone(gate.parse_code_exec_result({"status": "ok"}))
        self.assertIsNone(
            gate.parse_code_exec_result({"code_exec_id": "x"})  # no status
        )
        self.assertIsNone(gate.parse_code_exec_result(None))

    def test_chip_is_honest(self):
        # A blocked/timeout run is NEVER dressed up as ok.
        blocked = gate.parse_code_exec_result(
            {"code_exec_id": "x", "status": "blocked", "truncated": True}
        )
        chip = gate.code_exec_result_chip(blocked)
        self.assertIn("blocked", chip)
        self.assertIn("truncated", chip)
        ok = gate.parse_code_exec_result(
            {"code_exec_id": "x", "status": "ok", "duration_s": 2}
        )
        self.assertIn("succeeded", gate.code_exec_result_chip(ok))


# =========================================================================== #
# secrets-list -- pure logic
# =========================================================================== #


class TestSecretsListParsing(unittest.TestCase):
    def test_parse_rows_no_raw_key(self):
        rows = gate.parse_secrets_list(
            {
                "secrets": [
                    {
                        "secret_id": "s1",
                        "provider": "firms",
                        "case_id": "c1",
                        "label": "my FIRMS key",
                        "vault_ref": "file-vault://x",
                        "is_active": True,
                    },
                    {"provider": "airnow"},  # no secret_id -> skipped
                    "not-a-dict",  # skipped
                ]
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "firms")
        self.assertEqual(rows[0].display, "my FIRMS key")
        # No raw key material anywhere on the parsed row.
        self.assertFalse(hasattr(rows[0], "key_value"))

    def test_lines_skip_inactive(self):
        rows = gate.parse_secrets_list(
            {
                "secrets": [
                    {"secret_id": "s1", "provider": "firms", "is_active": True},
                    {"secret_id": "s2", "provider": "airnow", "is_active": False},
                ]
            }
        )
        lines = gate.secrets_list_lines(rows)
        self.assertEqual(len(lines), 1)
        self.assertIn("firms", lines[0])


# =========================================================================== #
# impact-envelope -- pure logic
# =========================================================================== #


class TestImpactEnvelopeParsing(unittest.TestCase):
    def test_parse_fields(self):
        s = gate.parse_impact_envelope(IMPACT_ENVELOPE_ROW)
        self.assertEqual(s.n_structures_total, 1840)
        self.assertEqual(s.n_structures_damaged, 612)
        self.assertEqual(s.expected_loss_usd, 12_400_000.0)

    def test_parse_malformed_is_none(self):
        # n_structures_total is the ImpactEnvelope key signal.
        self.assertIsNone(gate.parse_impact_envelope({"expected_loss_usd": 1.0}))
        self.assertIsNone(gate.parse_impact_envelope(None))

    def test_summary_lines(self):
        s = gate.parse_impact_envelope(IMPACT_ENVELOPE_ROW)
        lines = gate.impact_summary_lines(s)
        joined = "\n".join(lines)
        self.assertIn("1,840", joined)
        self.assertIn("612 damaged", joined)
        self.assertIn("$12.4M", joined)


# =========================================================================== #
# lesson-added -- pure logic
# =========================================================================== #


class TestLessonAddedParsing(unittest.TestCase):
    def test_parse_fields(self):
        added = gate.parse_lesson_added(LESSON_ADDED_ROW)
        self.assertEqual(added.lesson_id, LESSON_ADDED_ROW["lesson_id"])
        self.assertIn("OSM Overpass", gate.lesson_added_line(added))

    def test_parse_malformed_is_none(self):
        self.assertIsNone(gate.parse_lesson_added({}))
        self.assertIsNone(gate.parse_lesson_added({"lesson": "   "}))
        self.assertIsNone(gate.parse_lesson_added(None))


# =========================================================================== #
# Wire round trips against the stub
# =========================================================================== #


class _RoundTripBase(unittest.TestCase):
    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("envelope gap test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")


class TestRegionChoiceRoundTrip(_RoundTripBase):
    def test_classified_not_raw(self):
        # The regression itself: the gate-WAIT surfaces as its own kind, never
        # the dropped-on-the-floor "raw" fallthrough that hung the turn.
        self.client.send_chat("narrow-region south florida flood")
        ev = self._await_kind("region-choice-request")
        self.assertEqual(ev.data["request_id"], STUB_REGION_CHOICE_REQUEST_ID)

    def test_region_pick_resumes_turn(self):
        self.client.send_chat("narrow-region south florida flood")
        ev = self._await_kind("region-choice-request")
        req = gate.parse_region_choice(ev.data)
        wire = gate.resolve_region_choice(req, STUB_REGION_ID)
        self.client.send_region_choice(
            wire["request_id"], wire["choice"],
            selected_region_id=wire["selected_region_id"],
            selected_bbox=wire["selected_bbox"],
        )
        chunk = self._await_kind("chunk")
        self.assertIn(STUB_REGION_ID, chunk.data["delta"])
        self._await_kind("turn-complete")
        got = self.server.region_choices[-1]
        self.assertEqual(got["choice"], "region")
        self.assertEqual(got["selected_region_id"], STUB_REGION_ID)

    def test_whole_state_is_decline_path(self):
        self.client.send_chat("narrow-region south florida flood")
        ev = self._await_kind("region-choice-request")
        req = gate.parse_region_choice(ev.data)
        wire = gate.resolve_region_choice(req, None)  # keep the whole state
        self.client.send_region_choice(
            wire["request_id"], wire["choice"],
            selected_region_id=wire["selected_region_id"],
            selected_bbox=wire["selected_bbox"],
        )
        chunk = self._await_kind("chunk")
        self.assertIn("whole state", chunk.data["delta"].lower())
        self._await_kind("turn-complete")
        self.assertEqual(self.server.region_choices[-1]["choice"], "whole_state")


class TestSpatialInputRoundTrip(_RoundTripBase):
    def test_point_pick_resumes_turn(self):
        self.client.send_chat("pick-point the release location")
        ev = self._await_kind("spatial-input-request")
        req = gate.parse_spatial_input_request(ev.data)
        self.assertEqual(req.request_id, STUB_SPATIAL_POINT_REQUEST_ID)
        wire = gate.resolve_spatial_input_point(req.request_id, -82.55, 27.9)
        self.client.send_spatial_input(
            wire["request_id"], geometry_type=wire["geometry_type"],
            coordinates=wire["coordinates"], cancelled=wire["cancelled"],
        )
        chunk = self._await_kind("chunk")
        self.assertIn("point", chunk.data["delta"])
        self._await_kind("turn-complete")
        got = self.server.spatial_inputs[-1]
        self.assertEqual(got["geometry_type"], "point")
        self.assertEqual(got["coordinates"], [-82.55, 27.9])

    def test_bbox_pick_resumes_turn(self):
        self.client.send_chat("pick-bbox the study area")
        ev = self._await_kind("spatial-input-request")
        req = gate.parse_spatial_input_request(ev.data)
        wire = gate.resolve_spatial_input_bbox(
            req.request_id, [-82.6, 27.8, -82.5, 27.95]
        )
        self.client.send_spatial_input(
            wire["request_id"], geometry_type=wire["geometry_type"],
            coordinates=wire["coordinates"], cancelled=wire["cancelled"],
        )
        self._await_kind("chunk")
        self._await_kind("turn-complete")
        self.assertEqual(self.server.spatial_inputs[-1]["geometry_type"], "bbox")

    def test_vector_draw_cancel_closes_gate(self):
        # The plugin cannot draw tagged barriers -- the honest degrade cancels,
        # which STILL closes the paused gate (never a hung turn).
        self.client.send_chat("pick-vector the flood barriers")
        ev = self._await_kind("spatial-input-request")
        req = gate.parse_spatial_input_request(ev.data)
        self.assertFalse(req.supported)
        wire = gate.resolve_spatial_input_cancel(req.request_id)
        self.client.send_spatial_input(
            wire["request_id"], geometry_type=wire["geometry_type"],
            coordinates=wire["coordinates"], cancelled=wire["cancelled"],
        )
        chunk = self._await_kind("chunk")
        self.assertIn("without it", chunk.data["delta"])
        self._await_kind("turn-complete")
        self.assertTrue(self.server.spatial_inputs[-1]["cancelled"])


class TestCodeExecResultRoundTrip(_RoundTripBase):
    def test_result_follows_approved_run(self):
        self.client.send_chat("please run-code the depth analysis")
        ev = self._await_kind("code-exec-request")
        req = gate.parse_code_exec_request(ev.data)
        decision = gate.resolve_code_exec_decision(True)
        self.client.confirm_payload(
            req.code_exec_id, decision.decision, decision.revised_args
        )
        # The run outcome surfaces as its own kind, never "raw".
        res_ev = self._await_kind("code-exec-result")
        res = gate.parse_code_exec_result(res_ev.data)
        self.assertEqual(res.code_exec_id, STUB_CODE_EXEC_ID)
        self.assertTrue(res.ok)
        self.assertIn("succeeded", gate.code_exec_result_chip(res))
        self._await_kind("turn-complete")


class TestSecretsListRoundTrip(_RoundTripBase):
    def test_secrets_list_after_secret_add(self):
        # The credential flow's secret-add emits a refreshed secrets-list --
        # previously that fell through to "raw".
        self.client.send_chat("need-key the fire detections")
        ev = self._await_kind("credential-request")
        req = gate.parse_credential_request(ev.data)
        self.client.submit_credential(req.request_id, req.provider_id, "SECRET123")
        roster_ev = self._await_kind("secrets-list")
        rows = gate.parse_secrets_list(roster_ev.data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "firms")
        # KEY HYGIENE: the raw key never rode the secrets-list envelope.
        self.assertNotIn("SECRET123", roster_ev.data.get("secrets", [{}])[0].get(
            "vault_ref", ""
        ))
        self._await_kind("turn-complete")


class TestImpactEnvelopeRoundTrip(_RoundTripBase):
    def test_classified_not_raw(self):
        self.client.send_chat("assess the impact over the AOI")
        ev = self._await_kind("impact-envelope")
        s = gate.parse_impact_envelope(ev.data)
        self.assertEqual(s.n_structures_total, 1840)
        self._await_kind("turn-complete")


class TestLessonAddedRoundTrip(_RoundTripBase):
    def test_classified_not_raw(self):
        self.client.send_chat("save that lesson for next time")
        ev = self._await_kind("lesson-added")
        added = gate.parse_lesson_added(ev.data)
        self.assertIn("OSM Overpass", gate.lesson_added_line(added))
        self._await_kind("turn-complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
