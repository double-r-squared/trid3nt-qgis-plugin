"""Tests for the thin streaming QGIS WMS proxy (job-0255, sprint-13.5 Stage 2).

Coverage (per the kickoff §4):
  1. param passthrough — the client query string reaches the upstream verbatim,
     forwarded ONLY to the fixed configured base URL.
  2. credential stripping — the forwarded upstream request carries NONE of the
     inbound user credentials (Authorization / Cookie / Firebase / IAP).
  3. streaming — a large fake body is relayed chunk-by-chunk; the proxy never
     materialises the whole body (asserted via bounded chunk iteration).
  4. open-proxy rejection — an inbound param that tries to change the upstream
     host is ignored; the upstream is always the configured base.
  5. disabled-by-default — ``QGIS_PROXY_ENABLED`` unset/false ⇒ route 404s.
  6. dev no-token graceful path — no credentials ⇒ forward without a token,
     never crash.
  7. upstream 5xx relayed honestly — a 502 from the upstream surfaces as a 502
     (not masked as success).
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from grace2_agent import qgis_proxy
from grace2_agent.qgis_proxy import (
    PASSTHROUGH_RESPONSE_HEADERS,
    STRIPPED_REQUEST_HEADERS,
    ProxyResult,
    qgis_proxy_enabled,
    qgis_server_base_url,
    stream_qgis_response,
)


# ---------------------------------------------------------------------------
# Fakes for the ``requests`` streaming surface
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` with ``stream=True``."""

    def __init__(self, status: int, headers: dict, body: bytes, chunk: int):
        self.status_code = status
        self.headers = headers
        self._body = body
        self._chunk = chunk
        self.closed = False
        self.iter_chunk_sizes: list[int] = []

    def iter_content(self, chunk_size: int = 1):
        # Record the chunk size used (proves the relay reads bounded chunks).
        for i in range(0, len(self._body), chunk_size):
            piece = self._body[i : i + chunk_size]
            self.iter_chunk_sizes.append(len(piece))
            yield piece

    def close(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


async def _drive_proxy(query_string: str, *, base_url=None, chunk_size=64 * 1024):
    """Drive ``stream_qgis_response`` collecting the head + body chunks."""
    head: dict = {}
    chunks: list[bytes] = []

    async def _write_head(result: ProxyResult):
        head["status"] = result.status
        head["headers"] = result.headers

    async def _write_chunk(chunk: bytes):
        chunks.append(chunk)

    result = await stream_qgis_response(
        query_string,
        _write_head,
        _write_chunk,
        base_url=base_url,
        chunk_size=chunk_size,
    )
    return head, chunks, result


# ---------------------------------------------------------------------------
# 5. disabled-by-default
# ---------------------------------------------------------------------------


def test_proxy_disabled_by_default(monkeypatch):
    monkeypatch.delenv("QGIS_PROXY_ENABLED", raising=False)
    assert qgis_proxy_enabled() is False


@pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "FALSE", "garbage"])
def test_proxy_disabled_falsey_values(monkeypatch, val):
    monkeypatch.setenv("QGIS_PROXY_ENABLED", val)
    assert qgis_proxy_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE", "On"])
def test_proxy_enabled_truthy_values(monkeypatch, val):
    monkeypatch.setenv("QGIS_PROXY_ENABLED", val)
    assert qgis_proxy_enabled() is True


# ---------------------------------------------------------------------------
# 1 + 4. param passthrough + open-proxy rejection
# ---------------------------------------------------------------------------


def test_param_passthrough_to_fixed_base(monkeypatch):
    monkeypatch.setenv(
        "QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms"
    )
    captured = {}

    def _fake_get(url, headers=None, stream=False, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"Content-Type": "image/png"}, b"PNGDATA", 4)

    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: None
    ):
        head, chunks, result = _run(
            _drive_proxy("MAP=/mnt/qgs/x.qgs&LAYERS=flood&BBOX=1,2,3,4")
        )

    # Query string reaches the upstream verbatim, on the configured base.
    assert captured["url"] == (
        "https://qgis.example.test/ogc/wms"
        "?MAP=/mnt/qgs/x.qgs&LAYERS=flood&BBOX=1,2,3,4"
    )
    assert result.status == 200
    assert b"".join(chunks) == b"PNGDATA"


def test_open_proxy_rejected_inbound_host_ignored(monkeypatch):
    """An inbound attempt to redirect the upstream host is ignored — the
    upstream is ALWAYS the configured base, with the query string appended."""
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")
    captured = {}

    def _fake_get(url, headers=None, stream=False, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, {"Content-Type": "image/png"}, b"OK", 2)

    # A malicious query string carrying an absolute URL / alternate host must
    # NOT change where we forward — it only ever lands as query params on the
    # fixed base.
    evil_qs = "MAP=https://evil.example/steal&LAYERS=x&host=evil.example"
    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: None
    ):
        _run(_drive_proxy(evil_qs))

    assert captured["url"].startswith("https://qgis.example.test/ogc/wms?")
    # The fixed base host is the only netloc the request was sent to.
    from urllib.parse import urlsplit

    assert urlsplit(captured["url"]).netloc == "qgis.example.test"


# ---------------------------------------------------------------------------
# 2. credential stripping
# ---------------------------------------------------------------------------


def test_credential_stripping_no_inbound_headers_forwarded(monkeypatch):
    """The forwarded request carries ONLY the proxy's own headers (UA + the
    OIDC bearer). NONE of the inbound user-credential headers reach upstream —
    the proxy forwards nothing it received from the client."""
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")
    captured = {}

    def _fake_get(url, headers=None, stream=False, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
        return _FakeResponse(200, {"Content-Type": "image/png"}, b"X", 1)

    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: None
    ):
        _run(_drive_proxy("MAP=x&LAYERS=y"))

    fwd = captured["headers"]
    # Every credential-class header is absent from the forwarded request.
    for h in STRIPPED_REQUEST_HEADERS:
        assert h not in fwd, f"credential header {h!r} leaked to upstream"
    # Only the proxy's own user-agent is present (no token in this dev path).
    assert fwd == {"user-agent": "grace-2-agent-qgis-proxy/0.1"}


def test_oidc_token_attached_when_present(monkeypatch):
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")
    captured = {}

    def _fake_get(url, headers=None, stream=False, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
        return _FakeResponse(200, {"Content-Type": "image/png"}, b"X", 1)

    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: "TESTTOKEN"
    ):
        _run(_drive_proxy("MAP=x&LAYERS=y"))

    assert captured["headers"]["authorization"] == "Bearer TESTTOKEN"


# ---------------------------------------------------------------------------
# 3. streaming — large body relayed in bounded chunks
# ---------------------------------------------------------------------------


def test_streaming_large_body_chunked(monkeypatch):
    """A multi-MB body is relayed in fixed-size chunks; the proxy reads it via
    ``iter_content(chunk_size=...)`` so memory stays O(chunk), not O(body)."""
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")
    big = b"A" * (3 * 1024 * 1024 + 123)  # 3 MB + tail
    fake = _FakeResponse(200, {"Content-Type": "image/png"}, big, 0)

    def _fake_get(url, headers=None, stream=False, timeout=None):
        assert stream is True  # streaming MUST be requested
        return fake

    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: None
    ):
        head, chunks, result = _run(_drive_proxy("MAP=x&LAYERS=y", chunk_size=65536))

    # Full body relayed, byte-exact.
    assert b"".join(chunks) == big
    # More than one chunk was read (proves chunked iteration, not one slurp).
    assert len(chunks) > 1
    # Every chunk the upstream yielded was at most the requested chunk_size.
    assert max(fake.iter_chunk_sizes) <= 65536
    # No single chunk equals the whole body (bounded memory).
    assert all(len(c) <= 65536 for c in chunks)
    assert fake.closed is True


# ---------------------------------------------------------------------------
# 6. dev no-token graceful path
# ---------------------------------------------------------------------------


def test_dev_no_token_graceful(monkeypatch):
    """With no credentials, ``fetch_oidc_token`` returns None and the relay
    forwards unauthenticated without crashing."""
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")

    # Force the real fetch_id_token path to fail (no ADC) — must degrade.
    def _boom(*a, **k):
        raise RuntimeError("no ADC in dev")

    with patch("google.oauth2.id_token.fetch_id_token", _boom):
        token = qgis_proxy.fetch_oidc_token("https://qgis.example.test/ogc/wms")
    assert token is None

    def _fake_get(url, headers=None, stream=False, timeout=None):
        return _FakeResponse(200, {"Content-Type": "image/png"}, b"OK", 2)

    with patch("requests.get", _fake_get):
        head, chunks, result = _run(_drive_proxy("MAP=x&LAYERS=y"))
    assert result.status == 200
    assert b"".join(chunks) == b"OK"


# ---------------------------------------------------------------------------
# 7. upstream 5xx relayed honestly
# ---------------------------------------------------------------------------


def test_upstream_5xx_relayed_honestly(monkeypatch):
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")

    def _fake_get(url, headers=None, stream=False, timeout=None):
        return _FakeResponse(
            502, {"Content-Type": "text/xml"}, b"<ServerException/>", 8
        )

    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: None
    ):
        head, chunks, result = _run(_drive_proxy("MAP=x&LAYERS=y"))

    # The upstream status is relayed verbatim — never masked as 200.
    assert result.status == 502
    assert head["status"] == 502
    assert b"".join(chunks) == b"<ServerException/>"


# ---------------------------------------------------------------------------
# response-header filtering
# ---------------------------------------------------------------------------


def test_response_headers_filtered_to_allowlist(monkeypatch):
    monkeypatch.setenv("QGIS_SERVER_URL", "https://qgis.example.test/ogc/wms")
    upstream_headers = {
        "Content-Type": "image/png",
        "Content-Length": "7",
        "Cache-Control": "max-age=60",
        "Set-Cookie": "session=leak",  # must NOT be relayed
        "Server": "nginx",  # must NOT be relayed
        "X-Goog-Authenticated-User-Id": "user123",  # must NOT be relayed
    }

    def _fake_get(url, headers=None, stream=False, timeout=None):
        return _FakeResponse(200, upstream_headers, b"PNGDATA", 4)

    with patch("requests.get", _fake_get), patch.object(
        qgis_proxy, "fetch_oidc_token", lambda base: None
    ):
        head, chunks, result = _run(_drive_proxy("MAP=x&LAYERS=y"))

    relayed = {k.lower() for k in result.headers}
    assert "set-cookie" not in relayed
    assert "server" not in relayed
    assert "x-goog-authenticated-user-id" not in relayed
    assert "content-type" in relayed
    assert "cache-control" in relayed
    # Sanity: everything relayed is in the allowlist.
    assert relayed <= {h.lower() for h in PASSTHROUGH_RESPONSE_HEADERS}


def test_oidc_audience_is_service_root(monkeypatch):
    """The OIDC audience is the Cloud Run service ROOT (scheme+host), not the
    /ogc/wms path — Cloud Run validates ``aud`` against the root URL."""
    aud = qgis_proxy._oidc_audience(
        "https://grace-2-qgis-server-425352658356.us-central1.run.app/ogc/wms"
    )
    assert aud == (
        "https://grace-2-qgis-server-425352658356.us-central1.run.app"
    )


def test_qgis_server_base_url_env(monkeypatch):
    monkeypatch.setenv("QGIS_SERVER_URL", "https://x.example/ogc/wms/")
    assert qgis_server_base_url() == "https://x.example/ogc/wms"
    monkeypatch.delenv("QGIS_SERVER_URL", raising=False)
    assert qgis_server_base_url() == qgis_proxy.DEFAULT_QGIS_SERVER_URL
