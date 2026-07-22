"""HTTP-route wiring tests for the QGIS proxy on the catalog listener (job-0255).

Exercises ``tool_catalog_http._handle_http`` dispatch for ``/qgis-proxy``:
  - disabled-by-default ⇒ 404 (route absent, today's behavior unchanged);
  - enabled ⇒ streamed response head + body relayed to the writer;
  - the existing ``/api/tool-catalog`` + ``/api/health`` paths are unaffected.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from grace2_agent import qgis_proxy
from grace2_agent.qgis_proxy import ProxyResult
from grace2_agent import tool_catalog_http


class _FakeReader:
    """Feed a single HTTP/1.1 GET request, then EOF."""

    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
        # Re-join with CRLF so readline returns CRLF-terminated lines.
        self._buf = [ln + b"\r\n" for ln in self._lines]

    async def readline(self):
        if self._buf:
            return self._buf.pop(0)
        return b""


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


def _request(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        "Host: agent.local\r\n"
        "Authorization: Bearer USERTOKEN\r\n"
        "Cookie: session=abc\r\n"
        "\r\n"
    ).encode()


def _run(coro):
    return asyncio.run(coro)


def test_qgis_proxy_route_404_when_disabled(monkeypatch):
    monkeypatch.delenv("QGIS_PROXY_ENABLED", raising=False)
    reader = _FakeReader(_request("/qgis-proxy?MAP=x&LAYERS=y"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    assert b"404 Not Found" in bytes(writer.buffer)
    assert writer.closed is True


def test_qgis_proxy_route_streams_when_enabled(monkeypatch):
    monkeypatch.setenv("QGIS_PROXY_ENABLED", "true")
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")

    captured = {}

    async def _fake_stream(qs, write_head, write_chunk, **kwargs):
        captured["qs"] = qs
        result = ProxyResult(200, {"Content-Type": "image/png", "Content-Length": "4"})
        await write_head(result)
        await write_chunk(b"PNG1")
        return result

    with patch.object(qgis_proxy, "stream_qgis_response", _fake_stream):
        reader = _FakeReader(_request("/qgis-proxy?MAP=/mnt/qgs/x.qgs&LAYERS=flood"))
        writer = _FakeWriter()
        _run(tool_catalog_http._handle_http(reader, writer))

    out = bytes(writer.buffer)
    # Query string reached the proxy verbatim (the inbound creds did not — the
    # proxy module never sees the client headers via this path).
    assert captured["qs"] == "MAP=/mnt/qgs/x.qgs&LAYERS=flood"
    assert b"200 OK" in out
    assert b"Content-Type: image/png" in out
    assert b"Access-Control-Allow-Origin: *" in out
    assert out.endswith(b"PNG1")
    assert writer.closed is True


def test_qgis_proxy_route_502_when_upstream_unreachable(monkeypatch):
    monkeypatch.setenv("QGIS_PROXY_ENABLED", "true")

    async def _boom(qs, write_head, write_chunk, **kwargs):
        raise ConnectionError("upstream down")

    with patch.object(qgis_proxy, "stream_qgis_response", _boom):
        reader = _FakeReader(_request("/qgis-proxy?MAP=x"))
        writer = _FakeWriter()
        _run(tool_catalog_http._handle_http(reader, writer))

    out = bytes(writer.buffer)
    assert b"502 Bad Gateway" in out
    assert writer.closed is True


def test_catalog_route_unaffected(monkeypatch):
    """The proxy wiring must not perturb the existing health endpoint.

    The autostop liveness probe expanded the body to
    ``{"ok":true,"active_connections":<int>,"busy":<bool>}`` (idle Lambda gate);
    ``ok`` is still present + 200, and at rest the box reports zero connections
    and not busy."""
    monkeypatch.setenv("QGIS_PROXY_ENABLED", "true")
    reader = _FakeReader(_request("/api/health"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"200 OK" in out
    assert b'"ok":true' in out
    assert b'"active_connections":0' in out
    assert b'"busy":false' in out
