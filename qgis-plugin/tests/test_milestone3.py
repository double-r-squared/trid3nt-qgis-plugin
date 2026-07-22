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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from trid3nt.case import aoi  # noqa: E402
from trid3nt.case import case_export  # noqa: E402
from trid3nt.net import trid3nt_client as tc  # noqa: E402
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


# --------------------------------------------------------------------------- #
# Cold case list -- GET /api/case-list (items b/c, live-feedback 2026-07-09)
# --------------------------------------------------------------------------- #


class _CaseListStub(http.server.BaseHTTPRequestHandler):
    """Mirrors the agent's real ``GET /api/case-list`` route in miniature
    (services/agent ``tool_catalog_http.py``): 200 ``{"cases": [...]}`` on
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
# Mesh outputs (MDAL phase 1) -- pure parsing + MinIO-http download
# --------------------------------------------------------------------------- #


class _MinioStub(http.server.BaseHTTPRequestHandler):
    """Path-style ``GET /<bucket>/<key>`` -- mirrors the MinIO http form
    ``s3_to_http`` builds (``<endpoint>/<bucket>/<key>``): 200 + bytes for a
    known object, 404 otherwise."""

    objects: dict = {}  # "bucket/key" -> bytes

    def do_GET(self):  # noqa: N802
        key = self.path.lstrip("/")
        data = self.objects.get(key)
        if data is None:
            raw = json.dumps({"error": "no such key"}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        pass


class _MinioStubBase(unittest.TestCase):
    def setUp(self):
        _MinioStub.objects = {}
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _MinioStub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.minio_endpoint = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.dest = tempfile.mkdtemp(prefix="trid3nt_mesh_dl_")
        self.addCleanup(shutil.rmtree, self.dest, True)


class TestDownloadMeshFile(_MinioStubBase):
    def test_download_ok_lands_bytes_locally(self):
        _MinioStub.objects["trid3nt-runs/01RUN/sfincs_map.nc"] = b"fake-netcdf-bytes"
        local = case_export.download_mesh_file(
            self.minio_endpoint, "s3://trid3nt-runs/01RUN/sfincs_map.nc", self.dest, timeout=10
        )
        self.assertEqual(os.path.basename(local), "sfincs_map.nc")
        self.assertTrue(local.startswith(self.dest))
        with open(local, "rb") as f:
            self.assertEqual(f.read(), b"fake-netcdf-bytes")

    def test_download_404_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.download_mesh_file(
                self.minio_endpoint,
                "s3://trid3nt-runs/01GONE/sfincs_map.nc",
                self.dest,
                timeout=10,
            )
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_non_s3_uri_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.download_mesh_file(
                self.minio_endpoint, "https://example.test/x.nc", self.dest, timeout=10
            )
        self.assertIn("not a valid s3://", str(ctx.exception))

    def test_unreachable_endpoint_is_honest(self):
        with self.assertRaises(case_export.ExportRequestError) as ctx:
            case_export.download_mesh_file(
                "http://127.0.0.1:1",
                "s3://trid3nt-runs/01RUN/sfincs_map.nc",
                self.dest,
                timeout=2,
            )
        self.assertIn("unreachable", str(ctx.exception))


class TestLocalizeMeshEntries(_MinioStubBase):
    def test_two_entries_one_download_one_missing(self):
        _MinioStub.objects["trid3nt-runs/01RUN/sfincs_map.nc"] = b"ok-bytes"
        result = {
            "mesh": [
                {
                    "kind": "mesh",
                    "format": "sfincs_map_netcdf",
                    "s3_uri": "s3://trid3nt-runs/01RUN/sfincs_map.nc",
                    "crs_authid": "EPSG:32616",
                    "name": "SFINCS mesh (01RUN)",
                },
                {
                    "kind": "mesh",
                    "format": "sfincs_map_netcdf",
                    "s3_uri": "s3://trid3nt-runs/01GONE/sfincs_map.nc",
                    "crs_authid": None,
                    "name": "SFINCS mesh (01GONE)",
                },
            ]
        }
        localized = case_export.localize_mesh_entries(result, self.minio_endpoint, self.dest)
        entries = localized["mesh"]
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["local_path"].startswith(self.dest))
        self.assertNotIn("error", entries[0])
        self.assertIsNone(entries[1]["local_path"])
        self.assertIn("HTTP 404", entries[1]["error"])
        # Original result is not mutated in place.
        self.assertNotIn("local_path", result["mesh"][0])

    def test_missing_s3_uri_is_an_honest_error(self):
        result = {"mesh": [{"name": "broken", "s3_uri": None}]}
        localized = case_export.localize_mesh_entries(result, self.minio_endpoint, self.dest)
        self.assertIsNone(localized["mesh"][0]["local_path"])
        self.assertIn("no s3_uri", localized["mesh"][0]["error"])

    def test_no_mesh_key_is_a_no_op(self):
        localized = case_export.localize_mesh_entries({"status": "ok"}, self.minio_endpoint, self.dest)
        self.assertEqual(localized["mesh"], [])

    def test_malformed_entries_are_skipped(self):
        result = {"mesh": ["not-a-dict", 42, None]}
        localized = case_export.localize_mesh_entries(result, self.minio_endpoint, self.dest)
        self.assertEqual(localized["mesh"], [])


class TestPlanExportLayersMesh(unittest.TestCase):
    """Pure parsing: ``plan_export_layers`` mesh-entry handling (no network)."""

    def test_downloaded_mesh_lands_in_plan(self):
        tmp_dir = tempfile.mkdtemp(prefix="trid3nt_mesh_plan_")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        nc_path = os.path.join(tmp_dir, "sfincs_map.nc")
        with open(nc_path, "wb") as f:
            f.write(b"fake-netcdf")
        result = {
            "status": "ok",
            "mesh": [
                {
                    "name": "SFINCS mesh (01RUN)",
                    "local_path": nc_path,
                    "crs_authid": "EPSG:32616",
                }
            ],
        }
        plan = case_export.plan_export_layers(result)
        self.assertEqual(
            plan.mesh_entries,
            [{"name": "SFINCS mesh (01RUN)", "local_path": nc_path, "crs_authid": "EPSG:32616"}],
        )
        self.assertEqual(plan.notes, [])

    def test_undownloaded_mesh_becomes_honest_note(self):
        result = {
            "status": "ok",
            "mesh": [{"name": "SFINCS mesh (01RUN)", "local_path": None, "error": "mesh download unreachable"}],
        }
        plan = case_export.plan_export_layers(result)
        self.assertEqual(
            plan.mesh_entries,
            [{"name": "SFINCS mesh (01RUN)", "local_path": None, "crs_authid": None}],
        )
        self.assertTrue(any("mesh 'SFINCS mesh (01RUN)'" in n and "unreachable" in n for n in plan.notes))

    def test_missing_local_path_on_disk_is_also_a_note(self):
        """A local_path that does not actually exist on disk (stale/moved) is
        treated the same as "not downloaded" -- never handed to QGIS."""
        result = {
            "status": "ok",
            "mesh": [{"name": "gone", "local_path": "/no/such/file.nc", "crs_authid": "EPSG:32616"}],
        }
        plan = case_export.plan_export_layers(result)
        self.assertEqual(plan.mesh_entries, [{"name": "gone", "local_path": None, "crs_authid": None}])
        self.assertTrue(any("mesh 'gone'" in n for n in plan.notes))

    def test_no_mesh_key_yields_empty_list(self):
        plan = case_export.plan_export_layers({"status": "ok"})
        self.assertEqual(plan.mesh_entries, [])
        self.assertEqual(plan.notes, [])

    def test_malformed_mesh_entries_are_defensively_skipped(self):
        result = {"status": "ok", "mesh": ["oops", {"no_name": True}, {"name": ""}]}
        plan = case_export.plan_export_layers(result)
        self.assertEqual(plan.mesh_entries, [])


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
        self.assertIsNone(info.bbox)  # no bbox on the row -> honest None

    # -- item 1 (live-feedback 2026-07-09): case-open bbox extraction ---------- #

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


# --------------------------------------------------------------------------- #
# ITEM B (live-feedback 2026-07-10): chat-history replay extraction --
# ``session_state.chat_history`` (contracts ``case.py`` CaseChatMessage) ->
# plain role/content rows for the dock's case-open chat replay.
# --------------------------------------------------------------------------- #


class TestParseChatHistory(unittest.TestCase):
    def test_present_user_and_agent_rows_survive(self):
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    {"role": "user", "content": "how deep does it flood?"},
                    {"role": "agent", "content": "up to 1.2 m near the river"},
                ]
            }
        )
        self.assertEqual(
            rows,
            [
                {"role": "user", "content": "how deep does it flood?"},
                {"role": "agent", "content": "up to 1.2 m near the river"},
            ],
        )

    def test_absent_chat_history_yields_empty_list(self):
        self.assertEqual(tc.parse_chat_history({}), [])
        self.assertEqual(tc.parse_chat_history({"chat_history": None}), [])
        self.assertEqual(tc.parse_chat_history({"chat_history": "not-a-list"}), [])

    def test_malformed_rows_are_skipped_not_raised(self):
        rows = tc.parse_chat_history(
            {
                "chat_history": [
                    "not-a-dict",
                    {"role": "user"},  # no content
                    {"content": "no role"},
                    {"role": "user", "content": 42},  # non-string content
                    {"role": "user", "content": ""},  # empty content
                    {"role": "system", "content": "tool bookkeeping"},
                    {"role": "tool", "content": "{...}"},
                    {"role": "bogus", "content": "hi"},
                    {"role": "user", "content": "the one good row"},
                ]
            }
        )
        self.assertEqual(rows, [{"role": "user", "content": "the one good row"}])

    def test_capped_at_replay_max_keeping_the_tail(self):
        many = [
            {"role": "user" if i % 2 == 0 else "agent", "content": f"msg {i}"}
            for i in range(tc.CHAT_HISTORY_REPLAY_MAX + 10)
        ]
        rows = tc.parse_chat_history({"chat_history": many})
        self.assertEqual(len(rows), tc.CHAT_HISTORY_REPLAY_MAX)
        # the TAIL survives (most recent conversation), not the head
        self.assertEqual(rows[0]["content"], "msg 10")
        self.assertEqual(rows[-1]["content"], f"msg {tc.CHAT_HISTORY_REPLAY_MAX + 9}")

    def test_parse_case_open_surfaces_chat_messages(self):
        info = tc.parse_case_open(
            {
                "session_state": {
                    "case": {"case_id": "01OK", "title": "Asheville flood"},
                    "loaded_layers": [],
                    "chat_history": [
                        {"role": "user", "content": "start a flood sim"},
                        {"role": "tool", "content": "{tool_card}"},
                        {"role": "agent", "content": "here is the result"},
                    ],
                }
            }
        )
        self.assertIsNotNone(info)
        self.assertEqual(
            info.chat_messages,
            [
                {"role": "user", "content": "start a flood sim"},
                {"role": "agent", "content": "here is the result"},
            ],
        )

    def test_parse_case_open_without_chat_history_is_empty(self):
        info = tc.parse_case_open(
            {"session_state": {"case": {"case_id": "01OK"}, "loaded_layers": []}}
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.chat_messages, [])


# --------------------------------------------------------------------------- #
# ITEM D (live-feedback 2026-07-10): auto-focus fallback bbox scan --
# ``find_fallback_bbox`` covers a case-open payload OUTSIDE the primary
# session_state.case.bbox carrier ``parse_case_open`` already extracts.
# --------------------------------------------------------------------------- #


class TestFindFallbackBbox(unittest.TestCase):
    def test_top_level_payload_bbox(self):
        bbox = tc.find_fallback_bbox({"bbox": [-83, 35, -82, 36]})
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_session_state_level_bbox(self):
        bbox = tc.find_fallback_bbox(
            {"session_state": {"bbox": [-83, 35, -82, 36]}}
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_session_state_case_level_bbox(self):
        bbox = tc.find_fallback_bbox(
            {"session_state": {"case": {"bbox": [-83, 35, -82, 36]}}}
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_precedence_top_level_wins(self):
        # top-level payload.bbox is checked first, even when a DIFFERENT
        # bbox also sits deeper in the payload.
        bbox = tc.find_fallback_bbox(
            {
                "bbox": [-83, 35, -82, 36],
                "session_state": {"case": {"bbox": [-70, 40, -69, 41]}},
            }
        )
        self.assertEqual(bbox, (-83.0, 35.0, -82.0, 36.0))

    def test_absent_or_malformed_yields_none(self):
        for payload in (
            {},
            {"bbox": None},
            {"bbox": [-83, 35, -82]},  # only 3 elements
            {"bbox": "not-a-list"},
            {"session_state": None},
            {"session_state": {"case": "not-a-dict"}},
            "not-a-dict",
            None,
        ):
            self.assertIsNone(tc.find_fallback_bbox(payload), f"payload={payload!r}")

    def test_int_elements_coerced_to_float(self):
        bbox = tc.find_fallback_bbox({"bbox": [-83, 35, -82, 36]})
        self.assertTrue(all(isinstance(v, float) for v in bbox))


# --------------------------------------------------------------------------- #
# Generic case-command (create/delete) -- item 2/3 (live-feedback 2026-07-09)
# --------------------------------------------------------------------------- #


class TestCaseCommandCreateDelete(unittest.TestCase):
    """``AgentClient.case_command`` -- the New/Delete case plumbing.

    Unlike ``create_case`` (blocking, used only during the initial connect
    handshake), ``case_command`` sends without waiting: the reply flows
    through the normal ``next_event`` pump like ``select_case``'s does.
    """

    def setUp(self):
        self.server = StubAgentServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client = tc.AgentClient(self.server.url)
        self.addCleanup(self.client.close)
        self.client.connect()
        self.client.create_case("case-command test")

    def _await_kind(self, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = self.client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_create_sends_no_case_id_and_yields_case_open(self):
        self.client.case_command("create")
        ev = self._await_kind("case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)  # the stub's create branch always rehydrates
        create_frames = [
            e
            for e in self.server.received
            if e["type"] == "case-command" and e["payload"].get("command") == "create"
        ]
        # one from setUp's create_case, one from this test's case_command
        self.assertEqual(len(create_frames), 2)
        sent = create_frames[-1]
        self.assertNotIn("case_id", sent["payload"])
        self.assertEqual(sent["payload"]["args"], {})
        self.assertIsNone(sent["case_id"])  # envelope-level case_id too

    def test_create_reply_updates_wire_stamp(self):
        """F34: the pump must ADOPT the case-open rebind into client.case_id.

        Pre-fix, case_command("create") never updated the stamp, so the next
        user-message carried the PREVIOUS case_id and the turn ran/persisted
        into the wrong case (live-proven 2026-07-10: a fresh flood case ended
        up empty while its layers landed in the startup case).
        """
        before = self.client.case_id
        self.client.case_command("create")
        ev = self._await_kind("case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)
        self.assertEqual(self.client.case_id, info.case_id)
        if info.case_id != before:
            self.assertNotEqual(self.client.case_id, before)
        # and the very next chat frame is stamped with the OPENED case
        self.client.send_chat("hello after new case")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            chats = [
                e for e in self.server.received if e["type"] == "user-message"
            ]
            if chats:
                break
            time.sleep(0.05)
        else:
            self.fail("no user-message observed by the stub server")
        self.assertEqual(chats[-1]["case_id"], info.case_id)

    def test_delete_sends_case_id(self):
        target = CASE_LIST_ROWS[0]["case_id"]
        self.client.case_command("delete", target)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            frames = [
                e
                for e in self.server.received
                if e["type"] == "case-command"
                and e["payload"].get("command") == "delete"
            ]
            if frames:
                break
            time.sleep(0.05)
        else:
            self.fail("no delete case-command observed by the stub server")
        sent = frames[-1]
        self.assertEqual(sent["payload"]["case_id"], target)
        self.assertEqual(sent["payload"]["args"], {})
        self.assertEqual(sent["case_id"], target)  # envelope-level case_id set

    def test_case_command_queues_when_disconnected(self):
        """Mirrors select_case's queue-if-closed: a command tapped mid-
        reconnect must not be silently dropped."""
        client = tc.AgentClient("ws://127.0.0.1:1/ws")  # never connected
        client.case_command("create")
        self.assertEqual(client.queued_outbound, 1)


# --------------------------------------------------------------------------- #
# Startup case reuse (live-feedback 2026-07-09): never mint a fresh
# "QGIS session ..." case while the user already has one
# --------------------------------------------------------------------------- #


class TestChooseStartupCase(unittest.TestCase):
    """The pure connect-flow decision ladder (``choose_startup_case``):
    resume > select-newest > create."""

    @staticmethod
    def _case(case_id, updated_at="", status="active"):
        return tc.CaseInfo(
            case_id=case_id, title=case_id, status=status, updated_at=updated_at
        )

    def test_resumed_case_wins_over_list(self):
        cases = [self._case("01NEWEST", "2026-07-08T00:00:00Z")]
        self.assertEqual(
            tc.choose_startup_case("01RESUMED", cases), ("resume", "01RESUMED")
        )

    def test_newest_live_case_selected(self):
        cases = [
            self._case("01OLD", "2026-06-01T00:00:00Z"),
            self._case("01NEW", "2026-07-08T00:00:00Z"),
            self._case("01MID", "2026-07-01T00:00:00Z"),
        ]
        self.assertEqual(tc.choose_startup_case(None, cases), ("select", "01NEW"))

    def test_tombstones_and_malformed_rows_skipped(self):
        cases = [
            self._case("01ARCHIVED", "2026-07-09T00:00:00Z", status="archived"),
            self._case("01DELETED", "2026-07-09T00:00:00Z", status="deleted"),
            self._case("", "2026-07-09T00:00:00Z"),  # no case_id -- dropped
            self._case("01LIVE", "2026-06-15T00:00:00Z"),
        ]
        self.assertEqual(tc.choose_startup_case(None, cases), ("select", "01LIVE"))

    def test_missing_updated_at_sorts_oldest(self):
        cases = [
            self._case("01NODATE", ""),
            self._case("01DATED", "2026-07-01T00:00:00Z"),
        ]
        self.assertEqual(tc.choose_startup_case(None, cases), ("select", "01DATED"))

    def test_zero_cases_creates(self):
        self.assertEqual(tc.choose_startup_case(None, []), ("create", None))
        self.assertEqual(tc.choose_startup_case("", None), ("create", None))

    def test_all_tombstoned_creates(self):
        cases = [self._case("01GONE", "2026-07-01T00:00:00Z", status="archived")]
        self.assertEqual(tc.choose_startup_case(None, cases), ("create", None))

    def test_stub_rows_pick_the_active_newest(self):
        # The stub's canonical rows: Asheville (active) + Tampa (archived).
        cases = tc.parse_case_list({"cases": CASE_LIST_ROWS})
        self.assertEqual(
            tc.choose_startup_case(None, cases),
            ("select", "01STUBCASELISTAAAAAAAAAAAA"),
        )


class TestStartupCaseReuse(unittest.TestCase):
    """The client half of the connect-flow reuse: the handshake stashes the
    case-list + adopts a server-rebound case, and the reuse ladder ends in a
    full case-open rehydration (the worker's ``_bind_startup_case`` path,
    minus Qt)."""

    def _client(self, server, **kwargs):
        client = tc.AgentClient(server.url, **kwargs)
        self.addCleanup(client.close)
        return client

    def _await_kind(self, client, kind, deadline_s=10.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ev = client.next_event(timeout=1.0)
            if ev is not None and ev.kind == kind:
                return ev
        self.fail(f"no {kind!r} event within {deadline_s}s")

    def test_handshake_stashes_case_list(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        # The stub interleaves case-list BEFORE session-state -- the
        # handshake drain must stash it, not drop it.
        self.assertIsNotNone(client.last_case_list)
        self.assertEqual(
            [c.case_id for c in client.last_case_list],
            [r["case_id"] for r in CASE_LIST_ROWS],
        )

    def test_bare_resume_adopts_server_rebound_case(self):
        server = StubAgentServer()
        server.resume_rebind_case_id = CASE_LIST_ROWS[0]["case_id"]
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        # Rule 1: the persisted active case the resume rebound is adopted.
        self.assertEqual(client.case_id, CASE_LIST_ROWS[0]["case_id"])

    def test_client_stamp_beats_server_rebind(self):
        server = StubAgentServer()
        server.resume_rebind_case_id = CASE_LIST_ROWS[0]["case_id"]
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.case_id = "01CLIENTSTAMPAAAAAAAAAAAAA"  # reconnect posture
        client.connect()
        # job-CASE-AUTHORITY: the client's own stamp is never overwritten.
        self.assertEqual(client.case_id, "01CLIENTSTAMPAAAAAAAAAAAAA")

    def test_no_rebind_no_adoption(self):
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        self.assertIsNone(client.case_id)

    def test_reuse_ladder_selects_newest_and_rehydrates(self):
        """The worker's local-mode connect flow, minus Qt: connect ->
        choose_startup_case -> select -> the case-open rehydration carries
        the authoritative title + layers (the dock's rebind input)."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        action, target = tc.choose_startup_case(
            client.case_id, client.last_case_list or []
        )
        self.assertEqual((action, target), ("select", CASE_LIST_ROWS[0]["case_id"]))
        client.select_case(target)
        self.assertEqual(client.case_id, target)  # bound -- never caseless
        ev = self._await_kind(client, "case-open")
        info = tc.parse_case_open(ev.data)
        self.assertIsNotNone(info)
        self.assertEqual(info.case_id, target)
        self.assertEqual(info.title, "Asheville flood")
        self.assertEqual(len(info.layers), 1)  # persisted layers replayed
        # No create ever hit the wire -- the whole point of the reuse ladder.
        creates = [
            e
            for e in server.received
            if e["type"] == "case-command"
            and (e["payload"] or {}).get("command") == "create"
        ]
        self.assertEqual(creates, [])

    def test_reuse_ladder_resume_wins(self):
        server = StubAgentServer()
        server.resume_rebind_case_id = CASE_LIST_ROWS[0]["case_id"]
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        action, target = tc.choose_startup_case(
            client.case_id, client.last_case_list or []
        )
        self.assertEqual(
            (action, target), ("resume", CASE_LIST_ROWS[0]["case_id"])
        )

    def test_event_pump_stashes_case_list_too(self):
        """The live server emits case-list AFTER session-state -- the event
        pump path must stash it just like the handshake drain does."""
        server = StubAgentServer()
        server.start()
        self.addCleanup(server.stop)
        client = self._client(server)
        client.connect()
        client.last_case_list = None  # wipe the drain stash
        self.assertTrue(client.request_case_list_refresh())
        self._await_kind(client, "case-list")
        self.assertIsNotNone(client.last_case_list)
        self.assertEqual(len(client.last_case_list), 2)


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
            sys.modules.pop("trid3nt.plugin_settings", None)
            ps = importlib.import_module("trid3nt.plugin_settings")
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
            sys.modules.pop("trid3nt.plugin_settings", None)
            return importlib.import_module("trid3nt.plugin_settings").PluginSettings()
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
            sys.modules.pop("trid3nt.plugin_settings", None)
            ps = importlib.import_module("trid3nt.plugin_settings")
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


class TestAutoBasemapSettings(unittest.TestCase):
    """Item 4 (live-feedback 2026-07-09): auto_basemap preference in
    plugin_settings -- same shape as TestShowThinkingSettings above."""

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
            sys.modules.pop("trid3nt.plugin_settings", None)
            return importlib.import_module("trid3nt.plugin_settings").PluginSettings()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_default_is_true(self):
        s = self._make_settings()
        self.assertTrue(s.auto_basemap, "auto_basemap must default to True")

    def test_explicit_false_stored(self):
        s = self._make_settings({"trid3nt/auto_basemap": "false"})
        self.assertFalse(s.auto_basemap)

    def test_explicit_true_stored(self):
        s = self._make_settings({"trid3nt/auto_basemap": "true"})
        self.assertTrue(s.auto_basemap)

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
            sys.modules.pop("trid3nt.plugin_settings", None)
            ps = importlib.import_module("trid3nt.plugin_settings")
            s = ps.PluginSettings()
            s.auto_basemap = False
            self.assertEqual(FakeQSettings.store.get("trid3nt/auto_basemap"), "false")
            s.auto_basemap = True
            self.assertEqual(FakeQSettings.store.get("trid3nt/auto_basemap"), "true")
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


class TestProviderModelSettings(unittest.TestCase):
    """OpenRouter model-extensibility (design 2026-07-19): provider / model_id
    / openrouter_api_key round-trip through QSettings -- same FakeQSettings
    idiom as TestShowThinkingSettings / TestAutoBasemapSettings above."""

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
            sys.modules.pop("trid3nt.plugin_settings", None)
            return importlib.import_module("trid3nt.plugin_settings").PluginSettings(), FakeQSettings
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_provider_default_is_local_ollama(self):
        s, _ = self._make_settings()
        self.assertEqual(s.provider, "local-ollama")

    def test_provider_stored_value(self):
        s, _ = self._make_settings({"trid3nt/provider": "openrouter-paid"})
        self.assertEqual(s.provider, "openrouter-paid")

    def test_provider_setter_persists(self):
        s, store = self._make_settings()
        s.provider = "groq"
        self.assertEqual(store.store.get("trid3nt/provider"), "groq")

    def test_provider_blank_falls_back_to_default(self):
        s, store = self._make_settings()
        s.provider = "   "
        self.assertEqual(store.store.get("trid3nt/provider"), "local-ollama")

    def test_model_id_default_is_empty(self):
        s, _ = self._make_settings()
        self.assertEqual(s.model_id, "")

    def test_model_id_stored_value(self):
        s, _ = self._make_settings({"trid3nt/model_id": "deepseek/deepseek-chat"})
        self.assertEqual(s.model_id, "deepseek/deepseek-chat")

    def test_model_id_setter_persists_stripped(self):
        s, store = self._make_settings()
        s.model_id = "  meta-llama/llama-3.3-70b-instruct  "
        self.assertEqual(
            store.store.get("trid3nt/model_id"),
            "meta-llama/llama-3.3-70b-instruct",
        )

    def test_api_key_default_is_empty(self):
        s, _ = self._make_settings()
        self.assertEqual(s.openrouter_api_key, "")

    def test_api_key_round_trip(self):
        s, store = self._make_settings()
        s.openrouter_api_key = "sk-or-v1-SECRET"
        self.assertEqual(store.store.get("trid3nt/openrouter_api_key"), "sk-or-v1-SECRET")
        s2, _ = self._make_settings({"trid3nt/openrouter_api_key": "sk-or-v1-SECRET"})
        self.assertEqual(s2.openrouter_api_key, "sk-or-v1-SECRET")
