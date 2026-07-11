"""HTTP-route wiring tests for /api/probe-point on the catalog listener
(deterministic map-click probe -- the QGIS plugin dock's "Probe" tool).

Exercises ``tool_catalog_http._handle_http`` dispatch only -- the sampling
LOGIC (rasterio reads, frame-sequence grouping, honesty-floor null/error
entries, the layer cap) is covered by ``test_probe_point.py``. Mirrors
``test_ingest_layer_http_route.py``:

  - the route ABSENT (404) outside the local single-user seam;
  - POST /api/probe-point happy path (monkeypatched core fn) -> 200;
  - POST /api/probe-point missing/invalid fields -> typed 400 (core never
    invoked);
  - POST /api/probe-point typed core errors -> honest 404/400;
  - the existing /api/health path stays unaffected.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import tool_catalog_http
from grace2_agent.tools.probe_point import (
    ProbePointCaseNotFoundError,
    ProbePointInputError,
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


def test_probe_point_route_absent_outside_local_mode(monkeypatch):
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    out = _drive(_post("/api/probe-point", b"{}"))
    assert _status(out) == 404


# ---------------------------------------------------------------------------
# POST /api/probe-point
# ---------------------------------------------------------------------------


def test_probe_point_post_happy_path(monkeypatch):
    calls: list[dict] = []
    result = {
        "status": "ok",
        "point": {"lon": -85.42, "lat": 29.95},
        "case_id": "01CASE",
        "results": [
            {"layer_id": "l-1", "name": "Plume concentration", "value": 12.3, "units": "mg/L"},
            {
                "name": "flood depth",
                "series": [
                    {"label": "step 1", "value": 0.02},
                    {"label": "step 2", "value": 0.15},
                ],
                "units": "m",
                "layer_ids": ["f-1", "f-2"],
            },
        ],
        "truncated": False,
        "computed_at": "2026-07-11T00:00:00+00:00",
    }

    async def _fake_probe(**kwargs):
        calls.append(kwargs)
        return dict(result)

    monkeypatch.setattr(tool_catalog_http, "_probe_point_fn", lambda: _fake_probe)

    body = json.dumps(
        {"case_id": "01CASE", "lon": -85.42, "lat": 29.95}
    ).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 200
    assert _body_json(out) == result
    assert calls == [{"case_id": "01CASE", "lon": -85.42, "lat": 29.95}]


def test_probe_point_post_missing_case_id_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("probe fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_probe_point_fn", _never)
    body = json.dumps({"lon": -85.42, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 400
    assert "case_id" in _body_json(out)["error"]


def test_probe_point_post_missing_lon_400(monkeypatch):
    def _never():  # pragma: no cover
        raise AssertionError("probe fn must not be resolved on a bad request")

    monkeypatch.setattr(tool_catalog_http, "_probe_point_fn", _never)
    body = json.dumps({"case_id": "01CASE", "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 400
    assert "lon" in _body_json(out)["error"]


def test_probe_point_post_non_json_body_400():
    out = _drive(_post("/api/probe-point", b"not json"))
    assert _status(out) == 400
    assert "JSON" in _body_json(out)["error"]


def test_probe_point_post_case_not_found_404(monkeypatch):
    async def _fake_probe(**kwargs):
        raise ProbePointCaseNotFoundError("case '01GONE' not found.")

    monkeypatch.setattr(tool_catalog_http, "_probe_point_fn", lambda: _fake_probe)
    body = json.dumps({"case_id": "01GONE", "lon": -85.42, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 404
    assert _body_json(out)["error"] == "case '01GONE' not found."


def test_probe_point_post_invalid_point_400(monkeypatch):
    async def _fake_probe(**kwargs):
        raise ProbePointInputError("lon/lat out of range")

    monkeypatch.setattr(tool_catalog_http, "_probe_point_fn", lambda: _fake_probe)
    body = json.dumps({"case_id": "01CASE", "lon": 999.0, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 400
    assert "lon/lat" in _body_json(out)["error"]


def test_probe_point_post_unexpected_error_500(monkeypatch):
    async def _fake_probe(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tool_catalog_http, "_probe_point_fn", lambda: _fake_probe)
    body = json.dumps({"case_id": "01CASE", "lon": -85.42, "lat": 29.95}).encode()
    out = _drive(_post("/api/probe-point", body))
    assert _status(out) == 500


# ---------------------------------------------------------------------------
# Sibling routes unaffected
# ---------------------------------------------------------------------------


def test_probe_point_route_does_not_perturb_health():
    out = _drive(_get("/api/health"))
    assert _status(out) == 200
    assert b'"ok":true' in out
