"""HTTP-route wiring tests for /api/ingest-layer(-file) on the catalog
listener (bidirectional layer push -- the reverse seam of /api/export-qgis).

Exercises ``tool_catalog_http._handle_http`` dispatch only -- the ingestion
LOGIC (geopandas/rasterio round trips, Persistence merge, AOI pin) is covered
by ``test_import_user_layer.py``. Mirrors
``test_export_qgis_http_route.py`` / ``test_case_list_http_route.py``:

  - both routes ABSENT (404) outside the local single-user seam;
  - POST /api/ingest-layer happy path (monkeypatched core fn) -> 200;
  - POST /api/ingest-layer missing/invalid fields -> typed 400 (core never
    invoked);
  - POST /api/ingest-layer typed core errors -> honest 404/400;
  - POST /api/ingest-layer-file happy path (monkeypatched upload fn) -> 200
    {"s3_uri": ...};
  - POST /api/ingest-layer-file missing filename -> 400;
  - POST /api/ingest-layer-file oversized Content-Length -> 413 WITHOUT
    reading the body;
  - the existing /api/health path stays unaffected.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import tool_catalog_http
from grace2_agent.tools.import_user_layer import (
    CaseNotFoundError,
    ImportLayerInputError,
    ObjectNotFoundError,
    ObjectTooLargeError,
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


def _post(path: str, body: bytes) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\n"
        "Host: agent.local\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode() + body


def _post_no_length(path: str, content_length: int) -> bytes:
    """Headers-only request declaring ``content_length`` but with NO body --
    used to prove the 413 fires BEFORE any ``readexactly``."""
    return (
        f"POST {path} HTTP/1.1\r\n"
        "Host: agent.local\r\n"
        f"Content-Length: {content_length}\r\n"
        "\r\n"
    ).encode()


def _get(path: str) -> bytes:
    return (f"GET {path} HTTP/1.1\r\nHost: agent.local\r\n\r\n").encode()


def _drive(request: bytes) -> bytes:
    reader = _FakeReader(request)
    writer = _FakeWriter()
    asyncio.run(tool_catalog_http._handle_http(reader, writer))
    assert writer.closed is True
    return bytes(writer.buffer)


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _body_json(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")


# ---------------------------------------------------------------------------
# Route gating
# ---------------------------------------------------------------------------


def test_ingest_layer_route_absent_outside_local_mode(monkeypatch):
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    out = _drive(_post("/api/ingest-layer", b"{}"))
    assert _status(out) == 404


def test_ingest_layer_file_route_absent_outside_local_mode(monkeypatch):
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    out = _drive(_post("/api/ingest-layer-file?filename=x.tif", b"data"))
    assert _status(out) == 404


# ---------------------------------------------------------------------------
# POST /api/ingest-layer
# ---------------------------------------------------------------------------


def test_ingest_layer_post_happy_path(monkeypatch):
    calls: list[dict] = []
    result = {
        "status": "ok",
        "layer_id": "user-01ABC",
        "name": "AOI",
        "layer_type": "vector",
        "uri": "s3://runs/case-data/01CASE/user-01ABC.fgb",
        "bbox": [-1.0, -2.0, 3.0, 4.0],
        "aoi_pinned": True,
        "feature_count": 1,
    }

    async def _fake_ingest(**kwargs):
        calls.append(kwargs)
        return dict(result)

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", lambda: _fake_ingest)

    body = json.dumps(
        {
            "case_id": "01CASE",
            "name": "AOI",
            "kind": "vector",
            "s3_uri": "s3://cache/user-uploads/x/aoi.geojson",
            "make_aoi": True,
        }
    ).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 200
    assert _body_json(out) == result
    assert calls == [
        {
            "case_id": "01CASE",
            "name": "AOI",
            "kind": "vector",
            "s3_uri": "s3://cache/user-uploads/x/aoi.geojson",
            "crs_authid": None,
            "make_aoi": True,
        }
    ]


def test_ingest_layer_post_missing_case_id_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("ingest fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", _never)
    body = json.dumps({"name": "x", "kind": "vector", "s3_uri": "s3://b/k"}).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 400
    assert "case_id" in _body_json(out)["error"]


def test_ingest_layer_post_bad_kind_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("ingest fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", _never)
    body = json.dumps(
        {"case_id": "01CASE", "name": "x", "kind": "mesh", "s3_uri": "s3://b/k"}
    ).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 400
    assert "kind" in _body_json(out)["error"]


def test_ingest_layer_post_missing_s3_uri_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("ingest fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", _never)
    body = json.dumps({"case_id": "01CASE", "name": "x", "kind": "vector"}).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 400
    assert "s3_uri" in _body_json(out)["error"]


def test_ingest_layer_post_non_json_body_400():
    out = _drive(_post("/api/ingest-layer", b"not json"))
    assert _status(out) == 400
    assert "JSON" in _body_json(out)["error"]


def test_ingest_layer_post_case_not_found_404(monkeypatch):
    async def _fake_ingest(**kwargs):
        raise CaseNotFoundError("case '01GONE' not found")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", lambda: _fake_ingest)
    body = json.dumps(
        {"case_id": "01GONE", "name": "x", "kind": "vector", "s3_uri": "s3://b/k"}
    ).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 404
    assert _body_json(out)["error"] == "case '01GONE' not found"


def test_ingest_layer_post_object_not_found_404(monkeypatch):
    async def _fake_ingest(**kwargs):
        raise ObjectNotFoundError("no such object: s3://b/k")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", lambda: _fake_ingest)
    body = json.dumps(
        {"case_id": "01CASE", "name": "x", "kind": "vector", "s3_uri": "s3://b/k"}
    ).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 404


def test_ingest_layer_post_object_too_large_400(monkeypatch):
    async def _fake_ingest(**kwargs):
        raise ObjectTooLargeError("too large")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", lambda: _fake_ingest)
    body = json.dumps(
        {"case_id": "01CASE", "name": "x", "kind": "raster", "s3_uri": "s3://b/k"}
    ).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 400
    assert "too large" in _body_json(out)["error"]
    assert b"Traceback" not in out


def test_ingest_layer_post_typed_input_error_400(monkeypatch):
    async def _fake_ingest(**kwargs):
        raise ImportLayerInputError("bad input")

    monkeypatch.setattr(tool_catalog_http, "_ingest_layer_fn", lambda: _fake_ingest)
    body = json.dumps(
        {"case_id": "01CASE", "name": "x", "kind": "vector", "s3_uri": "s3://b/k"}
    ).encode()
    out = _drive(_post("/api/ingest-layer", body))
    assert _status(out) == 400


# ---------------------------------------------------------------------------
# POST /api/ingest-layer-file
# ---------------------------------------------------------------------------


def test_ingest_layer_file_happy_path(monkeypatch):
    calls: list[tuple[str, bytes]] = []

    def _fake_upload(filename: str, data: bytes) -> str:
        calls.append((filename, data))
        return f"s3://cache/user-uploads/01ULID/{filename}"

    monkeypatch.setattr(
        tool_catalog_http, "_upload_layer_file_fn", lambda: _fake_upload
    )

    body = b"PK\x03\x04fakebytes"
    out = _drive(_post("/api/ingest-layer-file?filename=aoi.gpkg", body))
    assert _status(out) == 200
    assert _body_json(out) == {"s3_uri": "s3://cache/user-uploads/01ULID/aoi.gpkg"}
    assert calls == [("aoi.gpkg", body)]


def test_ingest_layer_file_missing_filename_400(monkeypatch):
    def _never(filename, data):  # pragma: no cover
        raise AssertionError("upload fn must not run without a filename")

    monkeypatch.setattr(tool_catalog_http, "_upload_layer_file_fn", lambda: _never)
    out = _drive(_post("/api/ingest-layer-file", b"data"))
    assert _status(out) == 400
    assert "filename" in _body_json(out)["error"]


def test_ingest_layer_file_empty_body_400(monkeypatch):
    def _never(filename, data):  # pragma: no cover
        raise AssertionError("upload fn must not run on an empty body")

    monkeypatch.setattr(tool_catalog_http, "_upload_layer_file_fn", lambda: _never)
    out = _drive(_post("/api/ingest-layer-file?filename=x.tif", b""))
    assert _status(out) == 400


def test_ingest_layer_file_oversized_413_before_read(monkeypatch):
    """A Content-Length beyond the cap is rejected BEFORE the (absent) body
    is read -- ``_post_no_length`` sends NO body bytes at all, so a bug that
    tried to read first would raise/hang instead of cleanly 413ing."""

    def _never(filename, data):  # pragma: no cover
        raise AssertionError("upload fn must not run on an oversized body")

    monkeypatch.setattr(tool_catalog_http, "_upload_layer_file_fn", lambda: _never)
    from grace2_agent.tools.import_user_layer import MAX_INGEST_BYTES

    out = _drive(
        _post_no_length(
            "/api/ingest-layer-file?filename=huge.tif", MAX_INGEST_BYTES + 1
        )
    )
    assert _status(out) == 413


# ---------------------------------------------------------------------------
# Sibling routes unaffected
# ---------------------------------------------------------------------------


def test_ingest_layer_routes_do_not_perturb_health():
    out = _drive(_get("/api/health"))
    assert _status(out) == 200
    assert b'"ok":true' in out
