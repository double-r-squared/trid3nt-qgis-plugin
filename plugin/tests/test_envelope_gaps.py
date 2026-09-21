"""Envelope kinds that used to fall through the client's classifier and be dropped.

One is a gate-WAIT, where the server pauses the turn awaiting a reply, so an
unhandled envelope hung the turn forever; the rest are fire-and-forget side
effects the dock now renders. Offline: a missing correlation id parses to None,
a decision maps to the exact reply, and a gate resumes only on its own reply."""

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
    SPATIAL_INPUT_BBOX_ROW,
    SPATIAL_INPUT_POINT_ROW,
    SPATIAL_INPUT_VECTOR_ROW,
    STUB_SPATIAL_POINT_REQUEST_ID,
    STUB_TOKEN,
    StubAgentServer,
)




class TestSpatialInputParsing(unittest.TestCase):
    def test_parse_point_bbox_vector(self):
        p = gate.parse_spatial_input_request(SPATIAL_INPUT_POINT_ROW)
        self.assertEqual(p.mode, "point")
        b = gate.parse_spatial_input_request(SPATIAL_INPUT_BBOX_ROW)
        self.assertEqual(b.mode, "bbox")
        v = gate.parse_spatial_input_request(SPATIAL_INPUT_VECTOR_ROW)
        self.assertEqual(v.mode, "vector_draw")
        self.assertEqual(v.purpose, "aoi")
        self.assertEqual(v.draw_kind, "polygon")

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
        self.assertIsNone(wire["name"])
        self.assertFalse(wire["cancelled"])

    def test_resolve_point_carries_the_name_the_user_typed(self):
        wire = gate.resolve_spatial_input_point("r1", -82.55, 27.9, " outfall-a ")
        self.assertEqual(wire["name"], "outfall-a")
        self.assertIsNone(gate.resolve_spatial_input_point("r1", 0.0, 0.0, "  ")["name"])
        request = gate.parse_spatial_input_request(
            {"request_id": "r1", "mode": "point", "title": "Draw release"})
        self.assertIn("'outfall-a'", gate.spatial_input_summary(request, wire))

    def test_resolve_bbox(self):
        wire = gate.resolve_spatial_input_bbox("r1", [-82.6, 27.8, -82.5, 27.95])
        self.assertEqual(wire["geometry_type"], "bbox")
        self.assertEqual(wire["coordinates"], [-82.6, 27.8, -82.5, 27.95])

    def test_resolve_cancel(self):
        wire = gate.resolve_spatial_input_cancel("r1")
        self.assertTrue(wire["cancelled"])
        self.assertIsNone(wire["geometry_type"])
        self.assertIsNone(wire["coordinates"])




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




class _RoundTripBase(unittest.TestCase):
    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url, token=STUB_TOKEN)
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


class TestSpatialInputRoundTrip(_RoundTripBase):
    def test_point_pick_resumes_turn(self):
        self.client.send_chat("pick-point the release location")
        ev = self._await_kind("spatial-input-request")
        req = gate.parse_spatial_input_request(ev.data)
        self.assertEqual(req.request_id, STUB_SPATIAL_POINT_REQUEST_ID)
        wire = gate.resolve_spatial_input_point(req.request_id, -82.55, 27.9,
                                                "point-1")
        self.client.send_spatial_input(
            wire["request_id"], geometry_type=wire["geometry_type"],
            coordinates=wire["coordinates"], name=wire["name"],
            cancelled=wire["cancelled"],
        )
        chunk = self._await_kind("chunk")
        self.assertIn("point", chunk.data["delta"])
        self._await_kind("turn-complete")
        got = self.server.spatial_inputs[-1]
        self.assertEqual(got["geometry_type"], "point")
        self.assertEqual(got["coordinates"], [-82.55, 27.9])
        self.assertEqual(got["name"], "point-1")

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
        # A declined draw STILL closes the paused gate (never a hung turn).
        self.client.send_chat("pick-vector the study area")
        ev = self._await_kind("spatial-input-request")
        req = gate.parse_spatial_input_request(ev.data)
        self.assertEqual(req.draw_kind, "polygon")
        wire = gate.resolve_spatial_input_cancel(req.request_id)
        self.client.send_spatial_input(
            wire["request_id"], geometry_type=wire["geometry_type"],
            coordinates=wire["coordinates"], cancelled=wire["cancelled"],
        )
        chunk = self._await_kind("chunk")
        self.assertIn("without it", chunk.data["delta"])
        self._await_kind("turn-complete")
        self.assertTrue(self.server.spatial_inputs[-1]["cancelled"])


class TestSecretsListRoundTrip(_RoundTripBase):
    def test_secrets_list_after_secret_add(self):
        # A key the keys form stored is pushed over secret-add; the daemon
        # answers with a refreshed secrets-list, which must not fall to "raw".
        self.client.push_secret("firms", "SECRET123")
        roster_ev = self._await_kind("secrets-list")
        rows = gate.parse_secrets_list(roster_ev.data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "firms")
        # KEY HYGIENE: the raw key never rode the secrets-list envelope.
        self.assertNotIn("SECRET123", roster_ev.data.get("secrets", [{}])[0].get(
            "vault_ref", ""
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
