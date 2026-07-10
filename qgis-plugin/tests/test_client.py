"""Connection-layer tests for the TRID3NT QGIS plugin (no QGIS required).

Run with the trid3nt-local agent venv python (needs ``websockets`` for the
stub server only -- the client under test is pure stdlib):

    cd qgis-plugin
    ../venvs/agent/bin/python -m unittest discover -s tests -v

or simply ``make test`` from qgis-plugin/.
"""

from __future__ import annotations

import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trid3nt"))
sys.path.insert(0, os.path.dirname(__file__))

import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    RASTER_LAYER_ROW,
    S3_VECTOR_LAYER_ROW,
    STUB_CASE_ID,
    STUB_USER_ID,
    StubAgentServer,
    VECTOR_LAYER_ROW,
)


# --------------------------------------------------------------------------- #
# Pure helpers (no server)
# --------------------------------------------------------------------------- #


class TestPureHelpers(unittest.TestCase):
    def test_ulid_format(self):
        seen = {tc.new_ulid() for _ in range(50)}
        self.assertEqual(len(seen), 50, "ULIDs must be unique")
        for u in seen:
            self.assertRegex(u, r"^[0-9A-HJKMNP-TV-Z]{26}$")

    def test_envelope_shape(self):
        env = tc.make_envelope("user-message", "SESSION1", {"text": "hi"}, case_id="CASE1")
        self.assertEqual(env["type"], "user-message")
        self.assertEqual(env["session_id"], "SESSION1")
        self.assertEqual(env["case_id"], "CASE1")
        self.assertEqual(env["payload"], {"text": "hi"})
        self.assertRegex(env["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(len(env["id"]), 26)

    def test_build_ws_url_token_carrier(self):
        self.assertEqual(
            tc.build_ws_url("ws://127.0.0.1:8765/ws", None),
            "ws://127.0.0.1:8765/ws",
        )
        self.assertEqual(
            tc.build_ws_url("wss://example.com/ws", "abc/123=="),
            "wss://example.com/ws?st=abc%2F123%3D%3D",
        )
        # Existing query string joins with &
        self.assertEqual(
            tc.build_ws_url("wss://example.com/ws?sid=X", "tok"),
            "wss://example.com/ws?sid=X&st=tok",
        )

    def test_s3_to_http(self):
        self.assertEqual(
            tc.s3_to_http("s3://trid3nt-runs/vectors/rivers.geojson", "http://127.0.0.1:9000"),
            "http://127.0.0.1:9000/trid3nt-runs/vectors/rivers.geojson",
        )
        self.assertEqual(
            tc.s3_to_http("s3://bucket/a/b/c.fgb", "http://127.0.0.1:9000/"),
            "http://127.0.0.1:9000/bucket/a/b/c.fgb",
        )
        self.assertIsNone(tc.s3_to_http("gs://bucket/key", "http://127.0.0.1:9000"))
        self.assertIsNone(tc.s3_to_http("s3://bucketonly", "http://127.0.0.1:9000"))

    def test_qgis_xyz_uri(self):
        template = "http://127.0.0.1:8080/cog/tiles/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fk.tif&rescale=0,10"
        uri = tc.qgis_xyz_uri(template)
        self.assertTrue(uri.startswith("type=xyz&url="))
        self.assertTrue(uri.endswith("&zmin=0&zmax=24"))
        encoded = uri[len("type=xyz&url="):-len("&zmin=0&zmax=24")]
        # MINIMAL encoding contract (2026-07-10): the installed QGIS build
        # does not percent-decode the url component, so everything must stay
        # literal EXCEPT the template's own query ampersands (%26), which
        # would otherwise split the provider-uri parameter list.
        self.assertNotIn("&", encoded)
        self.assertEqual(encoded.replace("%26", "&"), template)
        # Placeholders, scheme and ? stay literal so QGIS can substitute
        # tiles and the tile server sees its query verbatim.
        self.assertIn("{z}", encoded)
        self.assertIn("http://", encoded)
        self.assertIn("?url=s3%3A%2F%2Fb%2Fk.tif", encoded)

    def test_qgis_xyz_uri_no_query(self):
        template = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        uri = tc.qgis_xyz_uri(template, zmin=0, zmax=19)
        self.assertEqual(uri, f"type=xyz&url={template}&zmin=0&zmax=19")

    def test_parse_layer_events(self):
        payload = {
            "loaded_layers": [
                RASTER_LAYER_ROW,
                VECTOR_LAYER_ROW,
                {"name": "no layer_id -> skipped"},
                "not-a-dict",
            ]
        }
        events = tc.parse_layer_events(payload)
        self.assertEqual(len(events), 2)
        raster, vector = events
        self.assertEqual(raster.layer_type, "raster")
        self.assertEqual(raster.tile_template, RASTER_LAYER_ROW["uri"])
        self.assertIn("{z}", raster.tile_template)
        self.assertEqual(vector.layer_type, "vector")
        self.assertIsNone(vector.tile_template)
        self.assertEqual(vector.inline_geojson["type"], "FeatureCollection")
        # Defensive: junk payloads
        self.assertEqual(tc.parse_layer_events({}), [])
        self.assertEqual(tc.parse_layer_events({"loaded_layers": "junk"}), [])

    def test_parse_pipeline_steps(self):
        payload = {
            "steps": [
                {"step_id": "s1", "name": "fetch_elevation", "tool_name": "fetch_elevation", "state": "running"},
                {"step_id": "s2", "name": "child", "tool_name": "run_solver", "state": "pending", "parent_step_id": "s1"},
                {"no_step_id": True},
            ]
        }
        steps = tc.parse_pipeline_steps(payload)
        self.assertEqual([s.step_id for s in steps], ["s1", "s2"])
        self.assertEqual(steps[0].state, "running")
        self.assertEqual(steps[1].parent_step_id, "s1")


# --------------------------------------------------------------------------- #
# Live protocol tests against the stub server
# --------------------------------------------------------------------------- #


class StubServerTestCase(unittest.TestCase):
    """Each test gets a fresh stub server (cheap; ephemeral port)."""

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def _connect(self, **kwargs) -> tc.AgentClient:
        client = tc.AgentClient(self.server.url, **kwargs)
        self.addCleanup(client.close)
        return client

    def _collect_until_turn_complete(self, client, deadline_s=15.0):
        events = []
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = client.next_event(timeout=1.0)
            if ev is None:
                continue
            events.append(ev)
            if ev.kind == "turn-complete":
                return events
        self.fail(f"no turn-complete within {deadline_s}s; got {[e.kind for e in events]}")


class TestHandshake(StubServerTestCase):
    def test_anonymous_handshake(self):
        client = self._connect()
        user_id = client.connect()
        self.assertEqual(user_id, STUB_USER_ID)
        self.assertTrue(client.is_anonymous)
        self.assertEqual(client.last_session_state.get("loaded_layers"), [])
        # First two frames the server saw: auth-token (empty token) then
        # session-resume -- the exact reference-driver ordering.
        types = [e["type"] for e in self.server.received]
        self.assertEqual(types[:2], ["auth-token", "session-resume"])
        self.assertEqual(self.server.received[0]["payload"]["token"], "")
        # Anonymous local mode: no ?st= on the upgrade path.
        self.assertNotIn("st=", self.server.paths[0])

    def test_remote_token_rides_query_and_envelope(self):
        client = self._connect(token="jwt-abc/123==")
        client.connect()
        self.assertFalse(client.is_anonymous)
        # ?st= carrier on the upgrade request path (URL-encoded)
        self.assertIn("st=jwt-abc%2F123%3D%3D", self.server.paths[0])
        # and the in-band auth-token envelope carries it verbatim
        self.assertEqual(self.server.received[0]["payload"]["token"], "jwt-abc/123==")

    def test_handshake_failure_on_dead_port(self):
        dead = tc.AgentClient("ws://127.0.0.1:1/ws", connect_timeout=2)
        with self.assertRaises((tc.WebSocketError, OSError)):
            dead.connect()


class TestCaseAndChat(StubServerTestCase):
    def test_create_case(self):
        client = self._connect()
        client.connect()
        case_id = client.create_case("QGIS session")
        self.assertEqual(case_id, STUB_CASE_ID)
        self.assertEqual(client.case_id, STUB_CASE_ID)
        create = [e for e in self.server.received if e["type"] == "case-command"][0]
        self.assertEqual(create["payload"]["command"], "create")
        self.assertEqual(create["payload"]["args"]["title"], "QGIS session")

    def test_chat_round_trip_streams_and_layers(self):
        client = self._connect()
        client.connect()
        client.create_case("chat test")
        client.send_chat("Fetch a DEM for Asheville")

        # The outbound user-message is stamped with the active case.
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        # (may lag; wait for the round trip below before asserting)

        events = self._collect_until_turn_complete(client)
        kinds = [e.kind for e in events]
        self.assertIn("pipeline", kinds)
        self.assertIn("chunk", kinds)
        self.assertIn("session-state", kinds)
        self.assertEqual(kinds[-1], "turn-complete")

        # Assembled narration text
        text = "".join(e.data["delta"] for e in events if e.kind == "chunk")
        self.assertEqual(text, "Here is the DEM you asked for.")
        done_flags = [e.data["done"] for e in events if e.kind == "chunk"]
        self.assertEqual(done_flags, [False, True])

        # Pipeline steps parsed into dataclasses
        pipeline_events = [e for e in events if e.kind == "pipeline"]
        self.assertEqual(pipeline_events[0].data["steps"][0].tool_name, "fetch_elevation")
        self.assertEqual(pipeline_events[-1].data["steps"][0].state, "complete")

        # Layer events: raster tile template + inline-geojson vector + s3-only
        # vector. The inline pad is >64 KiB so this round trip also proves the
        # 64-bit frame-length decode path.
        layer_events = [e for e in events if e.kind == "session-state"][-1].data["layers"]
        by_id = {le.layer_id: le for le in layer_events}
        raster = by_id[RASTER_LAYER_ROW["layer_id"]]
        self.assertEqual(raster.tile_template, RASTER_LAYER_ROW["uri"])
        vector = by_id[VECTOR_LAYER_ROW["layer_id"]]
        self.assertEqual(
            len(vector.inline_geojson["features"][0]["properties"]["pad"]), 70000
        )
        s3vec = by_id[S3_VECTOR_LAYER_ROW["layer_id"]]
        self.assertIsNone(s3vec.inline_geojson)
        self.assertEqual(
            tc.s3_to_http(s3vec.uri, "http://127.0.0.1:9000"),
            "http://127.0.0.1:9000/trid3nt-runs/vectors/rivers.geojson",
        )

        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["payload"]["case_id"], STUB_CASE_ID)
        self.assertEqual(sent[0]["case_id"], STUB_CASE_ID)

    def test_cancel_round_trip(self):
        client = self._connect()
        client.connect()
        client.create_case("cancel test")
        client.cancel(reason="test-cancel")
        events = self._collect_until_turn_complete(client)
        self.assertTrue(events[-1].data.get("cancelled"))
        sent = [e for e in self.server.received if e["type"] == "cancel"][0]
        self.assertEqual(sent["payload"]["reason"], "test-cancel")

    # F9 (live-feedback 2026-07-09): thinking-chunk round trip. ---------------

    def test_thinking_chunk_via_show_thinking_flag(self):
        """send_chat(show_thinking=True) -> stub emits agent-thinking-chunk
        events -> next_event yields thinking-chunk kind before chunk kind."""
        client = self._connect()
        client.connect()
        client.create_case("thinking test")
        # send_chat with show_thinking=True
        client.send_chat("Fetch a DEM for Asheville", show_thinking=True)
        # Verify the outbound payload carries show_thinking=True
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        # Wait for the round trip
        events = self._collect_until_turn_complete(client)
        # Refresh received list post round-trip
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["payload"].get("show_thinking"), "show_thinking must be True on wire")
        # Check thinking-chunk events arrived before the answer chunk
        kinds = [e.kind for e in events]
        first_thinking = kinds.index("thinking-chunk") if "thinking-chunk" in kinds else -1
        first_chunk = kinds.index("chunk") if "chunk" in kinds else -1
        self.assertIn("thinking-chunk", kinds, "thinking-chunk events expected when show_thinking=True")
        self.assertGreater(first_chunk, first_thinking, "thinking-chunk must arrive before answer chunk")

    def test_thinking_chunk_accumulated_text(self):
        """Two agent-thinking-chunk deltas concatenate into a single full text."""
        client = self._connect()
        client.connect()
        client.create_case("thinking acc test")
        client.send_chat("think about this", show_thinking=False)
        # "think" keyword triggers thinking in the stub
        events = self._collect_until_turn_complete(client)
        thinking_events = [e for e in events if e.kind == "thinking-chunk"]
        self.assertGreater(len(thinking_events), 0, "at least one thinking-chunk expected")
        accumulated = "".join(e.data["delta"] for e in thinking_events)
        self.assertIn("Considering", accumulated)
        self.assertIn("Fetching", accumulated)

    def test_send_chat_without_show_thinking_omits_key(self):
        """Default send_chat (show_thinking=False) must NOT include show_thinking on wire."""
        client = self._connect()
        client.connect()
        client.create_case("no thinking test")
        client.send_chat("plain message")
        events = self._collect_until_turn_complete(client)
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["payload"].get("show_thinking", False),
                         "show_thinking must be absent or False when not requested")


if __name__ == "__main__":
    unittest.main(verbosity=2)
