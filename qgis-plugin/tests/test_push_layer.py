"""Tests for ``push_layer.py`` -- bidirectional layer push (the reverse seam
of ``case_export.py``'s open-in-QGIS).

No QGIS required (pure request-builder/parser tests, mirroring
``test_milestone3.py``'s ``TestRemoteDownload`` pattern): a small
``http.server.BaseHTTPRequestHandler`` stub mirrors the agent's real
``/api/ingest-layer-file`` + ``/api/ingest-layer`` route semantics
(services/agent ``tool_catalog_http.py``), and the tests drive
``push_layer.upload_layer_bytes`` / ``post_ingest_layer`` /
``push_exported_file`` / ``format_push_note`` against it. The ONE
QGIS-touching function (``export_active_layer_to_tempfile``) is intentionally
NOT exercised here -- see ``tests/headless_push_layer_proof.py`` for the
full plugin-side flow proof (temp file already on disk -> upload -> ingest
-> note) and the module docstring for why that split exists.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trid3nt"))
sys.path.insert(0, os.path.dirname(__file__))

import push_layer  # noqa: E402


# ---------------------------------------------------------------------------
# Ingest-route stub -- the REAL routes' semantics, in miniature
# ---------------------------------------------------------------------------


class _IngestStub(http.server.BaseHTTPRequestHandler):
    """POST /api/ingest-layer-file?filename=... + POST /api/ingest-layer with
    the agent's guard ladder (400 missing/empty, 200 otherwise)."""

    upload_result: dict = {"s3_uri": "s3://cache/user-uploads/01ULID/x.gpkg"}
    ingest_result: dict = {}
    ingest_status: int = 200
    last_upload_body: bytes = b""
    last_upload_filename: str = ""
    last_ingest_body: dict = {}

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""

        if path == "/api/ingest-layer-file":
            params = urllib.parse.parse_qs(qs)
            filename = (params.get("filename") or [""])[0]
            if not filename:
                self._json(400, {"error": "missing `filename` query param"})
                return
            if not raw_body:
                self._json(400, {"error": "missing or empty request body"})
                return
            _IngestStub.last_upload_filename = filename
            _IngestStub.last_upload_body = raw_body
            self._json(200, self.upload_result)
            return

        if path == "/api/ingest-layer":
            body = json.loads(raw_body) if raw_body else {}
            if not body.get("case_id") or not body.get("s3_uri"):
                self._json(400, {"error": "missing case_id or s3_uri"})
                return
            _IngestStub.last_ingest_body = body
            self._json(self.ingest_status, self.ingest_result)
            return

        self._json(404, {"error": "not found"})

    def log_message(self, *args):  # silence
        pass


class _IngestStubBase(unittest.TestCase):
    def setUp(self):
        _IngestStub.upload_result = {
            "s3_uri": "s3://cache/user-uploads/01ULID/x.gpkg"
        }
        _IngestStub.ingest_result = {
            "status": "ok",
            "layer_id": "user-01ABC",
            "name": "My layer",
            "layer_type": "vector",
            "uri": "s3://runs/case-data/01CASE/user-01ABC.fgb",
            "bbox": [-1.0, -2.0, 3.0, 4.0],
            "aoi_pinned": False,
            "feature_count": 3,
        }
        _IngestStub.ingest_status = 200
        _IngestStub.last_upload_body = b""
        _IngestStub.last_upload_filename = ""
        _IngestStub.last_ingest_body = {}
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _IngestStub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"


# ---------------------------------------------------------------------------
# upload_layer_bytes
# ---------------------------------------------------------------------------


class TestUploadLayerBytes(_IngestStubBase):
    def test_upload_returns_s3_uri(self):
        s3_uri = push_layer.upload_layer_bytes(self.base, "aoi.gpkg", b"gpkg-bytes")
        self.assertEqual(s3_uri, "s3://cache/user-uploads/01ULID/x.gpkg")
        self.assertEqual(_IngestStub.last_upload_filename, "aoi.gpkg")
        self.assertEqual(_IngestStub.last_upload_body, b"gpkg-bytes")

    def test_upload_empty_body_rejected_locally(self):
        with self.assertRaises(push_layer.PushLayerRequestError):
            push_layer.upload_layer_bytes(self.base, "x.gpkg", b"")

    def test_upload_server_400_surfaced(self):
        with self.assertRaises(push_layer.PushLayerRequestError) as ctx:
            push_layer.upload_layer_bytes(self.base, "", b"data")
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_upload_unreachable_host(self):
        with self.assertRaises(push_layer.PushLayerRequestError) as ctx:
            push_layer.upload_layer_bytes(
                "http://127.0.0.1:1", "x.gpkg", b"data", timeout=1.0
            )
        self.assertIn("unreachable", str(ctx.exception))


# ---------------------------------------------------------------------------
# post_ingest_layer
# ---------------------------------------------------------------------------


class TestPostIngestLayer(_IngestStubBase):
    def test_post_ingest_happy_path(self):
        result = push_layer.post_ingest_layer(
            self.base,
            "01CASE",
            "My layer",
            "vector",
            "s3://cache/user-uploads/01ULID/x.gpkg",
            crs_authid="EPSG:4326",
            make_aoi=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["layer_id"], "user-01ABC")
        sent = _IngestStub.last_ingest_body
        self.assertEqual(sent["case_id"], "01CASE")
        self.assertEqual(sent["name"], "My layer")
        self.assertEqual(sent["kind"], "vector")
        self.assertEqual(sent["s3_uri"], "s3://cache/user-uploads/01ULID/x.gpkg")
        self.assertEqual(sent["crs_authid"], "EPSG:4326")
        self.assertIs(sent["make_aoi"], True)

    def test_post_ingest_omits_crs_authid_when_absent(self):
        push_layer.post_ingest_layer(
            self.base, "01CASE", "x", "raster", "s3://b/k.tif"
        )
        self.assertNotIn("crs_authid", _IngestStub.last_ingest_body)
        self.assertIs(_IngestStub.last_ingest_body["make_aoi"], False)

    def test_post_ingest_case_not_found_404_surfaced(self):
        _IngestStub.ingest_status = 404
        _IngestStub.ingest_result = {"error": "case '01GONE' not found"}
        with self.assertRaises(push_layer.PushLayerRequestError) as ctx:
            push_layer.post_ingest_layer(
                self.base, "01GONE", "x", "vector", "s3://b/k.geojson"
            )
        self.assertIn("not found", str(ctx.exception))


# ---------------------------------------------------------------------------
# push_exported_file -- the full pure (no-PyQGIS) orchestration: an
# already-on-disk file -> upload -> ingest -> the temp file is removed.
# ---------------------------------------------------------------------------


class TestPushExportedFile(_IngestStubBase):
    def setUp(self):
        super().setUp()
        fd, self.tmp_path = tempfile.mkstemp(suffix=".gpkg", prefix="push_test_")
        os.close(fd)
        with open(self.tmp_path, "wb") as f:
            f.write(b"fake-gpkg-bytes")

    def test_push_exported_file_happy_path_deletes_temp(self):
        result = push_layer.push_exported_file(
            self.base,
            "01CASE",
            self.tmp_path,
            "vector",
            "My layer",
            crs_authid="EPSG:4326",
            make_aoi=False,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(_IngestStub.last_upload_body, b"fake-gpkg-bytes")
        self.assertEqual(
            _IngestStub.last_ingest_body["s3_uri"],
            "s3://cache/user-uploads/01ULID/x.gpkg",
        )
        self.assertEqual(_IngestStub.last_ingest_body["crs_authid"], "EPSG:4326")
        self.assertFalse(os.path.exists(self.tmp_path))

    def test_push_exported_file_deletes_temp_on_upload_failure(self):
        os.unlink(self.tmp_path)
        with open(self.tmp_path, "wb") as f:
            f.write(b"")  # empty -> upload_layer_bytes raises locally
        with self.assertRaises(push_layer.PushLayerRequestError):
            push_layer.push_exported_file(
                self.base, "01CASE", self.tmp_path, "vector", "x"
            )
        self.assertFalse(os.path.exists(self.tmp_path))


# ---------------------------------------------------------------------------
# format_push_note
# ---------------------------------------------------------------------------


class TestFormatPushNote(unittest.TestCase):
    def test_vector_with_feature_count(self):
        note = push_layer.format_push_note(
            "Rivers", {"layer_type": "vector", "feature_count": 5}
        )
        self.assertEqual(note, "'Rivers' pushed to case (vector, 5 features)")

    def test_vector_singular_feature(self):
        note = push_layer.format_push_note(
            "AOI", {"layer_type": "vector", "feature_count": 1}
        )
        self.assertEqual(note, "'AOI' pushed to case (vector, 1 feature)")

    def test_raster(self):
        note = push_layer.format_push_note("DEM", {"layer_type": "raster"})
        self.assertEqual(note, "'DEM' pushed to case (raster)")

    def test_aoi_pinned_suffix(self):
        note = push_layer.format_push_note(
            "AOI", {"layer_type": "vector", "feature_count": 1, "aoi_pinned": True}
        )
        self.assertTrue(note.endswith("-- case AOI updated"))


if __name__ == "__main__":
    unittest.main()
