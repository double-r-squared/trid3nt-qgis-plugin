"""Milestone 2 tests -- gate card logic, canvas AOI, reconnect, case list,
case export. No QGIS required (Qt widgets excluded, as in milestone 1).

Run via ``make test`` from qgis-plugin/ (the stub server needs ``websockets``;
everything under test is pure stdlib).
"""

from __future__ import annotations

import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trid3nt"))
sys.path.insert(0, os.path.dirname(__file__))

import aoi  # noqa: E402
import case_export  # noqa: E402
import gate  # noqa: E402
import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    CASE_LIST_ROWS,
    PAYLOAD_WARNING_HARDCAP_ROW,
    PAYLOAD_WARNING_ROW,
    STUB_WARNING_ID,
    StubAgentServer,
)


# --------------------------------------------------------------------------- #
# Gate logic (pure)
# --------------------------------------------------------------------------- #


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
        # Local-cloud fingerprint fix (2026-07-08): the "local" compute lane
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


# --------------------------------------------------------------------------- #
# Canvas AOI (pure)
# --------------------------------------------------------------------------- #


class TestAoi(unittest.TestCase):
    def test_merc_to_lonlat(self):
        lon, lat = aoi.merc_to_lonlat(0.0, 0.0)
        self.assertAlmostEqual(lon, 0.0)
        self.assertAlmostEqual(lat, 0.0)
        lon, lat = aoi.merc_to_lonlat(20037508.342789244, 0.0)
        self.assertAlmostEqual(lon, 180.0, places=6)
        # Asheville-ish sanity point
        lon, lat = aoi.merc_to_lonlat(-9190000.0, 4241000.0)
        self.assertAlmostEqual(lon, -82.556, places=2)
        self.assertAlmostEqual(lat, 35.566, places=2)

    def test_extent_to_bbox4326(self):
        # 4326 passthrough
        self.assertEqual(
            aoi.extent_to_bbox4326(-82.6, 35.5, -82.5, 35.6, "EPSG:4326"),
            (-82.6, 35.5, -82.5, 35.6),
        )
        # 3857 conversion (web-mercator canvas, the QGIS default with OSM tiles)
        bbox = aoi.extent_to_bbox4326(
            -9196000.0, 4238000.0, -9185000.0, 4249000.0, "EPSG:3857"
        )
        self.assertIsNotNone(bbox)
        self.assertAlmostEqual(bbox[0], -82.610, places=2)
        self.assertAlmostEqual(bbox[2], -82.512, places=2)
        self.assertTrue(35.4 < bbox[1] < bbox[3] < 35.8)
        # unknown CRS -> None (caller falls back to QgsCoordinateTransform)
        self.assertIsNone(aoi.extent_to_bbox4326(0, 0, 1000, 1000, "EPSG:26917"))
        # degenerate / non-finite -> None, never a fabricated bbox
        self.assertIsNone(aoi.extent_to_bbox4326(1, 1, 1, 1, "EPSG:4326"))
        self.assertIsNone(
            aoi.extent_to_bbox4326(float("nan"), 0, 1, 1, "EPSG:4326")
        )

    def test_two_deg_guard(self):
        small = (-82.6, 35.5, -82.5, 35.6)
        self.assertTrue(aoi.bbox_within_guard(small))
        # exactly 2.0 deg is still allowed ("exceeds" the guard means >)
        edge = (-83.0, 35.0, -81.0, 37.0)
        self.assertTrue(aoi.bbox_within_guard(edge))
        wide = (-85.0, 35.0, -80.0, 36.0)  # 5 deg lon
        self.assertFalse(aoi.bbox_within_guard(wide))
        tall = (-82.0, 30.0, -81.0, 36.0)  # 6 deg lat
        self.assertFalse(aoi.bbox_within_guard(tall))

    def test_attach_and_status_text(self):
        bbox = (-82.62, 35.55, -82.50, 35.64)
        attached = aoi.attach_aoi_to_text("Fetch a DEM here", bbox)
        self.assertTrue(attached.startswith("Fetch a DEM here\n\n["))
        # the exact structured shape survives verbatim in the context line
        self.assertIn(
            "bbox = [-82.620000, 35.550000, -82.500000, 35.640000]", attached
        )
        self.assertIn("EPSG:4326", attached)
        # status line formats
        self.assertEqual(
            aoi.aoi_status_text(bbox, True), "AOI: canvas 0.12 x 0.09 deg"
        )
        self.assertEqual(aoi.aoi_status_text(bbox, False), "AOI: off")
        self.assertIn("unavailable", aoi.aoi_status_text(None, True))
        wide = (-85.0, 35.0, -80.0, 36.0)
        status = aoi.aoi_status_text(wide, True)
        self.assertIn("too large", status)
        self.assertIn("sent without AOI", status)

    def test_create_case_sends_aoi_first_bbox(self):
        """The #170 AOI-first mirror: args.bbox on case-command create."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url)
        self.addCleanup(client.close)
        client.connect()
        client.create_case("aoi case", bbox=[-82.6, 35.5, -82.5, 35.6])
        create = [e for e in server.received if e["type"] == "case-command"][0]
        self.assertEqual(create["payload"]["command"], "create")
        self.assertEqual(
            create["payload"]["args"]["bbox"], [-82.6, 35.5, -82.5, 35.6]
        )
        # no bbox -> args carries no bbox key (byte-identical legacy path)
        client2 = tc.AgentClient(server.url)
        self.addCleanup(client2.close)
        client2.connect()
        client2.create_case("no aoi case")
        create2 = [e for e in server.received if e["type"] == "case-command"][-1]
        self.assertNotIn("bbox", create2["payload"]["args"])


# --------------------------------------------------------------------------- #
# Reconnect + outbound queue
# --------------------------------------------------------------------------- #


class TestBackoff(unittest.TestCase):
    def test_next_backoff_ladder(self):
        # deterministic rng: factor = 0.5 + 0.5*rng()
        delay, nxt = tc.next_backoff(1500, rng=lambda: 0.0)
        self.assertEqual(delay, 750)  # 0.5 x base (max jitter, earliest retry)
        self.assertEqual(nxt, 3000)  # doubles
        delay, nxt = tc.next_backoff(3000, rng=lambda: 0.999999)
        self.assertLessEqual(delay, 3000)  # never later than base
        self.assertGreater(delay, 2990)
        self.assertEqual(nxt, 5000)  # capped at RECONNECT_MAX_MS
        _, nxt = tc.next_backoff(5000, rng=lambda: 0.5)
        self.assertEqual(nxt, 5000)  # stays at the ceiling
        self.assertEqual(tc.RECONNECT_FLOOR_MS, 1500)
        self.assertEqual(tc.RECONNECT_MAX_MS, 5000)


class TestReconnect(unittest.TestCase):
    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("reconnect test")

    def _drain_until_closed(self, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                self.client.next_event(timeout=1.0)
            except tc.ConnectionClosed:
                return
        self.fail("socket never closed")

    def test_resume_rebinds_case_and_flushes_queue(self):
        session_id = self.client.session_id
        # Server drops the connection mid-turn.
        self.client.send_chat("please drop-connection now")
        self._drain_until_closed()
        self.assertFalse(self.client.connected)

        # Intent issued while down QUEUES instead of raising (sendOrQueue).
        self.client.send_chat("queued while offline")
        self.client.cancel(reason="queued-cancel")
        self.assertEqual(self.client.queued_outbound, 2)

        self.client.reconnect()
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.queued_outbound, 0)
        # Same session resumed; session-resume carried the ACTIVE case_id
        # (SessionResumePayload.case_id, job-CASE-AUTHORITY).
        self.assertEqual(self.client.session_id, session_id)
        self.assertEqual(self.server.connection_count, 2)
        self.assertEqual(self.server.resume_case_ids[0], None)
        self.assertEqual(self.server.resume_case_ids[1], self.client.case_id)
        # Queued frames flushed FIFO after the handshake. The flush is sent
        # before reconnect() returns but the stub INGESTS asynchronously --
        # wait for both frames to land.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if sum(1 for e in self.server.received if e["type"] == "cancel"):
                break
            time.sleep(0.05)
        types = [e["type"] for e in self.server.received]
        resume2 = [i for i, t in enumerate(types) if t == "session-resume"][1]
        flushed = types[resume2 + 1: resume2 + 3]
        self.assertEqual(flushed, ["user-message", "cancel"])
        flushed_msg = [
            e for e in self.server.received if e["type"] == "user-message"
        ][-1]
        self.assertEqual(flushed_msg["payload"]["text"], "queued while offline")

    def test_sticky_anonymous_user_replayed_on_reconnect(self):
        self.client.send_chat("drop-connection")
        self._drain_until_closed()
        self.client.reconnect()
        auth_frames = [e for e in self.server.received if e["type"] == "auth-token"]
        self.assertEqual(len(auth_frames), 2)
        self.assertIsNone(auth_frames[0]["payload"]["anonymous_user_id"])
        self.assertEqual(
            auth_frames[1]["payload"]["anonymous_user_id"], self.client.user_id
        )

    def test_queue_bounded_50_drops_oldest(self):
        self.client.send_chat("drop-connection")
        self._drain_until_closed()
        for i in range(55):
            self.client.send_chat(f"msg-{i}")
        self.assertEqual(self.client.queued_outbound, tc.OUTBOUND_QUEUE_MAX)
        with self.client._queue_lock:
            queued = [json.loads(raw) for raw in self.client._outbound_queue]
        # OLDEST dropped first: msg-0..msg-4 gone, msg-5 is now the head.
        self.assertEqual(queued[0]["payload"]["text"], "msg-5")
        self.assertEqual(queued[-1]["payload"]["text"], "msg-54")


# --------------------------------------------------------------------------- #
# Case list
# --------------------------------------------------------------------------- #


class TestCaseList(unittest.TestCase):
    def test_parse_case_list(self):
        cases = tc.parse_case_list({"cases": CASE_LIST_ROWS})
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0].case_id, "01STUBCASELISTAAAAAAAAAAAA")
        self.assertEqual(cases[0].title, "Asheville flood")
        self.assertEqual(cases[0].status, "active")
        self.assertEqual(cases[0].bbox, [-82.6, 35.55, -82.5, 35.65])
        self.assertEqual(cases[1].status, "archived")
        self.assertIsNone(cases[1].bbox)
        # defensive: malformed rows skipped, never raised on
        cases = tc.parse_case_list(
            {"cases": [None, {"title": "no id"}, {"case_id": ""}, 42,
                       {"case_id": "OK1", "bbox": [1, 2, 3]}]}
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].case_id, "OK1")
        self.assertIsNone(cases[0].bbox)  # wrong-length bbox dropped
        self.assertEqual(tc.parse_case_list({"cases": "nope"}), [])

    def test_case_list_event_surfaces_on_connect(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = tc.AgentClient(server.url)
        self.addCleanup(client.close)
        client.connect()
        client.create_case("case-list test")
        # Trigger a fresh emission cycle: the stub replies to session-resume
        # with case-list first; ask for one more resume round.
        client._send("session-resume", {"case_id": client.case_id})
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            ev = client.next_event(timeout=1.0)
            if ev is not None and ev.kind == "case-list":
                cases = ev.data["cases"]
                self.assertEqual(len(cases), 2)
                self.assertIsInstance(cases[0], tc.CaseInfo)
                self.assertEqual(cases[1].title, "Tampa surge")
                return
        self.fail("no case-list event surfaced")


# --------------------------------------------------------------------------- #
# Case export (open case in QGIS)
# --------------------------------------------------------------------------- #


def _make_gpkg(path: str, tables: list) -> None:
    """Minimal gpkg_contents so the pure sqlite3 listing works."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)"
    )
    for name, data_type in tables:
        conn.execute(
            "INSERT INTO gpkg_contents VALUES (?, ?)", (name, data_type)
        )
    conn.commit()
    conn.close()


class TestExportPlan(unittest.TestCase):
    def test_plan_export_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpkg = os.path.join(tmp, "export.gpkg")
            _make_gpkg(
                gpkg,
                [("buildings", "features"), ("rivers", "features"),
                 ("style_notes", "attributes")],  # non-feature table excluded
            )
            for name in ("depth.tif", "dem.TIFF", "readme.txt"):
                with open(os.path.join(tmp, name), "w") as f:
                    f.write("x")
            result = {
                "status": "partial",
                "qgz_path": os.path.join(tmp, "project.qgz"),
                "gpkg_path": gpkg,
                "exported_vector_count": 2,
                "exported_raster_count": 2,
                "skipped": [{"name": "Basemap", "reason": "tile template only"}],
                "output_dir": tmp,
            }
            plan = case_export.plan_export_layers(result)
            self.assertEqual(plan.gpkg_path, gpkg)
            self.assertEqual(plan.vector_layers, ["buildings", "rivers"])
            self.assertEqual(
                [os.path.basename(p) for p in plan.raster_paths],
                ["dem.TIFF", "depth.tif"],
            )
            self.assertEqual(
                plan.notes, ["skipped 'Basemap': tile template only"]
            )

    def test_plan_export_layers_honest_on_missing_artifacts(self):
        plan = case_export.plan_export_layers(
            {
                "status": "ok",
                "gpkg_path": "/nonexistent/export.gpkg",
                "exported_raster_count": 1,
                "output_dir": "/nonexistent",
                "skipped": [],
            }
        )
        self.assertIsNone(plan.gpkg_path)
        self.assertEqual(plan.raster_paths, [])
        self.assertTrue(any("missing on disk" in n for n in plan.notes))
        self.assertTrue(any("1 raster(s)" in n for n in plan.notes))

    def test_plan_joins_qml_styles_to_rasters(self):
        """Sidecar .qml join: result-declared qml_paths first, same-stem disk
        sidecar as fallback, unstyled rasters simply get no entry."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in (
                "depth.tif", "depth.qml",   # declared in qml_paths
                "dem.tif", "dem.qml",       # NOT declared -> disk fallback
                "plain.tif",                # no style at all
            ):
                with open(os.path.join(tmp, name), "w") as f:
                    f.write("x")
            result = {
                "status": "ok",
                "exported_raster_count": 3,
                "qml_paths": [
                    os.path.join(tmp, "depth.qml"),
                    os.path.join(tmp, "ghost.qml"),  # missing file: ignored
                ],
                "skipped": [],
                "output_dir": tmp,
            }
            plan = case_export.plan_export_layers(result)
            depth = os.path.join(tmp, "depth.tif")
            dem = os.path.join(tmp, "dem.tif")
            plain = os.path.join(tmp, "plain.tif")
            self.assertEqual(
                plan.raster_paths, sorted([dem, depth, plain])
            )
            self.assertEqual(
                plan.raster_styles,
                {
                    depth: os.path.join(tmp, "depth.qml"),
                    dem: os.path.join(tmp, "dem.qml"),
                },
            )

    def test_localize_remote_export_drops_qml_paths(self):
        """Remote mode: rasters stay on the remote box, so remote qml paths
        must not leak into the localized result."""
        localized = case_export.localize_remote_export(
            "http://127.0.0.1:1",
            {
                "status": "ok",
                "qgz_path": None,
                "gpkg_path": None,
                "exported_raster_count": 2,
                "qml_paths": ["/remote/depth.qml", "/remote/dem.qml"],
                "skipped": [],
                "output_dir": "/remote",
            },
            tempfile.gettempdir(),
        )
        self.assertEqual(localized["qml_paths"], [])
        self.assertEqual(localized["exported_raster_count"], 0)


# --------------------------------------------------------------------------- #
# materialize_export applies sidecar styles (stubbed qgis -- pure python)
# --------------------------------------------------------------------------- #


class TestMaterializeExportStyles(unittest.TestCase):
    """``LayerMaterializer.materialize_export`` must ``loadNamedStyle`` every
    raster that has a plan-joined .qml (the black-flood-raster fix) and stay
    honest (note, never crash) when a style does not apply.

    ``layers.py`` imports ``qgis.core`` / ``qgis.PyQt`` at module top, so this
    installs in-memory stub modules (the milestone-3 ``plugin_settings``
    pattern) and imports ``trid3nt.layers`` as a package module.
    """

    def _import_layers(self):
        import importlib
        import types

        class _FakeQSettings:
            def value(self, key, default=None):
                return default

            def setValue(self, key, value):
                pass

        class _FakeQDateTime:
            @staticmethod
            def fromString(text, fmt=None):
                return text

        class _FakeQt:
            ISODate = 1

        class _FakeNode:
            def setItemVisibilityChecked(self, checked):
                pass

        class _FakeGroup:
            def insertLayer(self, idx, layer):
                return _FakeNode()

        class _FakeRoot:
            def __init__(self):
                self._groups = {}

            def findGroup(self, name):
                return self._groups.get(name)

            def insertGroup(self, idx, name):
                group = _FakeGroup()
                self._groups[name] = group
                return group

        class _FakeProject:
            _instance = None

            @classmethod
            def instance(cls):
                if cls._instance is None:
                    cls._instance = cls()
                return cls._instance

            def __init__(self):
                self._root = _FakeRoot()
                self.added = []

            def layerTreeRoot(self):
                return self._root

            def addMapLayer(self, layer, add_to_legend=True):
                self.added.append(layer)

        class _FakeRasterLayer:
            instances = []
            style_result = ("", True)  # (message, ok) -- PyQGIS tuple shape

            def __init__(self, path, name, provider=""):
                self._path, self._name = path, name
                self.style_loads = []
                self.repainted = False
                _FakeRasterLayer.instances.append(self)

            def isValid(self):
                return True

            def name(self):
                return self._name

            def loadNamedStyle(self, qml_path):
                self.style_loads.append(qml_path)
                return self.style_result

            def triggerRepaint(self):
                self.repainted = True

        class _FakeVectorLayer(_FakeRasterLayer):
            pass

        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = _FakeQSettings
        qtcore.QDateTime = _FakeQDateTime
        qtcore.Qt = _FakeQt
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        core = types.ModuleType("qgis.core")
        core.QgsDateTimeRange = type("QgsDateTimeRange", (), {})
        core.QgsProject = _FakeProject
        core.QgsRasterLayer = _FakeRasterLayer
        core.QgsVectorLayer = _FakeVectorLayer
        # Not exercised by these tests (no basemap/zoom calls here) -- just
        # need to exist so ``layers.py``'s module-level import succeeds.
        core.QgsCoordinateReferenceSystem = type("QgsCoordinateReferenceSystem", (), {})
        core.QgsCoordinateTransform = type("QgsCoordinateTransform", (), {})
        core.QgsRectangle = type("QgsRectangle", (), {})
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        qgis_mod.core = core

        stub_keys = ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore", "qgis.core")
        saved = {k: sys.modules.get(k) for k in stub_keys}
        sys.modules.update(
            {
                "qgis": qgis_mod,
                "qgis.PyQt": pyqt,
                "qgis.PyQt.QtCore": qtcore,
                "qgis.core": core,
            }
        )
        plugin_root = os.path.join(os.path.dirname(__file__), "..")
        sys.path.insert(0, plugin_root)
        pkg_keys = [k for k in list(sys.modules) if k.split(".")[0] == "trid3nt"]
        saved_pkg = {k: sys.modules.pop(k) for k in pkg_keys}
        try:
            layers = importlib.import_module("trid3nt.layers")
        finally:
            sys.path.remove(plugin_root)
            for k in [k for k in list(sys.modules) if k.split(".")[0] == "trid3nt"]:
                sys.modules.pop(k, None)
            sys.modules.update(saved_pkg)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        return layers, _FakeRasterLayer

    def _plan(self, raster_paths, raster_styles):
        return case_export.ExportPlan(
            status="ok",
            raster_paths=list(raster_paths),
            raster_styles=dict(raster_styles),
        )

    def test_styles_applied_via_load_named_style(self):
        layers, fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        plan = self._plan(
            ["/exp/depth.tif", "/exp/plain.tif"],
            {"/exp/depth.tif": "/exp/depth.qml"},
        )
        notes = m.materialize_export(plan, "styled case")
        by_name = {l.name(): l for l in fake_raster.instances}
        self.assertEqual(by_name["depth"].style_loads, ["/exp/depth.qml"])
        self.assertTrue(by_name["depth"].repainted)
        self.assertEqual(by_name["plain"].style_loads, [])  # no qml, no call
        self.assertTrue(any("style applied to 'depth'" in n for n in notes))
        self.assertFalse(any("plain" in n and "style" in n for n in notes))

    def test_failed_style_is_note_not_crash(self):
        layers, fake_raster = self._import_layers()
        fake_raster.style_result = ("bad xml", False)
        m = layers.LayerMaterializer(settings=None)
        plan = self._plan(["/exp/depth.tif"], {"/exp/depth.tif": "/exp/depth.qml"})
        notes = m.materialize_export(plan, "styled case")
        # Layer still added; failure is an honest note.
        self.assertTrue(any("export layer 'depth' added" in n for n in notes))
        self.assertTrue(
            any("style for 'depth' did not apply" in n and "bad xml" in n for n in notes)
        )

    def test_plan_without_styles_attr_is_tolerated(self):
        """An old-shape plan object (no raster_styles) must not break the
        materializer (getattr guard)."""
        layers, fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)

        class OldPlan:
            status = "ok"
            qgz_path = None
            gpkg_path = None
            vector_layers = []
            raster_paths = ["/exp/depth.tif"]
            notes = []

        notes = m.materialize_export(OldPlan(), "old plan")
        self.assertTrue(any("export layer 'depth' added" in n for n in notes))


class TestGroupClearingAndAnimationGrouping(unittest.TestCase):
    """ITEM A (case-switch group clear) + ITEM C (frame-sequence animation
    subgroup) -- live-feedback 2026-07-10.

    ``layers.py`` imports ``qgis.core`` / ``qgis.PyQt`` at module top, so
    this installs an in-memory fake ``qgis.core`` (the same stub-module
    pattern ``TestMaterializeExportStyles`` uses) rich enough to model a
    QgsLayerTreeGroup/QgsLayerTreeRoot's group/layer nesting -- groups,
    nested subgroups, ``findGroups``/``findLayer``/``findLayerIds``,
    ``removeChildNode``, and per-layer visibility -- since ``set_case``'s
    stale-group sweep and ``group_frame_sequences`` both drive that API
    directly (not exercised by ``TestMaterializeExportStyles``'s flatter
    stub, which never calls ``set_case``).
    """

    def _import_layers(self):
        import importlib
        import types

        class _FakeQSettings:
            def value(self, key, default=None):
                return default

            def setValue(self, key, value):
                pass

        class _FakeQDateTime:
            @staticmethod
            def fromString(text, fmt=None):
                return text

        class _FakeQt:
            ISODate = 1

        class _FakeLayerNode:
            """Stands in for QgsLayerTreeLayer."""

            def __init__(self, layer):
                self._layer = layer
                self._visible = True

            def layer(self):
                return self._layer

            def itemVisibilityChecked(self):
                return self._visible

            def setItemVisibilityChecked(self, checked):
                self._visible = checked

        class _FakeGroup:
            """Stands in for QgsLayerTreeGroup (and QgsLayerTreeRoot, which
            IS a group in real QGIS -- same fake class serves both)."""

            def __init__(self, name=""):
                self._name = name
                self.children_ = []  # list[_FakeGroup | _FakeLayerNode]
                self._expanded = True

            def name(self):
                return self._name

            def setName(self, name):
                self._name = name

            def setExpanded(self, expanded):
                self._expanded = expanded

            def isExpanded(self):
                return self._expanded

            def children(self):
                return list(self.children_)

            def findGroup(self, name):
                for child in self.children_:
                    if isinstance(child, _FakeGroup) and child.name() == name:
                        return child
                return None

            def findGroups(self):
                return [c for c in self.children_ if isinstance(c, _FakeGroup)]

            def insertGroup(self, idx, name):
                group = _FakeGroup(name)
                self.children_.insert(0, group) if idx == 0 else self.children_.append(group)
                return group

            def insertLayer(self, idx, layer):
                node = _FakeLayerNode(layer)
                self.children_.insert(0, node) if idx == 0 else self.children_.append(node)
                return node

            def findLayer(self, layer_or_id):
                target_id = (
                    layer_or_id if isinstance(layer_or_id, str) else layer_or_id.id()
                )
                for child in self.children_:
                    if isinstance(child, _FakeLayerNode) and child.layer().id() == target_id:
                        return child
                    if isinstance(child, _FakeGroup):
                        found = child.findLayer(layer_or_id)
                        if found is not None:
                            return found
                return None

            def findLayerIds(self):
                ids = []
                for child in self.children_:
                    if isinstance(child, _FakeLayerNode):
                        ids.append(child.layer().id())
                    elif isinstance(child, _FakeGroup):
                        ids.extend(child.findLayerIds())
                return ids

            def removeChildNode(self, node):
                if node in self.children_:
                    self.children_.remove(node)

            def takeChild(self, node):
                if node in self.children_:
                    self.children_.remove(node)
                    return True
                return False

            def insertChildNode(self, idx, node):
                self.children_.insert(0, node) if idx == 0 else self.children_.append(node)

        class _FakeProject:
            _instance = None

            @classmethod
            def instance(cls):
                if cls._instance is None:
                    cls._instance = cls()
                return cls._instance

            def __init__(self):
                self._root = _FakeGroup("")
                self.added = []
                self.removed_ids = []

            def layerTreeRoot(self):
                return self._root

            def addMapLayer(self, layer, add_to_legend=True):
                self.added.append(layer)

            def removeMapLayers(self, ids):
                self.removed_ids.extend(ids)
                self.added = [l for l in self.added if l.id() not in ids]

        class _FakeRasterLayer:
            _counter = 0
            instances = []
            style_result = ("", True)

            def __init__(self, path, name, provider=""):
                self._path, self._name = path, name
                _FakeRasterLayer._counter += 1
                self._id = f"{name}_{_FakeRasterLayer._counter}"
                self.style_loads = []
                self.repainted = False
                _FakeRasterLayer.instances.append(self)

            def isValid(self):
                return True

            def id(self):
                return self._id

            def name(self):
                return self._name

            def loadNamedStyle(self, qml_path):
                self.style_loads.append(qml_path)
                return self.style_result

            def triggerRepaint(self):
                self.repainted = True

        class _FakeVectorLayer(_FakeRasterLayer):
            pass

        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = _FakeQSettings
        qtcore.QDateTime = _FakeQDateTime
        qtcore.Qt = _FakeQt
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        core = types.ModuleType("qgis.core")
        core.QgsDateTimeRange = type("QgsDateTimeRange", (), {})
        core.QgsProject = _FakeProject
        core.QgsRasterLayer = _FakeRasterLayer
        core.QgsVectorLayer = _FakeVectorLayer
        core.QgsCoordinateReferenceSystem = type("QgsCoordinateReferenceSystem", (), {})
        core.QgsCoordinateTransform = type("QgsCoordinateTransform", (), {})
        core.QgsRectangle = type("QgsRectangle", (), {})
        qgis_mod = types.ModuleType("qgis")
        qgis_mod.PyQt = pyqt
        qgis_mod.core = core

        stub_keys = ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore", "qgis.core")
        saved = {k: sys.modules.get(k) for k in stub_keys}
        sys.modules.update(
            {
                "qgis": qgis_mod,
                "qgis.PyQt": pyqt,
                "qgis.PyQt.QtCore": qtcore,
                "qgis.core": core,
            }
        )
        plugin_root = os.path.join(os.path.dirname(__file__), "..")
        sys.path.insert(0, plugin_root)
        pkg_keys = [k for k in list(sys.modules) if k.split(".")[0] == "trid3nt"]
        saved_pkg = {k: sys.modules.pop(k) for k in pkg_keys}
        try:
            layers = importlib.import_module("trid3nt.layers")
        finally:
            sys.path.remove(plugin_root)
            for k in [k for k in list(sys.modules) if k.split(".")[0] == "trid3nt"]:
                sys.modules.pop(k, None)
            sys.modules.update(saved_pkg)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        return layers, core.QgsProject, _FakeRasterLayer

    def _event(self, tc_mod, layer_id, name, uri="https://tiles/{z}/{x}/{y}.png"):
        return tc_mod.LayerEvent(layer_id=layer_id, name=name, layer_type="raster", uri=uri)

    # -- ITEM A: case-switch clears stale TRID3NT groups ------------------- #

    def test_set_case_clears_previous_case_group_and_its_layers(self):
        import trid3nt_client as tc

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        # The basemap: added directly to root, NEVER inside a group.
        root = FakeProject.instance().layerTreeRoot()
        osm = _fake_raster("", "OpenStreetMap")
        FakeProject.instance().addMapLayer(osm, False)
        root.insertLayer(-1, osm)

        m.set_case("case-a", "Case A")
        m.materialize([self._event(tc, "L1", "DEM")])
        self.assertEqual([g.name() for g in root.findGroups()], ["TRID3NT Case A"])

        m.set_case("case-b", "Case B")
        # Case A's group is gone before Case B's layers land.
        self.assertEqual(root.findGroups(), [])
        # The basemap (never inside a group) survives untouched.
        self.assertIn(osm, FakeProject.instance().added)
        self.assertNotIn(osm.id(), FakeProject.instance().removed_ids)

        m.materialize([self._event(tc, "L2", "Bridges")])
        names = [g.name() for g in root.findGroups()]
        self.assertEqual(names, ["TRID3NT Case B"])

    def test_set_case_clears_export_group_too(self):
        import case_export

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        root = FakeProject.instance().layerTreeRoot()

        plan = case_export.ExportPlan(status="ok", raster_paths=["/exp/depth.tif"])
        m.materialize_export(plan, "case-a")
        self.assertIn("TRID3NT export case-a", [g.name() for g in root.findGroups()])

        m.set_case("case-b", "Case B")
        self.assertEqual(root.findGroups(), [])

    def test_reselecting_the_same_case_does_not_duplicate_layers(self):
        import trid3nt_client as tc

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        root = FakeProject.instance().layerTreeRoot()

        m.set_case("case-a", "Case A")
        m.materialize([self._event(tc, "L1", "DEM")])
        m.set_case("case-a", "Case A")  # re-open the SAME case
        m.materialize([self._event(tc, "L1", "DEM")])

        self.assertEqual([g.name() for g in root.findGroups()], ["TRID3NT Case A"])
        case_group = root.findGroup("TRID3NT Case A")
        self.assertEqual(len(case_group.findLayerIds()), 1)  # no duplicate

    # -- ITEM C: frame-sequence rasters land in a collapsed subgroup ------- #

    def test_frame_sequence_lands_in_collapsed_animation_subgroup(self):
        import trid3nt_client as tc

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        root = FakeProject.instance().layerTreeRoot()

        m.set_case("case-a", "Case A")
        notes = m.materialize(
            [
                self._event(tc, "L1", "DEM"),  # non-sequence -- stays flat
                self._event(tc, "L2", "Flood_depth_step_1"),
                self._event(tc, "L3", "Flood_depth_step_2"),
                self._event(tc, "L4", "Flood_depth_step_3"),
            ]
        )
        case_group = root.findGroup("TRID3NT Case A")
        top_level_names = [
            c.layer().name() for c in case_group.children() if hasattr(c, "layer")
        ]
        top_level_groups = [c.name() for c in case_group.children() if hasattr(c, "findGroups")]
        self.assertEqual(top_level_names, ["DEM"])  # non-sequence stays flat
        self.assertEqual(top_level_groups, ["flood depth (animation, 3 frames)"])

        subgroup = case_group.findGroup("flood depth (animation, 3 frames)")
        self.assertFalse(subgroup.isExpanded())  # collapsed
        self.assertEqual(
            sorted(c.layer().name() for c in subgroup.children()),
            ["Flood_depth_step_1", "Flood_depth_step_2", "Flood_depth_step_3"],
        )
        self.assertTrue(
            any(
                "flood depth: 3 frames grouped - open View > Panels > "
                "Temporal Controller and press play to animate." == n
                for n in notes
            )
        )

    def test_growing_sequence_renames_subgroup_without_losing_members(self):
        import trid3nt_client as tc

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        root = FakeProject.instance().layerTreeRoot()

        m.set_case("case-a", "Case A")
        m.materialize(
            [
                self._event(tc, "L1", "Flood_depth_step_1"),
                self._event(tc, "L2", "Flood_depth_step_2"),
            ]
        )
        case_group = root.findGroup("TRID3NT Case A")
        self.assertIsNotNone(case_group.findGroup("flood depth (animation, 2 frames)"))

        # session-state replay grows the sequence to 3 members.
        m.materialize(
            [
                self._event(tc, "L1", "Flood_depth_step_1"),
                self._event(tc, "L2", "Flood_depth_step_2"),
                self._event(tc, "L3", "Flood_depth_step_3"),
            ]
        )
        self.assertIsNone(case_group.findGroup("flood depth (animation, 2 frames)"))
        subgroup = case_group.findGroup("flood depth (animation, 3 frames)")
        self.assertIsNotNone(subgroup)
        self.assertEqual(
            sorted(c.layer().name() for c in subgroup.children()),
            ["Flood_depth_step_1", "Flood_depth_step_2", "Flood_depth_step_3"],
        )
        # only ONE animation subgroup exists -- no leftover stale-named one
        self.assertEqual(
            len([g for g in case_group.findGroups() if "animation" in g.name()]), 1
        )

    def test_materialize_export_groups_frame_sequences_too(self):
        import case_export

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        root = FakeProject.instance().layerTreeRoot()

        plan = case_export.ExportPlan(
            status="ok",
            raster_paths=[
                "/exp/Flood_depth_step_1.tif",
                "/exp/Flood_depth_step_2.tif",
            ],
        )
        m.materialize_export(plan, "case-a")
        export_group = root.findGroup("TRID3NT export case-a")
        subgroup = export_group.findGroup("flood depth (animation, 2 frames)")
        self.assertIsNotNone(subgroup)
        self.assertEqual(len(subgroup.children()), 2)


class _ExportApiStub(http.server.BaseHTTPRequestHandler):
    responses: dict = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        case_id = body.get("case_id")
        status, payload = self.responses.get(
            case_id, (404, {"error": f"no such case: {case_id}"})
        )
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # silence
        pass


class TestExportApi(unittest.TestCase):
    def setUp(self):
        _ExportApiStub.responses = {
            "01GOODCASE": (200, {"status": "ok", "qgz_path": "/x/project.qgz",
                                 "gpkg_path": None, "exported_vector_count": 0,
                                 "exported_raster_count": 1, "skipped": [],
                                 "output_dir": "/x"}),
            "01BADCASE": (404, {"error": "CASE_NOT_FOUND: no such case"}),
        }
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _ExportApiStub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def test_post_export_case_ok(self):
        result = case_export.post_export_case(self.base, "01GOODCASE", timeout=10)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["qgz_path"], "/x/project.qgz")

    def test_post_export_case_typed_error_surfaces_verbatim(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.post_export_case(self.base, "01BADCASE", timeout=10)
        self.assertIn("CASE_NOT_FOUND", str(ctx.exception))

    def test_post_export_case_unreachable_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.post_export_case("http://127.0.0.1:1", "X", timeout=2)
        self.assertIn("unreachable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
