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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.net import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    LEGACY_RASTER_LAYER_ROW,
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
                LEGACY_RASTER_LAYER_ROW,
                VECTOR_LAYER_ROW,
                {"name": "no layer_id -> skipped"},
                "not-a-dict",
            ]
        }
        events = tc.parse_layer_events(payload)
        self.assertEqual(len(events), 3)
        raster, legacy, vector = events
        # NEW shape (TiTiler->QGIS swap): raw s3 COG uri + explicit legend;
        # no XYZ template on the event.
        self.assertEqual(raster.layer_type, "raster")
        self.assertEqual(raster.uri, RASTER_LAYER_ROW["uri"])
        self.assertTrue(raster.uri.startswith("s3://"))
        self.assertIsNone(raster.tile_template)
        self.assertEqual(raster.legend["colormap"], "viridis")
        self.assertEqual(raster.legend["vmin"], 600.0)
        # LEGACY shape (old persisted cases): the TiTiler template still
        # parses as a raster event with the template intact for unwrapping.
        self.assertEqual(legacy.layer_type, "raster")
        self.assertEqual(legacy.tile_template, LEGACY_RASTER_LAYER_ROW["uri"])
        self.assertIn("{z}", legacy.tile_template)
        self.assertIn("/cog/tiles/", legacy.uri)
        self.assertEqual(vector.layer_type, "vector")
        self.assertIsNone(vector.tile_template)
        self.assertEqual(vector.inline_geojson["type"], "FeatureCollection")
        # Defensive: junk payloads
        self.assertEqual(tc.parse_layer_events({}), [])
        self.assertEqual(tc.parse_layer_events({"loaded_layers": "junk"}), [])

    # -- remote-daemon (tailnet) endpoint derivation (LANE P) ---------------- #

    def test_derive_http_base_from_ws_host(self):
        # ws:// + default port 8765 -> http:// same host, :8766.
        self.assertEqual(
            tc.derive_http_base("ws://100.64.0.5:8765/ws"),
            "http://100.64.0.5:8766",
        )
        # wss:// -> https://, port still overridden to :8766.
        self.assertEqual(
            tc.derive_http_base("wss://example.com/ws"),
            "https://example.com:8766",
        )
        # localhost default unchanged (matches the old DEFAULT_EXPORT_API).
        self.assertEqual(
            tc.derive_http_base("ws://127.0.0.1:8765/ws"),
            "http://127.0.0.1:8766",
        )
        # A custom port override still applies.
        self.assertEqual(
            tc.derive_http_base("ws://127.0.0.1:8765/ws", port=9999),
            "http://127.0.0.1:9999",
        )
        # Junk input never raises -- falls back to the 127.0.0.1 host.
        self.assertEqual(tc.derive_http_base(""), "http://127.0.0.1:8766")

    def test_resolve_http_base_prefers_advertised(self):
        # Advertised wins outright (trailing slash stripped).
        self.assertEqual(
            tc.resolve_http_base("http://100.64.0.5:9001/", "ws://127.0.0.1:8765/ws"),
            "http://100.64.0.5:9001",
        )
        # Absent/empty/None advertised -> WS-host-derived fallback.
        for advertised in (None, ""):
            self.assertEqual(
                tc.resolve_http_base(advertised, "ws://100.64.0.5:8765/ws"),
                "http://100.64.0.5:8766",
            )

    def test_resolve_data_base_prefers_advertised(self):
        self.assertEqual(
            tc.resolve_data_base("http://100.64.0.5:9000/", "http://127.0.0.1:9000"),
            "http://100.64.0.5:9000",
        )
        # Absent -> the caller's fallback verbatim (current localhost
        # behavior for old daemons -- NEVER WS-host-derived).
        for advertised in (None, ""):
            self.assertEqual(
                tc.resolve_data_base(advertised, "http://127.0.0.1:9000"),
                "http://127.0.0.1:9000",
            )

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

    # -- remote-daemon (tailnet) endpoint advertisement (LANE P) ------------- #

    def test_no_advertised_endpoints_is_none(self):
        """Old-daemon stub (default): auth-ack carries no endpoints at all --
        both attrs stay None so callers fall through to their fallback."""
        client = self._connect()
        client.connect()
        self.assertIsNone(client.advertised_http_base)
        self.assertIsNone(client.advertised_data_base)

    def test_flat_advertised_endpoints_are_parsed(self):
        self.server.advertise_endpoints = {
            "http_base": "http://100.64.0.5:8766/",
            "data_base": "http://100.64.0.5:9000",
        }
        client = self._connect()
        client.connect()
        # Trailing slash stripped.
        self.assertEqual(client.advertised_http_base, "http://100.64.0.5:8766")
        self.assertEqual(client.advertised_data_base, "http://100.64.0.5:9000")

    def test_nested_endpoints_dict_is_parsed(self):
        """Defensive dual-shape read: the server lane may land ``endpoints``
        as a nested dict instead of flat auth-ack fields -- both shapes must
        work since the contract has not landed yet."""
        self.server.advertise_endpoints = {
            "endpoints": {
                "http_base": "http://100.64.0.5:8766",
                "data_base": "http://100.64.0.5:9000",
            }
        }
        client = self._connect()
        client.connect()
        self.assertEqual(client.advertised_http_base, "http://100.64.0.5:8766")
        self.assertEqual(client.advertised_data_base, "http://100.64.0.5:9000")

    def test_partial_advertisement_leaves_the_other_none(self):
        self.server.advertise_endpoints = {"http_base": "http://100.64.0.5:8766"}
        client = self._connect()
        client.connect()
        self.assertEqual(client.advertised_http_base, "http://100.64.0.5:8766")
        self.assertIsNone(client.advertised_data_base)

    def test_malformed_advertisement_is_ignored_not_raised(self):
        """A non-string / empty-string endpoint value never crashes the
        handshake -- it degrades to None (fall back), same as absence."""
        self.server.advertise_endpoints = {"http_base": 12345, "data_base": ""}
        client = self._connect()
        client.connect()  # must not raise
        self.assertIsNone(client.advertised_http_base)
        self.assertIsNone(client.advertised_data_base)


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

        # Layer events: raw-s3 COG raster + LEGACY tile-template raster +
        # inline-geojson vector + s3-only vector. The inline pad is >64 KiB so
        # this round trip also proves the 64-bit frame-length decode path.
        layer_events = [e for e in events if e.kind == "session-state"][-1].data["layers"]
        by_id = {le.layer_id: le for le in layer_events}
        raster = by_id[RASTER_LAYER_ROW["layer_id"]]
        self.assertEqual(raster.uri, RASTER_LAYER_ROW["uri"])
        self.assertIsNone(raster.tile_template)
        self.assertEqual(raster.legend["kind"], "continuous")
        legacy = by_id[LEGACY_RASTER_LAYER_ROW["layer_id"]]
        self.assertEqual(legacy.tile_template, LEGACY_RASTER_LAYER_ROW["uri"])
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

    def test_send_chat_carries_model_id_when_set(self):
        """OpenRouter model-extensibility (design 2026-07-19): send_chat with a
        truthy model_id rides ``model_id`` on the user-message payload verbatim
        (mirrors show_thinking) so the server picks the model for the turn."""
        client = self._connect()
        client.connect()
        client.create_case("model id test")
        client.send_chat("hello", model_id="deepseek/deepseek-chat")
        events = self._collect_until_turn_complete(client)
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["payload"].get("model_id"), "deepseek/deepseek-chat")

    def test_send_chat_without_model_id_omits_key(self):
        """Default send_chat (model_id="") must NOT include model_id on wire --
        an empty picker means 'use the agent's env default model'."""
        client = self._connect()
        client.connect()
        client.create_case("no model id test")
        client.send_chat("plain message")
        events = self._collect_until_turn_complete(client)
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("model_id", sent[0]["payload"],
                         "model_id must be absent when the picker is empty")

    # ADR 0017 mechanism 2 (structured AOI, 2026-07-22). ----------------------

    def test_send_chat_carries_structured_aoi_bbox_and_clean_text(self):
        """send_chat(aoi_bbox=...) rides the STRUCTURED ``aoi_bbox`` payload
        field ([min_lon, min_lat, max_lon, max_lat], EPSG:4326) and the text
        goes out CLEAN -- no legacy "[QGIS map canvas AOI ...]" prose line.
        The stub validates the payload the way the live extra=forbid server
        would; a normal turn-complete proves the field was ACCEPTED."""
        client = self._connect()
        client.connect()
        client.create_case("structured aoi test")
        bbox = [-82.62, 35.55, -82.5, 35.64]
        client.send_chat("Fetch a DEM here", aoi_bbox=bbox)
        events = self._collect_until_turn_complete(client)
        self.assertEqual(events[-1].kind, "turn-complete")
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        payload = sent[0]["payload"]
        self.assertEqual(payload["aoi_bbox"], bbox)
        # The message text is EXACTLY the user's prose -- no bracket line.
        self.assertEqual(payload["text"], "Fetch a DEM here")
        self.assertNotIn("QGIS map canvas AOI", payload["text"])
        self.assertNotIn("bbox =", payload["text"])
        # Stub-side contract gate saw a legal payload + recorded the bbox.
        self.assertEqual(self.server.protocol_violations, [])
        self.assertEqual(self.server.user_message_aoi_bboxes, [bbox])

    def test_send_chat_without_aoi_omits_key(self):
        """No AOI -> the ``aoi_bbox`` key is OMITTED entirely (mirrors the
        show_thinking / model_id convention): a plain message stays
        byte-identical to the pre-field payload, so it keeps working against
        a one-deploy-behind extra=forbid server."""
        client = self._connect()
        client.connect()
        client.create_case("no aoi test")
        client.send_chat("plain message, no AOI")
        events = self._collect_until_turn_complete(client)
        self.assertEqual(events[-1].kind, "turn-complete")
        sent = [e for e in self.server.received if e["type"] == "user-message"]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("aoi_bbox", sent[0]["payload"])
        self.assertEqual(self.server.protocol_violations, [])
        self.assertEqual(self.server.user_message_aoi_bboxes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
