"""Milestone 3 tests -- remote export download, case switching, resume
refresh + debounce, selection-bbox AOI math, token-expiry classification.

No QGIS required (Qt widgets excluded, as in milestones 1-2). The WS stub
needs ``websockets`` (trid3nt-local agent venv); the export-file stub mirrors
the agent's real /api/export-qgis[/file] route semantics (services/agent
``tool_catalog_http.py``): file serving is .qgz/.gpkg-only, export-root
guarded (403), missing file (404), Content-Disposition attachment on 200.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trid3nt"))
sys.path.insert(0, os.path.dirname(__file__))

import aoi  # noqa: E402
import case_export  # noqa: E402
import trid3nt_client as tc  # noqa: E402
from stub_server import (  # noqa: E402
    CASE_LIST_ROWS,
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


class _RemoteExportStub(http.server.BaseHTTPRequestHandler):
    """POST /api/export-qgis + GET /api/export-qgis/file with the agent's
    guard ladder: 400 missing path, 403 non-.qgz/.gpkg or outside the export
    root, 404 missing file, 200 attachment otherwise."""

    export_root: str = ""
    post_result: dict = {}

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/api/export-qgis":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        if not body.get("case_id"):
            self._json(400, {"error": "missing or empty `case_id`"})
            return
        self._json(200, self.post_result)

    def do_GET(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        if path != "/api/export-qgis/file":
            self._json(404, {"error": "not found"})
            return
        params = urllib.parse.parse_qs(qs)
        raw = (params.get("path") or [""])[0].strip()
        if not raw:
            self._json(400, {"error": "missing `path` query param"})
            return
        suffix = os.path.splitext(raw)[1].lower()
        content_type = {
            ".qgz": "application/zip",
            ".gpkg": "application/geopackage+sqlite3",
        }.get(suffix)
        if content_type is None:
            self._json(403, {"error": "only .qgz and .gpkg export artifacts are served"})
            return
        real = os.path.realpath(raw)
        root = os.path.realpath(self.export_root)
        if real != root and not real.startswith(root + os.sep):
            self._json(403, {"error": f"path is outside the export root {root}"})
            return
        if not os.path.isfile(real):
            self._json(404, {"error": f"no such export file: {real}"})
            return
        with open(real, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{os.path.basename(real)}"',
        )
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


class _RemoteExportBase(unittest.TestCase):
    """Spins the export stub over a real on-disk export root."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="trid3nt_export_root_")
        self.addCleanup(self._rmtree, self.root)
        self.gpkg = os.path.join(self.root, "export.gpkg")
        _make_gpkg(self.gpkg, [("buildings", "features"), ("rivers", "features")])
        self.qgz = os.path.join(self.root, "project.qgz")
        with open(self.qgz, "wb") as f:
            f.write(b"PK-fake-qgz-bytes")
        _RemoteExportStub.export_root = self.root
        _RemoteExportStub.post_result = {
            "status": "ok",
            "qgz_path": self.qgz,
            "gpkg_path": self.gpkg,
            "exported_vector_count": 2,
            "exported_raster_count": 1,
            "skipped": [{"name": "Basemap", "reason": "tile template only"}],
            "output_dir": self.root,
        }
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _RemoteExportStub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.dest = tempfile.mkdtemp(prefix="trid3nt_export_dl_")
        self.addCleanup(self._rmtree, self.dest)

    @staticmethod
    def _rmtree(path: str) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


class TestWsUrlToHttpBase(unittest.TestCase):
    def test_ws_url_to_http_base(self):
        f = case_export.ws_url_to_http_base
        self.assertEqual(f("wss://host.example/ws"), "https://host.example")
        self.assertEqual(f("ws://127.0.0.1:8765/ws"), "http://127.0.0.1:8765")
        # port + credentials-free netloc preserved; path + query dropped
        self.assertEqual(
            f("wss://d125.example.net:8443/ws?st=tok"),
            "https://d125.example.net:8443",
        )
        # a bare host pasted without a scheme still yields something usable
        self.assertEqual(f("host.example/ws"), "http://host.example")


class TestRemoteDownload(_RemoteExportBase):
    def test_download_ok_lands_bytes_locally(self):
        local = case_export.download_export_file(
            self.base, self.gpkg, self.dest, timeout=10
        )
        self.assertEqual(os.path.basename(local), "export.gpkg")
        self.assertTrue(local.startswith(self.dest))
        with open(local, "rb") as f_local, open(self.gpkg, "rb") as f_remote:
            self.assertEqual(f_local.read(), f_remote.read())

    def test_download_403_outside_root_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.download_export_file(
                self.base, "/etc/secrets.gpkg", self.dest, timeout=10
            )
        msg = str(ctx.exception)
        self.assertIn("HTTP 403", msg)
        self.assertIn("outside the export root", msg)

    def test_download_403_wrong_type_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.download_export_file(
                self.base,
                os.path.join(self.root, "depth.tif"),
                self.dest,
                timeout=10,
            )
        msg = str(ctx.exception)
        self.assertIn("HTTP 403", msg)
        self.assertIn("only .qgz and .gpkg", msg)

    def test_download_404_missing_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.download_export_file(
                self.base,
                os.path.join(self.root, "gone.gpkg"),
                self.dest,
                timeout=10,
            )
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_remote_round_trip_post_localize_plan(self):
        """The full remote Open-in-QGIS path: POST -> download -> plan."""
        result = case_export.post_export_case(self.base, "01CASE", timeout=10)
        localized = case_export.localize_remote_export(
            self.base, result, self.dest
        )
        self.assertEqual(localized["output_dir"], self.dest)
        self.assertTrue(localized["gpkg_path"].startswith(self.dest))
        self.assertTrue(localized["qgz_path"].startswith(self.dest))
        plan = case_export.plan_export_layers(localized)
        self.assertEqual(plan.vector_layers, ["buildings", "rivers"])
        self.assertEqual(plan.raster_paths, [])  # rasters never download
        # honest notes: the original skip survives + the raster carve-out
        self.assertTrue(any("Basemap" in n for n in plan.notes))
        self.assertTrue(
            any("only .qgz/.gpkg" in n for n in plan.notes),
            f"no raster carve-out note in {plan.notes}",
        )

    def test_localize_survives_per_file_failure(self):
        """A 404 on one artifact becomes a skipped note; the rest still lands."""
        result = dict(_RemoteExportStub.post_result)
        result["qgz_path"] = os.path.join(self.root, "vanished.qgz")
        localized = case_export.localize_remote_export(
            self.base, result, self.dest
        )
        self.assertIsNone(localized["qgz_path"])
        self.assertTrue(localized["gpkg_path"].startswith(self.dest))
        reasons = [row["reason"] for row in localized["skipped"]]
        self.assertTrue(any("HTTP 404" in r for r in reasons))


# --------------------------------------------------------------------------- #
# Case switching (case-command select -> case-open rebind)
# --------------------------------------------------------------------------- #


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

    def test_selection_status_and_context_line(self):
        sel = (-82.62, 35.55, -82.50, 35.64)
        self.assertEqual(
            aoi.aoi_status_text(sel, True, source="selection"),
            "AOI: selection 0.12 x 0.09 deg",
        )
        wide = (-85.0, 35.0, -80.0, 36.0)
        status = aoi.aoi_status_text(wide, True, source="selection")
        self.assertIn("selection", status)
        self.assertIn("too large", status)
        # the per-message context line names the selection origin honestly
        attached = aoi.attach_aoi_to_text("Run a flood sim", sel, source="selection")
        self.assertIn("selected-feature AOI", attached)
        self.assertIn("bbox of the selection", attached)
        self.assertIn(
            "bbox = [-82.620000, 35.550000, -82.500000, 35.640000]", attached
        )
        # default stays byte-identical to the milestone 2 canvas wording
        canvas_attached = aoi.attach_aoi_to_text("Run a flood sim", sel)
        self.assertIn("QGIS map canvas AOI", canvas_attached)
        self.assertNotIn("selected-feature", canvas_attached)

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


class TestAnonymousIdGuard(unittest.TestCase):
    """The sticky anonymous_user_id read guard (plugin_settings).

    plugin_settings imports qgis.PyQt, so this test stubs QtCore.QSettings
    with an in-memory dict -- the guard logic itself is pure. Regression for
    the stub-server user id ("01STUBUSERAAAAAAAAAAAAAAAA", not Crockford --
    contains U) leaking into the real profile and poisoning every live
    handshake with an opaque auth-ack timeout.
    """

    def _settings_with(self, stored: str):
        import types
        import importlib

        class FakeQSettings:
            store = {}

            def value(self, key, default=None):
                return self.store.get(key, default)

            def setValue(self, key, value):
                self.store[key] = value

        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = FakeQSettings
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        qgis = types.ModuleType("qgis")
        qgis.PyQt = pyqt
        saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore")}
        sys.modules.update({"qgis": qgis, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore})
        try:
            sys.modules.pop("plugin_settings", None)
            ps = importlib.import_module("plugin_settings")
            FakeQSettings.store = {"trid3nt/anonymous_user_id": stored}
            return ps.PluginSettings()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_stub_id_filtered(self):
        s = self._settings_with("01STUBUSERAAAAAAAAAAAAAAAA")
        self.assertEqual(s.anonymous_user_id, "")

    def test_garbage_filtered(self):
        for bad in ("", "not-a-ulid", "01KWYB", "01kwyb5ad95pzrgmf9qahm5bv1"):
            s = self._settings_with(bad)
            self.assertEqual(s.anonymous_user_id, "", bad)

    def test_real_ulid_passes(self):
        s = self._settings_with("01KWYB5AD95PZRGMF9QAHM5BV1")
        self.assertEqual(s.anonymous_user_id, "01KWYB5AD95PZRGMF9QAHM5BV1")


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
            sys.modules.pop("plugin_settings", None)
            return importlib.import_module("plugin_settings").PluginSettings()
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
            sys.modules.pop("plugin_settings", None)
            ps = importlib.import_module("plugin_settings")
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
