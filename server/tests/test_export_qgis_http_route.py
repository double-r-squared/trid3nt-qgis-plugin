"""HTTP-route wiring tests for /api/export-qgis on the catalog listener.

User-driven QGIS export (NATE 2026-07-06): the web's per-case "Export to
QGIS" kebab item POSTs a case_id here; the route awaits the
``export_case_to_qgis`` tool and returns its result dict. A sibling GET
serves the produced .qgz/.gpkg bytes, path-traversal guarded to the export
root (GRACE2_EXPORT_DIR, default ~/trid3nt-exports).

Exercises ``tool_catalog_http._handle_http`` dispatch:
  - POST happy path (monkeypatched export fn) -> 200 with the result dict;
  - POST with missing / empty case_id -> typed 400 (tool never invoked);
  - POST with a typed tool error -> honest 4xx {"error": message};
  - GET file happy path (.qgz -> application/zip, .gpkg ->
    application/geopackage+sqlite3) with Content-Disposition;
  - GET file ../ traversal escape -> 403; outside-root absolute path -> 403;
  - GET file for a missing in-root file -> 404;
  - the existing /api/tool-catalog path stays unaffected.
"""

from __future__ import annotations

import asyncio
import json

from grace2_agent import tool_catalog_http
from grace2_agent.tools.export_case_to_qgis import (
    CaseNotFoundError,
    NoExportableLayersError,
)


class _FakeReader:
    """Feed a single raw HTTP/1.1 request (headers + optional body), then EOF."""

    def __init__(self, request: bytes):
        self._data = request
        self._pos = 0

    async def readline(self):
        idx = self._data.find(b"\n", self._pos)
        if idx == -1:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : idx + 1]
        self._pos = idx + 1
        return chunk

    async def readexactly(self, n: int):
        if len(self._data) - self._pos < n:
            raise asyncio.IncompleteReadError(self._data[self._pos :], n)
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


class _FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def _get(path: str) -> bytes:
    return (f"GET {path} HTTP/1.1\r\nHost: agent.local\r\n\r\n").encode()


def _post(path: str, body: bytes) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\n"
        "Host: agent.local\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode() + body


def _drive(request: bytes) -> bytes:
    reader = _FakeReader(request)
    writer = _FakeWriter()
    asyncio.run(tool_catalog_http._handle_http(reader, writer))
    assert writer.closed is True
    return bytes(writer.buffer)


def _body_json(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def _body_bytes(out: bytes) -> bytes:
    _, _, body = out.partition(b"\r\n\r\n")
    return body


# ---------------------------------------------------------------------------
# POST /api/export-qgis
# ---------------------------------------------------------------------------


def test_export_qgis_post_happy_path(monkeypatch):
    """A well-formed case_id runs the export fn and relays its result dict."""
    calls: list[dict] = []
    result = {
        "status": "ok",
        "qgz_path": "/exports/case-abc/project.qgz",
        "gpkg_path": "/exports/case-abc/export.gpkg",
        "exported_vector_count": 2,
        "exported_raster_count": 1,
        "skipped": [],
        "output_dir": "/exports/case-abc",
    }

    async def _fake_export(**kwargs):
        calls.append(kwargs)
        return dict(result)

    monkeypatch.setattr(tool_catalog_http, "_export_qgis_fn", lambda: _fake_export)

    out = _drive(_post("/api/export-qgis", b'{"case_id": "01CASE"}'))
    assert b"200 OK" in out
    assert _body_json(out) == result
    assert calls == [{"case_id": "01CASE"}]


def test_export_qgis_post_missing_case_id_400(monkeypatch):
    """A body without case_id is a typed 400; the tool is never invoked."""

    def _never():  # pragma: no cover -- validation must run first
        raise AssertionError("export fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_export_qgis_fn", _never)

    out = _drive(_post("/api/export-qgis", b"{}"))
    assert b"400 Bad Request" in out
    assert "case_id" in _body_json(out)["error"]


def test_export_qgis_post_empty_case_id_400(monkeypatch):
    """A blank case_id is a typed 400; the tool is never invoked."""

    def _never():  # pragma: no cover
        raise AssertionError("export fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_export_qgis_fn", _never)

    out = _drive(_post("/api/export-qgis", b'{"case_id": "   "}'))
    assert b"400 Bad Request" in out
    assert "case_id" in _body_json(out)["error"]


def test_export_qgis_post_non_json_body_400(monkeypatch):
    """An unparseable body is a typed 400, not a 500."""
    out = _drive(_post("/api/export-qgis", b"not json"))
    assert b"400 Bad Request" in out
    assert "JSON" in _body_json(out)["error"]


def test_export_qgis_post_case_not_found_404(monkeypatch):
    """The tool's typed CASE_NOT_FOUND maps to an honest 404 {"error": msg}."""

    async def _fake_export(**kwargs):
        raise CaseNotFoundError("case '01GONE' not found.")

    monkeypatch.setattr(tool_catalog_http, "_export_qgis_fn", lambda: _fake_export)

    out = _drive(_post("/api/export-qgis", b'{"case_id": "01GONE"}'))
    assert b"404 Not Found" in out
    assert _body_json(out)["error"] == "case '01GONE' not found."


def test_export_qgis_post_typed_tool_error_400(monkeypatch):
    """Other typed ExportCaseError subclasses map to an honest 400."""

    async def _fake_export(**kwargs):
        raise NoExportableLayersError("case '01EMPTY' has no layers to export.")

    monkeypatch.setattr(tool_catalog_http, "_export_qgis_fn", lambda: _fake_export)

    out = _drive(_post("/api/export-qgis", b'{"case_id": "01EMPTY"}'))
    assert b"400 Bad Request" in out
    assert "no layers" in _body_json(out)["error"]
    # Honest text only -- never a traceback.
    assert b"Traceback" not in out


# ---------------------------------------------------------------------------
# GET /api/export-qgis/file
# ---------------------------------------------------------------------------


def test_export_qgis_file_serves_qgz(tmp_path, monkeypatch):
    """An in-root .qgz is served as application/zip with its bytes intact."""
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path))
    export_dir = tmp_path / "case-abc"
    export_dir.mkdir()
    qgz = export_dir / "project.qgz"
    qgz.write_bytes(b"PK\x03\x04fakeqgz")

    out = _drive(_get(f"/api/export-qgis/file?path={qgz}"))
    assert b"200 OK" in out
    assert b"Content-Type: application/zip" in out
    assert b'attachment; filename="project.qgz"' in out
    assert _body_bytes(out) == b"PK\x03\x04fakeqgz"


def test_export_qgis_file_serves_gpkg(tmp_path, monkeypatch):
    """An in-root .gpkg is served with the GeoPackage media type."""
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path))
    gpkg = tmp_path / "export.gpkg"
    gpkg.write_bytes(b"SQLite format 3\x00")

    out = _drive(_get(f"/api/export-qgis/file?path={gpkg}"))
    assert b"200 OK" in out
    assert b"Content-Type: application/geopackage+sqlite3" in out
    assert _body_bytes(out) == b"SQLite format 3\x00"


def test_export_qgis_file_traversal_403(tmp_path, monkeypatch):
    """A ../ escape that resolves outside the export root is a 403."""
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path / "exports"))
    (tmp_path / "exports").mkdir()
    # Lives OUTSIDE the root; reached via an in-root-looking ../ path.
    secret = tmp_path / "secret.qgz"
    secret.write_bytes(b"outside")

    traversal = f"{tmp_path}/exports/../secret.qgz"
    out = _drive(_get(f"/api/export-qgis/file?path={traversal}"))
    assert b"403 Forbidden" in out
    assert b"outside" not in _body_bytes(out).replace(b"outside the export root", b"")


def test_export_qgis_file_outside_root_403(tmp_path, monkeypatch):
    """A plain absolute path outside the root is a 403 (no bytes leak)."""
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path))
    elsewhere = tmp_path.parent / "elsewhere.qgz"
    elsewhere.write_bytes(b"leak")

    out = _drive(_get(f"/api/export-qgis/file?path={elsewhere}"))
    assert b"403 Forbidden" in out


def test_export_qgis_file_disallowed_extension_403(tmp_path, monkeypatch):
    """Even in-root, only the .qgz/.gpkg artifacts are served."""
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path))
    other = tmp_path / "notes.txt"
    other.write_text("private")

    out = _drive(_get(f"/api/export-qgis/file?path={other}"))
    assert b"403 Forbidden" in out


def test_export_qgis_file_missing_404(tmp_path, monkeypatch):
    """An in-root path that does not exist is a 404, not a 500."""
    monkeypatch.setenv("GRACE2_EXPORT_DIR", str(tmp_path))
    out = _drive(_get(f"/api/export-qgis/file?path={tmp_path}/gone/project.qgz"))
    assert b"404 Not Found" in out


def test_export_qgis_routes_do_not_perturb_catalog():
    """The new routes must not break the sibling tool-catalog route."""
    out = _drive(_get("/api/tool-catalog"))
    assert b"200 OK" in out
