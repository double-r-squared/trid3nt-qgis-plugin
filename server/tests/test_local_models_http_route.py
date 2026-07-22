"""HTTP-route tests for GET /api/local-models (F2, live-feedback 2026-07-08).

The TRID3NT local build's web model selector lists the REAL installed Ollama
models. The browser cannot reach Ollama (:11434) directly, so the agent's
:8766 catalog listener proxies ``GET /api/tags`` and returns::

    {"models": [{"id": "...", "label": "..."}, ...], "default": "..."|null}

Covered here:
  - CLOUD posture: route ABSENT (404) unless MODEL_PROVIDER=openai;
  - 200 with the mapped model list, configured default moved first;
  - upstream (Ollama) unreachable -> honest 502, never a fabricated success;
  - ``_ollama_tags_url`` derivation from TRID3NT_OPENAI_BASE_URL.
"""

from __future__ import annotations

import asyncio
import json

from trid3nt_server import tool_catalog_http


class _FakeReader:
    """Feed a single HTTP/1.1 GET request, then EOF."""

    def __init__(self, request: bytes):
        self._lines = request.split(b"\r\n")
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
    return (f"GET {path} HTTP/1.1\r\nHost: agent.local\r\n\r\n").encode()


def _run(coro):
    return asyncio.run(coro)


def _status(out: bytes) -> int:
    return int(out.split(b" ", 2)[1])


def _body(out: bytes) -> dict:
    _, _, body = out.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def _dispatch() -> _FakeWriter:
    reader = _FakeReader(_request("/api/local-models"))
    writer = _FakeWriter()
    _run(tool_catalog_http._handle_http(reader, writer))
    return writer


# ---------------------------------------------------------------------------
# Route gating (cloud posture identical: 404 like any unknown path)
# ---------------------------------------------------------------------------


def test_route_absent_when_provider_unset(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    writer = _dispatch()
    assert _status(bytes(writer.buffer)) == 404


def test_route_absent_when_provider_bedrock(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    writer = _dispatch()
    assert _status(bytes(writer.buffer)) == 404


# ---------------------------------------------------------------------------
# Happy path (MODEL_PROVIDER=openai)
# ---------------------------------------------------------------------------


def test_local_models_listed_with_default_first(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_OPENAI_MODEL", "qwen3:8b-16k")

    body = json.dumps(
        {
            "models": [
                {"id": "llama3.2:3b", "label": "llama3.2:3b"},
                {"id": "qwen3:8b-16k", "label": "qwen3:8b-16k"},
            ],
            "default": "qwen3:8b-16k",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    monkeypatch.setattr(tool_catalog_http, "_fetch_local_models", lambda: body)

    writer = _dispatch()
    out = bytes(writer.buffer)
    assert _status(out) == 200
    payload = _body(out)
    assert payload["default"] == "qwen3:8b-16k"
    assert [m["id"] for m in payload["models"]] == [
        "llama3.2:3b",
        "qwen3:8b-16k",
    ]


def test_upstream_unreachable_is_typed_502(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")

    def _boom():
        raise tool_catalog_http._LocalModelsUpstreamError("ollama down")

    monkeypatch.setattr(tool_catalog_http, "_fetch_local_models", _boom)
    writer = _dispatch()
    out = bytes(writer.buffer)
    assert _status(out) == 502
    assert "ollama down" in _body(out)["error"]


# ---------------------------------------------------------------------------
# _fetch_local_models parsing (fake httpx client, no network)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpxClient:
    payload: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return _FakeResponse(self.payload)


def test_fetch_local_models_maps_ollama_tags(monkeypatch):
    import httpx

    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("TRID3NT_OPENAI_MODEL", "qwen3:8b-16k")
    _FakeHttpxClient.payload = {
        "models": [
            {"name": "llama3.2:3b", "size": 1},
            {"name": "qwen3:8b-16k", "size": 2},
            {"nope": True},  # malformed entry skipped
        ]
    }
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)

    payload = json.loads(tool_catalog_http._fetch_local_models())
    # Configured default moved first; malformed entry dropped.
    assert payload == {
        "models": [
            {"id": "qwen3:8b-16k", "label": "qwen3:8b-16k"},
            {"id": "llama3.2:3b", "label": "llama3.2:3b"},
        ],
        "default": "qwen3:8b-16k",
    }


def test_fetch_local_models_null_default_when_env_unset(monkeypatch):
    import httpx

    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.delenv("TRID3NT_OPENAI_MODEL", raising=False)
    _FakeHttpxClient.payload = {"models": [{"name": "llama3.2:3b"}]}
    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)

    payload = json.loads(tool_catalog_http._fetch_local_models())
    assert payload["default"] is None
    assert payload["models"] == [{"id": "llama3.2:3b", "label": "llama3.2:3b"}]


# ---------------------------------------------------------------------------
# _ollama_tags_url derivation
# ---------------------------------------------------------------------------


def test_tags_url_strips_v1_suffix(monkeypatch):
    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    assert (
        tool_catalog_http._ollama_tags_url()
        == "http://127.0.0.1:11434/api/tags"
    )


def test_tags_url_trailing_slash_and_default(monkeypatch):
    monkeypatch.setenv("TRID3NT_OPENAI_BASE_URL", "http://box:11434/v1/")
    assert tool_catalog_http._ollama_tags_url() == "http://box:11434/api/tags"
    monkeypatch.delenv("TRID3NT_OPENAI_BASE_URL", raising=False)
    assert (
        tool_catalog_http._ollama_tags_url()
        == "http://127.0.0.1:11434/api/tags"
    )
